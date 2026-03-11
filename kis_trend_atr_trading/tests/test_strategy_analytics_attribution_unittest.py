from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from kis_trend_atr_trading.analytics.event_logger import StrategyAnalyticsEventLogger
from kis_trend_atr_trading.analytics.materializer import StrategyAnalyticsMaterializer


def _attr_rows(result, strategy_tag: str, *, slice_key: str = "overall", slice_value: str = "all"):
    return [
        dict(row)
        for row in list(result.get("attribution_rows", []))
        if str(row.get("strategy_tag") or "") == strategy_tag
        and str(row.get("slice_key") or "") == slice_key
        and str(row.get("slice_value") or "") == slice_value
    ]


def _attr_count(result, strategy_tag: str, reason_group: str, *, slice_key: str = "overall", slice_value: str = "all") -> int:
    return sum(
        int(row.get("count", 0) or 0)
        for row in _attr_rows(result, strategy_tag, slice_key=slice_key, slice_value=slice_value)
        if str(row.get("reason_group") or "") == reason_group
    )


def test_reject_reason_attribution_groups_core_taxonomy(tmp_path: Path) -> None:
    logger = StrategyAnalyticsEventLogger(event_dir=str(tmp_path), enabled=True)
    base_ts = datetime.fromisoformat("2026-03-11T09:00:00+09:00")

    logger.log_event(
        event_ts=base_ts,
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        event_type="timing_rejected",
        stage="timing",
        decision="rejected",
        reject_reason="timing_window_closed",
        source_component="unit_test",
        payload_json={},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=1),
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        intent_id="trend-1",
        event_type="precheck_rejected",
        stage="precheck",
        decision="rejected",
        reject_reason="existing_position",
        source_component="unit_test",
        payload_json={},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=2),
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        intent_id="trend-2",
        event_type="precheck_rejected",
        stage="precheck",
        decision="rejected",
        reject_reason="pending_order",
        source_component="unit_test",
        payload_json={},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=3),
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        intent_id="trend-3",
        event_type="precheck_rejected",
        stage="precheck",
        decision="rejected",
        reject_reason="existing_holding",
        source_component="unit_test",
        payload_json={},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=4),
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        intent_id="trend-4",
        event_type="intent_ingressed",
        stage="ingress",
        decision="rejected",
        reject_reason="duplicate",
        source_component="unit_test",
        payload_json={},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=5),
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        intent_id="trend-5",
        event_type="native_handoff_rejected",
        stage="handoff",
        decision="rejected",
        reject_reason="stale_quote",
        source_component="unit_test",
        payload_json={},
    )
    logger.close()

    result = StrategyAnalyticsMaterializer(event_dir=str(tmp_path)).materialize_trade_date(
        trade_date="2026-03-11",
        persist=False,
    )

    assert _attr_count(result, "trend_atr", "timing_rejected") == 1
    assert _attr_count(result, "trend_atr", "precheck_existing_position") == 1
    assert _attr_count(result, "trend_atr", "precheck_pending_order") == 1
    assert _attr_count(result, "trend_atr", "precheck_risk_or_holdings") == 1
    assert _attr_count(result, "trend_atr", "duplicate_blocked") == 1
    assert _attr_count(result, "trend_atr", "native_handoff_rejected") == 1


def test_degraded_recovery_and_tiebreak_diagnostics_are_attributed(tmp_path: Path) -> None:
    logger = StrategyAnalyticsEventLogger(event_dir=str(tmp_path), enabled=True)
    shared_ts = datetime.fromisoformat("2026-03-11T09:30:00+09:00")

    logger.log_event(
        event_ts=shared_ts,
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        intent_id="trend-1",
        event_type="intent_ingressed",
        stage="ingress",
        decision="accepted",
        source_component="unit_test",
        payload_json={},
    )
    logger.log_event(
        event_ts=shared_ts,
        trade_date="2026-03-11",
        strategy_tag="pullback_rebreakout",
        symbol="005930",
        intent_id="pullback-1",
        event_type="intent_ingressed",
        stage="ingress",
        decision="accepted",
        source_component="unit_test",
        payload_json={},
    )
    logger.log_event(
        event_ts=shared_ts + timedelta(seconds=1),
        trade_date="2026-03-11",
        strategy_tag="trend_atr",
        symbol="005930",
        intent_id="trend-1",
        event_type="precheck_rejected",
        stage="precheck",
        decision="rejected",
        reject_reason="existing_position",
        source_component="unit_test",
        payload_json={},
    )
    logger.log_event(
        event_ts=shared_ts + timedelta(seconds=2),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="000660",
        intent_id="orb-1",
        event_type="intent_ingressed",
        stage="ingress",
        decision="rejected",
        reject_reason="degraded_mode",
        degraded_mode=True,
        source_component="unit_test",
        payload_json={},
    )
    logger.log_event(
        event_ts=shared_ts + timedelta(seconds=3),
        trade_date="2026-03-11",
        strategy_tag="pipeline_recovery",
        symbol="005930",
        event_type="recovery_duplicate_prevented",
        stage="recovery",
        decision="blocked",
        source_component="unit_test",
        payload_json={"duplicate_prevented_count": 3},
    )
    logger.close()

    result = StrategyAnalyticsMaterializer(event_dir=str(tmp_path)).materialize_trade_date(
        trade_date="2026-03-11",
        persist=False,
    )

    assert _attr_count(result, "pullback_rebreakout", "tie_break_applied") == 1
    assert _attr_count(result, "trend_atr", "tie_break_loser") == 1
    assert _attr_count(result, "opening_range_breakout", "degraded_rejected") == 1
    assert _attr_count(result, "pipeline_recovery", "recovery_duplicate_prevented") == 3


def test_orb_source_state_rejects_are_grouped_by_quality(tmp_path: Path) -> None:
    logger = StrategyAnalyticsEventLogger(event_dir=str(tmp_path), enabled=True)
    base_ts = datetime.fromisoformat("2026-03-11T09:15:00+09:00")

    logger.log_event(
        event_ts=base_ts,
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="005930",
        event_type="timing_rejected",
        stage="timing",
        decision="rejected",
        reject_reason="orb_intraday_missing",
        source_component="unit_test",
        payload_json={"intraday_source_state": "missing"},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=1),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="005930",
        event_type="timing_rejected",
        stage="timing",
        decision="rejected",
        reject_reason="orb_intraday_stale",
        source_component="unit_test",
        payload_json={"intraday_source_state": "stale"},
    )
    logger.log_event(
        event_ts=base_ts + timedelta(seconds=2),
        trade_date="2026-03-11",
        strategy_tag="opening_range_breakout",
        symbol="005930",
        event_type="native_handoff_rejected",
        stage="handoff",
        decision="rejected",
        reject_reason="orb_intraday_insufficient",
        source_component="unit_test",
        payload_json={"intraday_source_state": "insufficient"},
    )
    logger.close()

    result = StrategyAnalyticsMaterializer(event_dir=str(tmp_path)).materialize_trade_date(
        trade_date="2026-03-11",
        persist=False,
    )

    assert _attr_count(result, "opening_range_breakout", "orb_source_unavailable") == 1
    assert _attr_count(result, "opening_range_breakout", "orb_source_stale") == 1
    assert _attr_count(result, "opening_range_breakout", "orb_source_insufficient") == 1
    assert _attr_count(
        result,
        "opening_range_breakout",
        "orb_source_unavailable",
        slice_key="source_state",
        slice_value="missing",
    ) == 1
    assert _attr_count(
        result,
        "opening_range_breakout",
        "orb_source_stale",
        slice_key="source_state",
        slice_value="stale",
    ) == 1
    assert _attr_count(
        result,
        "opening_range_breakout",
        "orb_source_insufficient",
        slice_key="source_state",
        slice_value="insufficient",
    ) == 1
