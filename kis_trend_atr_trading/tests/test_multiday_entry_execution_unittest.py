from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import engine.multiday_executor as multiday_executor
from engine.pullback_pipeline_models import (
    AccountRiskSnapshot,
    HoldingsRiskSnapshot,
    PullbackEntryIntent,
    PullbackSetupCandidate,
)
from engine.pullback_pipeline_stores import AccountRiskStore, ArmedCandidateStore, EntryIntentQueue
from engine.pullback_pipeline_workers import OrderExecutionWorker
from engine.multiday_executor import MultidayExecutor
from strategy.multiday_trend_atr import TradingSignal, SignalType, TrendType
from strategy.pullback_rebreakout import PullbackCandidate, PullbackDecision
from utils.market_hours import KST


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


def _make_account_snapshot(
    *,
    cash_balance: float = 5_000_000.0,
    total_eval: float = 12_000_000.0,
    source: str = "background_refresh",
) -> AccountRiskSnapshot:
    fetched_at = datetime.now(KST)
    return AccountRiskSnapshot(
        fetched_at=fetched_at,
        total_eval=total_eval,
        cash_balance=cash_balance,
        total_pnl=0.0,
        holdings=(),
        source=source,
        success=True,
        stale=False,
        version="acct-1",
    )


def _make_holdings_snapshot(
    *,
    holdings: tuple[dict, ...] = (),
    source: str = "background_refresh",
) -> HoldingsRiskSnapshot:
    fetched_at = datetime.now(KST)
    return HoldingsRiskSnapshot(
        fetched_at=fetched_at,
        holdings=holdings,
        source=source,
        success=True,
        stale=False,
        version="hold-1",
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


def test_executor_builds_protected_limit_plan_using_orb_reference_price_and_cap():
    executor = _make_executor()
    signal = _make_buy_signal(prev_high=50000.0, strategy_tag="opening_range_breakout")
    signal.price = 50900.0
    signal.meta["current_price_at_signal"] = 50900.0
    signal.meta["entry_reference_price"] = 50750.0
    signal.meta["entry_reference_label"] = "opening_range_high"
    signal.meta["max_allowed_pct"] = 0.006

    with patch.object(multiday_executor.settings, "ENTRY_ORDER_STYLE", "protected_limit"), \
         patch.object(multiday_executor.settings, "ENTRY_PROTECT_TICKS_STOCK", 0), \
         patch.object(multiday_executor.settings, "ENTRY_PROTECT_TICKS_ETF", 1), \
         patch.object(multiday_executor.settings, "ENTRY_MAX_SLIPPAGE_PCT", 0.02), \
         patch.object(multiday_executor.settings, "ENABLE_BREAKOUT_EXTENSION_CAP", True), \
         patch.object(multiday_executor.settings, "MAX_BREAKOUT_EXTENSION_PCT_STOCK", 0.001), \
         patch.object(multiday_executor.settings, "MAX_BREAKOUT_EXTENSION_PCT_ETF", 0.001):
        plan = executor._build_entry_order_plan(
            signal,
            {
                "stock_name": "삼성전자",
                "current_price": 50900.0,
                "best_ask": 50900.0,
            },
        )

    assert plan["blocked"] is False
    assert plan["entry_reference_price"] == 50750.0
    assert plan["entry_reference_label"] == "opening_range_high"
    assert round(plan["extension_pct_at_order"], 6) == round((50900.0 / 50750.0) - 1.0, 6)


class _OrderWorkerStrategyStub:
    def __init__(self):
        self.has_position = False
        self.pullback_strategy = self

    def evaluate(self, **_kwargs):
        return PullbackCandidate(
            decision=PullbackDecision.BUY,
            reason="건강한 눌림 후 재돌파 진입",
            atr=2.0,
            trigger_price=174.5,
            meta={"strategy_tag": "pullback_rebreakout", "adx": 32.0},
        )

    def build_pullback_buy_signal(self, **_kwargs):
        return TradingSignal(
            signal_type=SignalType.BUY,
            price=175.2,
            atr=2.0,
            trend=TrendType.UPTREND,
            reason="threaded pullback",
            meta={"strategy_tag": "pullback_rebreakout"},
        )

    def add_indicators(self, df):
        return df


class _OrderWorkerExecutorStub:
    def __init__(self):
        self.stock_code = "005930"
        self.strategy = _OrderWorkerStrategyStub()
        self.market_phase_context = None
        self.market_venue_context = "KRX"
        self.market_regime_snapshot = None
        self.execute_buy_calls = 0
        self.cached_account_has_holding = lambda _symbol: False

    def _has_active_pending_buy_order(self):
        return False

    def fetch_quote_snapshot(self):
        return {
            "stock_code": "005930",
            "stock_name": "삼성전자",
            "current_price": 175.2,
            "open_price": 173.4,
            "received_at": KST.localize(datetime(2026, 2, 20, 10, 30)),
        }

    def fetch_market_data(self):
        return pd.DataFrame(
            {
                "date": [datetime(2026, 2, 19), datetime(2026, 2, 20)],
                "open": [173.0, 174.0],
                "high": [174.0, 175.0],
                "low": [172.0, 173.0],
                "close": [173.5, 174.5],
                "atr": [2.0, 2.0],
                "adx": [32.0, 32.0],
                "ma": [160.0, 160.0],
                "ma20": [171.0, 171.0],
                "prev_close": [172.0, 173.5],
            }
        )

    def execute_buy(self, signal):
        self.execute_buy_calls += 1
        return {"success": True, "signal_price": signal.price}


def test_order_worker_single_dispatches_pullback_entry_intent_once():
    executor = _OrderWorkerExecutorStub()
    store = ArmedCandidateStore()
    entry_queue = EntryIntentQueue(maxsize=8)
    worker = OrderExecutionWorker(
        executor=executor,
        candidate_store=store,
        entry_queue=entry_queue,
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )
    candidate = PullbackSetupCandidate(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=KST.localize(datetime(2026, 2, 20, 10, 0)),
        expires_at=KST.localize(datetime(2026, 2, 20, 12, 0)),
        context_version="ctx-1",
        swing_high=180.5,
        swing_low=170.0,
        micro_high=174.5,
        atr=2.0,
        source="daily_setup",
        extra_json={"market_phase": "KRX_CONTINUOUS"},
    )
    store.upsert(candidate)
    first_intent = PullbackEntryIntent(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=KST.localize(datetime(2026, 2, 20, 10, 30)),
        candidate_created_at=candidate.created_at,
        expires_at=candidate.expires_at,
        context_version="ctx-1",
        entry_reference_price=174.5,
        source="fallback_daily",
    )
    duplicate_intent = PullbackEntryIntent(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=KST.localize(datetime(2026, 2, 20, 10, 31)),
        candidate_created_at=candidate.created_at,
        expires_at=candidate.expires_at,
        context_version="ctx-1",
        entry_reference_price=174.5,
        source="fallback_daily",
    )

    assert entry_queue.put_if_absent(first_intent) is True
    assert entry_queue.put_if_absent(duplicate_intent) is False

    intent = entry_queue.get(timeout=0.1)
    try:
        worker._process_intent(intent)
    finally:
        entry_queue.complete(intent)

    assert executor.execute_buy_calls == 1


def test_order_worker_prefers_cached_holdings_snapshot_precheck():
    executor = _OrderWorkerExecutorStub()
    executor.cached_account_has_holding = lambda symbol: str(symbol).zfill(6) == "005930"
    store = ArmedCandidateStore()
    entry_queue = EntryIntentQueue(maxsize=8)
    worker = OrderExecutionWorker(
        executor=executor,
        candidate_store=store,
        entry_queue=entry_queue,
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )
    candidate = PullbackSetupCandidate(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=KST.localize(datetime(2026, 2, 20, 10, 0)),
        expires_at=KST.localize(datetime(2026, 2, 20, 12, 0)),
        context_version="ctx-1",
        swing_high=180.5,
        swing_low=170.0,
        micro_high=174.5,
        atr=2.0,
        source="daily_setup",
    )
    store.upsert(candidate)
    intent = PullbackEntryIntent(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=KST.localize(datetime(2026, 2, 20, 10, 30)),
        candidate_created_at=candidate.created_at,
        expires_at=candidate.expires_at,
        context_version="ctx-1",
        entry_reference_price=174.5,
        source="background_refresh",
    )

    worker._process_intent(intent)

    assert executor.execute_buy_calls == 0
    assert store.get("005930") is None


def test_order_final_validation_uses_fresh_store_snapshot_without_sync_fallback():
    executor = _make_executor()
    executor.order_quantity = 2
    executor._report_mode = "REAL"
    executor._threaded_pullback_pipeline_disabled = False
    executor._pullback_account_risk_store = AccountRiskStore()
    executor._pullback_account_risk_store.replace_account_snapshot(_make_account_snapshot(cash_balance=5_000_000.0))
    executor._pullback_account_risk_store.replace_holdings_snapshot(_make_holdings_snapshot())
    executor.api = SimpleNamespace(
        get_account_balance=lambda: (_ for _ in ()).throw(AssertionError("account fallback should not run")),
        get_holdings=lambda: (_ for _ in ()).throw(AssertionError("holdings fallback should not run")),
    )

    with patch.object(multiday_executor.settings, "ENABLE_THREADED_PULLBACK_PIPELINE", True), \
         patch.object(multiday_executor.settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", True), \
         patch.object(multiday_executor.settings, "ENABLE_RISK_SNAPSHOT_THREAD", True), \
         patch.object(multiday_executor.settings, "ORDER_FINAL_VALIDATION_MODE", "light"):
        result = executor._run_order_final_validation(_make_buy_signal(strategy_tag="pullback_rebreakout"))

    assert result["allowed"] is True


def test_order_final_validation_falls_back_sync_when_real_snapshot_is_stale():
    executor = _make_executor()
    executor.order_quantity = 1
    executor._report_mode = "REAL"
    executor._threaded_pullback_pipeline_disabled = False
    executor._pullback_account_risk_store = AccountRiskStore()
    stale_time = KST.localize(datetime(2026, 2, 20, 10, 0))
    executor._pullback_account_risk_store.replace_account_snapshot(
        AccountRiskSnapshot(
            fetched_at=stale_time,
            total_eval=10_000_000.0,
            cash_balance=0.0,
            total_pnl=0.0,
            holdings=(),
            source="background_refresh",
            success=True,
            stale=False,
            version="acct-stale",
        )
    )
    executor._pullback_account_risk_store.replace_holdings_snapshot(
        HoldingsRiskSnapshot(
            fetched_at=stale_time,
            holdings=(),
            source="background_refresh",
            success=True,
            stale=False,
            version="hold-stale",
        )
    )
    account_calls = {"count": 0}
    holdings_calls = {"count": 0}
    executor.api = SimpleNamespace(
        get_account_balance=lambda: (account_calls.__setitem__("count", account_calls["count"] + 1) or {
            "success": True,
            "total_eval": 12_000_000.0,
            "cash_balance": 8_000_000.0,
            "total_pnl": 0.0,
            "holdings": [],
        }),
        get_holdings=lambda: (holdings_calls.__setitem__("count", holdings_calls["count"] + 1) or []),
    )

    with patch.object(multiday_executor.settings, "ENABLE_THREADED_PULLBACK_PIPELINE", True), \
         patch.object(multiday_executor.settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", True), \
         patch.object(multiday_executor.settings, "ENABLE_RISK_SNAPSHOT_THREAD", True), \
         patch.object(multiday_executor.settings, "ORDER_FINAL_VALIDATION_MODE", "light"), \
         patch.object(multiday_executor.settings, "RISK_SNAPSHOT_TTL_SEC", 60), \
         patch.object(multiday_executor.settings, "HOLDINGS_SNAPSHOT_TTL_SEC", 30):
        result = executor._run_order_final_validation(_make_buy_signal(strategy_tag="pullback_rebreakout"))

    assert result["allowed"] is True
    assert account_calls["count"] == 1
    assert holdings_calls["count"] == 1


def test_order_final_validation_feature_flag_off_preserves_existing_behavior():
    executor = _make_executor()
    executor.order_quantity = 1
    executor._report_mode = "REAL"
    executor._threaded_pullback_pipeline_disabled = False
    executor._pullback_account_risk_store = AccountRiskStore()
    executor.api = SimpleNamespace(
        get_account_balance=lambda: (_ for _ in ()).throw(AssertionError("account fallback should not run when feature is off")),
        get_holdings=lambda: (_ for _ in ()).throw(AssertionError("holdings fallback should not run when feature is off")),
    )

    with patch.object(multiday_executor.settings, "ENABLE_THREADED_PULLBACK_PIPELINE", False), \
         patch.object(multiday_executor.settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", True), \
         patch.object(multiday_executor.settings, "ENABLE_RISK_SNAPSHOT_THREAD", True), \
         patch.object(multiday_executor.settings, "ORDER_FINAL_VALIDATION_MODE", "light"):
        result = executor._run_order_final_validation(_make_buy_signal(strategy_tag="pullback_rebreakout"))

    assert result["allowed"] is True
