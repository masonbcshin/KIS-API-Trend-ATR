from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import strategy.multiday_trend_atr as multiday_trend_atr
from strategy.multiday_trend_atr import MultidayTrendATRStrategy, SignalType
from strategy.pullback_rebreakout import PullbackDecision, PullbackRebreakoutStrategy
from utils.market_hours import KST
from utils.market_phase import VenueMarketPhase


def _kst_dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return KST.localize(datetime(year, month, day, hour, minute, 0))


def _make_pullback_indicator_df() -> pd.DataFrame:
    dates = pd.date_range(end=datetime(2026, 2, 20), periods=60, freq="D")
    close = list(np.linspace(100.0, 165.0, 45))
    close += [170.0, 174.0, 179.0, 176.0, 174.0, 172.0, 171.0, 170.5, 171.0, 172.0]
    close += [173.2, 173.8, 173.6, 173.5, 173.4]
    high = [value + 1.2 for value in close]
    low = [value - 1.0 for value in close]
    open_price = [value - 0.3 for value in close]
    volume = [700000 for _ in close]

    high[47] = 180.5
    low[47] = 177.0
    high[-3:] = [174.2, 174.5, 174.3]
    low[-3:] = [172.5, 173.0, 172.9]

    df = pd.DataFrame(
        {
            "date": dates,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )
    df["atr"] = 2.0
    df["adx"] = 32.0
    df["ma"] = 160.0
    df["ma20"] = 171.0
    df["trend"] = "UPTREND"
    df["prev_high"] = df["high"].shift(1)
    df["prev_close"] = df["close"].shift(1)
    df.loc[df.index[-1], "prev_high"] = 176.5
    return df


def _patch_pullback_enabled():
    return patch.multiple(
        multiday_trend_atr.settings,
        ENABLE_PULLBACK_REBREAKOUT_STRATEGY=True,
        PULLBACK_LOOKBACK_BARS=12,
        PULLBACK_SWING_LOOKBACK_BARS=15,
        PULLBACK_MIN_PULLBACK_PCT=0.015,
        PULLBACK_MAX_PULLBACK_PCT=0.06,
        PULLBACK_REQUIRE_ABOVE_MA20=True,
        PULLBACK_REBREAKOUT_LOOKBACK_BARS=3,
        PULLBACK_USE_ADX_FILTER=True,
        PULLBACK_MIN_ADX=20.0,
        PULLBACK_ONLY_MAIN_MARKET=True,
        PULLBACK_ALLOWED_ENTRY_VENUES="KRX",
        PULLBACK_BLOCK_IF_EXISTING_POSITION=True,
        PULLBACK_BLOCK_IF_PENDING_ORDER=True,
        ENABLE_OPENING_NO_ENTRY_GUARD=False,
        ENABLE_BREAKOUT_EXTENSION_CAP=False,
        ENABLE_ENTRY_GAP_FILTER=False,
    )


def test_pullback_rebreakout_buy_signal_when_setup_is_valid():
    strategy = MultidayTrendATRStrategy()
    prepared_df = _make_pullback_indicator_df()

    with _patch_pullback_enabled(), patch.object(strategy, "add_indicators", return_value=prepared_df):
        signal = strategy.generate_signal(
            df=prepared_df,
            current_price=175.2,
            open_price=173.4,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
        )

    assert signal.signal_type == SignalType.BUY
    assert signal.meta["strategy_tag"] == "pullback_rebreakout"
    assert signal.meta["micro_high"] == 174.5


def test_pullback_rebreakout_fails_when_trend_filter_is_not_met():
    sleeve = PullbackRebreakoutStrategy()
    prepared_df = _make_pullback_indicator_df()
    prepared_df.loc[prepared_df.index[-1], "ma20"] = 159.0
    prepared_df.loc[prepared_df.index[-1], "ma"] = 160.0

    with _patch_pullback_enabled():
        candidate = sleeve.evaluate(
            df=prepared_df,
            current_price=175.2,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
        )

    assert candidate.decision == PullbackDecision.NOOP
    assert candidate.reason_code == "pullback_invalid_setup"
    assert "추세" in candidate.reason


def test_pullback_rebreakout_fails_when_pullback_is_too_shallow():
    sleeve = PullbackRebreakoutStrategy()
    prepared_df = _make_pullback_indicator_df()

    with _patch_pullback_enabled():
        candidate = sleeve.evaluate(
            df=prepared_df,
            current_price=179.3,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
        )

    assert candidate.decision == PullbackDecision.NOOP
    assert candidate.reason_code == "pullback_invalid_setup"
    assert "눌림 부족" in candidate.reason


def test_pullback_rebreakout_fails_when_pullback_is_too_deep():
    sleeve = PullbackRebreakoutStrategy()
    prepared_df = _make_pullback_indicator_df()
    prepared_df.loc[prepared_df.index[-1], "ma20"] = 168.0

    with _patch_pullback_enabled():
        candidate = sleeve.evaluate(
            df=prepared_df,
            current_price=169.0,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
        )

    assert candidate.decision == PullbackDecision.NOOP
    assert candidate.reason_code == "pullback_invalid_setup"
    assert "눌림 과도" in candidate.reason


def test_pullback_rebreakout_fails_when_price_is_below_ma20():
    sleeve = PullbackRebreakoutStrategy()
    prepared_df = _make_pullback_indicator_df()
    prepared_df.loc[prepared_df.index[-3:], "high"] = [167.8, 168.5, 168.2]
    prepared_df.loc[prepared_df.index[-3:], "low"] = [166.0, 166.8, 166.9]
    prepared_df.loc[prepared_df.index[-1], "ma20"] = 170.0
    prepared_df.loc[prepared_df.index[-1], "ma"] = 160.0

    with _patch_pullback_enabled():
        candidate = sleeve.evaluate(
            df=prepared_df,
            current_price=169.8,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
        )

    assert candidate.decision == PullbackDecision.NOOP
    assert candidate.reason_code == "pullback_invalid_setup"
    assert "MA20" in candidate.reason


def test_pullback_rebreakout_fails_when_rebreakout_is_not_confirmed():
    sleeve = PullbackRebreakoutStrategy()
    prepared_df = _make_pullback_indicator_df()

    with _patch_pullback_enabled():
        candidate = sleeve.evaluate(
            df=prepared_df,
            current_price=174.4,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
        )

    assert candidate.decision == PullbackDecision.NOOP
    assert candidate.reason_code == "pullback_invalid_setup"
    assert "재돌파" in candidate.reason


def test_pullback_rebreakout_fails_when_market_phase_is_not_main_market():
    strategy = MultidayTrendATRStrategy()
    prepared_df = _make_pullback_indicator_df()

    with _patch_pullback_enabled(), patch.object(strategy, "add_indicators", return_value=prepared_df):
        signal = strategy.generate_signal(
            df=prepared_df,
            current_price=175.2,
            open_price=173.4,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 8, 55),
            market_phase=VenueMarketPhase.KRX_PREOPEN,
        )

    assert signal.signal_type == SignalType.HOLD
    assert signal.reason_code == "pullback_not_main_market"


def test_pullback_rebreakout_fails_when_existing_position_exists():
    sleeve = PullbackRebreakoutStrategy()
    prepared_df = _make_pullback_indicator_df()

    with _patch_pullback_enabled():
        candidate = sleeve.evaluate(
            df=prepared_df,
            current_price=175.2,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            has_existing_position=True,
        )

    assert candidate.decision == PullbackDecision.BLOCKED
    assert candidate.reason_code == "pullback_existing_position"


def test_pullback_rebreakout_fails_when_pending_order_exists():
    strategy = MultidayTrendATRStrategy()
    prepared_df = _make_pullback_indicator_df()

    with _patch_pullback_enabled(), patch.object(strategy, "add_indicators", return_value=prepared_df):
        signal = strategy.generate_signal(
            df=prepared_df,
            current_price=175.2,
            open_price=173.4,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            has_pending_order=True,
        )

    assert signal.signal_type == SignalType.HOLD
    assert signal.reason_code == "pullback_pending_order"


def test_pullback_rebreakout_respects_opening_guard_gap_guard_and_extension_cap():
    strategy = MultidayTrendATRStrategy()
    prepared_df = _make_pullback_indicator_df()

    with _patch_pullback_enabled(), patch.object(strategy, "add_indicators", return_value=prepared_df), \
         patch.object(multiday_trend_atr.settings, "ENABLE_OPENING_NO_ENTRY_GUARD", True), \
         patch.object(multiday_trend_atr.settings, "OPENING_NO_ENTRY_MINUTES", 10):
        opening_signal = strategy.generate_signal(
            df=prepared_df,
            current_price=175.2,
            open_price=173.4,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 9, 5),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
        )
    assert opening_signal.signal_type == SignalType.HOLD
    assert opening_signal.reason_code == "opening_guard"

    with _patch_pullback_enabled(), patch.object(strategy, "add_indicators", return_value=prepared_df), \
         patch.object(multiday_trend_atr.settings, "ENABLE_BREAKOUT_EXTENSION_CAP", True), \
         patch.object(multiday_trend_atr.settings, "MAX_BREAKOUT_EXTENSION_PCT_STOCK", 0.003), \
         patch.object(multiday_trend_atr.settings, "MAX_BREAKOUT_EXTENSION_PCT_ETF", 0.003):
        extension_signal = strategy.generate_signal(
            df=prepared_df,
            current_price=175.2,
            open_price=173.4,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
        )
    assert extension_signal.signal_type == SignalType.HOLD
    assert extension_signal.reason_code == "breakout_extension_exceeded"

    with _patch_pullback_enabled(), patch.object(strategy, "add_indicators", return_value=prepared_df), \
         patch.object(multiday_trend_atr.settings, "ENABLE_ENTRY_GAP_FILTER", True), \
         patch.object(multiday_trend_atr.settings, "MAX_ENTRY_GAP_PCT_STOCK", 0.015), \
         patch.object(multiday_trend_atr.settings, "MAX_ENTRY_GAP_PCT_ETF", 0.01), \
         patch.object(multiday_trend_atr.settings, "MAX_OPEN_VS_PREV_HIGH_PCT", 0.005):
        gap_signal = strategy.generate_signal(
            df=prepared_df,
            current_price=175.2,
            open_price=179.0,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
        )
    assert gap_signal.signal_type == SignalType.HOLD
    assert gap_signal.reason_code == "entry_gap_filter"


def test_pullback_rebreakout_off_preserves_existing_trend_atr_behavior():
    strategy = MultidayTrendATRStrategy()
    prepared_df = _make_pullback_indicator_df()

    with patch.object(multiday_trend_atr.settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", False), \
         patch.object(strategy, "add_indicators", return_value=prepared_df):
        signal = strategy.generate_signal(
            df=prepared_df,
            current_price=175.2,
            open_price=173.4,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
        )

    assert signal.signal_type == SignalType.HOLD
    assert signal.reason_code == ""
