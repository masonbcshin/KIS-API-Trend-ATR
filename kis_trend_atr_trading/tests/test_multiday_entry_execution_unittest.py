from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import engine.multiday_executor as multiday_executor
from engine.multiday_executor import MultidayExecutor
from strategy.multiday_trend_atr import TradingSignal, SignalType, TrendType


def _make_executor() -> MultidayExecutor:
    executor = MultidayExecutor.__new__(MultidayExecutor)
    executor.stock_code = "005930"
    return executor


def _make_buy_signal(prev_high: float = 50000.0, strategy_tag: str = "trend_atr") -> TradingSignal:
    return TradingSignal(
        signal_type=SignalType.BUY,
        price=50100.0,
        atr=1200.0,
        trend=TrendType.UPTREND,
        reason="UNITTEST",
        meta={
            "asset_type": "STOCK",
            "prev_high": prev_high,
            "current_price_at_signal": 50100.0,
            "extension_pct": 0.002,
            "strategy_tag": strategy_tag,
        },
    )


def test_executor_blocks_buy_when_ws_quote_is_stale():
    executor = _make_executor()
    signal = _make_buy_signal()

    with patch.object(multiday_executor.settings, "ENABLE_STALE_QUOTE_GUARD", True), \
         patch.object(multiday_executor.settings, "QUOTE_MAX_AGE_SEC", 3):
        blocked = executor._apply_stale_quote_guard(
            signal,
            {
                "data_feed": "ws",
                "source": "ws_tick",
                "ws_connected": True,
                "quote_age_sec": 4.2,
            },
        )

    assert getattr(blocked.signal_type, "value", blocked.signal_type) == SignalType.HOLD.value
    assert blocked.reason_code == "stale_quote"


def test_executor_builds_protected_limit_buy_order_plan_successfully():
    executor = _make_executor()
    signal = _make_buy_signal(prev_high=50000.0)

    with patch.object(multiday_executor.settings, "ENTRY_ORDER_STYLE", "protected_limit"), \
         patch.object(multiday_executor.settings, "ENTRY_PROTECT_TICKS_STOCK", 2), \
         patch.object(multiday_executor.settings, "ENTRY_PROTECT_TICKS_ETF", 1), \
         patch.object(multiday_executor.settings, "ENTRY_MAX_SLIPPAGE_PCT", 0.004), \
         patch.object(multiday_executor.settings, "ENABLE_BREAKOUT_EXTENSION_CAP", True), \
         patch.object(multiday_executor.settings, "MAX_BREAKOUT_EXTENSION_PCT_STOCK", 0.007), \
         patch.object(multiday_executor.settings, "MAX_BREAKOUT_EXTENSION_PCT_ETF", 0.004):
        plan = executor._build_entry_order_plan(
            signal,
            {
                "stock_name": "삼성전자",
                "current_price": 50100.0,
                "best_ask": 50100.0,
            },
        )

    assert plan["blocked"] is False
    assert plan["order_type"] == "00"
    assert plan["style"] == "protected_limit"
    assert plan["price"] == 50300.0


def test_executor_blocks_protected_limit_when_cap_is_exceeded():
    executor = _make_executor()
    signal = _make_buy_signal(prev_high=50000.0)

    with patch.object(multiday_executor.settings, "ENTRY_ORDER_STYLE", "protected_limit"), \
         patch.object(multiday_executor.settings, "ENTRY_PROTECT_TICKS_STOCK", 2), \
         patch.object(multiday_executor.settings, "ENTRY_PROTECT_TICKS_ETF", 1), \
         patch.object(multiday_executor.settings, "ENTRY_MAX_SLIPPAGE_PCT", 0.004), \
         patch.object(multiday_executor.settings, "ENABLE_BREAKOUT_EXTENSION_CAP", True), \
         patch.object(multiday_executor.settings, "MAX_BREAKOUT_EXTENSION_PCT_STOCK", 0.004), \
         patch.object(multiday_executor.settings, "MAX_BREAKOUT_EXTENSION_PCT_ETF", 0.004):
        plan = executor._build_entry_order_plan(
            signal,
            {
                "stock_name": "삼성전자",
                "current_price": 50150.0,
                "best_ask": 50150.0,
            },
        )

    assert plan["blocked"] is True
    assert plan["reason_code"] == "protected_limit_exceeds_cap"


def test_executor_builds_protected_limit_plan_for_pullback_signal():
    executor = _make_executor()
    signal = _make_buy_signal(prev_high=174.5, strategy_tag="pullback_rebreakout")
    signal.price = 175.2
    signal.meta["current_price_at_signal"] = 175.2

    with patch.object(multiday_executor.settings, "ENTRY_ORDER_STYLE", "protected_limit"), \
         patch.object(multiday_executor.settings, "ENTRY_PROTECT_TICKS_STOCK", 0), \
         patch.object(multiday_executor.settings, "ENTRY_PROTECT_TICKS_ETF", 1), \
         patch.object(multiday_executor.settings, "ENTRY_MAX_SLIPPAGE_PCT", 0.05), \
         patch.object(multiday_executor.settings, "ENABLE_BREAKOUT_EXTENSION_CAP", False):
        plan = executor._build_entry_order_plan(
            signal,
            {
                "stock_name": "삼성전자",
                "current_price": 175.2,
                "best_ask": 175.2,
            },
        )

    assert plan["blocked"] is False
    assert plan["style"] == "protected_limit"
    assert plan["order_type"] == "00"
