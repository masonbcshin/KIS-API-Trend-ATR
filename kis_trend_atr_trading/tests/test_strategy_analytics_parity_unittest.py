from __future__ import annotations

from kis_trend_atr_trading.analytics.alerts import build_alert_rows
from kis_trend_atr_trading.analytics.parity import build_metric_snapshot, build_parity_rows


def _row_index(rows):
    return {
        (
            str(row.get("strategy_tag") or ""),
            str(row.get("slice_key") or ""),
            str(row.get("slice_value") or ""),
            str(row.get("metric_name") or ""),
        ): dict(row)
        for row in list(rows or [])
    }


def test_parity_summary_uses_shared_keys_and_detects_metric_mismatch() -> None:
    live_payload = {
        "summary_rows": [
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "trend_atr",
                "candidate_count": 5,
                "authoritative_ingress_count": 4,
                "submitted_count": 3,
                "filled_count": 2,
                "precheck_reject_count": 1,
                "native_handoff_reject_count": 0,
                "avg_markout_3m_bps": 12.0,
            }
        ],
        "funnel_rows": [
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "overall", "slice_value": "all", "stage_name": "candidate_created", "stage_count": 5},
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "overall", "slice_value": "all", "stage_name": "authoritative_ingress", "stage_count": 4},
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "overall", "slice_value": "all", "stage_name": "submitted", "stage_count": 3},
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "overall", "slice_value": "all", "stage_name": "filled", "stage_count": 2},
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "overall", "slice_value": "all", "stage_name": "precheck_reject", "stage_count": 1},
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "session_bucket", "slice_value": "opening", "stage_name": "candidate_created", "stage_count": 5},
        ],
        "attribution_rows": [
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "trend_atr",
                "slice_key": "overall",
                "slice_value": "all",
                "reason_group": "tie_break_applied",
                "count": 1,
            }
        ],
    }
    replay_payload = {
        "summary_rows": [
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "trend_atr",
                "candidate_count": 5,
                "authoritative_ingress_count": 4,
                "submitted_count": 3,
                "filled_count": 1,
                "precheck_reject_count": 1,
                "native_handoff_reject_count": 0,
                "avg_markout_3m_bps": 0.0,
            }
        ],
        "funnel_rows": [
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "overall", "slice_value": "all", "stage_name": "candidate_created", "stage_count": 5},
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "overall", "slice_value": "all", "stage_name": "authoritative_ingress", "stage_count": 4},
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "overall", "slice_value": "all", "stage_name": "submitted", "stage_count": 3},
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "overall", "slice_value": "all", "stage_name": "filled", "stage_count": 1},
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "overall", "slice_value": "all", "stage_name": "precheck_reject", "stage_count": 1},
            {"trade_date": "2026-03-11", "strategy_tag": "trend_atr", "slice_key": "session_bucket", "slice_value": "opening", "stage_name": "candidate_created", "stage_count": 5},
        ],
        "attribution_rows": [],
    }

    first = build_parity_rows("2026-03-11", live_payload, replay_payload)
    second = build_parity_rows("2026-03-11", live_payload, replay_payload)
    index = _row_index(first)

    assert first == second
    assert ("trend_atr", "session_bucket", "opening", "candidate_count") in index
    assert index[("trend_atr", "session_bucket", "opening", "candidate_count")]["mismatch_flag"] is False
    assert index[("trend_atr", "overall", "all", "filled_count")]["diff_abs"] == 1.0
    assert index[("trend_atr", "overall", "all", "filled_count")]["mismatch_flag"] is True
    assert index[("trend_atr", "overall", "all", "avg_markout_3m_bps")]["mismatch_flag"] is True


def test_parity_metric_snapshot_tracks_tie_break_count() -> None:
    payload = {
        "summary_rows": [],
        "funnel_rows": [],
        "attribution_rows": [
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "pullback_rebreakout",
                "slice_key": "overall",
                "slice_value": "all",
                "reason_group": "tie_break_applied",
                "count": 2,
            }
        ],
    }

    snapshot = build_metric_snapshot(payload)
    assert snapshot[("pullback_rebreakout", "overall", "all")]["tie_break_count"] == 2.0


def test_live_replay_parity_mismatch_alert_fires() -> None:
    analytics_payload = {
        "summary_rows": [
            {
                "trade_date": "2026-03-11",
                "strategy_tag": "trend_atr",
                "candidate_count": 5,
                "authoritative_ingress_count": 4,
                "precheck_reject_count": 1,
                "native_handoff_reject_count": 0,
                "submitted_count": 3,
                "filled_count": 2,
                "cancelled_count": 0,
                "exit_count": 0,
                "avg_markout_3m_bps": 12.0,
                "avg_markout_5m_bps": 18.0,
                "fill_rate": 0.67,
                "top_reject_reason_json": [],
                "degraded_event_count": 0,
                "recovery_duplicate_prevented_count": 0,
            }
        ],
        "attribution_rows": [],
    }
    parity_rows = [
        {
            "trade_date": "2026-03-11",
            "strategy_tag": "trend_atr",
            "slice_key": "overall",
            "slice_value": "all",
            "metric_name": "filled_count",
            "live_value": 2.0,
            "replay_value": 0.0,
            "diff_abs": 2.0,
            "diff_ratio": 2.0,
            "mismatch_flag": True,
            "mismatch_reason": "count_diff_exceeds_threshold",
        }
    ]

    alerts = build_alert_rows("2026-03-11", analytics_payload, parity_rows=parity_rows)
    assert {
        str(row.get("alert_type") or "")
        for row in list(alerts or [])
        if str(row.get("strategy_tag") or "") == "trend_atr"
    } >= {"live_replay_parity_mismatch"}
