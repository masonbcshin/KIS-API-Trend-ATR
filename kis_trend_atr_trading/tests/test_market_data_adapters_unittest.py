"""Unit tests for unified market-data adapters."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.kis_rest.market_data import KISRestMarketDataProvider
from adapters.kis_ws.bar_aggregator import MarketTick, MinuteBarAggregator
from adapters.kis_ws.market_data import KISWSMarketDataProvider
from adapters.kis_ws.ws_client import KISWSClient


class DummyRestAPI:
    def get_daily_ohlcv(self, stock_code: str, period_type: str = "D"):
        return pd.DataFrame(
            [
                {
                    "date": datetime(2026, 2, 12),
                    "open": 100.0,
                    "high": 110.0,
                    "low": 90.0,
                    "close": 105.0,
                    "volume": 1000,
                },
                {
                    "date": datetime(2026, 2, 13),
                    "open": 105.0,
                    "high": 112.0,
                    "low": 101.0,
                    "close": 111.0,
                    "volume": 1200,
                },
            ]
        )

    def get_current_price(self, stock_code: str):
        return {"current_price": 111.0}


def test_rest_provider_returns_expected_bar_count_and_format():
    provider = KISRestMarketDataProvider(api=DummyRestAPI())
    bars = provider.get_recent_bars("005930", n=1, timeframe="D")

    assert len(bars) == 1
    assert bars[0]["open"] == 105.0
    assert bars[0]["close"] == 111.0
    assert provider.get_latest_price("005930") == 111.0


def test_bar_aggregator_emits_only_completed_minute_bar():
    agg = MinuteBarAggregator(timeframe="1m")
    t1 = MarketTick("005930", price=100.0, volume=10, timestamp=datetime(2026, 2, 16, 9, 0, 1))
    t2 = MarketTick("005930", price=101.0, volume=5, timestamp=datetime(2026, 2, 16, 9, 0, 40))
    t3 = MarketTick("005930", price=99.0, volume=7, timestamp=datetime(2026, 2, 16, 9, 1, 0))

    assert agg.add_tick(t1) is None
    assert agg.add_tick(t2) is None  # not completed yet
    completed = agg.add_tick(t3)

    assert completed is not None
    assert completed.start_at == datetime(2026, 2, 16, 9, 0, 0)
    assert completed.end_at == datetime(2026, 2, 16, 9, 1, 0)
    assert completed.open == 100.0
    assert completed.high == 101.0
    assert completed.low == 100.0
    assert completed.close == 101.0
    assert completed.volume == 15.0


def test_ws_provider_does_not_emit_callback_before_bar_completion():
    provider = KISWSMarketDataProvider()
    emitted = []
    provider._on_bar_callback = lambda bar: emitted.append(bar)

    provider._handle_tick(
        MarketTick("005930", price=100.0, volume=1, timestamp=datetime(2026, 2, 16, 9, 0, 5))
    )
    provider._handle_tick(
        MarketTick("005930", price=101.0, volume=2, timestamp=datetime(2026, 2, 16, 9, 0, 35))
    )

    assert emitted == []  # evaluation gate: incomplete minute must not emit

    provider._handle_tick(
        MarketTick("005930", price=102.0, volume=3, timestamp=datetime(2026, 2, 16, 9, 1, 2))
    )

    assert len(emitted) == 1
    bars = provider.get_recent_bars("005930", n=10, timeframe="1m")
    assert len(bars) == 1


class AlwaysFailWSClient(KISWSClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.listen_calls = 0

    async def _listen_once(self, stock_codes, on_tick):  # type: ignore[override]
        self.listen_calls += 1
        raise RuntimeError("disconnect")


def test_ws_client_reconnect_attempts_capped_at_five(monkeypatch):
    async def _fast_sleep(_delay: float):
        return None

    monkeypatch.setattr("adapters.kis_ws.ws_client.asyncio.sleep", _fast_sleep)

    client = AlwaysFailWSClient(max_reconnect_attempts=5, failure_policy="rest_fallback")
    result = asyncio.run(client.run(["005930"], lambda _tick: None))

    assert result.success is False
    assert result.failure_policy == "rest_fallback"
    # Initial try + up to 5 retries.
    assert client.listen_calls == 6


def test_ws_client_parse_tick_accepts_numeric_envelope_segment():
    # Paper WS may deliver: 0|H0STCNT0|017|000660^...
    msg = "0|H0STCNT0|017|000660^090008^905000^0^0^0^0^0^0^0^0^0^1200"

    tick = KISWSClient._parse_tick(msg)

    assert tick is not None
    assert tick.stock_code == "000660"
    assert tick.price == 905000.0
    assert tick.volume == 1200.0


def test_ws_client_parse_tick_keeps_legacy_payload_format():
    msg = "0|H0STCNT0|000660^090008^905000^0^0^0^0^0^0^0^0^0^300"

    tick = KISWSClient._parse_tick(msg)

    assert tick is not None
    assert tick.stock_code == "000660"
    assert tick.price == 905000.0
    assert tick.volume == 300.0


class _CountingRestProvider:
    def __init__(self):
        self.calls = 0

    def get_recent_bars(self, **_kwargs):
        self.calls += 1
        return []


def test_ws_provider_backfill_cooldown_prevents_back_to_back_calls():
    rest_provider = _CountingRestProvider()
    provider = KISWSMarketDataProvider(
        rest_fallback_provider=rest_provider,
        backfill_cooldown_sec=60,
    )

    provider._attempt_backfill("005930", 2)
    provider._attempt_backfill("005930", 3)
    provider._attempt_backfill("000660", 2)

    assert rest_provider.calls == 2
