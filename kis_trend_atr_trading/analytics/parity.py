from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from analytics.summary_drilldown import SLICE_KEY_ORDER
    from config import settings
except ImportError:
    from kis_trend_atr_trading.analytics.summary_drilldown import SLICE_KEY_ORDER
    from kis_trend_atr_trading.config import settings


COUNT_METRICS: Tuple[str, ...] = (
    "candidate_count",
    "authoritative_ingress_count",
    "submitted_count",
    "filled_count",
    "precheck_reject_count",
    "native_handoff_reject_count",
    "tie_break_count",
)
MARKOUT_METRICS: Tuple[str, ...] = ("avg_markout_3m_bps",)
PARITY_METRICS: Tuple[str, ...] = COUNT_METRICS + MARKOUT_METRICS


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _resolve_thresholds(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    resolved = {
        "count_abs_threshold": float(getattr(settings, "STRATEGY_PARITY_COUNT_ABS_THRESHOLD", 1.0) or 1.0),
        "ratio_threshold": float(getattr(settings, "STRATEGY_PARITY_RATIO_THRESHOLD", 0.2) or 0.2),
        "markout_bps_threshold": float(getattr(settings, "STRATEGY_PARITY_MARKOUT_BPS_THRESHOLD", 10.0) or 10.0),
    }
    for key, value in dict(overrides or {}).items():
        if value is not None:
            resolved[str(key)] = float(value)
    return resolved


def build_metric_snapshot(payload: Dict[str, Any]) -> Dict[Tuple[str, str, str], Dict[str, Optional[float]]]:
    snapshot: Dict[Tuple[str, str, str], Dict[str, Optional[float]]] = defaultdict(dict)
    stage_metric_map = {
        "candidate_created": "candidate_count",
        "authoritative_ingress": "authoritative_ingress_count",
        "submitted": "submitted_count",
        "filled": "filled_count",
        "precheck_reject": "precheck_reject_count",
        "native_handoff_reject": "native_handoff_reject_count",
    }

    for row in list(payload.get("funnel_rows") or []):
        metric_name = stage_metric_map.get(str(row.get("stage_name") or ""))
        if not metric_name:
            continue
        key = (
            str(row.get("strategy_tag") or ""),
            str(row.get("slice_key") or ""),
            str(row.get("slice_value") or ""),
        )
        snapshot[key][metric_name] = float(row.get("stage_count", 0) or 0.0)

    for row in list(payload.get("attribution_rows") or []):
        if str(row.get("reason_group") or "") != "tie_break_applied":
            continue
        key = (
            str(row.get("strategy_tag") or ""),
            str(row.get("slice_key") or ""),
            str(row.get("slice_value") or ""),
        )
        snapshot[key]["tie_break_count"] = float(snapshot[key].get("tie_break_count", 0.0) or 0.0) + float(
            row.get("count", 0) or 0.0
        )

    for row in list(payload.get("summary_rows") or []):
        key = (str(row.get("strategy_tag") or ""), "overall", "all")
        if "avg_markout_3m_bps" not in snapshot[key]:
            snapshot[key]["avg_markout_3m_bps"] = _safe_float(row.get("avg_markout_3m_bps"))
        for metric_name in (
            "candidate_count",
            "authoritative_ingress_count",
            "submitted_count",
            "filled_count",
            "precheck_reject_count",
            "native_handoff_reject_count",
        ):
            snapshot[key].setdefault(metric_name, _safe_float(row.get(metric_name)))
    return snapshot


def build_parity_rows(
    trade_date: str,
    live_payload: Dict[str, Any],
    replay_payload: Dict[str, Any],
    *,
    thresholds: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    resolved_thresholds = _resolve_thresholds(thresholds)
    live_snapshot = build_metric_snapshot(live_payload)
    replay_snapshot = build_metric_snapshot(replay_payload)
    keys = sorted(
        set(live_snapshot.keys()) | set(replay_snapshot.keys()),
        key=lambda item: (
            str(item[0]),
            int(SLICE_KEY_ORDER.get(str(item[1]), 99)),
            str(item[2]),
        ),
    )

    rows: List[Dict[str, Any]] = []
    for strategy_tag, slice_key, slice_value in keys:
        metrics = {
            metric_name
            for metric_name in PARITY_METRICS
            if metric_name in live_snapshot.get((strategy_tag, slice_key, slice_value), {})
            or metric_name in replay_snapshot.get((strategy_tag, slice_key, slice_value), {})
        }
        for metric_name in sorted(metrics):
            live_value = live_snapshot.get((strategy_tag, slice_key, slice_value), {}).get(metric_name)
            replay_value = replay_snapshot.get((strategy_tag, slice_key, slice_value), {}).get(metric_name)
            mismatch_flag = False
            mismatch_reason = ""
            diff_abs = None
            diff_ratio = None

            if metric_name in MARKOUT_METRICS:
                if live_value is None and replay_value is None:
                    continue
                if live_value is None:
                    mismatch_flag = True
                    mismatch_reason = "live_missing_metric"
                elif replay_value is None:
                    mismatch_flag = True
                    mismatch_reason = "replay_missing_metric"
                else:
                    diff_abs = abs(float(live_value) - float(replay_value))
                    diff_ratio = diff_abs / max(abs(float(replay_value)), 1.0)
                    if diff_abs >= float(resolved_thresholds["markout_bps_threshold"]):
                        mismatch_flag = True
                        mismatch_reason = "markout_diff_exceeds_threshold"
            else:
                live_numeric = float(live_value or 0.0)
                replay_numeric = float(replay_value or 0.0)
                diff_abs = abs(live_numeric - replay_numeric)
                diff_ratio = diff_abs / max(abs(replay_numeric), 1.0)
                if diff_abs >= float(resolved_thresholds["count_abs_threshold"]) and diff_ratio >= float(
                    resolved_thresholds["ratio_threshold"]
                ):
                    mismatch_flag = True
                    mismatch_reason = "count_diff_exceeds_threshold"

            rows.append(
                {
                    "trade_date": trade_date,
                    "strategy_tag": strategy_tag,
                    "slice_key": slice_key,
                    "slice_value": slice_value,
                    "metric_name": metric_name,
                    "live_value": live_value,
                    "replay_value": replay_value,
                    "diff_abs": diff_abs,
                    "diff_ratio": diff_ratio,
                    "mismatch_flag": bool(mismatch_flag),
                    "mismatch_reason": mismatch_reason,
                }
            )

    rows.sort(
        key=lambda item: (
            str(item.get("strategy_tag") or ""),
            int(SLICE_KEY_ORDER.get(str(item.get("slice_key") or ""), 99)),
            str(item.get("slice_value") or ""),
            str(item.get("metric_name") or ""),
        )
    )
    return rows
