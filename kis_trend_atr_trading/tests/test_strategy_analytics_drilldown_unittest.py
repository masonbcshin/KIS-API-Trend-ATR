from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import threading

import pandas as pd

import kis_trend_atr_trading.engine.pullback_pipeline_workers as pullback_pipeline_workers_module
from kis_trend_atr_trading.analytics.event_logger import (
    StrategyAnalyticsEventLogger,
    analytics_events_from_replay_report,
)
from kis_trend_atr_trading.analytics.materializer import StrategyAnalyticsMaterializer
from kis_trend_atr_trading.engine.pullback_pipeline_stores import ArmedCandidateStore, EntryIntentQueue
from kis_trend_atr_trading.engine.pullback_pipeline_workers import OrderExecutionWorker
from kis_trend_atr_trading.strategy.multiday_trend_atr import SignalType, TradingSignal, TrendType
from kis_trend_atr_trading.strategy.pullback_rebreakout import PullbackCandidate, PullbackDecision
from kis_trend_atr_trading.utils.market_hours import KST


def _overall_funnel(rows, strategy_tag: str):
    return {
        str(row.get("stage_name") or ""): dict(row)
        for row in list(rows or [])
        if str(row.get("strategy_tag") or "") == strategy_tag
        and str(row.get("slice_key") or "") == "overall"
        and str(row.get("slice_value") or "") == "all"
    }


def _slice_funnel(rows, strategy_tag: str, *, slice_key: str, slice_value: str):
    return {
        str(row.get("stage_name") or ""): dict(row)
        for row in list(rows or [])
        if str(row.get("strategy_tag") or "") == strategy_tag
        and str(row.get("slice_key") or "") == slice_key
        and str(row.get("slice_value") or "") == slice_value
    }


def _overall_attr_count(rows, strategy_tag: str, reason_group: str) -> int:
    return sum(
        int(row.get("count", 0) or 0)
        for row in list(rows or [])
        if str(row.get("strategy_tag") or "") == strategy_tag
        and str(row.get("slice_key") or "") == "overall"
        and str(row.get("slice_value") or "") == "all"
        and str(row.get("reason_group") or "") == reason_group
    )


def _append_events(path: Path, events: list[dict]) -> None:
    logger = StrategyAnalyticsEventLogger(event_dir=str(path), enabled=True)
    for event in events:
        logger.append(event)
    logger.close()


def test_strategy_funnel_drilldown_materializes_stage_counts_and_slices(tmp_path: Path) -> None:
    logger = StrategyAnalyticsEventLogger(event_dir=str(tmp_path), enabled=True)
    base_ts = datetime.fromisoformat("2026-03-11T09:05:00+09:00")
    shared_payload = {"intraday_source_state": "fresh", "entry_reference_label": "opening_range_high"}

    logger.log_event(
        event_ts=base_ts,
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="005930",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        regime_state="bull",
        source_component="unit_test",
        payload_json={**shared_payload, "current_price": 100.0},
    )
    logger.log_event(
        event_ts=base_ts.replace(hour=9, minute=31),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="005930",
        intent_id="orb-1",
        candidate_id="candidate-1",
        event_type="timing_confirmed",
        stage="timing",
        decision="accepted",
        regime_state="bull",
        source_component="unit_test",
        payload_json=shared_payload,
    )
    logger.log_event(
        event_ts=base_ts.replace(hour=9, minute=31, second=1),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="005930",
        intent_id="orb-1",
        candidate_id="candidate-1",
        event_type="intent_ingressed",
        stage="ingress",
        decision="accepted",
        regime_state="bull",
        source_component="unit_test",
        payload_json=shared_payload,
    )
    logger.log_event(
        event_ts=base_ts.replace(hour=9, minute=32),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="005930",
        intent_id="orb-1",
        broker_order_id="ORD-1",
        event_type="order_submitted",
        stage="order",
        decision="submitted",
        regime_state="bull",
        source_component="unit_test",
        payload_json={**shared_payload, "side": "BUY"},
    )
    logger.log_event(
        event_ts=base_ts.replace(hour=9, minute=33),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="005930",
        intent_id="orb-1",
        broker_order_id="ORD-1",
        event_type="order_filled",
        stage="order",
        decision="filled",
        regime_state="bull",
        source_component="unit_test",
        payload_json={**shared_payload, "fill_price": 100.0, "side": "BUY"},
    )
    logger.log_event(
        event_ts=base_ts.replace(hour=14, minute=40),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="005930",
        broker_order_id="ORD-1",
        event_type="exit_decision",
        stage="exit",
        decision="started",
        regime_state="bull",
        source_component="unit_test",
        payload_json={"exit_reason": "manual_exit"},
    )

    second_candidate_ts = base_ts.replace(hour=10, minute=35)
    logger.log_event(
        event_ts=second_candidate_ts,
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="000660",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        regime_state="neutral",
        source_component="unit_test",
        payload_json={**shared_payload, "current_price": 120.0},
    )
    logger.log_event(
        event_ts=second_candidate_ts + timedelta(minutes=1),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="000660",
        intent_id="orb-2",
        candidate_id="candidate-2",
        event_type="timing_confirmed",
        stage="timing",
        decision="accepted",
        regime_state="neutral",
        source_component="unit_test",
        payload_json=shared_payload,
    )
    logger.log_event(
        event_ts=second_candidate_ts + timedelta(minutes=1, seconds=1),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="000660",
        intent_id="orb-2",
        candidate_id="candidate-2",
        event_type="intent_ingressed",
        stage="ingress",
        decision="accepted",
        regime_state="neutral",
        source_component="unit_test",
        payload_json=shared_payload,
    )
    logger.log_event(
        event_ts=second_candidate_ts + timedelta(minutes=2),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="000660",
        intent_id="orb-2",
        event_type="precheck_rejected",
        stage="precheck",
        decision="rejected",
        reject_reason="existing_position",
        regime_state="neutral",
        source_component="unit_test",
        payload_json={"dedupe": True},
    )
    logger.close()

    materializer = StrategyAnalyticsMaterializer(event_dir=str(tmp_path), enable_markouts=True)
    first = materializer.materialize_trade_date(trade_date="2026-03-11", persist=False)
    second = materializer.materialize_trade_date(trade_date="2026-03-11", persist=False)

    assert first["funnel_rows"] == second["funnel_rows"]
    assert first["attribution_rows"] == second["attribution_rows"]

    overall = _overall_funnel(first["funnel_rows"], "opening_range_breakout")
    assert overall["candidate_created"]["stage_count"] == 2
    assert overall["timing_confirmed"]["stage_count"] == 2
    assert overall["authoritative_ingress"]["stage_count"] == 2
    assert overall["precheck_pass"]["stage_count"] == 1
    assert overall["precheck_reject"]["stage_count"] == 1
    assert overall["submitted"]["stage_count"] == 1
    assert overall["filled"]["stage_count"] == 1
    assert overall["exit"]["stage_count"] == 1
    assert round(overall["precheck_pass"]["conversion_rate"], 2) == 0.50
    assert round(overall["precheck_reject"]["conversion_rate"], 2) == 0.50

    opening_slice = _slice_funnel(first["funnel_rows"], "opening_range_breakout", slice_key="session_bucket", slice_value="opening")
    mid_slice = _slice_funnel(first["funnel_rows"], "opening_range_breakout", slice_key="session_bucket", slice_value="mid")
    source_slice = _slice_funnel(first["funnel_rows"], "opening_range_breakout", slice_key="source_state", slice_value="fresh")
    regime_slice = _slice_funnel(first["funnel_rows"], "opening_range_breakout", slice_key="regime_state", slice_value="bull")

    assert opening_slice["candidate_created"]["stage_count"] == 1
    assert mid_slice["candidate_created"]["stage_count"] == 1
    assert source_slice["candidate_created"]["stage_count"] == 2
    assert source_slice["filled"]["stage_count"] == 1
    assert regime_slice["filled"]["stage_count"] == 1


def test_live_and_replay_strategy_summary_keys_are_compatible(tmp_path: Path) -> None:
    live_dir = tmp_path / "live"
    replay_dir = tmp_path / "replay"
    live_logger = StrategyAnalyticsEventLogger(event_dir=str(live_dir), enabled=True)
    base_ts = datetime.fromisoformat("2026-03-11T09:00:00+09:00")

    live_logger.log_event(
        event_ts=base_ts,
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        regime_state="replay",
        source_component="unit_test",
        payload_json={},
    )
    live_logger.log_event(
        event_ts=base_ts + timedelta(seconds=1),
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        intent_id="trend-1",
        event_type="timing_confirmed",
        stage="timing",
        decision="accepted",
        regime_state="replay",
        source_component="unit_test",
        payload_json={},
    )
    live_logger.log_event(
        event_ts=base_ts + timedelta(seconds=2),
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        intent_id="trend-1",
        event_type="intent_ingressed",
        stage="ingress",
        decision="accepted",
        regime_state="replay",
        source_component="unit_test",
        payload_json={},
    )
    live_logger.log_event(
        event_ts=base_ts + timedelta(seconds=3),
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        intent_id="trend-1",
        event_type="precheck_rejected",
        stage="precheck",
        decision="rejected",
        reject_reason="pending_order",
        regime_state="replay",
        source_component="unit_test",
        payload_json={},
    )
    live_logger.close()

    replay_report = {
        "candidate_timeline": [
            {
                "event_ts": "2026-03-11T09:00:00+09:00",
                "strategy_tag": "trend_atr",
                "symbol": "005930",
                "setup_candidate_created": True,
            }
        ],
        "intent_timeline": [
            {
                "event_ts": "2026-03-11T09:00:01+09:00",
                "strategy_tag": "trend_atr",
                "symbol": "005930",
                "timing_confirmed": True,
                "intent_emitted": True,
                "queue_depth": 1,
            }
        ],
        "order_timeline": [
            {
                "event_ts": "2026-03-11T09:00:03+09:00",
                "strategy_tag": "trend_atr",
                "symbol": "005930",
                "order_decision": "precheck_rejected",
                "reject_reason": "pending_order",
                "queue_depth": 0,
            }
        ],
    }
    _append_events(replay_dir, analytics_events_from_replay_report(replay_report))

    live_result = StrategyAnalyticsMaterializer(event_dir=str(live_dir)).materialize_trade_date(
        trade_date="2026-03-11",
        persist=False,
    )
    replay_result = StrategyAnalyticsMaterializer(event_dir=str(replay_dir)).materialize_trade_date(
        trade_date="2026-03-11",
        persist=False,
    )

    live_overall = _overall_funnel(live_result["funnel_rows"], "trend_atr")
    replay_overall = _overall_funnel(replay_result["funnel_rows"], "trend_atr")
    for stage_name in ("candidate_created", "timing_confirmed", "authoritative_ingress", "precheck_reject"):
        assert live_overall[stage_name]["stage_count"] == replay_overall[stage_name]["stage_count"]

    assert _overall_attr_count(live_result["attribution_rows"], "trend_atr", "precheck_pending_order") == 1
    assert _overall_attr_count(replay_result["attribution_rows"], "trend_atr", "precheck_pending_order") == 1


class _AnalyticsOffStrategyStub:
    def __init__(self) -> None:
        self.has_position = False
        self.pullback_strategy = self

    def evaluate(self, **_kwargs):
        return PullbackCandidate(
            decision=PullbackDecision.BUY,
            reason="analytics off path",
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
            reason="analytics_off",
            meta={"strategy_tag": "pullback_rebreakout"},
        )

    def add_indicators(self, df):
        return df


class _AnalyticsOffExecutorStub:
    def __init__(self) -> None:
        self.stock_code = "005930"
        self.strategy = _AnalyticsOffStrategyStub()
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
            "received_at": KST.localize(datetime(2026, 3, 11, 10, 30)),
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

    def execute_buy(self, _signal):
        self.execute_buy_calls += 1
        return {"success": True, "signal_price": 175.2}


def test_runtime_semantics_are_unchanged_when_analytics_is_off() -> None:
    executor = _AnalyticsOffExecutorStub()
    store = ArmedCandidateStore()
    queue = EntryIntentQueue(maxsize=8)
    worker = OrderExecutionWorker(
        executor=executor,
        candidate_store=store,
        entry_queue=queue,
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )
    created_at = datetime.now(KST).replace(second=0, microsecond=0) + timedelta(minutes=30)
    candidate = pullback_pipeline_workers_module.PullbackSetupCandidate(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=created_at - timedelta(minutes=20),
        expires_at=created_at + timedelta(minutes=60),
        context_version="ctx-1",
        swing_high=180.5,
        swing_low=170.0,
        micro_high=174.5,
        atr=2.0,
        source="daily_setup",
    )
    store.upsert(candidate)
    intent = pullback_pipeline_workers_module.PullbackEntryIntent(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=created_at,
        candidate_created_at=candidate.created_at,
        expires_at=candidate.expires_at,
        context_version="ctx-1",
        entry_reference_price=174.5,
        source="fallback_daily",
    )

    worker._process_intent(intent)

    assert executor.execute_buy_calls == 1
