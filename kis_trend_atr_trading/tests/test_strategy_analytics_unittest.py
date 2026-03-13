from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kis_trend_atr_trading.analytics.event_logger import (
    StrategyAnalyticsEventLogger,
    analytics_events_from_replay_report,
    inspect_strategy_event_input,
    load_strategy_events,
)
from kis_trend_atr_trading.analytics.materializer import StrategyAnalyticsMaterializer
import kis_trend_atr_trading.engine.multiday_executor as multiday_executor_module
from kis_trend_atr_trading.engine.multiday_executor import MultidayExecutor
from kis_trend_atr_trading.engine.order_synchronizer import OrderExecutionResult
from kis_trend_atr_trading.strategy.multiday_trend_atr import SignalType, TradingSignal, TrendType


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


def _make_analytics_executor(tmp_path: Path) -> MultidayExecutor:
    executor = MultidayExecutor.__new__(MultidayExecutor)
    executor.stock_code = "005930"
    executor._strategy_analytics_logger = StrategyAnalyticsEventLogger(event_dir=str(tmp_path), enabled=True)
    executor._strategy_regime_snapshot_state_used = "fresh"
    executor.market_regime_snapshot = None
    executor._strategy_pipeline_degraded_controller = None
    executor._authoritative_intent_queue_depth = 0
    return executor


def test_direct_live_runtime_helpers_append_authoritative_events(tmp_path: Path) -> None:
    executor = _make_analytics_executor(tmp_path)
    event_ts = datetime.fromisoformat("2026-03-11T09:32:00+09:00")
    signal = TradingSignal(
        signal_type=SignalType.BUY,
        price=70100.0,
        stop_loss=69000.0,
        take_profit=72000.0,
        trailing_stop=69000.0,
        reason="돌파 진입",
        atr=1200.0,
        trend=TrendType.UPTREND,
        meta={
            "strategy_tag": "trend_atr",
            "signal_time": event_ts.isoformat(),
            "decision_time": event_ts.isoformat(),
            "current_price_at_signal": 70100.0,
            "entry_reference_price": 70000.0,
            "entry_reference_label": "prev_high",
        },
    )
    context = SimpleNamespace(
        decision_time=event_ts,
        current_price=70100.0,
        open_price=69500.0,
        has_pending_order=False,
        quote_snapshot={"quote_age_sec": 0.4, "source": "ws_tick"},
    )

    with patch.object(multiday_executor_module.settings, "ENABLE_STRATEGY_ANALYTICS", True), patch.object(
        multiday_executor_module.settings,
        "STRATEGY_ANALYTICS_EVENT_DIR",
        str(tmp_path),
    ):
        executor._log_direct_strategy_signal_analytics(signal=signal, context=context)
        executor._log_direct_strategy_execution_event(
            signal=signal,
            event_type="native_handoff_started",
            stage="handoff",
            decision="started",
            payload_json={"order_style": "protected_limit"},
            event_ts=event_ts + timedelta(seconds=1),
        )
        executor._log_direct_strategy_execution_event(
            signal=signal,
            event_type="order_submitted",
            stage="order",
            decision="submitted",
            broker_order_id="ORD-1",
            payload_json={"side": "BUY", "requested_price": 70100.0},
            event_ts=event_ts + timedelta(seconds=2),
        )
        executor._log_direct_strategy_execution_event(
            signal=signal,
            event_type="order_filled",
            stage="order",
            decision="filled",
            broker_order_id="ORD-1",
            payload_json={"side": "BUY", "fill_price": 70100.0, "exec_qty": 1},
            event_ts=event_ts + timedelta(seconds=3),
        )
        executor._log_direct_strategy_execution_event(
            signal=signal,
            event_type="order_cancelled",
            stage="order",
            decision="cancelled",
            reject_reason="cancelled",
            broker_order_id="ORD-2",
            payload_json={"message": "cancelled"},
            event_ts=event_ts + timedelta(seconds=4),
        )
        executor._log_strategy_analytics_event(
            event_type="recovery_duplicate_prevented",
            stage="recovery",
            decision="blocked",
            strategy_tag="pipeline_recovery",
            symbol="005930",
            event_ts=event_ts + timedelta(seconds=5),
            source_component="executor_recovery",
            payload_json={"duplicate_prevented_count": 1},
        )
        reject_signal = TradingSignal(
            signal_type=SignalType.HOLD,
            price=70100.0,
            reason="ORB intraday missing",
            atr=1200.0,
            trend=TrendType.UPTREND,
            reason_code="orb_intraday_missing",
            meta={
                "strategy_tag": "opening_range_breakout",
                "signal_time": (event_ts + timedelta(minutes=1)).isoformat(),
                "decision_time": (event_ts + timedelta(minutes=1)).isoformat(),
            },
        )
        reject_context = SimpleNamespace(
            decision_time=event_ts + timedelta(minutes=1),
            current_price=70100.0,
            open_price=69500.0,
            has_pending_order=False,
            quote_snapshot={"quote_age_sec": 0.6, "source": "rest_quote"},
        )
        executor._log_direct_strategy_timing_reject(signal=reject_signal, context=reject_context)
        executor._strategy_analytics_logger.close()

    loaded = load_strategy_events(event_dir=str(tmp_path), trade_date="2026-03-11")
    event_types = [str(row.get("event_type") or "") for row in loaded]
    assert "candidate_created" in event_types
    assert "timing_confirmed" in event_types
    assert "intent_ingressed" in event_types
    assert "native_handoff_started" in event_types
    assert "order_submitted" in event_types
    assert "order_filled" in event_types
    assert "order_cancelled" in event_types
    assert "timing_rejected" in event_types
    assert "recovery_duplicate_prevented" in event_types


def test_executor_strategy_analytics_startup_logging_exposes_writer_attachment(tmp_path: Path, caplog) -> None:
    executor = MultidayExecutor.__new__(MultidayExecutor)
    executor.stock_code = "005930"
    executor._strategy_analytics_logger = None

    with patch.object(multiday_executor_module.settings, "ENABLE_STRATEGY_ANALYTICS", True), patch.object(
        multiday_executor_module.settings,
        "STRATEGY_ANALYTICS_EVENT_DIR",
        str(tmp_path),
    ), patch.object(multiday_executor_module.settings, "ENABLE_STRATEGY_MARKOUTS", True):
        with caplog.at_level(logging.INFO):
            executor._log_strategy_analytics_startup_state()

    assert "effective_event_dir=" in caplog.text
    assert "live_writer_initialized=True" in caplog.text
    assert "live_runtime_emit_wiring_active=True" in caplog.text
    assert "markout_enabled=True" in caplog.text


def test_materializer_surfaces_missing_dir_file_and_empty_file_diagnostics(tmp_path: Path, caplog) -> None:
    live_dir = tmp_path / "live"

    with patch("kis_trend_atr_trading.analytics.materializer.settings.ENABLE_STRATEGY_ANALYTICS", True), patch(
        "kis_trend_atr_trading.analytics.materializer.settings.STRATEGY_ANALYTICS_EVENT_DIR",
        str(live_dir),
    ):
        materializer = StrategyAnalyticsMaterializer(event_dir=str(live_dir))
        with caplog.at_level(logging.WARNING):
            missing_dir = materializer.materialize_trade_date(trade_date="2026-03-11", persist=False)
        assert missing_dir["event_input_diagnostics"]["missing_input_state"] == "event_dir_missing"
        assert missing_dir["event_input_diagnostics"]["live_writer_expected"] is True
        assert "writer_never_initialized_or_live_emit_unwired" in caplog.text

        live_dir.mkdir(parents=True, exist_ok=True)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            missing_file = materializer.materialize_trade_date(trade_date="2026-03-11", persist=False)
        assert missing_file["event_input_diagnostics"]["missing_input_state"] == "trade_date_file_missing"
        assert "live_writer_attached_but_no_events_emitted_or_wiring_missing" in caplog.text

        empty_file = live_dir / "strategy_events_2026-03-11.jsonl"
        empty_file.write_text("", encoding="utf-8")
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            empty_payload = materializer.materialize_trade_date(trade_date="2026-03-11", persist=False)
        assert empty_payload["event_input_diagnostics"]["missing_input_state"] == "trade_date_file_empty"
        assert "event_file_created_but_no_events_appended" in caplog.text

    inspected = inspect_strategy_event_input(event_dir=str(live_dir), trade_date="2026-03-11")
    assert inspected["missing_input_state"] == "trade_date_file_empty"
