from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from kis_trend_atr_trading.analytics.event_logger import (
    StrategyAnalyticsEventLogger,
    analytics_events_from_replay_report,
    load_strategy_events,
)
from kis_trend_atr_trading.analytics.materializer import StrategyAnalyticsMaterializer


def test_strategy_event_append_and_load(tmp_path: Path) -> None:
    logger = StrategyAnalyticsEventLogger(event_dir=str(tmp_path), enabled=True)
    event = logger.log_event(
        event_ts=datetime.fromisoformat("2026-03-11T09:05:00+09:00"),
        trade_date="2026-03-11",
        strategy_tag="pullback_rebreakout",
        symbol="5930",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        source_component="unit_test",
        payload_json={"current_price": 70000.0},
    )
    assert event is not None
    logger.close()

    loaded = load_strategy_events(event_dir=str(tmp_path), trade_date="2026-03-11")
    assert len(loaded) == 1
    assert loaded[0]["event_type"] == "candidate_created"
    assert loaded[0]["symbol"] == "005930"
    assert loaded[0]["event_id"]
    assert loaded[0]["session_bucket"] == "opening"
    assert loaded[0]["source_state"] == "na"


def test_strategy_analytics_disabled_logger_is_noop(tmp_path: Path) -> None:
    logger = StrategyAnalyticsEventLogger(event_dir=str(tmp_path), enabled=False)
    assert (
        logger.log_event(
            event_ts=datetime.fromisoformat("2026-03-11T09:00:00+09:00"),
            trade_date="2026-03-11",
            strategy_tag="trend_atr",
            symbol="005930",
            event_type="candidate_created",
            stage="setup",
            source_component="unit_test",
            payload_json={},
        )
        is None
    )
    assert list(tmp_path.glob("*.jsonl")) == []


def test_materializer_daily_summary_and_reject_reason_are_deterministic(tmp_path: Path) -> None:
    logger = StrategyAnalyticsEventLogger(event_dir=str(tmp_path), enabled=True)
    base_ts = datetime.fromisoformat("2026-03-11T09:00:00+09:00")
    logger.log_event(
        event_ts=base_ts,
        trade_date="2026-03-11",
        strategy_tag="pullback_rebreakout",
        symbol="005930",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        source_component="unit_test",
        payload_json={"current_price": 100.0},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=10),
        trade_date="2026-03-11",
        strategy_tag="pullback_rebreakout",
        symbol="005930",
        event_type="timing_confirmed",
        stage="timing",
        decision="accepted",
        source_component="unit_test",
        payload_json={"current_price": 101.0},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=11),
        trade_date="2026-03-11",
        strategy_tag="pullback_rebreakout",
        symbol="005930",
        event_type="intent_ingressed",
        stage="ingress",
        decision="accepted",
        source_component="unit_test",
        payload_json={"current_price": 101.0},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=12),
        trade_date="2026-03-11",
        strategy_tag="pullback_rebreakout",
        symbol="005930",
        event_type="order_submitted",
        stage="order",
        decision="submitted",
        broker_order_id="A1",
        source_component="unit_test",
        payload_json={"current_price": 101.0, "side": "BUY"},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=15),
        trade_date="2026-03-11",
        strategy_tag="pullback_rebreakout",
        symbol="005930",
        event_type="order_filled",
        stage="order",
        decision="filled",
        broker_order_id="A1",
        source_component="unit_test",
        payload_json={"fill_price": 100.0, "exec_qty": 1, "side": "BUY"},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=40),
        trade_date="2026-03-11",
        strategy_tag="pullback_rebreakout",
        symbol="005930",
        event_type="precheck_rejected",
        stage="precheck",
        decision="rejected",
        reject_reason="existing_position",
        source_component="unit_test",
        payload_json={"current_price": 101.0},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=220),
        trade_date="2026-03-11",
        strategy_tag="pullback_rebreakout",
        symbol="005930",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        source_component="unit_test",
        payload_json={"current_price": 101.0},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=320),
        trade_date="2026-03-11",
        strategy_tag="pullback_rebreakout",
        symbol="005930",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        source_component="unit_test",
        payload_json={"current_price": 102.0},
    )
    logger.close()

    materializer = StrategyAnalyticsMaterializer(event_dir=str(tmp_path), enable_markouts=True)
    first = materializer.materialize_trade_date(trade_date="2026-03-11", persist=False)
    second = materializer.materialize_trade_date(trade_date="2026-03-11", persist=False)
    assert first == second
    summary = first["summary_rows"][0]
    assert summary["candidate_count"] == 3
    assert summary["timing_confirm_count"] == 1
    assert summary["authoritative_ingress_count"] == 1
    assert summary["submitted_count"] == 1
    assert summary["filled_count"] == 1
    assert summary["precheck_reject_count"] == 1
    assert summary["top_reject_reason_json"][0]["reject_reason"] == "existing_position"
    reject_rows = first["reject_rows"]
    assert reject_rows == [
        {
            "trade_date": "2026-03-11",
            "strategy_tag": "pullback_rebreakout",
            "reject_stage": "precheck",
            "reject_reason": "existing_position",
            "count": 1,
        }
    ]


def test_replay_event_schema_compatibility() -> None:
    report = {
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
                "event_ts": "2026-03-11T09:00:02+09:00",
                "strategy_tag": "trend_atr",
                "symbol": "005930",
                "order_decision": "order_would_submit",
                "queue_depth": 0,
            }
        ],
    }
    events = analytics_events_from_replay_report(report)
    assert len(events) >= 4
    required_keys = {
        "event_id",
        "trade_date",
        "event_ts",
        "strategy_tag",
        "symbol",
        "event_type",
        "stage",
        "decision",
        "reject_reason",
        "regime_state",
        "degraded_mode",
        "queue_depth",
        "payload_schema_version",
        "source_component",
        "session_bucket",
        "source_state",
        "tie_break_applied",
        "tie_break_winner_strategy",
        "ingress_reject_reason",
        "recovery_flag",
        "payload_json",
    }
    assert required_keys.issubset(events[0].keys())
