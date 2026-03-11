"""Deterministic replay support for the threaded multi-strategy pipeline."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

try:
    from config import settings
    from engine.pullback_pipeline_models import (
        AuthoritativeEntryIntent,
        DailyContext,
        PullbackEntryIntent,
        StrategyEntryIntent,
        StrategySetupCandidate,
        pullback_timing_decision_from_strategy,
        strategy_setup_candidate_from_pullback,
    )
    from engine.pullback_pipeline_stores import ArmedCandidateStore, EntryIntentQueue
    from engine.pullback_pipeline_workers import OrderExecutionWorker
    from engine.strategy_pipeline_registry import build_default_strategy_registry
    from strategy.multiday_trend_atr import MultidayTrendATRStrategy
    from utils.market_hours import KST
    from utils.market_phase import resolve_market_phase_context
except ImportError:
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        AuthoritativeEntryIntent,
        DailyContext,
        PullbackEntryIntent,
        StrategyEntryIntent,
        StrategySetupCandidate,
        pullback_timing_decision_from_strategy,
        strategy_setup_candidate_from_pullback,
    )
    from kis_trend_atr_trading.engine.pullback_pipeline_stores import ArmedCandidateStore, EntryIntentQueue
    from kis_trend_atr_trading.engine.pullback_pipeline_workers import OrderExecutionWorker
    from kis_trend_atr_trading.engine.strategy_pipeline_registry import build_default_strategy_registry
    from kis_trend_atr_trading.strategy.multiday_trend_atr import MultidayTrendATRStrategy
    from kis_trend_atr_trading.utils.market_hours import KST
    from kis_trend_atr_trading.utils.market_phase import resolve_market_phase_context


SUPPORTED_EVENT_TYPES = {
    "quote",
    "day_boundary",
    "intraday_bar",
    "daily_context",
    "regime_snapshot",
    "risk_snapshot",
    "degraded_state_change",
}
SUPPORTED_STRATEGIES = {
    "pullback_rebreakout",
    "trend_atr",
    "opening_range_breakout",
}


def _parse_datetime(value: object) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("timestamp is required")
    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return KST.localize(parsed)
    return parsed.astimezone(KST)


def _normalize_symbol(value: object) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if token.isdigit():
        return token.zfill(6)
    return token


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


@dataclass(frozen=True)
class ReplayEvent:
    event_type: str
    event_ts: datetime
    input_index: int
    symbol: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplayRegimeSnapshot:
    regime: Optional[str] = None
    is_stale: bool = False
    error_state: str = ""
    as_of: Optional[datetime] = None


@dataclass
class ReplayRiskState:
    holdings_symbols: set[str] = field(default_factory=set)
    pending_symbols: set[str] = field(default_factory=set)
    raw_payload: Dict[str, Any] = field(default_factory=dict)


class SyntheticDegradedController:
    def __init__(self) -> None:
        self._is_degraded = False
        self._reason = ""

    def set_state(self, *, is_degraded: bool, reason: str = "") -> None:
        self._is_degraded = bool(is_degraded)
        self._reason = str(reason or "")

    def is_degraded(self) -> bool:
        return bool(self._is_degraded)

    @property
    def reason(self) -> str:
        return self._reason


class _ReplayExecutor:
    def __init__(self) -> None:
        self.strategy = MultidayTrendATRStrategy()
        self.stock_code = "000000"
        self.market_phase_context = None
        self.market_venue_context = "KRX"
        self.market_regime_snapshot: Optional[ReplayRegimeSnapshot] = None
        self._pullback_threaded_context_version = ""
        self._pullback_daily_context_version = ""
        self._strategy_shadow_state_lock = threading.Lock()
        self._strategy_shadow_candidates: Dict[str, StrategySetupCandidate] = {}
        self._strategy_shadow_intents: Dict[str, StrategyEntryIntent] = {}
        self._quote_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._daily_df_by_symbol: Dict[str, pd.DataFrame] = {}
        self._daily_context_by_symbol: Dict[str, DailyContext] = {}
        self._intraday_bars_by_symbol: Dict[str, List[dict]] = {}
        self._intraday_ready_by_symbol: Dict[str, bool] = {}
        self._risk_state = ReplayRiskState()
        self._submitted_orders: List[Dict[str, Any]] = []
        self._last_signal: Optional[Any] = None
        self._replay_now: Optional[datetime] = None
        self._degraded_controller = SyntheticDegradedController()
        self._authoritative_order_handoff_path = ""
        self._authoritative_intent_reject_reason = ""
        self._strategy_end_to_end_latency_ms = 0.0
        self._pullback_end_to_end_latency_ms = 0.0

    def set_event_context(self, *, symbol: str, event_ts: datetime) -> None:
        self.stock_code = _normalize_symbol(symbol)
        self._replay_now = event_ts
        phase_context = resolve_market_phase_context(check_time=event_ts, venue="KRX")
        self.market_phase_context = phase_context.phase
        self.market_venue_context = phase_context.venue.value
        context = self._daily_context_by_symbol.get(self.stock_code)
        self._pullback_daily_context_version = str(getattr(context, "context_version", "") or "")
        self._pullback_threaded_context_version = self._pullback_daily_context_version

    def set_quote(self, symbol: str, payload: Dict[str, Any]) -> None:
        normalized = _normalize_symbol(symbol)
        snapshot = dict(self._quote_by_symbol.get(normalized) or {})
        snapshot.update(payload)
        snapshot["stock_code"] = normalized
        self._quote_by_symbol[normalized] = snapshot

    def set_daily_context(self, symbol: str, context: DailyContext, *, daily_df: Optional[pd.DataFrame] = None) -> None:
        normalized = _normalize_symbol(symbol)
        self._daily_context_by_symbol[normalized] = context
        if daily_df is not None:
            self._daily_df_by_symbol[normalized] = daily_df.copy()
        else:
            self._daily_df_by_symbol[normalized] = pd.DataFrame(list(context.recent_bars or ()))

    def set_intraday_bar(self, symbol: str, bar: Dict[str, Any], *, provider_ready: bool = True) -> None:
        normalized = _normalize_symbol(symbol)
        bucket = list(self._intraday_bars_by_symbol.get(normalized) or [])
        bucket.append(dict(bar))
        bucket.sort(key=lambda item: _parse_datetime(item.get("start_at") or item.get("date")))
        self._intraday_bars_by_symbol[normalized] = bucket
        self._intraday_ready_by_symbol[normalized] = bool(provider_ready)

    def clear_for_day_boundary(self) -> None:
        self._intraday_bars_by_symbol.clear()
        self._intraday_ready_by_symbol.clear()
        self.clear_strategy_shadow_state()

    def set_regime_snapshot(self, snapshot: ReplayRegimeSnapshot) -> None:
        self.market_regime_snapshot = snapshot

    def set_risk_state(self, payload: Dict[str, Any]) -> None:
        holdings = {
            _normalize_symbol(item.get("symbol") or item.get("stock_code") or item)
            for item in list(payload.get("holdings") or [])
            if _normalize_symbol(item.get("symbol") or item.get("stock_code") or item)
        }
        pending = {
            _normalize_symbol(item.get("symbol") or item.get("stock_code") or item)
            for item in list(payload.get("pending_symbols") or [])
            if _normalize_symbol(item.get("symbol") or item.get("stock_code") or item)
        }
        self._risk_state = ReplayRiskState(
            holdings_symbols=holdings,
            pending_symbols=pending,
            raw_payload=dict(payload or {}),
        )

    def _shadow_key(self, strategy_tag: str, symbol: str) -> str:
        return f"{str(strategy_tag or '').strip()}:{_normalize_symbol(symbol)}"

    def upsert_strategy_shadow_candidate(self, strategy_tag: str, symbol: str, candidate: Any) -> None:
        self._strategy_shadow_candidates[self._shadow_key(strategy_tag, symbol)] = candidate

    def get_strategy_shadow_candidate(self, strategy_tag: str, symbol: str) -> Optional[Any]:
        return self._strategy_shadow_candidates.get(self._shadow_key(strategy_tag, symbol))

    def remove_strategy_shadow_candidate(self, strategy_tag: str, symbol: str) -> Optional[Any]:
        return self._strategy_shadow_candidates.pop(self._shadow_key(strategy_tag, symbol), None)

    def upsert_strategy_shadow_intent(self, strategy_tag: str, symbol: str, intent: Any) -> None:
        self._strategy_shadow_intents[self._shadow_key(strategy_tag, symbol)] = intent

    def get_strategy_shadow_intent(self, strategy_tag: str, symbol: str) -> Optional[Any]:
        return self._strategy_shadow_intents.get(self._shadow_key(strategy_tag, symbol))

    def remove_strategy_shadow_intent(self, strategy_tag: str, symbol: str) -> Optional[Any]:
        return self._strategy_shadow_intents.pop(self._shadow_key(strategy_tag, symbol), None)

    def clear_strategy_shadow_state(self) -> None:
        self._strategy_shadow_candidates.clear()
        self._strategy_shadow_intents.clear()

    def get_strategy_shadow_counts(self) -> Dict[str, Dict[str, int]]:
        candidate_counts: Dict[str, int] = {}
        intent_counts: Dict[str, int] = {}
        for key in self._strategy_shadow_candidates.keys():
            strategy_tag = key.split(":", 1)[0]
            candidate_counts[strategy_tag] = candidate_counts.get(strategy_tag, 0) + 1
        for key in self._strategy_shadow_intents.keys():
            strategy_tag = key.split(":", 1)[0]
            intent_counts[strategy_tag] = intent_counts.get(strategy_tag, 0) + 1
        return {"candidates": candidate_counts, "intents": intent_counts}

    def fetch_quote_snapshot(self) -> Dict[str, Any]:
        return dict(self._quote_by_symbol.get(self.stock_code) or {})

    def get_cached_pullback_quote_snapshot(self) -> Dict[str, Any]:
        return self.fetch_quote_snapshot()

    def fetch_market_data(self) -> pd.DataFrame:
        df = self._daily_df_by_symbol.get(self.stock_code)
        if df is not None:
            return df.copy()
        context = self._daily_context_by_symbol.get(self.stock_code)
        if context is None:
            return pd.DataFrame()
        return pd.DataFrame(list(context.recent_bars or ()))

    def fetch_market_data_for_symbol(self, symbol: str) -> pd.DataFrame:
        previous_symbol = self.stock_code
        self.stock_code = _normalize_symbol(symbol)
        try:
            return self.fetch_market_data()
        finally:
            self.stock_code = previous_symbol

    def is_cached_intraday_provider_ready(self) -> bool:
        return bool(self._intraday_ready_by_symbol.get(self.stock_code, False))

    def fetch_cached_intraday_bars_if_available(self, n: int = 120) -> List[dict]:
        bars = list(self._intraday_bars_by_symbol.get(self.stock_code) or [])
        return bars[-max(int(n), 1) :]

    def _has_active_pending_buy_order(self) -> bool:
        return self.stock_code in self._risk_state.pending_symbols

    def cached_account_has_holding(self, stock_code: str) -> bool:
        return _normalize_symbol(stock_code) in self._risk_state.holdings_symbols

    def execute_buy(self, signal: Any) -> Dict[str, Any]:
        strategy_tag = str((getattr(signal, "meta", {}) or {}).get("strategy_tag") or "")
        payload = {
            "success": True,
            "would_submit": True,
            "strategy_tag": strategy_tag,
            "symbol": self.stock_code,
            "price": float(getattr(signal, "price", 0.0) or 0.0),
            "reason": str(getattr(signal, "reason", "") or ""),
            "submitted_at": self._replay_now.isoformat() if isinstance(self._replay_now, datetime) else "",
        }
        self._submitted_orders.append(payload)
        self._last_signal = signal
        self._risk_state.pending_symbols.add(self.stock_code)
        return payload

    def refresh_shared_market_regime_snapshot(self, *_args, **_kwargs):
        raise AssertionError("replay must not call sync market regime refresh")


class ReplayOrderExecutionWorker(OrderExecutionWorker):
    def _now(self) -> datetime:
        current = getattr(self._executor, "_replay_now", None)
        if isinstance(current, datetime):
            return current
        return super()._now()


class ThreadedPipelineReplayRunner:
    def __init__(
        self,
        *,
        strategy_filter: Sequence[str] | None = None,
        strict: bool = False,
    ) -> None:
        enabled_tags = tuple(
            tag
            for tag in (strategy_filter or tuple(sorted(SUPPORTED_STRATEGIES)))
            if tag in SUPPORTED_STRATEGIES
        )
        self.strict = bool(strict)
        self.enabled_tags = enabled_tags or tuple(sorted(SUPPORTED_STRATEGIES))
        self.executor = _ReplayExecutor()
        self.registry = build_default_strategy_registry(
            pullback_strategy=self.executor.strategy.pullback_strategy,
            trend_atr_strategy=self.executor.strategy,
            orb_strategy=self.executor.strategy.orb_strategy,
        )
        self.candidate_store = ArmedCandidateStore()
        self.entry_queue = EntryIntentQueue(
            maxsize=max(int(getattr(settings, "MAX_INTENT_QUEUE_DEPTH", 1024) or 1024), 1),
            authoritative=True,
            drop_policy="reject_new",
            max_pending_per_symbol=max(int(getattr(settings, "MAX_PENDING_INTENTS_PER_SYMBOL", 1) or 1), 0),
        )
        self.order_worker = ReplayOrderExecutionWorker(
            executor=self.executor,
            candidate_store=self.candidate_store,
            entry_queue=self.entry_queue,
            stop_event=threading.Event(),
            on_error=lambda *_args: None,
            health_store=None,
        )
        self._last_setup_eval_at: Dict[str, datetime] = {}
        self._candidate_timeline: List[Dict[str, Any]] = []
        self._intent_timeline: List[Dict[str, Any]] = []
        self._order_timeline: List[Dict[str, Any]] = []
        self._skip_reason_counts: Dict[str, int] = {}
        self._queue_ingress_count: int = 0

    def _setup_interval_sec(self) -> float:
        return max(float(getattr(settings, "PULLBACK_SETUP_REFRESH_SEC", 60) or 60.0), 1.0)

    def _active_quote(self, symbol: str) -> Dict[str, Any]:
        self.executor.stock_code = _normalize_symbol(symbol)
        return self.executor.fetch_quote_snapshot()

    def _daily_context_for_symbol(self, symbol: str) -> Optional[DailyContext]:
        return self.executor._daily_context_by_symbol.get(_normalize_symbol(symbol))

    def _record_timeline(self, bucket: List[Dict[str, Any]], payload: Dict[str, Any]) -> None:
        serializable = dict(payload)
        for key, value in list(serializable.items()):
            if isinstance(value, datetime):
                serializable[key] = value.isoformat()
        bucket.append(serializable)

    def _record_skip_reason(self, reason: str) -> None:
        token = str(reason or "").strip()
        if not token:
            return
        self._skip_reason_counts[token] = int(self._skip_reason_counts.get(token, 0) or 0) + 1

    def _setup_due(self, symbol: str, event_ts: datetime) -> bool:
        previous = self._last_setup_eval_at.get(_normalize_symbol(symbol))
        if previous is None:
            return True
        return (event_ts - previous).total_seconds() >= self._setup_interval_sec()

    def _snapshot_candidate_state(self, symbol: str) -> Dict[str, Any]:
        normalized = _normalize_symbol(symbol)
        return {
            "pullback_rebreakout": self.candidate_store.get(normalized),
            "trend_atr": self.executor.get_strategy_shadow_candidate("trend_atr", normalized),
            "opening_range_breakout": self.executor.get_strategy_shadow_candidate("opening_range_breakout", normalized),
        }

    def _setup_for_symbol(self, symbol: str, event_ts: datetime) -> None:
        normalized = _normalize_symbol(symbol)
        if not normalized:
            return
        quote = self._active_quote(normalized)
        daily_context = self._daily_context_for_symbol(normalized)
        if daily_context is None:
            self._record_skip_reason("missing_daily_context")
            return
        self.executor.set_event_context(symbol=normalized, event_ts=event_ts)
        self.executor._pullback_daily_context_version = str(daily_context.context_version or "")
        before_state = self._snapshot_candidate_state(normalized)
        current_price = float(quote.get("current_price", 0.0) or 0.0)
        open_price = float(quote.get("open_price", 0.0) or 0.0)
        intraday_bars = self.executor.fetch_cached_intraday_bars_if_available(120)
        provider_ready = self.executor.is_cached_intraday_provider_ready()

        for strategy_tag in self.enabled_tags:
            entry = self.registry.get(strategy_tag)
            if entry is None:
                continue
            evaluation = entry.setup_evaluator.evaluate_setup(
                stock_code=normalized,
                stock_name=str(quote.get("stock_name") or ""),
                current_price=current_price,
                open_price=open_price,
                intraday_bars=intraday_bars,
                intraday_provider_ready=provider_ready,
                check_time=event_ts,
                market_phase=self.executor.market_phase_context,
                market_venue=self.executor.market_venue_context,
                has_existing_position=False,
                has_pending_order=self.executor._has_active_pending_buy_order(),
                market_regime_snapshot=self.executor.market_regime_snapshot,
                daily_context=daily_context,
                daily_df=self.executor.fetch_market_data(),
            )
            if strategy_tag == "pullback_rebreakout":
                if evaluation.native_candidate is None:
                    self.candidate_store.remove(normalized)
                else:
                    self.candidate_store.upsert(evaluation.native_candidate)
                    self.executor._pullback_threaded_context_version = str(
                        getattr(evaluation.native_candidate, "context_version", "") or ""
                    )
            else:
                if evaluation.candidate is None:
                    self.executor.remove_strategy_shadow_candidate(strategy_tag, normalized)
                else:
                    self.executor.upsert_strategy_shadow_candidate(strategy_tag, normalized, evaluation.candidate)
            if evaluation.candidate is None:
                self._record_skip_reason(str(evaluation.skip_code or evaluation.skip_reason or "setup_skipped"))
                self._record_timeline(
                    self._candidate_timeline,
                    {
                        "event_ts": event_ts,
                        "symbol": normalized,
                        "strategy_tag": strategy_tag,
                        "setup_candidate_created": False,
                        "skip_reason": str(evaluation.skip_code or evaluation.skip_reason or ""),
                    },
                )

        after_state = self._snapshot_candidate_state(normalized)
        for strategy_tag, candidate in after_state.items():
            if strategy_tag not in self.enabled_tags or candidate is None:
                continue
            previous = before_state.get(strategy_tag)
            if previous is candidate:
                continue
            if _stable_json(getattr(previous, "__dict__", previous or {})) == _stable_json(getattr(candidate, "__dict__", candidate or {})):
                continue
            self._record_timeline(
                self._candidate_timeline,
                {
                    "event_ts": event_ts,
                    "symbol": normalized,
                    "strategy_tag": strategy_tag,
                    "setup_candidate_created": True,
                    "expires_at": getattr(candidate, "expires_at", None),
                },
            )
        self._last_setup_eval_at[normalized] = event_ts

    def _enqueue_authoritative(self, intent: AuthoritativeEntryIntent, *, event_ts: datetime) -> None:
        if self.executor._degraded_controller.is_degraded():
            self._record_timeline(
                self._intent_timeline,
                {
                    "event_ts": event_ts,
                    "symbol": intent.symbol,
                    "strategy_tag": intent.strategy_tag,
                    "timing_confirmed": True,
                    "intent_emitted": False,
                    "queue_depth": self.entry_queue.qsize(),
                    "reject_reason": f"degraded_mode:{self.executor._degraded_controller.reason}",
                },
            )
            self._record_skip_reason("degraded_mode")
            return
        queued = self.entry_queue.put_if_absent(intent)
        self._record_timeline(
            self._intent_timeline,
            {
                "event_ts": event_ts,
                "symbol": intent.symbol,
                "strategy_tag": intent.strategy_tag,
                "timing_confirmed": True,
                "intent_emitted": bool(queued),
                "queue_depth": self.entry_queue.qsize(),
                "reject_reason": "" if queued else self.entry_queue.last_reject_reason(),
            },
        )
        if queued:
            self._queue_ingress_count += 1
        else:
            self._record_skip_reason(self.entry_queue.last_reject_reason())

    def _timing_for_symbol(self, symbol: str, event_ts: datetime) -> None:
        normalized = _normalize_symbol(symbol)
        if not normalized:
            return
        self.executor.set_event_context(symbol=normalized, event_ts=event_ts)
        quote = self.executor.fetch_quote_snapshot()
        current_price = float(quote.get("current_price", 0.0) or 0.0)
        if current_price <= 0.0:
            self._record_skip_reason("invalid_current_price")
            return
        intraday_bars = self.executor.fetch_cached_intraday_bars_if_available(120)
        pending = self.executor._has_active_pending_buy_order()

        pullback_candidate = self.candidate_store.get(normalized)
        if pullback_candidate is not None and "pullback_rebreakout" in self.enabled_tags:
            entry = self.registry.get("pullback_rebreakout")
            strategy_decision = entry.timing_evaluator.evaluate_timing(
                candidate=strategy_setup_candidate_from_pullback(pullback_candidate),
                native_candidate=pullback_candidate,
                current_price=current_price,
                stock_code=normalized,
                check_time=event_ts,
                market_phase=self.executor.market_phase_context,
                market_venue=self.executor.market_venue_context,
                intraday_bars=intraday_bars,
                has_existing_position=False,
                has_pending_order=pending,
                current_context_version=str(self.executor._pullback_threaded_context_version or ""),
            )
            pullback_decision = pullback_timing_decision_from_strategy(strategy_decision)
            if pullback_decision.invalidate_candidate:
                self.candidate_store.remove(normalized)
            if pullback_decision.should_emit_intent:
                native_intent = PullbackEntryIntent(
                    symbol=normalized,
                    strategy_tag="pullback_rebreakout",
                    created_at=event_ts,
                    candidate_created_at=pullback_candidate.created_at,
                    expires_at=pullback_candidate.expires_at,
                    context_version=pullback_candidate.context_version,
                    entry_reference_price=float(
                        pullback_decision.entry_reference_price or pullback_candidate.micro_high or 0.0
                    ),
                    source=str(pullback_decision.timing_source or "fallback_daily"),
                    current_price=current_price,
                    meta={**dict(pullback_candidate.extra_json or {}), **dict(pullback_decision.meta or {})},
                )
                self._enqueue_authoritative(
                    AuthoritativeEntryIntent(
                        strategy_tag="pullback_rebreakout",
                        symbol=normalized,
                        created_at=event_ts,
                        expires_at=native_intent.expires_at,
                        trade_date=event_ts.date().isoformat(),
                        entry_reference_price=native_intent.entry_reference_price,
                        entry_reference_label=str(native_intent.meta.get("entry_reference_label") or "pullback_intraday_high"),
                        native_payload=native_intent,
                        source=native_intent.source,
                        meta=dict(native_intent.meta or {}),
                    ),
                    event_ts=event_ts,
                )
            else:
                self._record_skip_reason(str(pullback_decision.reason_code or pullback_decision.reason or "timing_skipped"))

        for strategy_tag in ("trend_atr", "opening_range_breakout"):
            if strategy_tag not in self.enabled_tags:
                continue
            shadow_candidate = self.executor.get_strategy_shadow_candidate(strategy_tag, normalized)
            if shadow_candidate is None:
                continue
            entry = self.registry.get(strategy_tag)
            decision = entry.timing_evaluator.evaluate_timing(
                candidate=shadow_candidate,
                native_candidate=None,
                current_price=current_price,
                stock_code=normalized,
                check_time=event_ts,
                market_phase=self.executor.market_phase_context,
                market_venue=self.executor.market_venue_context,
                intraday_bars=intraday_bars,
                has_existing_position=False,
                has_pending_order=pending,
                current_context_version=None,
            )
            if not bool(decision.should_emit_intent):
                self._record_skip_reason(str(decision.reason_code or decision.reason or "timing_skipped"))
                continue
            strategy_intent = StrategyEntryIntent(
                strategy_tag=strategy_tag,
                symbol=normalized,
                created_at=event_ts,
                expires_at=decision.expires_at,
                trade_date=str(decision.trade_date or ""),
                entry_reference_price=float(decision.entry_reference_price or 0.0),
                entry_reference_label=str(decision.entry_reference_label or "prev_high"),
                meta=dict(decision.meta or {}),
            )
            self.executor.upsert_strategy_shadow_intent(strategy_tag, normalized, strategy_intent)
            authoritative_payload = {
                "strategy_entry_intent": strategy_intent,
                "shadow_candidate": shadow_candidate,
                "decision": decision,
                "quote_snapshot": dict(quote or {}),
                "intraday_bars": list(intraday_bars or []),
            }
            self._enqueue_authoritative(
                AuthoritativeEntryIntent(
                    strategy_tag=strategy_tag,
                    symbol=normalized,
                    created_at=event_ts,
                    expires_at=decision.expires_at,
                    trade_date=str(decision.trade_date or ""),
                    entry_reference_price=float(decision.entry_reference_price or 0.0),
                    entry_reference_label=str(decision.entry_reference_label or "prev_high"),
                    native_payload=authoritative_payload,
                    source=str((decision.meta or {}).get("timing_source") or ""),
                    meta=dict(strategy_intent.meta or {}),
                ),
                event_ts=event_ts,
            )

    def _drain_queue(self, event_ts: datetime) -> None:
        while self.entry_queue.qsize() > 0:
            intent = self.entry_queue.get(timeout=0.001)
            try:
                self.executor.set_event_context(symbol=intent.symbol, event_ts=event_ts)
                self._record_timeline(
                    self._order_timeline,
                    {
                        "event_ts": event_ts,
                        "symbol": intent.symbol,
                        "strategy_tag": intent.strategy_tag,
                        "order_decision": "queue_ingressed",
                        "queue_depth": self.entry_queue.qsize() + 1,
                    },
                )
                allowed, reason = self.order_worker._common_precheck(intent)
                if not allowed:
                    self.order_worker._record_reject(
                        strategy_tag=intent.strategy_tag,
                        symbol=intent.symbol,
                        reason=reason,
                        dedupe=reason in {"existing_position", "pending_order", "existing_holding"},
                    )
                    if intent.strategy_tag == "pullback_rebreakout":
                        self.candidate_store.remove(intent.symbol)
                    else:
                        self.executor.remove_strategy_shadow_candidate(intent.strategy_tag, intent.symbol)
                        self.executor.remove_strategy_shadow_intent(intent.strategy_tag, intent.symbol)
                    self._record_timeline(
                        self._order_timeline,
                        {
                            "event_ts": event_ts,
                            "symbol": intent.symbol,
                            "strategy_tag": intent.strategy_tag,
                            "order_decision": "precheck_rejected",
                            "reject_reason": reason,
                            "queue_depth": self.entry_queue.qsize(),
                        },
                    )
                    continue
                self.order_worker._strategy_consumed(intent.strategy_tag)
                self._record_timeline(
                    self._order_timeline,
                    {
                        "event_ts": event_ts,
                        "symbol": intent.symbol,
                        "strategy_tag": intent.strategy_tag,
                        "order_decision": "native_handoff_started",
                        "queue_depth": self.entry_queue.qsize(),
                    },
                )
                result = self.order_worker._dispatch_intent(intent)
                if bool(result.get("success")):
                    decision = "order_would_submit"
                elif bool(result.get("blocked")) or str(result.get("reason") or "").endswith("guard_block"):
                    decision = "order_blocked"
                else:
                    decision = "native_handoff_rejected"
                self.order_worker._mark_completed(intent, order_result=result)
                created_at = getattr(intent, "created_at", None)
                latency_ms = 0.0
                if isinstance(created_at, datetime):
                    latency_ms = max((event_ts - created_at).total_seconds() * 1000.0, 0.0)
                self._record_timeline(
                    self._order_timeline,
                    {
                        "event_ts": event_ts,
                        "symbol": intent.symbol,
                        "strategy_tag": intent.strategy_tag,
                        "order_decision": decision,
                        "reject_reason": str(result.get("reason") or result.get("message") or ""),
                        "queue_depth": self.entry_queue.qsize(),
                        "end_to_end_latency_ms": latency_ms,
                    },
                )
            finally:
                self.entry_queue.complete(intent)

    def _apply_event(self, event: ReplayEvent) -> None:
        payload = dict(event.payload or {})
        if event.event_type == "quote":
            symbol = event.symbol
            if not symbol:
                return
            self.executor.set_quote(
                symbol,
                {
                    "stock_name": str(payload.get("stock_name") or ""),
                    "current_price": float(payload.get("current_price", 0.0) or 0.0),
                    "open_price": float(payload.get("open_price", 0.0) or 0.0),
                    "best_bid": float(payload.get("best_bid", 0.0) or 0.0),
                    "best_ask": float(payload.get("best_ask", 0.0) or 0.0),
                    "session_high": float(payload.get("session_high", 0.0) or 0.0),
                    "session_low": float(payload.get("session_low", 0.0) or 0.0),
                    "received_at": event.event_ts,
                    "source": str(payload.get("source") or "replay_quote"),
                },
            )
            if self._setup_due(symbol, event.event_ts):
                self._setup_for_symbol(symbol, event.event_ts)
            self._timing_for_symbol(symbol, event.event_ts)
        elif event.event_type == "intraday_bar":
            symbol = event.symbol
            if not symbol:
                return
            bar = dict(payload)
            bar.setdefault("start_at", event.event_ts.isoformat())
            provider_ready = bool(payload.get("provider_ready", True))
            self.executor.set_intraday_bar(symbol, bar, provider_ready=provider_ready)
        elif event.event_type == "daily_context":
            symbol = event.symbol
            if not symbol:
                return
            self.executor.set_daily_context(symbol, _build_daily_context_from_payload(symbol, event.event_ts, payload))
            self._last_setup_eval_at.pop(_normalize_symbol(symbol), None)
        elif event.event_type == "regime_snapshot":
            self.executor.set_regime_snapshot(
                ReplayRegimeSnapshot(
                    regime=payload.get("regime"),
                    is_stale=bool(payload.get("is_stale", False)),
                    error_state=str(payload.get("error_state") or ""),
                    as_of=event.event_ts,
                )
            )
        elif event.event_type == "risk_snapshot":
            self.executor.set_risk_state(payload)
        elif event.event_type == "degraded_state_change":
            self.executor._degraded_controller.set_state(
                is_degraded=bool(payload.get("is_degraded", False)),
                reason=str(payload.get("reason") or ""),
            )
        elif event.event_type == "day_boundary":
            self.executor.clear_for_day_boundary()
            self._last_setup_eval_at.clear()
            self.candidate_store.cleanup_expired(event.event_ts + pd.Timedelta(days=365))
        else:
            if self.strict:
                raise ValueError(f"unsupported event_type={event.event_type}")

    def run(self, events: Sequence[ReplayEvent]) -> Dict[str, Any]:
        for event in list(events or []):
            self.executor._replay_now = event.event_ts
            self._apply_event(event)
            self._drain_queue(event.event_ts)
        return {
            "input": {
                "events": len(list(events or [])),
                "strategies": list(self.enabled_tags),
            },
            "candidate_timeline": self._candidate_timeline,
            "intent_timeline": self._intent_timeline,
            "order_timeline": self._order_timeline,
            "summary": {
                "candidates_created": sum(1 for item in self._candidate_timeline if item.get("setup_candidate_created")),
                "intents_emitted": sum(1 for item in self._intent_timeline if item.get("intent_emitted")),
                "orders_would_submit": sum(1 for item in self._order_timeline if item.get("order_decision") == "order_would_submit"),
                "queue_ingressed": self._queue_ingress_count,
                "reject_reason_summary": dict(self._skip_reason_counts),
                "submitted_orders": list(self.executor._submitted_orders),
            },
        }


def _build_daily_context_from_payload(symbol: str, event_ts: datetime, payload: Dict[str, Any]) -> DailyContext:
    bars = list(payload.get("recent_bars") or payload.get("bars") or [])
    if not bars:
        raise ValueError("daily_context event requires recent_bars or bars")
    normalized_bars = [dict(bar) for bar in bars]
    latest = normalized_bars[-1]
    trade_date = str(payload.get("trade_date") or str(latest.get("date") or "")[:10] or event_ts.date().isoformat())
    swing_high = float(payload.get("swing_high", max(float(bar.get("high", 0.0) or 0.0) for bar in normalized_bars)) or 0.0)
    swing_low = float(payload.get("swing_low", min(float(bar.get("low", 0.0) or 0.0) for bar in normalized_bars)) or 0.0)
    return DailyContext(
        symbol=_normalize_symbol(symbol),
        trade_date=trade_date,
        context_version=str(payload.get("context_version") or f"{trade_date}:{len(normalized_bars)}"),
        recent_bars=tuple(normalized_bars),
        prev_high=float(payload.get("prev_high", latest.get("prev_high", 0.0)) or 0.0),
        prev_close=float(payload.get("prev_close", latest.get("prev_close", 0.0)) or 0.0),
        atr=float(payload.get("atr", latest.get("atr", 0.0)) or 0.0),
        adx=float(payload.get("adx", latest.get("adx", 0.0)) or 0.0),
        trend=str(payload.get("trend", latest.get("trend", "")) or ""),
        ma20=float(payload.get("ma20", latest.get("ma20", 0.0)) or 0.0),
        ma50=float(payload.get("ma50", payload.get("ma", latest.get("ma", 0.0))) or 0.0),
        swing_high=swing_high,
        swing_low=swing_low,
        refreshed_at=event_ts,
        source=str(payload.get("source") or "replay_daily_context"),
    )


def load_replay_events(path: str | Path, *, strict: bool = False) -> List[ReplayEvent]:
    events: List[ReplayEvent] = []
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as handle:
        for input_index, raw_line in enumerate(handle):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            payload = json.loads(line)
            event_type = str(payload.get("event_type") or payload.get("type") or "").strip() or "quote"
            if strict and event_type not in SUPPORTED_EVENT_TYPES:
                raise ValueError(f"unsupported event_type={event_type} at line {input_index + 1}")
            if event_type not in SUPPORTED_EVENT_TYPES:
                continue
            event_ts = _parse_datetime(payload.get("event_ts") or payload.get("ts") or payload.get("event_at"))
            symbol = _normalize_symbol(payload.get("symbol") or "")
            events.append(
                ReplayEvent(
                    event_type=event_type,
                    event_ts=event_ts,
                    input_index=input_index,
                    symbol=symbol,
                    payload=payload,
                )
            )
    events.sort(key=lambda item: (item.event_ts, item.input_index))
    return events


def build_replay_report(
    events: Sequence[ReplayEvent],
    *,
    strategy_filter: Sequence[str] | None = None,
    strict: bool = False,
) -> Dict[str, Any]:
    runner = ThreadedPipelineReplayRunner(strategy_filter=strategy_filter, strict=strict)
    return runner.run(events)


def _parse_strategy_filter(raw_value: str) -> Sequence[str]:
    normalized = str(raw_value or "all").strip().lower()
    if normalized == "all":
        return tuple(sorted(SUPPORTED_STRATEGIES))
    if normalized not in SUPPORTED_STRATEGIES:
        raise ValueError(f"unsupported strategy filter: {raw_value}")
    return (normalized,)


def _emit_report(report: Dict[str, Any], *, pretty: bool, dump_candidates: bool, dump_intents: bool, dump_orders: bool) -> str:
    payload = dict(report)
    if not dump_candidates:
        payload.pop("candidate_timeline", None)
    if not dump_intents:
        payload.pop("intent_timeline", None)
    if not dump_orders:
        payload.pop("order_timeline", None)
    return json.dumps(payload, ensure_ascii=True, indent=2 if pretty else None, sort_keys=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic replay for threaded strategy pipeline.")
    parser.add_argument("--input", required=True, help="Path to replay JSONL input.")
    parser.add_argument("--strategy", default="all", help="pullback_rebreakout|opening_range_breakout|trend_atr|all")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    parser.add_argument("--dump-candidates", action="store_true", help="Include candidate timeline.")
    parser.add_argument("--dump-intents", action="store_true", help="Include intent timeline.")
    parser.add_argument("--dump-orders", action="store_true", help="Include order timeline.")
    parser.add_argument("--strict", action="store_true", help="Fail on unsupported/malformed events.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    events = load_replay_events(args.input, strict=bool(args.strict))
    report = build_replay_report(
        events,
        strategy_filter=_parse_strategy_filter(args.strategy),
        strict=bool(args.strict),
    )
    print(
        _emit_report(
            report,
            pretty=bool(args.pretty),
            dump_candidates=bool(args.dump_candidates),
            dump_intents=bool(args.dump_intents),
            dump_orders=bool(args.dump_orders),
        )
    )
    return 0
