from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
import queue
import threading
from typing import Dict, List, Optional, Tuple

try:
    from engine.pullback_pipeline_models import DailyContext, PullbackEntryIntent, PullbackSetupCandidate
except ImportError:
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        DailyContext,
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
    def __init__(self, maxsize: int = 256) -> None:
        self._queue: queue.Queue[PullbackEntryIntent] = queue.Queue(maxsize=max(int(maxsize), 1))
        self._lock = threading.Lock()
        self._active_keys: set[str] = set()

    def put_if_absent(self, intent: PullbackEntryIntent) -> bool:
        key = intent.intent_key
        with self._lock:
            if key in self._active_keys:
                return False
            self._active_keys.add(key)
        try:
            self._queue.put_nowait(intent)
            return True
        except queue.Full:
            with self._lock:
                self._active_keys.discard(key)
            return False

    def get(self, timeout: float = 0.5) -> PullbackEntryIntent:
        return self._queue.get(timeout=timeout)

    def complete(self, intent: PullbackEntryIntent) -> None:
        with self._lock:
            self._active_keys.discard(intent.intent_key)
        self._queue.task_done()

    def qsize(self) -> int:
        return self._queue.qsize()
