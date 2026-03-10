from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import strategy.multiday_trend_atr as multiday_trend_atr
from strategy.multiday_trend_atr import MultidayTrendATRStrategy, SignalType
from utils.market_hours import KST


def _kst_dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return KST.localize(datetime(year, month, day, hour, minute, 0))


def test_multiday_buy_fails_when_current_price_not_above_prev_high(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    df_with_indicators = strategy.add_indicators(sample_uptrend_df)
    prev_high = float(df_with_indicators.iloc[-1]["prev_high"])
    prev_close = float(df_with_indicators.iloc[-1]["prev_close"])

    signal = strategy.generate_signal(
        df=sample_uptrend_df,
        current_price=prev_high,
        open_price=prev_close,
        stock_code="005930",
        stock_name="삼성전자",
        check_time=_kst_dt(2026, 2, 16, 10, 30),
    )

    assert signal.signal_type == SignalType.HOLD
    assert "돌파" in signal.reason


def test_multiday_buy_fails_when_breakout_extension_exceeds_cap(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    df_with_indicators = strategy.add_indicators(sample_uptrend_df)
    prev_high = float(df_with_indicators.iloc[-1]["prev_high"])
    prev_close = float(df_with_indicators.iloc[-1]["prev_close"])

    with patch.object(multiday_trend_atr.settings, "ENABLE_BREAKOUT_EXTENSION_CAP", True), \
         patch.object(multiday_trend_atr.settings, "MAX_BREAKOUT_EXTENSION_PCT_STOCK", 0.005), \
         patch.object(multiday_trend_atr.settings, "MAX_BREAKOUT_EXTENSION_PCT_ETF", 0.004):
        signal = strategy.generate_signal(
            df=sample_uptrend_df,
            current_price=prev_high * 1.01,
            open_price=prev_close * 1.002,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=_kst_dt(2026, 2, 16, 10, 30),
        )

    assert signal.signal_type == SignalType.HOLD
    assert signal.reason_code == "breakout_extension_exceeded"


def test_multiday_buy_fails_when_entry_gap_filter_blocks_open_gap(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    df_with_indicators = strategy.add_indicators(sample_uptrend_df)
    prev_high = float(df_with_indicators.iloc[-1]["prev_high"])
    prev_close = float(df_with_indicators.iloc[-1]["prev_close"])

    with patch.object(multiday_trend_atr.settings, "ENABLE_ENTRY_GAP_FILTER", True), \
         patch.object(multiday_trend_atr.settings, "MAX_ENTRY_GAP_PCT_STOCK", 0.015), \
         patch.object(multiday_trend_atr.settings, "MAX_ENTRY_GAP_PCT_ETF", 0.01), \
         patch.object(multiday_trend_atr.settings, "MAX_OPEN_VS_PREV_HIGH_PCT", 0.005):
        signal = strategy.generate_signal(
            df=sample_uptrend_df,
            current_price=prev_high * 1.002,
            open_price=prev_close * 1.03,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=_kst_dt(2026, 2, 16, 10, 30),
        )

    assert signal.signal_type == SignalType.HOLD
    assert signal.reason_code == "entry_gap_filter"


def test_multiday_buy_allows_opening_extension_when_adaptive_cap_is_wider(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    df_with_indicators = strategy.add_indicators(sample_uptrend_df)
    prev_high = float(df_with_indicators.iloc[-1]["prev_high"])
    prev_close = float(df_with_indicators.iloc[-1]["prev_close"])

    with patch.object(multiday_trend_atr.settings, "ENABLE_BREAKOUT_EXTENSION_CAP", True), \
         patch.object(multiday_trend_atr.settings, "MAX_BREAKOUT_EXTENSION_PCT_STOCK", 0.005), \
         patch.object(multiday_trend_atr.settings, "MAX_BREAKOUT_EXTENSION_PCT_ETF", 0.004), \
         patch.object(multiday_trend_atr.settings, "BREAKOUT_EXTENSION_OPENING_CAP_MINUTES", 90), \
         patch.object(multiday_trend_atr.settings, "MAX_BREAKOUT_EXTENSION_PCT_STOCK_OPENING", 0.015), \
         patch.object(multiday_trend_atr.settings, "MAX_BREAKOUT_EXTENSION_PCT_ETF_OPENING", 0.012), \
         patch.object(multiday_trend_atr.settings, "ENABLE_BREAKOUT_EXTENSION_ATR_CAP", False), \
         patch.object(multiday_trend_atr.settings, "ENABLE_ENTRY_GAP_FILTER", False), \
         patch.object(multiday_trend_atr.settings, "ENABLE_OPENING_NO_ENTRY_GUARD", False):
        signal = strategy.generate_signal(
            df=sample_uptrend_df,
            current_price=prev_high * 1.009,
            open_price=prev_close * 1.001,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=_kst_dt(2026, 2, 16, 9, 35),
        )

    assert signal.signal_type == SignalType.BUY
    assert signal.meta["max_allowed_pct"] == 0.015
    assert signal.meta["breakout_cap_source"] == "opening"


def test_multiday_buy_fails_when_opening_guard_active(sample_uptrend_df):
    strategy = MultidayTrendATRStrategy()
    df_with_indicators = strategy.add_indicators(sample_uptrend_df)
    prev_high = float(df_with_indicators.iloc[-1]["prev_high"])
    prev_close = float(df_with_indicators.iloc[-1]["prev_close"])

    with patch.object(multiday_trend_atr.settings, "ENABLE_OPENING_NO_ENTRY_GUARD", True), \
         patch.object(multiday_trend_atr.settings, "OPENING_NO_ENTRY_MINUTES", 10):
        signal = strategy.generate_signal(
            df=sample_uptrend_df,
            current_price=prev_high * 1.002,
            open_price=prev_close * 1.002,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=_kst_dt(2026, 2, 16, 9, 5),
        )

    assert signal.signal_type == SignalType.HOLD
    assert signal.reason_code == "opening_guard"
