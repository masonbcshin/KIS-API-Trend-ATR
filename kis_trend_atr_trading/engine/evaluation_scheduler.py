from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Dict, List, Optional


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(min(float(pct), 1.0), 0.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] + ((ordered[high] - ordered[low]) * weight)


@dataclass(frozen=True)
class EvaluationSchedulerConfig:
    entry_cooldown_sec: float
    entry_debounce_sec: float
    exit_cooldown_sec: float
    exit_debounce_sec: float
    rest_fallback_cooldown_sec: float
    loop_sleep_sec: float


@dataclass
class SymbolEvaluationState:
    last_event_monotonic: Optional[float] = None
    dirty_since_monotonic: Optional[float] = None
    last_quote_received_at: Optional[datetime] = None
    last_eval_monotonic: Optional[float] = None
    last_eval_at: Optional[datetime] = None
    last_eval_reason: str = "startup"
    dirty: bool = False
    force_due: bool = True
    force_reason: str = "startup"


@dataclass(frozen=True)
class DueEvaluation:
    symbol: str
    reason: str
    has_position: bool


class SymbolEvaluationScheduler:
    """Per-symbol cooldown/debounce scheduler for fast evaluation."""

    def __init__(self, config: EvaluationSchedulerConfig):
        self.config = config
        self._states: Dict[str, SymbolEvaluationState] = {}

    def _state_for(self, symbol: str) -> SymbolEvaluationState:
        code = str(symbol).zfill(6)
        state = self._states.get(code)
        if state is None:
            state = SymbolEvaluationState()
            self._states[code] = state
        return state

    def mark_quote_event(
        self,
        symbol: str,
        *,
        event_monotonic: float,
        received_at: Optional[datetime],
    ) -> None:
        state = self._state_for(symbol)
        if not state.dirty or state.dirty_since_monotonic is None:
            state.dirty_since_monotonic = float(event_monotonic)
        state.last_event_monotonic = float(event_monotonic)
        state.last_quote_received_at = received_at
        state.dirty = True

    def mark_force(self, symbol: str, reason: str = "force") -> None:
        state = self._state_for(symbol)
        state.force_due = True
        state.force_reason = str(reason or "force")

    def state_snapshot(self, symbol: str) -> Dict[str, object]:
        state = self._state_for(symbol)
        return {
            "last_event_monotonic": state.last_event_monotonic,
            "dirty_since_monotonic": state.dirty_since_monotonic,
            "last_quote_received_at": state.last_quote_received_at,
            "last_eval_monotonic": state.last_eval_monotonic,
            "last_eval_at": state.last_eval_at,
            "last_eval_reason": state.last_eval_reason,
            "dirty": state.dirty,
            "force_due": state.force_due,
            "force_reason": state.force_reason,
        }

    def due_evaluations(
        self,
        *,
        symbols: List[str],
        has_position_by_symbol: Dict[str, bool],
        now_monotonic: float,
        ws_connected: bool,
    ) -> List[DueEvaluation]:
        due: List[DueEvaluation] = []
        now_value = float(now_monotonic)

        for symbol in symbols:
            code = str(symbol).zfill(6)
            state = self._state_for(code)
            has_position = bool(has_position_by_symbol.get(code, False))
            cooldown = (
                float(self.config.exit_cooldown_sec)
                if has_position
                else float(self.config.entry_cooldown_sec)
            )
            debounce = (
                float(self.config.exit_debounce_sec)
                if has_position
                else float(self.config.entry_debounce_sec)
            )
            last_eval = state.last_eval_monotonic
            cooldown_ready = last_eval is None or (now_value - last_eval) >= cooldown

            if state.force_due and cooldown_ready:
                due.append(
                    DueEvaluation(
                        symbol=code,
                        reason=state.force_reason or "force",
                        has_position=has_position,
                    )
                )
                continue

            if ws_connected:
                dirty_since = state.dirty_since_monotonic
                if not state.dirty or dirty_since is None:
                    continue
                if (now_value - dirty_since) < debounce:
                    continue
                if not cooldown_ready:
                    continue
                due.append(
                    DueEvaluation(
                        symbol=code,
                        reason="quote_event",
                        has_position=has_position,
                    )
                )
                continue

            if cooldown_ready:
                due.append(
                    DueEvaluation(
                        symbol=code,
                        reason="rest_fallback",
                        has_position=has_position,
                    )
                )

        due.sort(key=lambda item: (not item.has_position, item.symbol))
        return due

    def mark_evaluated(
        self,
        symbol: str,
        *,
        evaluated_at: datetime,
        evaluated_monotonic: float,
        reason: str,
    ) -> Optional[float]:
        state = self._state_for(symbol)
        interval_sec = None
        if state.last_eval_monotonic is not None:
            interval_sec = max(float(evaluated_monotonic) - float(state.last_eval_monotonic), 0.0)
        state.last_eval_monotonic = float(evaluated_monotonic)
        state.last_eval_at = evaluated_at
        state.last_eval_reason = str(reason or "unknown")
        state.force_due = False
        state.force_reason = ""
        if reason != "rest_fallback":
            state.dirty = False
            state.dirty_since_monotonic = None
        return interval_sec


@dataclass
class SymbolCadenceMetrics:
    last_eval_at: Optional[datetime] = None
    last_interval_sec: Optional[float] = None
    intervals_sec: List[float] = field(default_factory=list)
    quote_ages_sec: List[float] = field(default_factory=list)
    evaluations: int = 0
    daily_fetch_calls: int = 0
    rest_quote_calls: int = 0
    account_snapshot_calls: int = 0
    ws_reconnect_count: int = 0
    ws_fallback_count: int = 0
    last_path: str = ""
    last_reason: str = ""
    has_position: bool = False


class EvaluationCadenceTracker:
    """Collects per-symbol cadence and hot-path dependency counters."""

    def __init__(self) -> None:
        self._metrics: Dict[str, SymbolCadenceMetrics] = {}

    def record(
        self,
        *,
        symbol: str,
        evaluated_at: datetime,
        interval_sec: Optional[float],
        quote_age_sec: float,
        path: str,
        reason: str,
        has_position: bool,
        daily_fetch_calls: int,
        rest_quote_calls: int,
        account_snapshot_calls: int,
        ws_reconnect_count: int,
        ws_fallback_count: int,
    ) -> None:
        code = str(symbol).zfill(6)
        metric = self._metrics.get(code)
        if metric is None:
            metric = SymbolCadenceMetrics()
            self._metrics[code] = metric

        metric.evaluations += 1
        metric.last_eval_at = evaluated_at
        metric.last_interval_sec = interval_sec
        metric.last_path = str(path or "")
        metric.last_reason = str(reason or "")
        metric.has_position = bool(has_position)
        metric.daily_fetch_calls += max(int(daily_fetch_calls or 0), 0)
        metric.rest_quote_calls += max(int(rest_quote_calls or 0), 0)
        metric.account_snapshot_calls += max(int(account_snapshot_calls or 0), 0)
        metric.ws_reconnect_count += max(int(ws_reconnect_count or 0), 0)
        metric.ws_fallback_count += max(int(ws_fallback_count or 0), 0)

        quote_age = max(float(quote_age_sec or 0.0), 0.0)
        metric.quote_ages_sec.append(quote_age)
        if len(metric.quote_ages_sec) > 512:
            metric.quote_ages_sec = metric.quote_ages_sec[-512:]

        if interval_sec is not None:
            metric.intervals_sec.append(max(float(interval_sec), 0.0))
            if len(metric.intervals_sec) > 512:
                metric.intervals_sec = metric.intervals_sec[-512:]

    def summary(self) -> Dict[str, object]:
        per_symbol: Dict[str, Dict[str, object]] = {}
        merged_intervals: List[float] = []
        merged_quote_ages: List[float] = []
        total_daily_fetch_calls = 0
        total_rest_quote_calls = 0
        total_account_snapshot_calls = 0
        total_ws_reconnect_count = 0
        total_ws_fallback_count = 0

        for symbol, metric in sorted(self._metrics.items()):
            merged_intervals.extend(metric.intervals_sec)
            merged_quote_ages.extend(metric.quote_ages_sec)
            total_daily_fetch_calls += metric.daily_fetch_calls
            total_rest_quote_calls += metric.rest_quote_calls
            total_account_snapshot_calls += metric.account_snapshot_calls
            total_ws_reconnect_count += metric.ws_reconnect_count
            total_ws_fallback_count += metric.ws_fallback_count
            per_symbol[symbol] = {
                "last_eval_at": metric.last_eval_at.isoformat() if metric.last_eval_at else None,
                "last_interval_sec": metric.last_interval_sec,
                "p50_interval_sec": median(metric.intervals_sec) if metric.intervals_sec else 0.0,
                "p90_interval_sec": _percentile(metric.intervals_sec, 0.90),
                "quote_age_p50_sec": median(metric.quote_ages_sec) if metric.quote_ages_sec else 0.0,
                "evaluations": metric.evaluations,
                "daily_fetch_calls": metric.daily_fetch_calls,
                "rest_quote_calls": metric.rest_quote_calls,
                "account_snapshot_calls": metric.account_snapshot_calls,
                "ws_reconnect_count": metric.ws_reconnect_count,
                "ws_fallback_count": metric.ws_fallback_count,
                "last_path": metric.last_path,
                "last_reason": metric.last_reason,
                "has_position": metric.has_position,
            }

        return {
            "symbols": per_symbol,
            "global": {
                "p50_interval_sec": median(merged_intervals) if merged_intervals else 0.0,
                "p90_interval_sec": _percentile(merged_intervals, 0.90),
                "quote_age_p50_sec": median(merged_quote_ages) if merged_quote_ages else 0.0,
                "quote_age_p90_sec": _percentile(merged_quote_ages, 0.90),
                "daily_fetch_calls": total_daily_fetch_calls,
                "rest_quote_calls": total_rest_quote_calls,
                "account_snapshot_calls": total_account_snapshot_calls,
                "ws_reconnect_count": total_ws_reconnect_count,
                "ws_fallback_count": total_ws_fallback_count,
                "evaluations": sum(metric.evaluations for metric in self._metrics.values()),
            },
        }
