from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from kis_trend_atr_trading.engine.multiday_executor import MultidayExecutor


class _DummyFastProvider:
    def __init__(self):
        self.daily_fetch_calls = 0

    def get_recent_bars(self, stock_code: str, n: int, timeframe: str):
        if str(timeframe).upper() in ("D", "1D", "DAY", "DAILY"):
            self.daily_fetch_calls += 1
            return [
                {
                    "date": datetime(2026, 3, 7),
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "volume": 1000.0,
                },
                {
                    "date": datetime(2026, 3, 10),
                    "open": 101.0,
                    "high": 103.0,
                    "low": 100.0,
                    "close": 102.0,
                    "volume": 1100.0,
                },
            ]
        return []

    def get_quote_snapshot(self, stock_code: str):
        return {
            "stock_code": str(stock_code).zfill(6),
            "stock_name": "Dummy",
            "current_price": 104.0,
            "open_price": 101.0,
            "best_ask": 104.0,
            "best_bid": 103.0,
            "received_at": datetime(2026, 3, 10, 9, 5, 0),
            "quote_age_sec": 0.2,
            "source": "ws_tick",
            "data_feed": "ws",
            "ws_connected": True,
            "session_high": 105.0,
            "session_low": 100.0,
        }

    def metrics(self):
        return {
            "daily_fetch_calls": self.daily_fetch_calls,
            "rest_quote_calls": 0,
            "ws_reconnect_count": 0,
            "ws_fallback_count": 0,
        }


class _DummyFastStrategy:
    has_position = False

    @staticmethod
    def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["atr"] = 1.0
        out["ma"] = 100.0
        out["ma20"] = 100.0
        out["adx"] = 30.0
        out["trend"] = "UPTREND"
        out["prev_high"] = [0.0] * (len(out) - 1) + [102.0]
        out["prev_close"] = [0.0] * (len(out) - 1) + [101.0]
        return out

    @staticmethod
    def get_trend(_df: pd.DataFrame):
        return SimpleNamespace(value="UPTREND")

    @staticmethod
    def generate_signal(**_kwargs):
        return SimpleNamespace(
            signal_type=SimpleNamespace(value="HOLD"),
            price=104.0,
            stop_loss=None,
            take_profit=None,
            trailing_stop=None,
            exit_reason=None,
            reason="hold",
            reason_code="",
            atr=1.0,
            trend=SimpleNamespace(value="UPTREND"),
            meta={},
        )


def test_run_fast_cycle_reuses_daily_snapshot_between_invocations():
    provider = _DummyFastProvider()
    executor = MultidayExecutor.__new__(MultidayExecutor)
    executor.trading_mode = "PAPER"
    executor.stock_code = "005930"
    executor.market_checker = SimpleNamespace(is_tradeable=lambda: (True, "정규장"))
    executor.risk_manager = SimpleNamespace(
        check_kill_switch=lambda: SimpleNamespace(passed=True, should_exit=False, reason="")
    )
    executor.api = SimpleNamespace(is_network_disconnected_for=lambda _seconds: False)
    executor.strategy = _DummyFastStrategy()
    executor.market_data_provider = provider
    executor.market_phase_context = None
    executor.market_venue_context = "KRX"
    executor.market_regime_snapshot = None
    executor._entry_allowed = True
    executor._entry_block_reason = ""
    executor._last_market_closed_skip_log_at = None
    executor._daily_signal_cache = None
    executor._daily_fetch_count = 0
    executor._last_fast_risk_sync_at = None
    executor._persist_account_snapshot = lambda force=False: None
    executor._check_and_send_alerts = lambda _signal, _price: None
    executor._execute_exit_with_pending_control = lambda _signal: {"success": True}
    executor.execute_buy = lambda _signal: {"success": True}
    executor._has_active_pending_buy_order = lambda: False
    executor._sync_risk_account_snapshot = lambda: None
    executor._resolve_entry_order_style = lambda: "market"
    executor._apply_stale_quote_guard = lambda signal, _quote_snapshot: signal
    executor.__class__._shared_account_snapshot_fetch_count = 0

    first = executor.run_fast_cycle()
    second = executor.run_fast_cycle()

    assert first["metrics"]["daily_fetch_calls"] == 1
    assert second["metrics"]["daily_fetch_calls"] == 0
