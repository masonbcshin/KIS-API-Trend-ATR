from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import engine.multiday_executor as multiday_executor
import engine.market_regime_worker as market_regime_worker
import utils.market_regime as market_regime
from engine.multiday_executor import MultidayExecutor
from strategy.multiday_trend_atr import SignalType, TradingSignal, TrendType
from utils.market_hours import KST


def _kst_dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return KST.localize(datetime(year, month, day, hour, minute, 0))


def _make_daily_df(closes: list[float], end_date: str = "2026-02-27") -> pd.DataFrame:
    dates = pd.date_range(end=end_date, periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [value * 1.01 for value in closes],
            "low": [value * 0.99 for value in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        }
    )


class _DummyAPI:
    def __init__(self, daily_map, quote_map=None):
        self.daily_map = dict(daily_map)
        self.quote_map = dict(quote_map or {})

    def get_daily_ohlcv(self, stock_code: str, period_type: str = "D"):
        return self.daily_map[str(stock_code)].copy()

    def get_current_price(self, stock_code: str):
        return dict(
            self.quote_map.get(
                str(stock_code),
                {
                    "current_price": 100.0,
                    "open_price": 100.0,
                },
            )
        )


class _DummyStrategy:
    def __init__(self):
        self._position = None

    @property
    def has_position(self):
        return self._position is not None

    @property
    def position(self):
        return self._position

    def open_position(self, symbol, entry_price, quantity, atr, stop_loss, take_profit=None):
        self._position = SimpleNamespace(
            symbol=symbol,
            entry_price=float(entry_price),
            quantity=int(quantity),
            atr_at_entry=float(atr),
            stop_loss=float(stop_loss),
            take_profit=float(take_profit) if take_profit is not None else None,
            trailing_stop=float(stop_loss),
            highest_price=float(entry_price),
        )
        return self._position


class _DummyTelegram:
    def notify_buy_order(self, **_kwargs):
        return True

    def notify_info(self, *_args, **_kwargs):
        return True

    def notify_error(self, *_args, **_kwargs):
        return True


def _regime_settings(**overrides):
    values = {
        "ENABLE_MARKET_REGIME_FILTER": True,
        "MARKET_REGIME_KOSPI_SYMBOL": "069500",
        "MARKET_REGIME_KOSDAQ_SYMBOL": "229200",
        "MARKET_REGIME_MA_PERIOD": 20,
        "MARKET_REGIME_LOOKBACK_DAYS": 3,
        "MARKET_REGIME_BAD_3D_RETURN_PCT": -0.03,
        "MARKET_REGIME_INTRADAY_DROP_PCT": -0.015,
        "MARKET_REGIME_OPENING_GUARD_MINUTES": 30,
        "MARKET_REGIME_CACHE_TTL_SEC": 60,
        "MARKET_REGIME_OPENING_CACHE_TTL_SEC": 60,
        "MARKET_REGIME_STALE_MAX_SEC": 180,
        "MARKET_REGIME_FAIL_MODE": "closed",
        "MARKET_REGIME_REFRESH_BUDGET_SEC": 1.5,
        "MARKET_REGIME_BOOTSTRAP_BUDGET_SEC": 3.0,
        "ENABLE_MARKET_REGIME_REFRESH_THREAD": False,
        "MARKET_REGIME_REFRESH_INTERVAL_SEC": 30.0,
        "MARKET_REGIME_DAILY_CONTEXT_REFRESH_SEC": 300.0,
        "MARKET_REGIME_INTRADAY_USE_WS_CACHE_ONLY": True,
        "MARKET_REGIME_QUOTE_FALLBACK_MODE": "skip",
        "MARKET_REGIME_FORCE_DAILY_REFRESH_ON_TRADE_DATE_CHANGE": True,
        "MARKET_REGIME_BACKGROUND_STALE_GRACE_SEC": 180.0,
        "MARKET_REGIME_QUOTE_MAX_AGE_SEC": 15.0,
        "MARKET_REGIME_BAD_BLOCK_NEW_BUY": True,
        "MARKET_REGIME_NEUTRAL_ALLOW_BUY": True,
        "MARKET_REGIME_NEUTRAL_POSITION_SCALE": 1.0,
    }
    values.update(overrides)
    return patch.multiple(market_regime.settings, **values)


def _executor_settings(**overrides):
    values = {
        "ENABLE_MARKET_REGIME_FILTER": True,
        "MARKET_REGIME_BAD_BLOCK_NEW_BUY": True,
        "MARKET_REGIME_NEUTRAL_ALLOW_BUY": True,
        "MARKET_REGIME_NEUTRAL_POSITION_SCALE": 1.0,
        "MARKET_REGIME_FAIL_MODE": "closed",
    }
    values.update(overrides)
    return patch.multiple(multiday_executor.settings, **values)


def _make_shared_snapshot(
    regime_value: market_regime.MarketRegime,
    reason: str,
    *,
    as_of: datetime,
    expires_after_sec: float = 60.0,
    stale_after_sec: float = 180.0,
) -> market_regime.MarketRegimeSnapshot:
    return market_regime.MarketRegimeSnapshot(
        regime=regime_value,
        reason=reason,
        as_of=as_of,
        expires_at=as_of + timedelta(seconds=expires_after_sec),
        stale_after=as_of + timedelta(seconds=stale_after_sec),
        is_stale=False,
        kospi_symbol="069500",
        kosdaq_symbol="229200",
        kospi_close=100.0,
        kospi_ma=99.0,
        kosdaq_close=200.0,
        kosdaq_ma=198.0,
        kospi_3d_return_pct=0.01,
        kosdaq_3d_return_pct=0.02,
        intraday_guard_active=False,
        intraday_guard_reason=None,
        source="main_loop_cache",
    )


def _make_buy_signal() -> TradingSignal:
    return TradingSignal(
        signal_type=SignalType.BUY,
        price=50100.0,
        atr=1200.0,
        trend=TrendType.UPTREND,
        reason="UNITTEST",
        meta={"asset_type": "STOCK", "prev_high": 50000.0},
    )


def _make_executor_for_buy():
    submitted = {"count": 0}

    def _execute_buy_order(**_kwargs):
        submitted["count"] += 1
        return SimpleNamespace(
            success=True,
            order_no="B-001",
            message="filled",
            submitted_at=None,
        )

    ex = MultidayExecutor.__new__(MultidayExecutor)
    ex.trading_mode = "PAPER"
    ex.stock_code = "005930"
    ex.order_quantity = 1
    ex.strategy = _DummyStrategy()
    ex.telegram = _DummyTelegram()
    ex.risk_manager = SimpleNamespace(
        check_order_allowed=lambda is_closing_position: SimpleNamespace(
            passed=True,
            should_exit=False,
            reason="",
        )
    )
    ex.market_checker = SimpleNamespace(is_tradeable=lambda: (True, ""))
    ex.order_synchronizer = SimpleNamespace(execute_buy_order=_execute_buy_order)
    ex.fetch_quote_snapshot = lambda: {
        "current_price": 50100.0,
        "open_price": 50000.0,
        "best_ask": 50100.0,
        "quote_age_sec": 0.0,
        "source": "rest_quote",
    }
    ex._can_place_orders = lambda: False
    ex._build_entry_order_plan = lambda signal, quote_snapshot: {
        "blocked": False,
        "price": float(signal.price),
        "order_type": "01",
        "style": "market",
        "asset_type": "STOCK",
        "current_price": float(quote_snapshot.get("current_price", 0.0) or 0.0),
        "extension_pct_at_order": 0.0,
        "best_ask": quote_snapshot.get("best_ask"),
        "limit_price": None,
        "slippage_cap_pct": 0.0,
    }
    ex._extract_execution_fills = lambda sync_result, side: [
        {"price": 50100.0, "qty": 1, "executed_at": None}
    ]
    ex._record_execution_fill = lambda side, fill, reason: True
    ex._compute_entry_exit_prices = lambda entry_price, atr: (float(atr or 1200.0), 48000.0, 54000.0)
    ex._save_position_on_exit = lambda: None
    ex._sync_db_position_from_strategy = lambda: None
    ex._persist_account_snapshot = lambda force=False: None
    ex._log_entry_trace = lambda *args, **kwargs: None
    ex._log_entry_event = lambda *args, **kwargs: None
    ex._daily_trades = []
    ex.market_regime_snapshot = None
    ex.market_regime_service = SimpleNamespace(
        evaluate=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("market regime direct evaluate should not be called")
        )
    )
    return ex, submitted


def test_market_regime_good_when_both_etfs_above_ma():
    service = market_regime.MarketRegimeService(
        _DummyAPI(
            {
                "069500": _make_daily_df([100 + idx for idx in range(25)]),
                "229200": _make_daily_df([200 + idx for idx in range(25)]),
            }
        )
    )

    with _regime_settings():
        snapshot = service.build_snapshot(check_time=_kst_dt(2026, 3, 2, 11, 0))

    assert snapshot.regime == market_regime.MarketRegime.GOOD
    assert snapshot.reason == "both_above_ma_and_stable_3d"


def test_market_regime_neutral_when_only_one_etf_above_ma():
    service = market_regime.MarketRegimeService(
        _DummyAPI(
            {
                "069500": _make_daily_df([100 + idx for idx in range(25)]),
                "229200": _make_daily_df([225 - idx for idx in range(25)]),
            }
        )
    )

    with _regime_settings():
        snapshot = service.build_snapshot(check_time=_kst_dt(2026, 3, 2, 11, 0))

    assert snapshot.regime == market_regime.MarketRegime.NEUTRAL
    assert snapshot.reason == "mixed_ma_trend"


def test_market_regime_bad_when_both_etfs_below_ma():
    service = market_regime.MarketRegimeService(
        _DummyAPI(
            {
                "069500": _make_daily_df([125 - idx for idx in range(25)]),
                "229200": _make_daily_df([225 - idx for idx in range(25)]),
            }
        )
    )

    with _regime_settings():
        snapshot = service.build_snapshot(check_time=_kst_dt(2026, 3, 2, 11, 0))

    assert snapshot.regime == market_regime.MarketRegime.BAD
    assert snapshot.reason == "both_below_ma"


def test_market_regime_intraday_drop_downgrades_to_bad():
    service = market_regime.MarketRegimeService(
        _DummyAPI(
            {
                "069500": _make_daily_df([100 + idx for idx in range(25)]),
                "229200": _make_daily_df([200 + idx for idx in range(25)]),
            },
            quote_map={
                "069500": {"current_price": 98.0, "open_price": 100.0},
                "229200": {"current_price": 101.0, "open_price": 100.0},
            },
        )
    )

    with _regime_settings():
        snapshot = service.build_snapshot(check_time=_kst_dt(2026, 3, 2, 9, 10))

    assert snapshot.regime == market_regime.MarketRegime.BAD
    assert snapshot.reason == "intraday_drop_guard"
    assert snapshot.intraday_guard_active is True
    assert snapshot.intraday_guard_reason == "intraday_drop:069500"


def test_apply_intraday_guard_uses_ws_cache_quote_without_rest_fallback():
    now_kst = _kst_dt(2026, 3, 2, 9, 10)
    service = market_regime.MarketRegimeService(
        _DummyAPI(
            {
                "069500": _make_daily_df([100 + idx for idx in range(25)]),
                "229200": _make_daily_df([200 + idx for idx in range(25)]),
            }
        )
    )

    with _regime_settings():
        daily_context = service.build_daily_context(check_time=now_kst)
        with patch.object(
            service,
            "_load_intraday_probe",
            side_effect=AssertionError("REST intraday quote should not be called"),
        ):
            result = service.apply_intraday_guard(
                daily_context,
                check_time=now_kst,
                include_metrics=True,
                quote_snapshot_loader=lambda symbol: {
                    "current_price": 98.0 if str(symbol) == "069500" else 101.0,
                    "open_price": 100.0,
                    "received_at": now_kst,
                    "quote_age_sec": 0.0,
                },
                use_ws_cache_only=True,
                quote_fallback_mode="skip",
                quote_max_age_sec=15.0,
                snapshot_source="background_refresh",
            )

    assert result.snapshot.regime == market_regime.MarketRegime.BAD
    assert result.snapshot.reason == "intraday_drop_guard"
    assert result.snapshot.intraday_guard_reason == "intraday_drop:069500"
    assert result.quote_source == "ws_cache"
    assert result.quote_state == "fresh"


def test_quote_missing_with_skip_fallback_adopts_daily_only_snapshot_without_rest_call():
    now_kst = _kst_dt(2026, 3, 2, 9, 10)
    service = market_regime.MarketRegimeService(
        _DummyAPI(
            {
                "069500": _make_daily_df([100 + idx for idx in range(25)]),
                "229200": _make_daily_df([200 + idx for idx in range(25)]),
            }
        )
    )

    with _regime_settings():
        daily_context = service.build_daily_context(check_time=now_kst)
        with patch.object(
            service,
            "_load_intraday_probe",
            side_effect=AssertionError("REST intraday quote should not be called"),
        ):
            result = service.apply_intraday_guard(
                daily_context,
                check_time=now_kst,
                include_metrics=True,
                quote_snapshot_loader=lambda _symbol: {},
                use_ws_cache_only=True,
                quote_fallback_mode="skip",
                quote_max_age_sec=15.0,
                snapshot_source="background_refresh",
            )

    assert result.snapshot.regime == daily_context.regime
    assert result.snapshot.reason == daily_context.reason
    assert result.snapshot.intraday_guard_reason is None
    assert result.snapshot.intraday_guard_active is True
    assert result.quote_source == "skip"
    assert result.quote_state == "absent"


def test_quote_stale_with_skip_fallback_adopts_daily_only_snapshot_without_rest_call():
    now_kst = _kst_dt(2026, 3, 2, 9, 10)
    service = market_regime.MarketRegimeService(
        _DummyAPI(
            {
                "069500": _make_daily_df([100 + idx for idx in range(25)]),
                "229200": _make_daily_df([200 + idx for idx in range(25)]),
            }
        )
    )

    with _regime_settings():
        daily_context = service.build_daily_context(check_time=now_kst)
        with patch.object(
            service,
            "_load_intraday_probe",
            side_effect=AssertionError("REST intraday quote should not be called"),
        ):
            result = service.apply_intraday_guard(
                daily_context,
                check_time=now_kst,
                include_metrics=True,
                quote_snapshot_loader=lambda _symbol: {
                    "current_price": 98.0,
                    "open_price": 100.0,
                    "received_at": now_kst - timedelta(seconds=60),
                    "quote_age_sec": 60.0,
                },
                use_ws_cache_only=True,
                quote_fallback_mode="skip",
                quote_max_age_sec=15.0,
                snapshot_source="background_refresh",
            )

    assert result.snapshot.regime == daily_context.regime
    assert result.snapshot.reason == daily_context.reason
    assert result.snapshot.intraday_guard_reason is None
    assert result.quote_source == "skip"
    assert result.quote_state == "stale"


def test_market_regime_refresh_thread_bootstrap_refreshes_snapshot_immediately():
    service = market_regime.MarketRegimeService(
        _DummyAPI(
            {
                "069500": _make_daily_df([100 + idx for idx in range(25)]),
                "229200": _make_daily_df([200 + idx for idx in range(25)]),
            }
        )
    )
    stop_event = threading.Event()

    with _regime_settings(
        ENABLE_MARKET_REGIME_REFRESH_THREAD=True,
        MARKET_REGIME_REFRESH_INTERVAL_SEC=30.0,
    ):
        worker = market_regime_worker.MarketRegimeRefreshThread(
            service=service,
            quote_snapshot_loader=lambda _symbol: {},
            stop_event=stop_event,
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = worker.get_snapshot(check_time=_kst_dt(2026, 3, 2, 9, 10))
            if snapshot is not None:
                break
            time.sleep(0.01)
        stop_event.set()
        worker.join(timeout=1.0)

    assert snapshot is not None
    status = worker.get_status(now=_kst_dt(2026, 3, 2, 9, 10))
    assert status["refresh_state"] in ("refreshed", "background_stale")
    assert status["market_regime_background_refresh_fail_count"] == 0


def test_market_regime_refresh_thread_forces_daily_refresh_on_trade_date_change():
    service = market_regime.MarketRegimeService(api=SimpleNamespace())
    worker = market_regime_worker.MarketRegimeRefreshThread(service=service)
    worker._last_trade_date = "2026-03-02"
    worker._stop_event = SimpleNamespace(is_set=lambda: False, wait=lambda _timeout: False)

    with patch.object(
        market_regime_worker,
        "datetime",
        SimpleNamespace(now=lambda _tz=None: _kst_dt(2026, 3, 3, 9, 0)),
    ):
        should_force = worker._wait_until_next_cycle(
            interval_sec=30.0,
            force_on_trade_date_change=True,
        )

    assert should_force is True


def test_market_regime_refresh_thread_keeps_absent_snapshot_on_daily_context_failure():
    service = market_regime.MarketRegimeService(_DummyAPI({}))
    stop_event = threading.Event()

    with _regime_settings(
        ENABLE_MARKET_REGIME_REFRESH_THREAD=True,
        MARKET_REGIME_REFRESH_INTERVAL_SEC=30.0,
    ):
        worker = market_regime_worker.MarketRegimeRefreshThread(
            service=service,
            quote_snapshot_loader=lambda _symbol: {},
            stop_event=stop_event,
        )
        worker.start()
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not worker.get_status().get(
            "market_regime_worker_error_state"
        ):
            time.sleep(0.01)
        stop_event.set()
        worker.join(timeout=1.0)

    status = worker.get_status(now=_kst_dt(2026, 3, 2, 9, 10))
    assert status["snapshot"] is None
    assert status["refresh_state"] == "refresh_fail"
    assert status["market_regime_worker_error_state"]


def test_shared_snapshot_is_reused_within_ttl():
    now_kst = _kst_dt(2026, 3, 2, 10, 0)
    current_snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.GOOD,
        "fresh",
        as_of=now_kst - timedelta(seconds=10),
    )
    refresh_calls = {"count": 0}
    loop_context = market_regime.MarketRegimeLoopContext()

    def _refresh(_refresh_now):
        refresh_calls["count"] += 1
        return _make_shared_snapshot(
            market_regime.MarketRegime.BAD,
            "should_not_refresh",
            as_of=_refresh_now,
        )

    outcome = market_regime.refresh_shared_market_regime_snapshot(
        current_snapshot=current_snapshot,
        refresh_fn=_refresh,
        check_time=now_kst,
        loop_context=loop_context,
        budget_sec=1.5,
    )

    assert outcome.refreshed is False
    assert outcome.refresh_skipped_reason == "fresh_snapshot"
    assert refresh_calls["count"] == 0
    assert outcome.snapshot.as_of == current_snapshot.as_of


def test_stale_snapshot_refresh_attempted_once_per_loop():
    now_kst = _kst_dt(2026, 3, 2, 10, 0)
    expired_snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.GOOD,
        "expired",
        as_of=now_kst - timedelta(minutes=3),
        expires_after_sec=60,
        stale_after_sec=300,
    )
    refresh_calls = {"count": 0}
    loop_context = market_regime.MarketRegimeLoopContext()

    def _refresh(refresh_now):
        refresh_calls["count"] += 1
        return _make_shared_snapshot(
            market_regime.MarketRegime.NEUTRAL,
            "refreshed",
            as_of=refresh_now,
        )

    first = market_regime.refresh_shared_market_regime_snapshot(
        current_snapshot=expired_snapshot,
        refresh_fn=_refresh,
        check_time=now_kst,
        loop_context=loop_context,
        budget_sec=1.5,
    )
    second = market_regime.refresh_shared_market_regime_snapshot(
        current_snapshot=expired_snapshot,
        refresh_fn=_refresh,
        check_time=now_kst,
        loop_context=loop_context,
        budget_sec=1.5,
    )

    assert first.refreshed is True
    assert second.refreshed is False
    assert second.refresh_skipped_reason == "loop_refresh_already_attempted"
    assert refresh_calls["count"] == 1


def test_multiple_buy_candidates_share_single_refresh_per_loop():
    now_kst = _kst_dt(2026, 3, 2, 10, 0)
    expired_snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.GOOD,
        "expired",
        as_of=now_kst - timedelta(minutes=3),
        expires_after_sec=60,
        stale_after_sec=300,
    )
    refresh_calls = {"count": 0}
    loop_context = market_regime.MarketRegimeLoopContext()

    def _refresh(refresh_now):
        refresh_calls["count"] += 1
        return _make_shared_snapshot(
            market_regime.MarketRegime.GOOD,
            "refreshed",
            as_of=refresh_now,
        )

    for _symbol in ("005930", "000660", "035420"):
        market_regime.refresh_shared_market_regime_snapshot(
            current_snapshot=expired_snapshot,
            refresh_fn=_refresh,
            check_time=now_kst,
            loop_context=loop_context,
            budget_sec=1.5,
        )

    assert refresh_calls["count"] == 1


def test_refresh_budget_exceeded_adopts_new_snapshot_when_build_succeeds():
    now_kst = _kst_dt(2026, 3, 2, 10, 0)
    previous_snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.NEUTRAL,
        "previous",
        as_of=now_kst - timedelta(minutes=3),
        expires_after_sec=60,
        stale_after_sec=300,
    )

    with patch.object(market_regime, "monotonic", side_effect=[0.0, 2.0]):
        outcome = market_regime.refresh_shared_market_regime_snapshot(
            current_snapshot=previous_snapshot,
            refresh_fn=lambda refresh_now: _make_shared_snapshot(
                market_regime.MarketRegime.GOOD,
                "new_snapshot",
                as_of=refresh_now,
            ),
            check_time=now_kst,
            loop_context=market_regime.MarketRegimeLoopContext(),
            budget_sec=1.5,
        )

    assert outcome.refreshed is True
    assert outcome.budget_exceeded is True
    assert outcome.snapshot.as_of == now_kst
    assert outcome.snapshot.reason == "new_snapshot"
    assert outcome.using_previous_snapshot is False
    assert outcome.previous_as_of == previous_snapshot.as_of.isoformat()


def test_first_snapshot_bootstrap_budget_allows_initial_snapshot_without_previous():
    now_kst = _kst_dt(2026, 3, 2, 9, 1)

    with patch.object(market_regime, "monotonic", side_effect=[0.0, 2.0]):
        outcome = market_regime.refresh_shared_market_regime_snapshot(
            current_snapshot=None,
            refresh_fn=lambda refresh_now: _make_shared_snapshot(
                market_regime.MarketRegime.GOOD,
                "bootstrap_snapshot",
                as_of=refresh_now,
            ),
            check_time=now_kst,
            loop_context=market_regime.MarketRegimeLoopContext(),
            budget_sec=1.5,
        )

    assert outcome.refreshed is True
    assert outcome.budget_exceeded is False
    assert outcome.snapshot is not None
    assert outcome.used_bootstrap_budget is True
    assert outcome.effective_budget_sec == 3.0


def test_first_snapshot_bootstrap_budget_exceeded_keeps_valid_initial_snapshot():
    now_kst = _kst_dt(2026, 3, 2, 9, 1)

    with patch.object(market_regime, "monotonic", side_effect=[0.0, 4.0]):
        outcome = market_regime.refresh_shared_market_regime_snapshot(
            current_snapshot=None,
            refresh_fn=lambda refresh_now: _make_shared_snapshot(
                market_regime.MarketRegime.GOOD,
                "bootstrap_snapshot",
                as_of=refresh_now,
            ),
            check_time=now_kst,
            loop_context=market_regime.MarketRegimeLoopContext(),
            budget_sec=1.5,
        )

    assert outcome.refreshed is True
    assert outcome.budget_exceeded is True
    assert outcome.snapshot is not None
    assert outcome.snapshot.as_of == now_kst
    assert outcome.using_previous_snapshot is False
    assert outcome.previous_as_of is None


def test_budget_exceeded_bootstrap_snapshot_suppresses_no_snapshot_yet_warning(caplog):
    now_kst = _kst_dt(2026, 3, 2, 9, 1)
    observation_state = market_regime.MarketRegimeObservationState(startup_monotonic=0.0)

    with patch.object(market_regime, "monotonic", side_effect=[0.0, 4.0, 4.0]):
        outcome = market_regime.refresh_shared_market_regime_snapshot(
            current_snapshot=None,
            refresh_fn=lambda refresh_now: _make_shared_snapshot(
                market_regime.MarketRegime.GOOD,
                "bootstrap_snapshot",
                as_of=refresh_now,
            ),
            check_time=now_kst,
            loop_context=market_regime.MarketRegimeLoopContext(),
            budget_sec=1.5,
        )
        market_regime.observe_market_regime_snapshot(
            observation_state=observation_state,
            snapshot=outcome.snapshot,
            now_kst=now_kst,
            in_session=True,
            filter_enabled=True,
        )

    assert outcome.snapshot is not None
    assert "first_snapshot_created" in caplog.text
    assert "no_snapshot_yet" not in caplog.text


def test_observe_market_regime_snapshot_logs_first_snapshot_created(caplog):
    now_kst = _kst_dt(2026, 3, 2, 9, 5)
    observation_state = market_regime.MarketRegimeObservationState(startup_monotonic=0.0)
    snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.GOOD,
        "both_above_ma_and_stable_3d",
        as_of=now_kst,
    )

    with patch.object(market_regime, "monotonic", return_value=1.25):
        market_regime.observe_market_regime_snapshot(
            observation_state=observation_state,
            snapshot=snapshot,
            now_kst=now_kst,
            in_session=True,
            filter_enabled=True,
        )

    assert "first_snapshot_created" in caplog.text
    assert "startup_to_first_snapshot_sec=1.250" in caplog.text
    assert "session_first_snapshot_created" in caplog.text


def test_log_market_regime_refresh_outcome_includes_elapsed_breakdown_fields(caplog):
    now_kst = _kst_dt(2026, 3, 2, 9, 5)
    snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.GOOD,
        "both_above_ma_and_stable_3d",
        as_of=now_kst,
    )
    outcome = market_regime.MarketRegimeRefreshOutcome(
        snapshot=snapshot,
        refreshed=True,
        refresh_attempted=True,
        elapsed_sec=1.234,
        daily_fetch_elapsed_sec=0.456,
        intraday_fetch_elapsed_sec=0.321,
        classify_elapsed_sec=0.111,
    )

    market_regime.log_market_regime_refresh_outcome(outcome, snapshot)

    assert "total_refresh_elapsed_sec=1.234" in caplog.text
    assert "daily_fetch_elapsed_sec=0.456" in caplog.text
    assert "intraday_fetch_elapsed_sec=0.321" in caplog.text
    assert "classify_elapsed_sec=0.111" in caplog.text


def test_budget_exceeded_log_includes_elapsed_breakdown_fields(caplog):
    outcome = market_regime.MarketRegimeRefreshOutcome(
        snapshot=None,
        refreshed=False,
        refresh_attempted=True,
        elapsed_sec=2.345,
        budget_exceeded=True,
        daily_fetch_elapsed_sec=1.100,
        intraday_fetch_elapsed_sec=0.900,
        classify_elapsed_sec=0.200,
        effective_budget_sec=1.500,
        previous_as_of=None,
        using_previous_snapshot=False,
    )

    market_regime.log_market_regime_refresh_outcome(outcome, None)

    assert "snapshot_refresh_budget_exceeded" in caplog.text
    assert "budget_sec=1.500" in caplog.text
    assert "total_refresh_elapsed_sec=2.345" in caplog.text
    assert "daily_fetch_elapsed_sec=1.100" in caplog.text
    assert "intraday_fetch_elapsed_sec=0.900" in caplog.text
    assert "classify_elapsed_sec=0.200" in caplog.text


def test_budget_exceeded_log_still_warns_when_new_snapshot_is_adopted(caplog):
    now_kst = _kst_dt(2026, 3, 2, 9, 5)
    snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.GOOD,
        "budgeted_update",
        as_of=now_kst,
    )
    outcome = market_regime.MarketRegimeRefreshOutcome(
        snapshot=snapshot,
        refreshed=True,
        refresh_attempted=True,
        elapsed_sec=2.345,
        budget_exceeded=True,
        daily_fetch_elapsed_sec=1.100,
        intraday_fetch_elapsed_sec=0.900,
        classify_elapsed_sec=0.200,
        effective_budget_sec=1.500,
        previous_as_of=(now_kst - timedelta(minutes=1)).isoformat(),
        using_previous_snapshot=False,
    )

    market_regime.log_market_regime_refresh_outcome(outcome, snapshot)

    assert "snapshot_updated" in caplog.text
    assert "budget_exceeded=true" in caplog.text
    assert "snapshot_refresh_budget_exceeded" in caplog.text
    assert f"adopted_as_of={now_kst.isoformat()}" in caplog.text


def test_market_regime_build_log_uses_snapshot_built_label(caplog):
    snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.GOOD,
        "both_above_ma_and_stable_3d",
        as_of=_kst_dt(2026, 3, 2, 9, 5),
    )

    market_regime.MarketRegimeService(api=SimpleNamespace())._log_snapshot(snapshot)

    assert "snapshot_built" in caplog.text
    assert "snapshot_updated" not in caplog.text


def test_observe_market_regime_snapshot_logs_no_snapshot_yet_warning(caplog):
    now_kst = _kst_dt(2026, 3, 2, 9, 10)
    observation_state = market_regime.MarketRegimeObservationState(startup_monotonic=0.0)

    market_regime.observe_market_regime_snapshot(
        observation_state=observation_state,
        snapshot=None,
        now_kst=now_kst,
        in_session=True,
        filter_enabled=True,
    )

    assert "no_snapshot_yet" in caplog.text
    assert "elapsed_since_session_start_sec=600.0" in caplog.text


def test_execute_buy_blocks_when_market_regime_is_bad():
    ex, submitted = _make_executor_for_buy()
    now_kst = datetime.now(KST)
    ex.market_regime_snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.BAD,
        "both_below_ma",
        as_of=now_kst,
    )

    with _executor_settings():
        result = ex.execute_buy(_make_buy_signal())

    assert result["success"] is False
    assert result["skipped"] is True
    assert submitted["count"] == 0


def test_execute_buy_proceeds_when_market_regime_is_good_without_recalculation():
    ex, submitted = _make_executor_for_buy()
    now_kst = datetime.now(KST)
    ex.market_regime_snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.GOOD,
        "both_above_ma_and_stable_3d",
        as_of=now_kst,
    )

    with _executor_settings():
        result = ex.execute_buy(_make_buy_signal())

    assert result["success"] is True
    assert submitted["count"] == 1


def test_execute_buy_blocks_when_snapshot_is_stale_and_fail_mode_closed(caplog):
    ex, submitted = _make_executor_for_buy()
    ex.market_regime_snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.GOOD,
        "old_snapshot",
        as_of=_kst_dt(2026, 3, 2, 9, 0),
        expires_after_sec=60,
        stale_after_sec=120,
    )

    with _executor_settings(MARKET_REGIME_FAIL_MODE="closed"):
        result = ex.execute_buy(_make_buy_signal())

    assert result["success"] is False
    assert result["skipped"] is True
    assert submitted["count"] == 0
    assert "snapshot_stale" in caplog.text


def test_execute_buy_allows_when_snapshot_is_stale_and_fail_mode_open(caplog):
    ex, submitted = _make_executor_for_buy()
    ex.market_regime_snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.GOOD,
        "old_snapshot",
        as_of=_kst_dt(2026, 3, 2, 9, 0),
        expires_after_sec=60,
        stale_after_sec=120,
    )

    with _executor_settings(MARKET_REGIME_FAIL_MODE="open"):
        result = ex.execute_buy(_make_buy_signal())

    assert result["success"] is True
    assert submitted["count"] == 1
    assert "snapshot_stale" in caplog.text


def test_execute_buy_keeps_existing_behavior_when_filter_is_off():
    ex, submitted = _make_executor_for_buy()
    ex.market_regime_snapshot = None

    with _executor_settings(ENABLE_MARKET_REGIME_FILTER=False):
        result = ex.execute_buy(_make_buy_signal())

    assert result["success"] is True
    assert submitted["count"] == 1


def test_execute_buy_allows_after_budget_exceeded_refresh_advances_snapshot():
    ex, submitted = _make_executor_for_buy()
    now_kst = datetime.now(KST)
    previous_snapshot = _make_shared_snapshot(
        market_regime.MarketRegime.GOOD,
        "previous",
        as_of=now_kst - timedelta(minutes=5),
        expires_after_sec=60,
        stale_after_sec=180,
    )

    with patch.object(market_regime, "monotonic", side_effect=[0.0, 2.0]):
        outcome = market_regime.refresh_shared_market_regime_snapshot(
            current_snapshot=previous_snapshot,
            refresh_fn=lambda refresh_now: _make_shared_snapshot(
                market_regime.MarketRegime.GOOD,
                "advanced_on_budget_exceeded",
                as_of=refresh_now,
            ),
            check_time=now_kst,
            loop_context=market_regime.MarketRegimeLoopContext(),
            budget_sec=1.5,
        )
    ex.market_regime_snapshot = outcome.snapshot

    with _executor_settings(MARKET_REGIME_FAIL_MODE="closed"):
        result = ex.execute_buy(_make_buy_signal())

    assert outcome.budget_exceeded is True
    assert result["success"] is True
    assert submitted["count"] == 1
