from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import threading
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import strategy.multiday_trend_atr as multiday_trend_atr
from engine.pullback_pipeline_models import DailyContext
from engine.pullback_pipeline_stores import ArmedCandidateStore, DailyContextStore, DirtySymbolSet, EntryIntentQueue
from engine.pullback_pipeline_workers import DailyRefreshThread, PullbackSetupWorker, PullbackTimingWorker
from strategy.multiday_trend_atr import MultidayTrendATRStrategy, SignalType
from strategy.pullback_rebreakout import PullbackDecision, PullbackRebreakoutStrategy
from utils.market_hours import KST
from utils.market_phase import VenueMarketPhase


def _kst_dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return KST.localize(datetime(year, month, day, hour, minute, 0))


def _today_trade_date() -> str:
    return datetime.now(KST).date().isoformat()


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
        ENABLE_THREADED_PULLBACK_PIPELINE=True,
        ENABLE_PULLBACK_DAILY_REFRESH_THREAD=False,
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
        DAILY_CONTEXT_REFRESH_SEC=60,
        DAILY_CONTEXT_FORCE_REFRESH_ON_TRADE_DATE_CHANGE=True,
        DAILY_CONTEXT_STORE_MAX_SYMBOLS=256,
        DAILY_CONTEXT_STALE_SEC=180,
        ENABLE_OPENING_NO_ENTRY_GUARD=False,
        ENABLE_BREAKOUT_EXTENSION_CAP=False,
        ENABLE_ENTRY_GAP_FILTER=False,
    )


def _make_setup_candidate(current_price: float = 175.2):
    sleeve = PullbackRebreakoutStrategy()
    prepared_df = _make_pullback_indicator_df()
    with _patch_pullback_enabled():
        candidate, terminal = sleeve.evaluate_setup_candidate(
            df=prepared_df,
            current_price=current_price,
            stock_code="005930",
            stock_name="삼성전자",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
        )
    assert terminal is None
    assert candidate is not None
    return sleeve, prepared_df, candidate


class _TimingExecutorStub:
    def __init__(self, current_price: float = 175.2):
        self.stock_code = "005930"
        self.strategy = type("StrategyStub", (), {})()
        self.strategy.has_position = False
        self.strategy.pullback_strategy = PullbackRebreakoutStrategy()
        self.market_phase_context = VenueMarketPhase.KRX_CONTINUOUS
        self.market_venue_context = "KRX"
        self.market_regime_snapshot = None
        self._pullback_threaded_context_version = ""
        self._pullback_timing_skip_reason = ""
        self._pending_buy = False
        self._quote_snapshot = {
            "current_price": current_price,
            "open_price": 173.4,
            "stock_name": "삼성전자",
            "received_at": _kst_dt(2026, 2, 20, 10, 30),
        }

    def fetch_quote_snapshot(self):
        return dict(self._quote_snapshot)

    def get_cached_pullback_quote_snapshot(self):
        return dict(self._quote_snapshot)

    def fetch_cached_intraday_bars_if_available(self, n: int = 120):
        return []

    def _has_active_pending_buy_order(self):
        return self._pending_buy


class _DailyRefreshExecutorStub:
    def __init__(self):
        self.stock_code = "005930"
        self.strategy = type("StrategyStub", (), {})()
        self.strategy.has_position = False
        self.strategy.position = None
        self.strategy.pullback_strategy = PullbackRebreakoutStrategy()
        self.strategy.add_indicators = self._add_indicators
        self.market_phase_context = VenueMarketPhase.KRX_CONTINUOUS
        self.market_venue_context = "KRX"
        self.market_regime_snapshot = None
        self._pullback_daily_context_version = ""
        self._pullback_threaded_context_version = ""
        self._pullback_setup_skip_reason = ""
        self._pullback_latest_quote_snapshot = {
            "stock_code": "005930",
            "stock_name": "삼성전자",
            "current_price": 175.2,
            "open_price": 173.4,
            "received_at": _kst_dt(2026, 2, 20, 10, 30),
        }
        self.fetch_market_data_for_symbol_calls = 0
        self.fetch_market_data_called = 0

    @staticmethod
    def _add_indicators(df):
        prepared = _make_pullback_indicator_df()
        if "date" in df.columns and len(df) == len(prepared):
            prepared["date"] = pd.to_datetime(df["date"]).values
        return prepared

    def fetch_market_data_for_symbol(self, stock_code: str):
        self.fetch_market_data_for_symbol_calls += 1
        df = _make_pullback_indicator_df()[["date", "open", "high", "low", "close", "volume"]].copy()
        override_dates = getattr(self, "_context_trade_dates", None)
        if override_dates is not None:
            df.loc[df.index[-1], "date"] = next(override_dates)
        else:
            df.loc[df.index[-1], "date"] = pd.Timestamp(self._trade_date_key())
        return df

    def fetch_market_data(self):
        self.fetch_market_data_called += 1
        raise AssertionError("setup worker should not fetch market data directly when daily refresh is enabled")

    def get_pullback_daily_refresh_symbols(self):
        return [self.stock_code]

    def get_cached_pullback_quote_snapshot(self):
        return dict(self._pullback_latest_quote_snapshot)

    def _has_active_pending_buy_order(self):
        return False

    def _trade_date_key(self, check_time=None):
        return (check_time or datetime.now(KST)).astimezone(KST).date().isoformat()

    def _extract_market_data_trade_date(self, value):
        if isinstance(value, pd.Timestamp):
            return value.date().isoformat()
        if isinstance(value, datetime):
            return value.date().isoformat()
        return str(value)[:10]


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


def test_pullback_confirm_timing_falls_back_to_daily_when_real_intraday_is_missing():
    sleeve, _, candidate = _make_setup_candidate()

    with _patch_pullback_enabled():
        decision = sleeve.confirm_timing(
            candidate=candidate,
            current_price=175.2,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            intraday_bars=[],
            current_context_version=candidate.context_version,
        )

    assert decision.should_emit_intent is True
    assert decision.timing_source == "fallback_daily"
    assert decision.entry_reference_price == 174.5


def test_pullback_confirm_timing_invalidates_expired_and_context_mismatched_candidates():
    sleeve, _, candidate = _make_setup_candidate()
    expired = replace(candidate, expires_at=_kst_dt(2026, 2, 20, 10, 29))

    with _patch_pullback_enabled():
        expired_decision = sleeve.confirm_timing(
            candidate=expired,
            current_price=175.2,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            current_context_version=expired.context_version,
        )
        mismatch_decision = sleeve.confirm_timing(
            candidate=candidate,
            current_price=175.2,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 10, 30),
            market_phase=VenueMarketPhase.KRX_CONTINUOUS,
            current_context_version="stale-version",
        )

    assert expired_decision.invalidate_candidate is True
    assert expired_decision.reason_code == "pullback_candidate_expired"
    assert mismatch_decision.invalidate_candidate is True
    assert mismatch_decision.reason_code == "pullback_context_version_mismatch"


def test_pullback_confirm_timing_invalidates_market_phase_mismatch():
    sleeve, _, candidate = _make_setup_candidate()
    candidate = replace(candidate, expires_at=_kst_dt(2026, 2, 20, 16, 0))

    with _patch_pullback_enabled():
        decision = sleeve.confirm_timing(
            candidate=candidate,
            current_price=175.2,
            stock_code="005930",
            check_time=_kst_dt(2026, 2, 20, 15, 35),
            market_phase=VenueMarketPhase.KRX_POSTCLOSE,
            current_context_version=candidate.context_version,
        )

    assert decision.invalidate_candidate is True
    assert decision.reason_code == "pullback_market_phase_mismatch"


def test_armed_candidate_store_is_thread_safe_basic():
    _, _, base_candidate = _make_setup_candidate()
    store = ArmedCandidateStore()
    threads = []

    def _writer(idx: int) -> None:
        store.upsert(replace(base_candidate, symbol=f"{idx:06d}"))

    for idx in range(12):
        thread = threading.Thread(target=_writer, args=(idx,), daemon=True)
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()

    assert store.size() == 12


def test_dirty_symbol_set_dedupes_duplicate_marks():
    dirty = DirtySymbolSet()
    dirty.mark("005930")
    dirty.mark("005930")
    dirty.mark("5930")

    assert dirty.drain() == ["005930"]
    assert dirty.size() == 0


def test_timing_worker_skips_symbol_without_armed_candidate():
    executor = _TimingExecutorStub()
    worker = PullbackTimingWorker(
        executor=executor,
        candidate_store=ArmedCandidateStore(),
        dirty_symbols=DirtySymbolSet(),
        entry_queue=EntryIntentQueue(maxsize=8),
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )

    worker._process_symbol("005930")

    assert executor._pullback_timing_skip_reason == "no_candidate"


def test_duplicate_dirty_events_do_not_create_duplicate_pullback_intents():
    executor = _TimingExecutorStub()
    store = ArmedCandidateStore()
    dirty = DirtySymbolSet()
    entry_queue = EntryIntentQueue(maxsize=8)
    worker = PullbackTimingWorker(
        executor=executor,
        candidate_store=store,
        dirty_symbols=dirty,
        entry_queue=entry_queue,
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )
    _, _, candidate = _make_setup_candidate()
    executor._pullback_threaded_context_version = candidate.context_version
    store.upsert(candidate)

    worker._process_symbol(candidate.symbol)
    worker._process_symbol(candidate.symbol)

    assert entry_queue.qsize() == 1


def test_daily_context_store_validates_stale_trade_date_and_version():
    store = DailyContextStore(max_symbols=8)
    trade_date = _today_trade_date()
    refreshed_at = datetime.now(KST)
    context = DailyContext(
        symbol="005930",
        trade_date=trade_date,
        context_version="ctx-1",
        recent_bars=tuple(_make_pullback_indicator_df().tail(5).to_dict(orient="records")),
        prev_high=176.5,
        prev_close=173.2,
        atr=2.0,
        adx=32.0,
        trend="UPTREND",
        ma20=171.0,
        ma50=160.0,
        swing_high=180.5,
        swing_low=169.5,
        refreshed_at=refreshed_at,
        source="unittest",
    )
    store.upsert(context)

    valid, reason = store.get_validated(
        "005930",
        expected_trade_date=trade_date,
        stale_after_sec=180,
        expected_context_version="ctx-1",
        now=refreshed_at + timedelta(seconds=30),
    )
    stale, stale_reason = store.get_validated(
        "005930",
        expected_trade_date=trade_date,
        stale_after_sec=10,
        now=refreshed_at + timedelta(seconds=30),
    )
    mismatch, mismatch_reason = store.get_validated(
        "005930",
        expected_trade_date="2099-01-01",
        stale_after_sec=180,
        now=refreshed_at + timedelta(seconds=30),
    )
    version_mismatch, version_reason = store.get_validated(
        "005930",
        expected_trade_date=trade_date,
        expected_context_version="ctx-2",
        stale_after_sec=180,
        now=refreshed_at + timedelta(seconds=30),
    )

    assert valid is not None
    assert reason == ""
    assert stale is None
    assert stale_reason == "stale"
    assert mismatch is None
    assert mismatch_reason == "trade_date_mismatch"
    assert version_mismatch is None
    assert version_reason == "version_mismatch"


def test_daily_context_store_overwrite_updates_context_version():
    store = DailyContextStore(max_symbols=8)
    trade_date = _today_trade_date()
    base = DailyContext(
        symbol="005930",
        trade_date=trade_date,
        context_version="ctx-1",
        recent_bars=tuple(_make_pullback_indicator_df().tail(5).to_dict(orient="records")),
        prev_high=176.5,
        prev_close=173.2,
        atr=2.0,
        adx=32.0,
        trend="UPTREND",
        ma20=171.0,
        ma50=160.0,
        swing_high=180.5,
        swing_low=169.5,
        refreshed_at=datetime.now(KST),
        source="unittest",
    )
    store.upsert(base)
    store.upsert(replace(base, context_version="ctx-2"))

    assert store.get("005930").context_version == "ctx-2"


def test_daily_refresh_thread_populates_daily_context_store():
    executor = _DailyRefreshExecutorStub()
    store = DailyContextStore(max_symbols=8)
    worker = DailyRefreshThread(
        executor=executor,
        daily_context_store=store,
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )

    with _patch_pullback_enabled(), patch.object(multiday_trend_atr.settings, "ENABLE_PULLBACK_DAILY_REFRESH_THREAD", True):
        worker._run_cycle()

    context = store.get("005930")
    assert context is not None
    assert context.trade_date == _today_trade_date()
    assert context.context_version
    assert executor.fetch_market_data_for_symbol_calls == 1


def test_setup_worker_reads_daily_context_store_only_when_enabled():
    executor = _DailyRefreshExecutorStub()
    context_store = DailyContextStore(max_symbols=8)
    refresh_worker = DailyRefreshThread(
        executor=executor,
        daily_context_store=context_store,
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )
    setup_worker = PullbackSetupWorker(
        executor=executor,
        candidate_store=ArmedCandidateStore(),
        daily_context_store=context_store,
        dirty_symbols=DirtySymbolSet(),
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )

    with _patch_pullback_enabled(), patch.object(multiday_trend_atr.settings, "ENABLE_PULLBACK_DAILY_REFRESH_THREAD", True):
        refresh_worker._run_cycle()
        setup_worker._run_cycle()

    assert executor.fetch_market_data_called == 0
    assert setup_worker._candidate_store.get("005930") is not None


def test_setup_worker_skips_stale_daily_context():
    executor = _DailyRefreshExecutorStub()
    context_store = DailyContextStore(max_symbols=8)
    refreshed_at = datetime.now(KST) - timedelta(minutes=10)
    stale_context = DailyContext(
        symbol="005930",
        trade_date=_today_trade_date(),
        context_version="ctx-1",
        recent_bars=tuple(_make_pullback_indicator_df().tail(50).to_dict(orient="records")),
        prev_high=176.5,
        prev_close=173.2,
        atr=2.0,
        adx=32.0,
        trend="UPTREND",
        ma20=171.0,
        ma50=160.0,
        swing_high=180.5,
        swing_low=169.5,
        refreshed_at=refreshed_at,
        source="unittest",
    )
    context_store.upsert(stale_context)
    executor._pullback_daily_context_version = "ctx-1"
    setup_worker = PullbackSetupWorker(
        executor=executor,
        candidate_store=ArmedCandidateStore(),
        daily_context_store=context_store,
        dirty_symbols=DirtySymbolSet(),
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )

    with _patch_pullback_enabled(), \
         patch.object(multiday_trend_atr.settings, "ENABLE_PULLBACK_DAILY_REFRESH_THREAD", True), \
         patch.object(multiday_trend_atr.settings, "DAILY_CONTEXT_STALE_SEC", 60):
        setup_worker._run_cycle()

    assert setup_worker._candidate_store.get("005930") is None
    assert executor._pullback_setup_skip_reason == "stale"


def test_daily_refresh_thread_force_refreshes_on_trade_date_change():
    executor = _DailyRefreshExecutorStub()
    executor._context_trade_dates = iter([pd.Timestamp("2026-02-20"), pd.Timestamp("2026-02-21")])
    store = DailyContextStore(max_symbols=8)
    worker = DailyRefreshThread(
        executor=executor,
        daily_context_store=store,
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )
    times = iter([
        _kst_dt(2026, 2, 20, 10, 30),
        _kst_dt(2026, 2, 21, 9, 1),
    ])
    worker._now = lambda: next(times)

    with _patch_pullback_enabled(), patch.object(multiday_trend_atr.settings, "ENABLE_PULLBACK_DAILY_REFRESH_THREAD", True):
        worker._run_cycle()
        worker._run_cycle()

    assert executor.fetch_market_data_for_symbol_calls == 2
    assert store.get("005930").trade_date == "2026-02-21"
