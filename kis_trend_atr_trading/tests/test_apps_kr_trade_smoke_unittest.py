"""Smoke tests for unified app entrypoints."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_kr_trade_smoke_rest(monkeypatch):
    import apps.kr_trade as kr_trade

    state = {"run_called": False, "restore_called": False}

    class FakeExecutor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def restore_position_on_start(self):
            state["restore_called"] = True
            return False

        def run(self, interval_seconds, max_iterations):
            state["run_called"] = True
            assert interval_seconds >= 60
            assert max_iterations == 1

    monkeypatch.setattr(kr_trade, "MultidayExecutor", FakeExecutor)
    monkeypatch.setattr(kr_trade, "KISApi", lambda is_paper_trading: object())
    monkeypatch.setattr(kr_trade, "MultidayTrendATRStrategy", lambda: object())

    rc = kr_trade.main(["--feed", "rest", "--mode", "trade", "--max-runs", "1"])

    assert rc == 0
    assert state["restore_called"] is True
    assert state["run_called"] is True


def test_kr_trade_smoke_ws(monkeypatch):
    import apps.kr_trade as kr_trade

    state = {
        "subscribe_called": False,
        "stop_called": False,
        "run_called": False,
    }

    class FakeExecutor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def restore_position_on_start(self):
            return False

        def run(self, interval_seconds, max_iterations):
            state["run_called"] = True
            assert max_iterations == 1

    class FakeWSProvider:
        def __init__(self, **kwargs):
            self.ws_failed = True
            self.ws_running = False

        def subscribe_bars(self, stock_codes, timeframe, on_bar_callback):
            state["subscribe_called"] = True
            assert timeframe == "1m"

            def _stop():
                state["stop_called"] = True

            return _stop

    monkeypatch.setattr(kr_trade, "MultidayExecutor", FakeExecutor)
    monkeypatch.setattr(kr_trade, "KISApi", lambda is_paper_trading: object())
    monkeypatch.setattr(kr_trade, "MultidayTrendATRStrategy", lambda: object())
    monkeypatch.setattr(kr_trade, "KISWSMarketDataProvider", FakeWSProvider)

    rc = kr_trade.main(["--feed", "ws", "--mode", "trade", "--max-runs", "1"])

    assert rc == 0
    assert state["subscribe_called"] is True
    assert state["run_called"] is True
    assert state["stop_called"] is True
