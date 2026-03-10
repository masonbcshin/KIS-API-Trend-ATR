from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

from kis_trend_atr_trading.engine.evaluation_scheduler import (
    EvaluationCadenceTracker,
    EvaluationSchedulerConfig,
    SymbolEvaluationScheduler,
)
from kis_trend_atr_trading.engine.runtime_state_machine import SymbolBarGate


def test_fast_eval_scheduler_prioritizes_exit_symbols_and_respects_cooldowns():
    scheduler = SymbolEvaluationScheduler(
        EvaluationSchedulerConfig(
            entry_cooldown_sec=12.0,
            entry_debounce_sec=2.0,
            exit_cooldown_sec=5.0,
            exit_debounce_sec=1.0,
            rest_fallback_cooldown_sec=30.0,
            loop_sleep_sec=1.0,
        )
    )
    symbols = ["000001", "000002"]
    has_position = {"000001": True, "000002": False}

    for symbol in symbols:
        scheduler.mark_force(symbol, reason="startup")

    due = scheduler.due_evaluations(
        symbols=symbols,
        has_position_by_symbol=has_position,
        now_monotonic=0.0,
        ws_connected=True,
    )
    assert [item.symbol for item in due] == ["000001", "000002"]

    base_dt = datetime(2026, 3, 10, 9, 0, 0)
    for item in due:
        scheduler.mark_evaluated(
            item.symbol,
            evaluated_at=base_dt,
            evaluated_monotonic=0.0,
            reason=item.reason,
        )

    for second in range(1, 12):
        now_dt = base_dt + timedelta(seconds=second)
        for symbol in symbols:
            scheduler.mark_quote_event(symbol, event_monotonic=float(second), received_at=now_dt)
        due = scheduler.due_evaluations(
            symbols=symbols,
            has_position_by_symbol=has_position,
            now_monotonic=float(second),
            ws_connected=True,
        )
        if second < 5:
            assert due == []
        elif second in (5, 10):
            assert [item.symbol for item in due] == ["000001"]
            scheduler.mark_evaluated(
                "000001",
                evaluated_at=now_dt,
                evaluated_monotonic=float(second),
                reason=due[0].reason,
            )
        else:
            assert due == []

    now_dt = base_dt + timedelta(seconds=12)
    for symbol in symbols:
        scheduler.mark_quote_event(symbol, event_monotonic=12.0, received_at=now_dt)
    due = scheduler.due_evaluations(
        symbols=symbols,
        has_position_by_symbol=has_position,
        now_monotonic=12.0,
        ws_connected=True,
    )
    assert [item.symbol for item in due] == ["000002"]


def _simulate_legacy_bar_gate(symbols: list[str], duration_sec: int = 180) -> dict[str, list[float]]:
    gate = SymbolBarGate()
    intervals: dict[str, list[float]] = {symbol: [] for symbol in symbols}
    last_eval_at: dict[str, datetime] = {}
    start_dt = datetime(2026, 3, 10, 9, 0, 0)

    for loop_sec in range(30, duration_sec + 1, 30):
        completed_minute = (loop_sec // 60) - 1
        if completed_minute < 0:
            continue
        bar_ts = start_dt + timedelta(minutes=completed_minute)
        eval_dt = start_dt + timedelta(seconds=loop_sec)
        for symbol in symbols:
            if not gate.should_run(symbol, bar_ts):
                continue
            previous = last_eval_at.get(symbol)
            if previous is not None:
                intervals[symbol].append((eval_dt - previous).total_seconds())
            last_eval_at[symbol] = eval_dt
            gate.mark_processed(symbol, bar_ts)

    return intervals


def _simulate_fast_eval(symbols: list[str], holding_symbols: set[str], duration_sec: int = 180) -> dict:
    scheduler = SymbolEvaluationScheduler(
        EvaluationSchedulerConfig(
            entry_cooldown_sec=12.0,
            entry_debounce_sec=2.0,
            exit_cooldown_sec=5.0,
            exit_debounce_sec=1.0,
            rest_fallback_cooldown_sec=30.0,
            loop_sleep_sec=1.0,
        )
    )
    tracker = EvaluationCadenceTracker()
    start_dt = datetime(2026, 3, 10, 9, 0, 0)
    has_position_by_symbol = {symbol: (symbol in holding_symbols) for symbol in symbols}

    for symbol in symbols:
        scheduler.mark_force(symbol, reason="startup")

    for second in range(duration_sec + 1):
        now_dt = start_dt + timedelta(seconds=second)
        now_monotonic = float(second)
        for symbol in symbols:
            scheduler.mark_quote_event(symbol, event_monotonic=now_monotonic, received_at=now_dt)
        due = scheduler.due_evaluations(
            symbols=symbols,
            has_position_by_symbol=has_position_by_symbol,
            now_monotonic=now_monotonic,
            ws_connected=True,
        )
        for item in due:
            interval_sec = scheduler.mark_evaluated(
                item.symbol,
                evaluated_at=now_dt,
                evaluated_monotonic=now_monotonic,
                reason=item.reason,
            )
            tracker.record(
                symbol=item.symbol,
                evaluated_at=now_dt,
                interval_sec=interval_sec,
                quote_age_sec=0.2,
                path="fast_ws",
                reason=item.reason,
                has_position=item.has_position,
                daily_fetch_calls=0,
                rest_quote_calls=0,
                account_snapshot_calls=0,
                ws_reconnect_count=0,
                ws_fallback_count=0,
            )

    return tracker.summary()


def test_fast_eval_benchmark_reduces_entry_cadence_to_target_band():
    symbols = [f"{idx:06d}" for idx in range(1, 9)]
    holding_symbols = {symbols[0], symbols[1]}

    legacy = _simulate_legacy_bar_gate(symbols)
    fast = _simulate_fast_eval(symbols, holding_symbols)

    legacy_entry_intervals = [
        interval
        for symbol in symbols[2:]
        for interval in legacy.get(symbol, [])
    ]
    fast_entry_p50 = median(
        [
            float(fast["symbols"][symbol]["p50_interval_sec"] or 0.0)
            for symbol in symbols[2:]
        ]
    )
    fast_exit_p50 = median(
        [
            float(fast["symbols"][symbol]["p50_interval_sec"] or 0.0)
            for symbol in holding_symbols
        ]
    )
    legacy_entry_p50 = median(legacy_entry_intervals)

    print(
        {
            "legacy_entry_p50_sec": legacy_entry_p50,
            "fast_entry_p50_sec": fast_entry_p50,
            "fast_exit_p50_sec": fast_exit_p50,
            "fast_global": fast["global"],
        }
    )

    assert legacy_entry_p50 >= 40.0
    assert 10.0 <= fast_entry_p50 <= 15.0
    assert fast_exit_p50 <= 6.0
