from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from datetime import datetime
import queue
import threading
from typing import Any, Dict, List, Optional, Tuple

try:
    from engine.pullback_pipeline_models import (
        AccountRiskSnapshot,
        DailyContext,
        HoldingsRiskSnapshot,
        PullbackEntryIntent,
        PullbackSetupCandidate,
    )
except ImportError:
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        AccountRiskSnapshot,
        DailyContext,
        HoldingsRiskSnapshot,
        PullbackEntryIntent,
        PullbackSetupCandidate,
    )


class ArmedCandidateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._candidates: Dict[str, PullbackSetupCandidate] = {}

    def upsert(self, candidate: PullbackSetupCandidate) -> None:
        with self._lock:
            self._candidates[str(candidate.symbol).zfill(6)] = candidate

    def get(self, symbol: str) -> Optional[PullbackSetupCandidate]:
        with self._lock:
            return self._candidates.get(str(symbol).zfill(6))

    def remove(self, symbol: str) -> Optional[PullbackSetupCandidate]:
        with self._lock:
            return self._candidates.pop(str(symbol).zfill(6), None)

    def size(self) -> int:
        with self._lock:
            return len(self._candidates)

    def symbols(self) -> List[str]:
        with self._lock:
            return list(self._candidates.keys())

    def cleanup_expired(self, now: Optional[datetime] = None) -> int:
        current_now = now or datetime.now()
        removed = 0
        with self._lock:
            for symbol, candidate in list(self._candidates.items()):
                expires_at = getattr(candidate, "expires_at", None)
                if not isinstance(expires_at, datetime):
                    continue
                if expires_at <= current_now:
                    self._candidates.pop(symbol, None)
                    removed += 1
        return removed


class DailyContextStore:
    def __init__(self, max_symbols: int = 256) -> None:
        self._lock = threading.Lock()
        self._max_symbols = max(int(max_symbols), 1)
        self._contexts: "OrderedDict[str, DailyContext]" = OrderedDict()

    def upsert(self, context: DailyContext) -> None:
        symbol = str(context.symbol).zfill(6)
        with self._lock:
            if symbol in self._contexts:
                self._contexts.pop(symbol, None)
            self._contexts[symbol] = context
            while len(self._contexts) > self._max_symbols:
                self._contexts.popitem(last=False)

    def get(self, symbol: str) -> Optional[DailyContext]:
        with self._lock:
            return self._contexts.get(str(symbol).zfill(6))

    def remove(self, symbol: str) -> Optional[DailyContext]:
        with self._lock:
            return self._contexts.pop(str(symbol).zfill(6), None)

    def size(self) -> int:
        with self._lock:
            return len(self._contexts)

    def get_validated(
        self,
        symbol: str,
        *,
        expected_trade_date: Optional[str] = None,
        stale_after_sec: float = 180.0,
        expected_context_version: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[Optional[DailyContext], str]:
        context = self.get(symbol)
        if context is None:
            return None, "missing"
        if expected_trade_date and str(context.trade_date) != str(expected_trade_date):
            return None, "trade_date_mismatch"
        if expected_context_version and str(context.context_version) != str(expected_context_version):
            return None, "version_mismatch"

        refreshed_at = context.refreshed_at
        if not isinstance(refreshed_at, datetime):
            return None, "stale"
        current_now = now
        if current_now is None:
            current_now = (
                datetime.now(refreshed_at.tzinfo)
                if refreshed_at.tzinfo is not None
                else datetime.now()
            )
        if max(float(stale_after_sec or 0.0), 0.0) > 0.0:
            age_sec = max((current_now - refreshed_at).total_seconds(), 0.0)
            if age_sec > float(stale_after_sec):
                return None, "stale"
        return context, ""


class DirtySymbolSet:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._symbols: set[str] = set()

    def mark(self, symbol: str) -> None:
        code = str(symbol).zfill(6)
        if not code:
            return
        with self._lock:
            self._symbols.add(code)

    def drain(self, max_items: Optional[int] = None) -> List[str]:
        with self._lock:
            if not self._symbols:
                return []
            symbols = sorted(self._symbols)
            if max_items is not None:
                drained = symbols[: max(int(max_items), 0)]
            else:
                drained = symbols
            for symbol in drained:
                self._symbols.discard(symbol)
            return drained

    def size(self) -> int:
        with self._lock:
            return len(self._symbols)


class EntryIntentQueue:
    def __init__(
        self,
        maxsize: int = 256,
        *,
        authoritative: bool = True,
        drop_policy: str = "reject_new",
        max_pending_per_symbol: int = 0,
    ) -> None:
        self._queue: queue.PriorityQueue[tuple[tuple[Any, ...], int, Any]] = queue.PriorityQueue(
            maxsize=max(int(maxsize), 1)
        )
        self._lock = threading.Lock()
        self._authoritative = bool(authoritative)
        self._drop_policy = str(drop_policy or "reject_new").strip().lower() or "reject_new"
        self._max_pending_per_symbol = max(int(max_pending_per_symbol or 0), 0)
        self._active_keys: set[str] = set()
        self._active_symbol_counts: Dict[str, int] = {}
        self._enqueue_seq: int = 0
        self._mixed_strategy_tiebreak_count: int = 0
        self._dropped_count: int = 0
        self._last_reject_reason: str = ""

    @staticmethod
    def _strategy_rank(strategy_tag: str) -> int:
        rank_map = {
            "pullback_rebreakout": 0,
            "trend_atr": 1,
            "opening_range_breakout": 2,
        }
        return int(rank_map.get(str(strategy_tag or "").strip(), 99))

    @staticmethod
    def _intent_priority(intent: Any) -> tuple[Any, ...]:
        created_at = getattr(intent, "created_at", None)
        if not isinstance(created_at, datetime):
            created_at = datetime.max
        return (
            created_at,
            EntryIntentQueue._strategy_rank(str(getattr(intent, "strategy_tag", "") or "")),
        )

    def _has_other_strategy_for_symbol(self, *, symbol: str, strategy_tag: str) -> bool:
        target_symbol = str(symbol).zfill(6)
        for key in self._active_keys:
            active_strategy, _, active_symbol = key.partition(":")
            if active_symbol == target_symbol and active_strategy != str(strategy_tag or "").strip():
                return True
        return False

    def _symbol_count(self, symbol: str) -> int:
        return int(self._active_symbol_counts.get(str(symbol).zfill(6), 0) or 0)

    def _inc_symbol_count(self, symbol: str) -> None:
        normalized = str(symbol).zfill(6)
        self._active_symbol_counts[normalized] = self._symbol_count(normalized) + 1

    def _dec_symbol_count(self, symbol: str) -> None:
        normalized = str(symbol).zfill(6)
        count = max(self._symbol_count(normalized) - 1, 0)
        if count <= 0:
            self._active_symbol_counts.pop(normalized, None)
        else:
            self._active_symbol_counts[normalized] = count

    def _remove_active_intent_locked(self, intent: Any) -> None:
        self._active_keys.discard(intent.intent_key)
        self._dec_symbol_count(getattr(intent, "symbol", ""))

    def _drop_oldest_locked(self) -> bool:
        if self._authoritative or self._drop_policy != "drop_oldest":
            return False
        try:
            _priority, _seq, dropped_intent = self._queue.get_nowait()
        except queue.Empty:
            return False
        self._remove_active_intent_locked(dropped_intent)
        self._dropped_count += 1
        self._queue.task_done()
        return True

    def put_if_absent(self, intent: Any) -> bool:
        key = intent.intent_key
        with self._lock:
            if key in self._active_keys:
                self._last_reject_reason = "duplicate"
                return False
            if self._max_pending_per_symbol > 0 and self._symbol_count(getattr(intent, "symbol", "")) >= self._max_pending_per_symbol:
                self._last_reject_reason = "pending_symbol_cap"
                return False
            if self._has_other_strategy_for_symbol(
                symbol=getattr(intent, "symbol", ""),
                strategy_tag=getattr(intent, "strategy_tag", ""),
            ):
                self._mixed_strategy_tiebreak_count += 1
            self._active_keys.add(key)
            self._inc_symbol_count(getattr(intent, "symbol", ""))
            self._enqueue_seq += 1
            enqueue_seq = self._enqueue_seq
        try:
            self._queue.put_nowait((self._intent_priority(intent), enqueue_seq, intent))
            with self._lock:
                self._last_reject_reason = ""
            return True
        except queue.Full:
            with self._lock:
                if not self._drop_oldest_locked():
                    self._active_keys.discard(key)
                    self._dec_symbol_count(getattr(intent, "symbol", ""))
                    self._last_reject_reason = "queue_full"
                    return False
            try:
                self._queue.put_nowait((self._intent_priority(intent), enqueue_seq, intent))
                with self._lock:
                    self._last_reject_reason = ""
                return True
            except queue.Full:
                with self._lock:
                    self._active_keys.discard(key)
                    self._dec_symbol_count(getattr(intent, "symbol", ""))
                    self._last_reject_reason = "queue_full"
                return False

    def get(self, timeout: float = 0.5) -> Any:
        _, _, intent = self._queue.get(timeout=timeout)
        return intent

    def complete(self, intent: Any) -> None:
        with self._lock:
            self._remove_active_intent_locked(intent)
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()

    def strategy_counts(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for key in self._active_keys:
                strategy_tag, _, _symbol = key.partition(":")
                counts[strategy_tag] = counts.get(strategy_tag, 0) + 1
            return counts

    def mixed_strategy_tiebreak_count(self) -> int:
        with self._lock:
            return int(self._mixed_strategy_tiebreak_count)

    def dropped_count(self) -> int:
        with self._lock:
            return int(self._dropped_count)

    def last_reject_reason(self) -> str:
        with self._lock:
            return str(self._last_reject_reason or "")


class AccountRiskStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._account_snapshot: Optional[AccountRiskSnapshot] = None
        self._holdings_snapshot: Optional[HoldingsRiskSnapshot] = None
        self._last_account_success_at: Optional[datetime] = None
        self._last_holdings_success_at: Optional[datetime] = None
        self._last_account_error: str = ""
        self._last_holdings_error: str = ""

    def replace_account_snapshot(self, snapshot: AccountRiskSnapshot) -> None:
        with self._lock:
            self._account_snapshot = snapshot
            if snapshot.success:
                self._last_account_success_at = snapshot.fetched_at
                self._last_account_error = ""
            elif snapshot.last_error:
                self._last_account_error = str(snapshot.last_error)

    def replace_holdings_snapshot(self, snapshot: HoldingsRiskSnapshot) -> None:
        with self._lock:
            self._holdings_snapshot = snapshot
            if snapshot.success:
                self._last_holdings_success_at = snapshot.fetched_at
                self._last_holdings_error = ""
            elif snapshot.last_error:
                self._last_holdings_error = str(snapshot.last_error)

    def get_account_snapshot(self) -> Optional[AccountRiskSnapshot]:
        with self._lock:
            return self._account_snapshot

    def get_holdings_snapshot(self) -> Optional[HoldingsRiskSnapshot]:
        with self._lock:
            return self._holdings_snapshot

    def get_account_state(
        self,
        *,
        ttl_sec: float,
        now: Optional[datetime] = None,
    ) -> Tuple[Optional[AccountRiskSnapshot], str]:
        snapshot = self.get_account_snapshot()
        if snapshot is None:
            return None, "absent"
        current_now = now
        if current_now is None:
            current_now = (
                datetime.now(snapshot.fetched_at.tzinfo)
                if snapshot.fetched_at.tzinfo is not None
                else datetime.now()
            )
        age_sec = max((current_now - snapshot.fetched_at).total_seconds(), 0.0)
        if max(float(ttl_sec or 0.0), 0.0) > 0.0 and age_sec > float(ttl_sec):
            return replace(snapshot, stale=True), "stale"
        return replace(snapshot, stale=False), "fresh"

    def get_holdings_state(
        self,
        *,
        ttl_sec: float,
        now: Optional[datetime] = None,
    ) -> Tuple[Optional[HoldingsRiskSnapshot], str]:
        snapshot = self.get_holdings_snapshot()
        if snapshot is None:
            return None, "absent"
        current_now = now
        if current_now is None:
            current_now = (
                datetime.now(snapshot.fetched_at.tzinfo)
                if snapshot.fetched_at.tzinfo is not None
                else datetime.now()
            )
        age_sec = max((current_now - snapshot.fetched_at).total_seconds(), 0.0)
        if max(float(ttl_sec or 0.0), 0.0) > 0.0 and age_sec > float(ttl_sec):
            return replace(snapshot, stale=True), "stale"
        return replace(snapshot, stale=False), "fresh"

    def get_last_account_success_age_sec(self, now: Optional[datetime] = None) -> Optional[float]:
        with self._lock:
            success_at = self._last_account_success_at
        if success_at is None:
            return None
        current_now = now
        if current_now is None:
            tzinfo = success_at.tzinfo if success_at is not None else None
            current_now = datetime.now(tzinfo) if tzinfo is not None else datetime.now()
        return max((current_now - success_at).total_seconds(), 0.0)

    def get_last_holdings_success_age_sec(self, now: Optional[datetime] = None) -> Optional[float]:
        with self._lock:
            success_at = self._last_holdings_success_at
        if success_at is None:
            return None
        current_now = now
        if current_now is None:
            current_now = (
                datetime.now(success_at.tzinfo)
                if success_at.tzinfo is not None
                else datetime.now()
            )
        return max((current_now - success_at).total_seconds(), 0.0)

    def get_last_errors(self) -> Dict[str, str]:
        with self._lock:
            return {
                "account": self._last_account_error,
                "holdings": self._last_holdings_error,
            }
