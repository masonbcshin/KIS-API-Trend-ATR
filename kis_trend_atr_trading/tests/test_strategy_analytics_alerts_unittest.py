from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from kis_trend_atr_trading.analytics.alerts import build_alert_rows
from kis_trend_atr_trading.analytics.diagnostics import render_diagnostics_text
from kis_trend_atr_trading.analytics.event_logger import StrategyAnalyticsEventLogger
from kis_trend_atr_trading.analytics.materializer import StrategyAnalyticsMaterializer


def _alert_types(rows, strategy_tag: str) -> set[str]:
    return {
        str(row.get("alert_type") or "")
        for row in list(rows or [])
        if str(row.get("strategy_tag") or "") == strategy_tag
    }


def test_diagnostics_report_generation_is_readable_and_deterministic(tmp_path: Path) -> None:
    logger = StrategyAnalyticsEventLogger(event_dir=str(tmp_path), enabled=True)
    base_ts = datetime.fromisoformat("2026-03-11T09:10:00+09:00")

    logger.log_event(
        event_ts=base_ts,
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="069500",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        regime_state="bull",
        source_component="unit_test",
        payload_json={"intraday_source_state": "fresh", "current_price": 100.0},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=10),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="069500",
        intent_id="orb-1",
        event_type="timing_confirmed",
        stage="timing",
        decision="accepted",
        regime_state="bull",
        source_component="unit_test",
        payload_json={"intraday_source_state": "fresh"},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=11),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="069500",
        intent_id="orb-1",
        event_type="intent_ingressed",
        stage="ingress",
        decision="accepted",
        regime_state="bull",
        source_component="unit_test",
        payload_json={"intraday_source_state": "fresh"},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=12),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="069500",
        intent_id="orb-1",
        broker_order_id="ORD-1",
        event_type="order_submitted",
        stage="order",
        decision="submitted",
        regime_state="bull",
        source_component="unit_test",
        payload_json={"intraday_source_state": "fresh", "side": "BUY"},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=13),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="069500",
        intent_id="orb-1",
        broker_order_id="ORD-1",
        event_type="order_filled",
        stage="order",
        decision="filled",
        regime_state="bull",
        source_component="unit_test",
        payload_json={"intraday_source_state": "fresh", "fill_price": 100.0, "side": "BUY"},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(minutes=3, seconds=5),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="069500",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        regime_state="bull",
        source_component="unit_test",
        payload_json={"intraday_source_state": "fresh", "current_price": 101.0},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(minutes=5, seconds=5),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="069500",
        event_type="candidate_created",
        stage="setup",
        decision="accepted",
        regime_state="bull",
        source_component="unit_test",
        payload_json={"intraday_source_state": "fresh", "current_price": 102.0},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(minutes=6),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="069500",
        event_type="timing_rejected",
        stage="timing",
        decision="rejected",
        reject_reason="orb_intraday_missing",
        regime_state="bull",
        source_component="unit_test",
        payload_json={"intraday_source_state": "missing"},
    )
    logger.close()

    materializer = StrategyAnalyticsMaterializer(
        event_dir=str(tmp_path),
        markout_horizons_sec=[180, 300],
        enable_markouts=True,
    )
    analytics_payload = materializer.materialize_trade_date(trade_date="2026-03-11", persist=False)
    alerts_first = materializer.build_alert_rows(trade_date="2026-03-11", analytics_payload=analytics_payload)
    alerts_second = materializer.build_alert_rows(trade_date="2026-03-11", analytics_payload=analytics_payload)
    report_first = materializer.build_diagnostics_report(
        trade_date="2026-03-11",
        analytics_payload=analytics_payload,
        alert_rows=alerts_first,
    )
    report_second = materializer.build_diagnostics_report(
        trade_date="2026-03-11",
        analytics_payload=analytics_payload,
        alert_rows=alerts_second,
    )
    rendered = render_diagnostics_text(report_first)

    assert alerts_first == alerts_second
    assert report_first == report_second
    assert "[STRATEGY_DIAGNOSTICS]" in rendered
    assert "opening_range_breakout" in rendered
    assert report_first["strategies"][0]["summary"]["avg_markout_3m_bps"] is not None
    assert "avg_3m=" in rendered
    assert "orb_source_quality" in rendered
    assert report_first["strategies"][0]["slice_summary"]["source_state"]


def test_alert_rules_fire_for_fill_rate_reject_spike_degraded_recovery_and_markout() -> None:
    analytics_payload = {
        "summary_rows": [
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "trend_atr",
                "candidate_count": 10,
                "authoritative_ingress_count": 8,
                "precheck_reject_count": 4,
                "native_handoff_reject_count": 0,
                "submitted_count": 4,
                "filled_count": 0,
                "cancelled_count": 0,
                "exit_count": 0,
                "avg_markout_3m_bps": -12.0,
                "avg_markout_5m_bps": -20.0,
                "fill_rate": 0.0,
                "top_reject_reason_json": [{"reject_reason": "existing_position", "count": 4}],
                "degraded_event_count": 3,
                "recovery_duplicate_prevented_count": 0,
            },
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "pipeline_recovery",
                "candidate_count": 0,
                "authoritative_ingress_count": 0,
                "precheck_reject_count": 0,
                "native_handoff_reject_count": 0,
                "submitted_count": 0,
                "filled_count": 0,
                "cancelled_count": 0,
                "exit_count": 0,
                "avg_markout_3m_bps": None,
                "avg_markout_5m_bps": None,
                "fill_rate": 0.0,
                "top_reject_reason_json": [],
                "degraded_event_count": 0,
                "recovery_duplicate_prevented_count": 3,
            },
        ],
        "attribution_rows": [
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "trend_atr",
                "slice_key": "overall",
                "slice_value": "all",
                "reject_stage": "precheck",
                "reject_reason": "existing_position",
                "reason_group": "precheck_existing_position",
                "outcome_class": "reject",
                "count": 4,
            },
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "trend_atr",
                "slice_key": "overall",
                "slice_value": "all",
                "reject_stage": "ingress",
                "reject_reason": "degraded_mode",
                "reason_group": "degraded_rejected",
                "outcome_class": "degraded",
                "count": 3,
            },
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "pipeline_recovery",
                "slice_key": "overall",
                "slice_value": "all",
                "reject_stage": "recovery",
                "reject_reason": "recovery_duplicate_prevented",
                "reason_group": "recovery_duplicate_prevented",
                "outcome_class": "recovery",
                "count": 3,
            },
        ],
    }
    baseline_summary_rows = [
        {"trade_date": "2026-03-10", "strategy_tag": "trend_atr", "fill_rate": 0.75},
        {"trade_date": "2026-03-09", "strategy_tag": "trend_atr", "fill_rate": 0.80},
    ]
    baseline_attribution_rows = [
        {
            "trade_date": "2026-03-10",
            "strategy_tag": "trend_atr",
            "slice_key": "overall",
            "slice_value": "all",
            "reason_group": "precheck_existing_position",
            "count": 1,
        },
        {
            "trade_date": "2026-03-10",
            "strategy_tag": "pipeline_recovery",
            "slice_key": "overall",
            "slice_value": "all",
            "reason_group": "recovery_duplicate_prevented",
            "count": 1,
        },
    ]

    alerts = build_alert_rows(
        "2026-03-11",
        analytics_payload,
        baseline_summary_rows=baseline_summary_rows,
        baseline_attribution_rows=baseline_attribution_rows,
    )

    assert "fill_rate_drop" in _alert_types(alerts, "trend_atr")
    assert "reject_reason_spike" in _alert_types(alerts, "trend_atr")
    assert "degraded_mode_persistent" in _alert_types(alerts, "trend_atr")
    assert "markout_quality_drop" in _alert_types(alerts, "trend_atr")
    assert "recovery_duplicate_prevented_spike" in _alert_types(alerts, "pipeline_recovery")


def test_orb_source_quality_bad_alert_fires() -> None:
    analytics_payload = {
        "summary_rows": [
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "opening_range_breakout",
                "candidate_count": 5,
                "authoritative_ingress_count": 1,
                "precheck_reject_count": 0,
                "native_handoff_reject_count": 0,
                "submitted_count": 1,
                "filled_count": 1,
                "cancelled_count": 0,
                "exit_count": 0,
                "avg_markout_3m_bps": 5.0,
                "avg_markout_5m_bps": 7.0,
                "fill_rate": 1.0,
                "top_reject_reason_json": [],
                "degraded_event_count": 0,
                "recovery_duplicate_prevented_count": 0,
            }
        ],
        "attribution_rows": [
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "opening_range_breakout",
                "slice_key": "overall",
                "slice_value": "all",
                "reject_stage": "timing",
                "reject_reason": "orb_intraday_missing",
                "reason_group": "orb_source_unavailable",
                "outcome_class": "reject",
                "count": 2,
            },
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "opening_range_breakout",
                "slice_key": "overall",
                "slice_value": "all",
                "reject_stage": "timing",
                "reject_reason": "orb_intraday_stale",
                "reason_group": "orb_source_stale",
                "outcome_class": "reject",
                "count": 1,
            },
        ],
    }

    alerts = build_alert_rows("2026-03-11", analytics_payload)
    assert "orb_source_quality_bad" in _alert_types(alerts, "opening_range_breakout")
