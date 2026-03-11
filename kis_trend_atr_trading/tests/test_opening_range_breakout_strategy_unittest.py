from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
import sys
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import strategy.multiday_trend_atr as multiday_trend_atr
from engine.strategy_pipeline_registry import build_default_strategy_registry
from strategy.multiday_trend_atr import MultidayTrendATRStrategy, SignalType
from utils.market_hours import KST
from utils.market_phase import VenueMarketPhase


def _kst_dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return KST.localize(datetime(year, month, day, hour, minute, 0))


def _make_intraday_bars(
    *,
    day: datetime,
    opening_range_high: float,
    opening_range_low: float,
    stale_breakout: bool = False,
) -> list[dict]:
    bars: list[dict] = []
    market_open = day.replace(hour=9, minute=0, second=0, microsecond=0)

    opening_closes = [
        opening_range_high * 0.997,
        opening_range_high * 0.998,
        opening_range_high * 0.999,
        opening_range_high * 0.9985,
        opening_range_high * 0.9992,
    ]
    for minute_idx in range(5):
        start_at = market_open + timedelta(minutes=minute_idx)
        close_price = opening_closes[minute_idx]
        bars.append(
            {
                "start_at": start_at,
                "open": close_price * 0.999,
                "high": opening_range_high if minute_idx == 1 else close_price * 1.001,
                "low": opening_range_low if minute_idx == 0 else close_price * 0.999,
                "close": close_price,
                "volume": 1000 + minute_idx * 100,
            }
        )

    for minute_idx in range(5, 31):
        start_at = market_open + timedelta(minutes=minute_idx)
        if stale_breakout and minute_idx >= 28:
            close_price = opening_range_high * 1.0035
        elif minute_idx >= 28:
            close_price = opening_range_high * (0.999 + ((minute_idx - 28) * 0.0004))
        else:
            close_price = opening_range_high * 0.9985
        bars.append(
            {
                "start_at": start_at,
                "open": close_price * 0.9995,
                "high": close_price * 1.001,
                "low": close_price * 0.9985,
                "close": close_price,
                "volume": 1500 + minute_idx * 50,
            }
        )
    return bars


def _orb_settings_patches() -> list[patch]:
    return [
        patch.object(multiday_trend_atr.settings, "ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY", True),
        patch.object(multiday_trend_atr.settings, "ORB_OPENING_RANGE_MINUTES", 5),
        patch.object(multiday_trend_atr.settings, "ORB_ENTRY_CUTOFF_MINUTES", 90),
        patch.object(multiday_trend_atr.settings, "ORB_MIN_OPEN_ABOVE_PREV_HIGH_PCT", 0.003),
        patch.object(multiday_trend_atr.settings, "ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_STOCK", 0.10),
        patch.object(multiday_trend_atr.settings, "ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_ETF", 0.05),
        patch.object(multiday_trend_atr.settings, "ORB_MAX_EXTENSION_PCT_STOCK", 0.01),
        patch.object(multiday_trend_atr.settings, "ORB_MAX_EXTENSION_PCT_ETF", 0.006),
        patch.object(multiday_trend_atr.settings, "ORB_REQUIRE_ABOVE_VWAP", True),
        patch.object(multiday_trend_atr.settings, "ORB_USE_ADX_FILTER", False),
        patch.object(multiday_trend_atr.settings, "ENABLE_ENTRY_GAP_FILTER", True),
        patch.object(multiday_trend_atr.settings, "MAX_ENTRY_GAP_PCT_STOCK", 0.015),
        patch.object(multiday_trend_atr.settings, "MAX_ENTRY_GAP_PCT_ETF", 0.01),
        patch.object(multiday_trend_atr.settings, "MAX_OPEN_VS_PREV_HIGH_PCT", 0.005),
        patch.object(multiday_trend_atr.settings, "ENABLE_BREAKOUT_EXTENSION_CAP", True),
        patch.object(multiday_trend_atr.settings, "MAX_BREAKOUT_EXTENSION_PCT_STOCK", 0.005),
        patch.object(multiday_trend_atr.settings, "MAX_BREAKOUT_EXTENSION_PCT_ETF", 0.004),
        patch.object(multiday_trend_atr.settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", False),
        patch.object(multiday_trend_atr.settings, "ENABLE_OPENING_NO_ENTRY_GUARD", False),
    ]


def test_orb_buy_signal_bypasses_prev_high_gap_filters_for_fresh_breakout(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    df_with_indicators = strategy.add_indicators(sample_uptrend_df)
    prev_high = float(df_with_indicators.iloc[-1]["prev_high"])
    decision_time = _kst_dt(2026, 2, 16, 9, 31)
    opening_range_high = prev_high * 1.018
    intraday_bars = _make_intraday_bars(
        day=decision_time,
        opening_range_high=opening_range_high,
        opening_range_low=prev_high * 1.006,
        stale_breakout=False,
    )
    actual_opening_range_high = max(float(bar["high"]) for bar in intraday_bars[:5])

    with ExitStack() as stack:
        for ctx in _orb_settings_patches():
            stack.enter_context(ctx)
        signal = strategy.generate_signal(
            df=sample_uptrend_df,
            current_price=opening_range_high * 1.004,
            open_price=prev_high * 1.015,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=decision_time,
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            intraday_bars=intraday_bars,
        )

    assert signal.signal_type == SignalType.BUY
    assert signal.meta["strategy_tag"] == "opening_range_breakout"
    assert signal.meta["entry_reference_label"] == "opening_range_high"
    assert signal.meta["entry_reference_price"] == actual_opening_range_high


def test_orb_blocks_late_chase_when_breakout_is_not_fresh(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    df_with_indicators = strategy.add_indicators(sample_uptrend_df)
    prev_high = float(df_with_indicators.iloc[-1]["prev_high"])
    decision_time = _kst_dt(2026, 2, 16, 9, 45)
    opening_range_high = prev_high * 1.018
    intraday_bars = _make_intraday_bars(
        day=decision_time,
        opening_range_high=opening_range_high,
        opening_range_low=prev_high * 1.006,
        stale_breakout=True,
    )

    with ExitStack() as stack:
        for ctx in _orb_settings_patches():
            stack.enter_context(ctx)
        stack.enter_context(
            patch.object(multiday_trend_atr.settings, "ORB_RECENT_BREAKOUT_LOOKBACK_BARS", 3)
        )
        stack.enter_context(
            patch.object(multiday_trend_atr.settings, "ORB_REARM_BAND_PCT", 0.002)
        )
        signal = strategy.generate_signal(
            df=sample_uptrend_df,
            current_price=opening_range_high * 1.006,
            open_price=prev_high * 1.015,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=decision_time,
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            intraday_bars=intraday_bars,
        )

    assert signal.signal_type == SignalType.HOLD
    assert signal.reason_code == "orb_breakout_not_fresh"


def test_orb_registry_lookup_returns_adapter_when_provided(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    registry = build_default_strategy_registry(
        pullback_strategy=strategy.pullback_strategy,
        trend_atr_strategy=strategy,
        orb_strategy=strategy.orb_strategy,
    )

    entry = registry.get("opening_range_breakout")

    assert entry is not None
    assert entry.strategy_tag == "opening_range_breakout"
    assert entry.capabilities.uses_intraday_timing is True
    assert entry.capabilities.market_regime_mode == "read_only"
    assert "opening_range_breakout" in registry.strategy_tags()


def test_orb_registry_adapter_parity_matches_legacy_buy_semantics(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    registry = build_default_strategy_registry(
        pullback_strategy=strategy.pullback_strategy,
        trend_atr_strategy=strategy,
        orb_strategy=strategy.orb_strategy,
    )
    entry = registry.get("opening_range_breakout")
    prepared_df = strategy.add_indicators(sample_uptrend_df)
    prev_high = float(prepared_df.iloc[-1]["prev_high"])
    decision_time = _kst_dt(2026, 2, 16, 9, 31)
    opening_range_high = prev_high * 1.018
    intraday_bars = _make_intraday_bars(
        day=decision_time,
        opening_range_high=opening_range_high,
        opening_range_low=prev_high * 1.006,
        stale_breakout=False,
    )
    actual_opening_range_high = max(float(bar["high"]) for bar in intraday_bars[:5])

    with ExitStack() as stack:
        for ctx in _orb_settings_patches():
            stack.enter_context(ctx)
        legacy_signal = strategy.generate_signal(
            df=sample_uptrend_df,
            current_price=opening_range_high * 1.004,
            open_price=prev_high * 1.015,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=decision_time,
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            intraday_bars=intraday_bars,
        )
        evaluation = entry.setup_evaluator.evaluate_setup(
            daily_df=sample_uptrend_df,
            daily_context=None,
            current_price=opening_range_high * 1.004,
            open_price=prev_high * 1.015,
            intraday_bars=intraday_bars,
            intraday_provider_ready=True,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=decision_time,
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            market_venue="KRX",
            has_existing_position=False,
            has_pending_order=False,
            market_regime_snapshot=None,
        )
        decision = entry.timing_evaluator.evaluate_timing(
            candidate=evaluation.candidate,
            native_candidate=evaluation.native_candidate,
            current_price=opening_range_high * 1.004,
            stock_code="005930",
            check_time=decision_time,
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            market_venue="KRX",
            intraday_bars=intraday_bars,
            has_existing_position=False,
            has_pending_order=False,
            current_context_version=None,
        )

    assert legacy_signal.signal_type == SignalType.BUY
    assert evaluation.candidate is not None
    assert decision.should_emit_intent is True
    assert evaluation.candidate.entry_reference_price == actual_opening_range_high
    assert evaluation.candidate.entry_reference_label == "opening_range_high"
    assert decision.entry_reference_price == legacy_signal.meta["entry_reference_price"]
    assert decision.meta["decision_meta"]["entry_reference_label"] == legacy_signal.meta["entry_reference_label"]
    assert round(decision.meta["decision_meta"]["extension_pct"], 6) == round(
        legacy_signal.meta["extension_pct"], 6
    )


def test_orb_registry_setup_blocks_before_opening_range_is_formed(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    registry = build_default_strategy_registry(
        pullback_strategy=strategy.pullback_strategy,
        trend_atr_strategy=strategy,
        orb_strategy=strategy.orb_strategy,
    )
    entry = registry.get("opening_range_breakout")
    prepared_df = strategy.add_indicators(sample_uptrend_df)
    prev_high = float(prepared_df.iloc[-1]["prev_high"])
    decision_time = _kst_dt(2026, 2, 16, 9, 3)
    intraday_bars = _make_intraday_bars(
        day=decision_time,
        opening_range_high=prev_high * 1.018,
        opening_range_low=prev_high * 1.006,
        stale_breakout=False,
    )[:3]

    with ExitStack() as stack:
        for ctx in _orb_settings_patches():
            stack.enter_context(ctx)
        evaluation = entry.setup_evaluator.evaluate_setup(
            daily_df=sample_uptrend_df,
            daily_context=None,
            current_price=prev_high * 1.003,
            open_price=prev_high * 1.015,
            intraday_bars=intraday_bars,
            intraday_provider_ready=True,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=decision_time,
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            market_venue="KRX",
            has_existing_position=False,
            has_pending_order=False,
            market_regime_snapshot=None,
        )

    assert evaluation.candidate is None
    assert evaluation.skip_code == "orb_intraday_insufficient"
    assert evaluation.meta["intraday_source_state"] == "insufficient"


def test_orb_registry_setup_marks_unsupported_intraday_provider(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    registry = build_default_strategy_registry(
        pullback_strategy=strategy.pullback_strategy,
        trend_atr_strategy=strategy,
        orb_strategy=strategy.orb_strategy,
    )
    entry = registry.get("opening_range_breakout")
    prepared_df = strategy.add_indicators(sample_uptrend_df)
    prev_high = float(prepared_df.iloc[-1]["prev_high"])
    decision_time = _kst_dt(2026, 2, 16, 9, 31)

    with ExitStack() as stack:
        for ctx in _orb_settings_patches():
            stack.enter_context(ctx)
        evaluation = entry.setup_evaluator.evaluate_setup(
            daily_df=prepared_df,
            daily_context=None,
            current_price=prev_high * 1.02,
            open_price=prev_high * 1.015,
            intraday_bars=[],
            intraday_provider_ready=False,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=decision_time,
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            market_venue="KRX",
            has_existing_position=False,
            has_pending_order=False,
            market_regime_snapshot=None,
        )

    assert evaluation.candidate is None
    assert evaluation.skip_code == "orb_intraday_unsupported"
    assert evaluation.meta["intraday_source_state"] == "unsupported"


def test_strategy_candidate_max_age_does_not_override_orb_native_expiry(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    registry = build_default_strategy_registry(
        pullback_strategy=strategy.pullback_strategy,
        trend_atr_strategy=strategy,
        orb_strategy=strategy.orb_strategy,
    )
    entry = registry.get("opening_range_breakout")
    prepared_df = strategy.add_indicators(sample_uptrend_df)
    prev_high = float(prepared_df.iloc[-1]["prev_high"])
    decision_time = _kst_dt(2026, 2, 16, 9, 31)
    intraday_bars = _make_intraday_bars(
        day=decision_time,
        opening_range_high=prev_high * 1.018,
        opening_range_low=prev_high * 1.006,
        stale_breakout=False,
    )

    with ExitStack() as stack:
        for ctx in _orb_settings_patches():
            stack.enter_context(ctx)
        stack.enter_context(
            patch.object(multiday_trend_atr.settings, "STRATEGY_CANDIDATE_MAX_AGE_SEC", 1)
        )
        evaluation = entry.setup_evaluator.evaluate_setup(
            daily_df=prepared_df,
            daily_context=None,
            current_price=prev_high * 1.02,
            open_price=prev_high * 1.015,
            intraday_bars=intraday_bars,
            intraday_provider_ready=True,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=decision_time,
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            market_venue="KRX",
            has_existing_position=False,
            has_pending_order=False,
            market_regime_snapshot=None,
        )

    assert evaluation.candidate is not None
    assert evaluation.candidate.expires_at == decision_time.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(minutes=90)
    assert evaluation.candidate.meta["entry_meta"]["expiry_authority"] == "orb_entry_cutoff"
