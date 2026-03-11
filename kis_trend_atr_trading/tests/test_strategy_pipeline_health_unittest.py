from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import threading
import sys
from types import SimpleNamespace

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine.pullback_pipeline_models import AuthoritativeEntryIntent, PullbackEntryIntent, PullbackSetupCandidate
from engine.pullback_pipeline_stores import ArmedCandidateStore, DirtySymbolSet, EntryIntentQueue
from engine.pullback_pipeline_workers import OrderExecutionWorker, PullbackTimingWorker
from engine.strategy_pipeline_health import (
    DegradedModeController,
    PipelineHealthMonitorThread,
    WorkerHealthStore,
    WorkerState,
)
from strategy.multiday_trend_atr import SignalType, TradingSignal, TrendType
from strategy.pullback_rebreakout import PullbackCandidate, PullbackDecision
from utils.market_hours import KST


def _kst_dt(hour: int, minute: int) -> datetime:
    return KST.localize(datetime(2026, 3, 11, hour, minute, 0))


def _future_kst(minutes: int = 30) -> datetime:
    now = datetime.now(KST)
    return now.replace(second=0, microsecond=0) + timedelta(minutes=minutes)


def _make_authoritative_intent(
    *,
    strategy_tag: str,
    symbol: str = "005930",
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> AuthoritativeEntryIntent:
    created = created_at or _future_kst(0)
    expires = expires_at or _future_kst(30)
    return AuthoritativeEntryIntent(
        strategy_tag=strategy_tag,
        symbol=str(symbol).zfill(6),
        created_at=created,
        expires_at=expires,
        trade_date=created.date().isoformat(),
        entry_reference_price=50000.0,
        entry_reference_label="prev_high",
        native_payload={"strategy_tag": strategy_tag, "symbol": str(symbol).zfill(6)},
        source="unittest",
        meta={"strategy_tag": strategy_tag},
    )


class _DegradedTimingExecutorStub:
    def __init__(self):
        self.stock_code = "005930"
        self._authoritative_intent_reject_reason = ""
        self._authoritative_queue_reject_reason = ""
        self._degraded_ingress_reject_count_by_strategy = {}
        self._mixed_strategy_tiebreak_count = 0
        self._authoritative_intent_queue_depth = 0
        self._authoritative_intent_queue_depth_by_strategy = {}
        self._pullback_intent_queue_depth = 0
        self._intent_queue_depth_by_strategy = {}
        self._pipeline_worker_stall_sec = 20.0
        self.strategy = SimpleNamespace(has_position=False)

    def _is_multi_strategy_threaded_pipeline_enabled(self):
        return True


class _PullbackOrderStrategyStub:
    def __init__(self):
        self.has_position = False
        self.pullback_strategy = self

    def evaluate(self, **_kwargs):
        return PullbackCandidate(
            decision=PullbackDecision.BUY,
            reason="threaded pullback",
            atr=2.0,
            trigger_price=174.5,
            meta={"strategy_tag": "pullback_rebreakout"},
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


class _DegradedDrainExecutorStub:
    def __init__(self):
        self.stock_code = "005930"
        self.market_phase_context = None
        self.market_venue_context = "KRX"
        self.market_regime_snapshot = None
        self.execute_buy_calls = 0
        self.cached_account_has_holding = lambda _symbol: False
        self.strategy = _PullbackOrderStrategyStub()
        self._degraded_mode_current = True

    def _has_active_pending_buy_order(self):
        return False

    def fetch_quote_snapshot(self):
        return {
            "stock_code": "005930",
            "stock_name": "삼성전자",
            "current_price": 175.2,
            "open_price": 173.4,
            "received_at": _kst_dt(10, 30),
        }

    def fetch_market_data(self):
        return pd.DataFrame(
            {
                "date": [datetime(2026, 3, 10), datetime(2026, 3, 11)],
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


def test_dirty_symbol_set_coalesces_and_respects_max_batch():
    dirty = DirtySymbolSet()
    dirty.mark("5930")
    dirty.mark("005930")
    dirty.mark("000660")

    first_batch = dirty.drain(max_items=1)
    assert len(first_batch) == 1
    assert dirty.size() == 1

    second_batch = dirty.drain(max_items=10)
    assert second_batch == ["005930"] or second_batch == ["000660"]
    assert dirty.size() == 0


def test_non_authoritative_buffer_drop_oldest_policy_replaces_old_entry():
    queue = EntryIntentQueue(maxsize=1, authoritative=False, drop_policy="drop_oldest")
    first = _make_authoritative_intent(strategy_tag="trend_atr", symbol="005930", created_at=_future_kst(0))
    second = _make_authoritative_intent(strategy_tag="trend_atr", symbol="000660", created_at=_future_kst(1))

    assert queue.put_if_absent(first) is True
    assert queue.put_if_absent(second) is True
    assert queue.dropped_count() == 1

    queued = queue.get(timeout=0.1)
    assert queued.symbol == "000660"
    queue.complete(queued)


def test_authoritative_queue_never_applies_drop_oldest_policy():
    queue = EntryIntentQueue(maxsize=1, authoritative=True, drop_policy="drop_oldest")
    first = _make_authoritative_intent(strategy_tag="pullback_rebreakout", symbol="005930", created_at=_future_kst(0))
    second = _make_authoritative_intent(strategy_tag="trend_atr", symbol="000660", created_at=_future_kst(1))

    assert queue.put_if_absent(first) is True
    assert queue.put_if_absent(second) is False
    assert queue.last_reject_reason() == "queue_full"
    assert queue.dropped_count() == 0

    queued = queue.get(timeout=0.1)
    assert queued.symbol == "005930"
    queue.complete(queued)


def test_authoritative_queue_per_symbol_cap_rejects_second_strategy_intent():
    queue = EntryIntentQueue(
        maxsize=8,
        authoritative=True,
        drop_policy="reject_new",
        max_pending_per_symbol=1,
    )
    pullback = _make_authoritative_intent(strategy_tag="pullback_rebreakout", symbol="005930")
    trend = _make_authoritative_intent(strategy_tag="trend_atr", symbol="005930", created_at=_future_kst(1))

    assert queue.put_if_absent(pullback) is True
    assert queue.put_if_absent(trend) is False
    assert queue.last_reject_reason() == "pending_symbol_cap"


def test_worker_health_store_detects_stalled_worker():
    store = WorkerHealthStore()
    store.ensure_worker("TimingWorker", stall_after_sec=1.0)
    store.heartbeat("TimingWorker")
    snapshots = store.evaluate(now=datetime.now(KST) + timedelta(seconds=2))

    assert snapshots["TimingWorker"].state == WorkerState.STALLED
    assert "stall_lag" in snapshots["TimingWorker"].state_reason


def test_degraded_mode_controller_enters_and_exits_with_hysteresis():
    controller = DegradedModeController(
        enabled=True,
        enter_queue_depth=5,
        exit_queue_depth=2,
        min_hold_sec=15.0,
    )
    now = _kst_dt(9, 0)

    entered = controller.evaluate(queue_depth=6, worker_snapshots={}, now=now)
    assert entered.is_degraded is True

    held = controller.evaluate(queue_depth=1, worker_snapshots={}, now=now + timedelta(seconds=10))
    assert held.is_degraded is True

    exited = controller.evaluate(queue_depth=1, worker_snapshots={}, now=now + timedelta(seconds=20))
    assert exited.is_degraded is False
    assert exited.transitions == 2


def test_degraded_mode_controller_disabled_is_regression_safe():
    controller = DegradedModeController(
        enabled=False,
        enter_queue_depth=1,
        exit_queue_depth=0,
        min_hold_sec=0.0,
    )
    snapshot = controller.evaluate(
        queue_depth=100,
        worker_snapshots={
            "Setup": SimpleNamespace(state=WorkerState.STALLED),
        },
        now=_kst_dt(9, 0),
    )
    assert snapshot.is_degraded is False


def test_timing_worker_rejects_new_authoritative_ingress_when_degraded_for_all_strategies():
    executor = _DegradedTimingExecutorStub()
    controller = DegradedModeController(
        enabled=True,
        enter_queue_depth=1,
        exit_queue_depth=0,
        min_hold_sec=15.0,
    )
    controller.evaluate(queue_depth=2, worker_snapshots={}, now=_kst_dt(9, 0))
    queue = EntryIntentQueue(maxsize=8, authoritative=True)
    worker = PullbackTimingWorker(
        executor=executor,
        candidate_store=ArmedCandidateStore(),
        dirty_symbols=DirtySymbolSet(),
        entry_queue=queue,
        strategy_registry=None,
        enabled_strategy_tags=("pullback_rebreakout", "trend_atr", "opening_range_breakout"),
        health_store=WorkerHealthStore(),
        degraded_controller=controller,
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )

    for strategy_tag in ("pullback_rebreakout", "trend_atr", "opening_range_breakout"):
        assert worker._enqueue_authoritative_intent(_make_authoritative_intent(strategy_tag=strategy_tag)) is False

    assert queue.qsize() == 0
    assert executor._authoritative_intent_reject_reason == "degraded_mode"
    assert executor._degraded_ingress_reject_count_by_strategy == {
        "pullback_rebreakout": 1,
        "trend_atr": 1,
        "opening_range_breakout": 1,
    }


def test_order_worker_drains_existing_authoritative_pullback_intent_even_when_degraded():
    executor = _DegradedDrainExecutorStub()
    queue = EntryIntentQueue(maxsize=8, authoritative=True)
    worker = OrderExecutionWorker(
        executor=executor,
        candidate_store=ArmedCandidateStore(),
        entry_queue=queue,
        health_store=WorkerHealthStore(),
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )
    native_intent = PullbackEntryIntent(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=_future_kst(5),
        candidate_created_at=_future_kst(0),
        expires_at=_future_kst(60),
        context_version="ctx-1",
        entry_reference_price=175.0,
        source="intraday_confirm",
        current_price=175.2,
    )
    envelope = AuthoritativeEntryIntent(
        strategy_tag="pullback_rebreakout",
        symbol="005930",
        created_at=native_intent.created_at,
        expires_at=native_intent.expires_at,
        trade_date=native_intent.created_at.date().isoformat(),
        entry_reference_price=native_intent.entry_reference_price,
        entry_reference_label="pullback_intraday_high",
        native_payload=native_intent,
        source="intraday_confirm",
        meta={},
    )

    worker._process_intent(envelope)
    assert executor.execute_buy_calls == 1


def test_candidate_cleanup_uses_native_expires_at_authority_only():
    store = ArmedCandidateStore()
    stale_created_but_future_expiry = PullbackSetupCandidate(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=_kst_dt(8, 0),
        expires_at=_future_kst(120),
        context_version="ctx-future",
        swing_high=176.0,
        swing_low=170.0,
        micro_high=174.5,
        atr=2.0,
        source="unittest",
    )
    recent_created_but_expired = PullbackSetupCandidate(
        symbol="000660",
        strategy_tag="pullback_rebreakout",
        created_at=_future_kst(0),
        expires_at=_kst_dt(9, 0),
        context_version="ctx-expired",
        swing_high=90.0,
        swing_low=85.0,
        micro_high=88.5,
        atr=1.0,
        source="unittest",
    )
    store.upsert(stale_created_but_future_expiry)
    store.upsert(recent_created_but_expired)

    removed = store.cleanup_expired(now=_kst_dt(10, 0))
    assert removed == 1
    assert store.get("005930") is not None
    assert store.get("000660") is None


def test_mixed_strategy_tiebreak_metric_is_preserved_outside_degraded_rejects():
    queue = EntryIntentQueue(maxsize=8, authoritative=True, max_pending_per_symbol=0)
    first = _make_authoritative_intent(strategy_tag="pullback_rebreakout", symbol="005930", created_at=_future_kst(0))
    second = _make_authoritative_intent(strategy_tag="trend_atr", symbol="005930", created_at=_future_kst(1))

    assert queue.put_if_absent(first) is True
    assert queue.put_if_absent(second) is True
    assert queue.mixed_strategy_tiebreak_count() == 1


def test_pipeline_health_monitor_does_not_call_market_regime_sync_refresh():
    queue = EntryIntentQueue(maxsize=8, authoritative=True)
    dirty = DirtySymbolSet()
    health_store = WorkerHealthStore()
    controller = DegradedModeController(
        enabled=True,
        enter_queue_depth=4,
        exit_queue_depth=2,
        min_hold_sec=15.0,
    )

    executor = SimpleNamespace(
        _pipeline_worker_heartbeat_sec=5.0,
        _candidate_cleanup_interval_sec=30.0,
        _authoritative_intent_queue_depth_by_strategy={},
        refresh_shared_market_regime_snapshot=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("sync refresh forbidden")),
    )
    monitor = PipelineHealthMonitorThread(
        executor=executor,
        health_store=health_store,
        degraded_controller=controller,
        entry_queue=queue,
        dirty_symbols=dirty,
        candidate_cleanup=lambda _now: {},
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )

    monitor._run_cycle()
    assert getattr(executor, "_degraded_mode_current", False) is False
