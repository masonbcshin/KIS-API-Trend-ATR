"""Paper-safe replay harness for comparing legacy and fast evaluation cadence.

Input format is JSONL with one quote event per line. Required fields:
  - ts or event_at: ISO-8601 timestamp
  - symbol: stock code

Optional fields:
  - received_at: quote receive timestamp (defaults to ts)
  - has_position: current holding state for the symbol
  - ws_connected: global WS connectivity state at the event time
  - current_price, open_price, best_bid, best_ask, stock_name
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence


def _load_local_module(module_name: str, relative_path: str):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    if not module_path.exists():
        raise ImportError(f"unable to load module: {module_path}")
    module = types.ModuleType(module_name)
    module.__file__ = str(module_path)
    sys.modules[module_name] = module
    exec(compile(module_path.read_text(encoding="utf-8"), str(module_path), "exec"), module.__dict__)
    return module


_evaluation_scheduler = _load_local_module(
    "_fast_eval_replay_evaluation_scheduler",
    "engine/evaluation_scheduler.py",
)

EvaluationCadenceTracker = _evaluation_scheduler.EvaluationCadenceTracker
EvaluationSchedulerConfig = _evaluation_scheduler.EvaluationSchedulerConfig
SymbolEvaluationScheduler = _evaluation_scheduler.SymbolEvaluationScheduler


class SymbolBarGate:
    """Local copy of the legacy completed-bar gate for replay use."""

    def __init__(self) -> None:
        self._last_processed: Dict[str, datetime] = {}

    def should_run(self, symbol: str, bar_ts: Optional[datetime]) -> bool:
        if bar_ts is None:
            return False
        code = str(symbol).zfill(6)
        prev = self._last_processed.get(code)
        if prev is not None and bar_ts <= prev:
            return False
        return True

    def mark_processed(self, symbol: str, bar_ts: datetime) -> None:
        self._last_processed[str(symbol).zfill(6)] = bar_ts


@dataclass(frozen=True)
class ReplayQuoteEvent:
    symbol: str
    event_at: datetime
    received_at: datetime
    has_position: Optional[bool] = None
    ws_connected: Optional[bool] = None
    current_price: Optional[float] = None
    open_price: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    stock_name: str = ""


def _parse_datetime(value: object) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("timestamp is required")
    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _load_bool(payload: dict, key: str) -> Optional[bool]:
    if key not in payload:
        return None
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_quote_replay_events(path: str | Path) -> List[ReplayQuoteEvent]:
    events: List[ReplayQuoteEvent] = []
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            payload = json.loads(line)
            symbol = str(payload.get("symbol") or "").strip().zfill(6)
            if not symbol:
                raise ValueError(f"missing symbol at line {line_number}")
            event_at = _parse_datetime(payload.get("ts") or payload.get("event_at"))
            received_at = _parse_datetime(payload.get("received_at") or event_at.isoformat())
            events.append(
                ReplayQuoteEvent(
                    symbol=symbol,
                    event_at=event_at,
                    received_at=received_at,
                    has_position=_load_bool(payload, "has_position"),
                    ws_connected=_load_bool(payload, "ws_connected"),
                    current_price=(
                        float(payload["current_price"])
                        if payload.get("current_price") is not None
                        else None
                    ),
                    open_price=(
                        float(payload["open_price"])
                        if payload.get("open_price") is not None
                        else None
                    ),
                    best_bid=(
                        float(payload["best_bid"])
                        if payload.get("best_bid") is not None
                        else None
                    ),
                    best_ask=(
                        float(payload["best_ask"])
                        if payload.get("best_ask") is not None
                        else None
                    ),
                    stock_name=str(payload.get("stock_name") or ""),
                )
            )
    events.sort(key=lambda item: (item.event_at, item.symbol))
    return events


def _quote_age_sec(now_at: datetime, event: ReplayQuoteEvent) -> float:
    return max((now_at - event.received_at).total_seconds(), 0.0)


def _summary_p50(summary: dict, symbols: Iterable[str]) -> float:
    values = [
        float(dict(summary.get("symbols") or {}).get(symbol, {}).get("p50_interval_sec", 0.0) or 0.0)
        for symbol in symbols
        if symbol in dict(summary.get("symbols") or {})
    ]
    if not values:
        return 0.0
    return float(median(values))


def _replay_legacy(
    events: Sequence[ReplayQuoteEvent],
    *,
    symbols: Sequence[str],
    holding_symbols: Sequence[str],
    legacy_interval_sec: float,
) -> dict:
    tracker = EvaluationCadenceTracker()
    gate = SymbolBarGate()
    latest_by_symbol: Dict[str, ReplayQuoteEvent] = {}
    last_eval_at_by_symbol: Dict[str, datetime] = {}
    has_position_by_symbol = {symbol: (symbol in set(holding_symbols)) for symbol in symbols}
    event_idx = 0
    start_at = events[0].event_at
    end_at = events[-1].event_at
    current_at = start_at + timedelta(seconds=float(legacy_interval_sec))

    while current_at <= end_at:
        while event_idx < len(events) and events[event_idx].event_at <= current_at:
            event = events[event_idx]
            latest_by_symbol[event.symbol] = event
            if event.has_position is not None:
                has_position_by_symbol[event.symbol] = bool(event.has_position)
            event_idx += 1

        bar_ts = current_at.replace(second=0, microsecond=0) - timedelta(minutes=1)
        for symbol in symbols:
            latest = latest_by_symbol.get(symbol)
            if latest is None:
                continue
            if not gate.should_run(symbol, bar_ts):
                continue
            previous_eval_at = last_eval_at_by_symbol.get(symbol)
            interval_sec = (
                (current_at - previous_eval_at).total_seconds()
                if previous_eval_at is not None
                else None
            )
            tracker.record(
                symbol=symbol,
                evaluated_at=current_at,
                interval_sec=interval_sec,
                quote_age_sec=_quote_age_sec(current_at, latest),
                path="legacy_bar",
                reason="completed_bar",
                has_position=bool(has_position_by_symbol.get(symbol, False)),
                daily_fetch_calls=0,
                rest_quote_calls=0,
                account_snapshot_calls=0,
                ws_reconnect_count=0,
                ws_fallback_count=0,
            )
            last_eval_at_by_symbol[symbol] = current_at
            gate.mark_processed(symbol, bar_ts)
        current_at += timedelta(seconds=float(legacy_interval_sec))

    return tracker.summary()


def _replay_fast(
    events: Sequence[ReplayQuoteEvent],
    *,
    symbols: Sequence[str],
    holding_symbols: Sequence[str],
    scheduler_config: EvaluationSchedulerConfig,
) -> dict:
    tracker = EvaluationCadenceTracker()
    scheduler = SymbolEvaluationScheduler(scheduler_config)
    latest_by_symbol: Dict[str, ReplayQuoteEvent] = {}
    has_position_by_symbol = {symbol: (symbol in set(holding_symbols)) for symbol in symbols}
    start_at = events[0].event_at
    end_at = events[-1].event_at
    loop_sleep_sec = max(float(scheduler_config.loop_sleep_sec), 0.1)
    current_at = start_at
    ws_connected = True
    ws_disconnects = 0
    ws_reconnects = 0
    event_idx = 0

    for symbol in symbols:
        scheduler.mark_force(symbol, reason="startup")

    while current_at <= end_at:
        while event_idx < len(events) and events[event_idx].event_at <= current_at:
            event = events[event_idx]
            latest_by_symbol[event.symbol] = event
            if event.has_position is not None:
                has_position_by_symbol[event.symbol] = bool(event.has_position)
            if event.ws_connected is not None and bool(event.ws_connected) != ws_connected:
                next_state = bool(event.ws_connected)
                if next_state:
                    ws_reconnects += 1
                    for symbol in symbols:
                        scheduler.mark_force(symbol, reason="feed_switch")
                else:
                    ws_disconnects += 1
                ws_connected = next_state
            scheduler.mark_quote_event(
                event.symbol,
                event_monotonic=(event.event_at - start_at).total_seconds(),
                received_at=event.received_at,
            )
            event_idx += 1

        due = scheduler.due_evaluations(
            symbols=list(symbols),
            has_position_by_symbol=has_position_by_symbol,
            now_monotonic=(current_at - start_at).total_seconds(),
            ws_connected=ws_connected,
        )
        for item in due:
            latest = latest_by_symbol.get(item.symbol)
            if latest is None:
                continue
            interval_sec = scheduler.mark_evaluated(
                item.symbol,
                evaluated_at=current_at,
                evaluated_monotonic=(current_at - start_at).total_seconds(),
                reason=item.reason,
            )
            tracker.record(
                symbol=item.symbol,
                evaluated_at=current_at,
                interval_sec=interval_sec,
                quote_age_sec=_quote_age_sec(current_at, latest),
                path="fast_ws" if ws_connected else "fast_rest_fallback",
                reason=item.reason,
                has_position=item.has_position,
                daily_fetch_calls=0,
                rest_quote_calls=0,
                account_snapshot_calls=0,
                ws_reconnect_count=0,
                ws_fallback_count=0,
            )
        current_at += timedelta(seconds=loop_sleep_sec)

    summary = tracker.summary()
    global_summary = dict(summary.get("global") or {})
    global_summary["ws_reconnect_count"] = ws_reconnects
    global_summary["ws_fallback_count"] = ws_disconnects
    summary["global"] = global_summary
    return summary


def build_replay_report(
    events: Sequence[ReplayQuoteEvent],
    *,
    holding_symbols: Optional[Sequence[str]] = None,
    legacy_interval_sec: float = 30.0,
    scheduler_config: Optional[EvaluationSchedulerConfig] = None,
    source_path: str = "",
) -> dict:
    if not events:
        raise ValueError("at least one replay event is required")

    symbols = sorted({event.symbol for event in events})
    holding = [str(symbol).zfill(6) for symbol in (holding_symbols or [])]
    config = scheduler_config or EvaluationSchedulerConfig(
        entry_cooldown_sec=12.0,
        entry_debounce_sec=2.0,
        exit_cooldown_sec=5.0,
        exit_debounce_sec=1.0,
        rest_fallback_cooldown_sec=30.0,
        loop_sleep_sec=1.0,
    )
    legacy = _replay_legacy(
        events,
        symbols=symbols,
        holding_symbols=holding,
        legacy_interval_sec=legacy_interval_sec,
    )
    fast = _replay_fast(
        events,
        symbols=symbols,
        holding_symbols=holding,
        scheduler_config=config,
    )
    entry_symbols = [symbol for symbol in symbols if symbol not in set(holding)]
    exit_symbols = [symbol for symbol in symbols if symbol in set(holding)]
    fast_entry_p50 = _summary_p50(fast, entry_symbols)
    legacy_entry_p50 = _summary_p50(legacy, entry_symbols)
    fast_exit_p50 = _summary_p50(fast, exit_symbols)
    legacy_exit_p50 = _summary_p50(legacy, exit_symbols)
    speedup_ratio = (legacy_entry_p50 / fast_entry_p50) if fast_entry_p50 > 0 else 0.0

    return {
        "input": {
            "source_path": source_path,
            "events": len(events),
            "symbols": symbols,
            "holding_symbols": holding,
            "start_at": events[0].event_at.isoformat(),
            "end_at": events[-1].event_at.isoformat(),
        },
        "legacy": legacy,
        "fast": fast,
        "comparison": {
            "legacy_entry_p50_sec": legacy_entry_p50,
            "legacy_exit_p50_sec": legacy_exit_p50,
            "fast_entry_p50_sec": fast_entry_p50,
            "fast_exit_p50_sec": fast_exit_p50,
            "entry_p50_improvement_sec": max(legacy_entry_p50 - fast_entry_p50, 0.0),
            "entry_speedup_ratio": speedup_ratio,
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper-safe fast-eval replay harness")
    parser.add_argument("--input", required=True, help="Path to replay JSONL file")
    parser.add_argument(
        "--holding-symbol",
        action="append",
        default=[],
        help="Treat symbol as already-held so exit cadence can be compared",
    )
    parser.add_argument("--legacy-interval-sec", type=float, default=30.0)
    parser.add_argument("--fast-entry-cooldown-sec", type=float, default=12.0)
    parser.add_argument("--fast-entry-debounce-sec", type=float, default=2.0)
    parser.add_argument("--fast-exit-cooldown-sec", type=float, default=5.0)
    parser.add_argument("--fast-exit-debounce-sec", type=float, default=1.0)
    parser.add_argument("--fast-rest-fallback-cooldown-sec", type=float, default=30.0)
    parser.add_argument("--fast-loop-sleep-sec", type=float, default=1.0)
    parser.add_argument("--output", help="Write report JSON to file instead of stdout")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    events = load_quote_replay_events(args.input)
    report = build_replay_report(
        events,
        holding_symbols=args.holding_symbol,
        legacy_interval_sec=float(args.legacy_interval_sec),
        scheduler_config=EvaluationSchedulerConfig(
            entry_cooldown_sec=float(args.fast_entry_cooldown_sec),
            entry_debounce_sec=float(args.fast_entry_debounce_sec),
            exit_cooldown_sec=float(args.fast_exit_cooldown_sec),
            exit_debounce_sec=float(args.fast_exit_debounce_sec),
            rest_fallback_cooldown_sec=float(args.fast_rest_fallback_cooldown_sec),
            loop_sleep_sec=float(args.fast_loop_sleep_sec),
        ),
        source_path=str(Path(args.input)),
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
