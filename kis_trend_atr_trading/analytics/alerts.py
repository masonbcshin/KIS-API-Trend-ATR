from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from config import settings
except ImportError:
    from kis_trend_atr_trading.config import settings


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _avg(values: Iterable[float]) -> Optional[float]:
    prepared = [float(value) for value in list(values or [])]
    if not prepared:
        return None
    return sum(prepared) / float(len(prepared))


def resolve_alert_thresholds(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    resolved = {
        "baseline_days": float(getattr(settings, "STRATEGY_ALERT_BASELINE_DAYS", 5) or 5),
        "min_submitted": float(getattr(settings, "STRATEGY_ALERT_MIN_SUBMITTED_COUNT", 2) or 2),
        "fill_rate_drop_threshold": float(getattr(settings, "STRATEGY_ALERT_FILL_RATE_DROP_THRESHOLD", 0.25) or 0.25),
        "fill_rate_baseline_ratio": float(getattr(settings, "STRATEGY_ALERT_FILL_RATE_BASELINE_RATIO", 0.6) or 0.6),
        "reject_spike_threshold": float(getattr(settings, "STRATEGY_ALERT_REJECT_SPIKE_THRESHOLD", 3) or 3),
        "reject_spike_baseline_ratio": float(
            getattr(settings, "STRATEGY_ALERT_REJECT_SPIKE_BASELINE_RATIO", 2.0) or 2.0
        ),
        "degraded_ratio_threshold": float(
            getattr(settings, "STRATEGY_ALERT_DEGRADED_RATIO_THRESHOLD", 0.25) or 0.25
        ),
        "recovery_spike_threshold": float(
            getattr(settings, "STRATEGY_ALERT_RECOVERY_SPIKE_THRESHOLD", 2) or 2
        ),
        "orb_bad_source_ratio_threshold": float(
            getattr(settings, "STRATEGY_ALERT_ORB_BAD_SOURCE_RATIO_THRESHOLD", 0.4) or 0.4
        ),
        "markout_3m_drop_bps": float(getattr(settings, "STRATEGY_ALERT_MARKOUT_3M_DROP_BPS", -10.0) or -10.0),
        "markout_5m_drop_bps": float(getattr(settings, "STRATEGY_ALERT_MARKOUT_5M_DROP_BPS", -15.0) or -15.0),
    }
    for key, value in dict(overrides or {}).items():
        if value is not None:
            resolved[str(key)] = float(value)
    return resolved


def _current_reason_group_counts(analytics_payload: Dict[str, Any], strategy_tag: str) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for row in list(analytics_payload.get("attribution_rows") or []):
        if str(row.get("strategy_tag") or "") != strategy_tag:
            continue
        if str(row.get("slice_key") or "") != "overall" or str(row.get("slice_value") or "") != "all":
            continue
        counts[str(row.get("reason_group") or "")] += int(row.get("count", 0) or 0)
    return counts


def _baseline_summary_map(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in list(rows or []):
        grouped[str(row.get("strategy_tag") or "")].append(dict(row))
    return grouped


def _baseline_reason_group_map(rows: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str], List[int]]:
    grouped: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    for row in list(rows or []):
        if str(row.get("slice_key") or "") != "overall" or str(row.get("slice_value") or "") != "all":
            continue
        grouped[(str(row.get("strategy_tag") or ""), str(row.get("reason_group") or ""))].append(
            int(row.get("count", 0) or 0)
        )
    return grouped


def _make_alert_row(
    *,
    trade_date: str,
    strategy_tag: str,
    alert_type: str,
    alert_key: str,
    severity: str,
    message: str,
    metric_name: str,
    metric_value: Optional[float],
    baseline_value: Optional[float] = None,
    threshold_value: Optional[float] = None,
    slice_key: str = "overall",
    slice_value: str = "all",
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload_json = dict(payload or {})
    return {
        "trade_date": trade_date,
        "strategy_tag": strategy_tag,
        "alert_type": alert_type,
        "alert_key": alert_key,
        "severity": severity,
        "slice_key": slice_key,
        "slice_value": slice_value,
        "metric_name": metric_name,
        "metric_value": metric_value,
        "baseline_value": baseline_value,
        "threshold_value": threshold_value,
        "message": message,
        "payload_json": payload_json,
    }


def render_alerts_text(alert_rows: Iterable[Dict[str, Any]]) -> str:
    rows = list(alert_rows or [])
    if not rows:
        return "[STRATEGY_ALERTS] no alerts"
    lines = [f"[STRATEGY_ALERTS] count={len(rows)}"]
    for row in rows:
        lines.append(
            f"- {row['severity']} {row['strategy_tag']} {row['alert_type']}[{row['alert_key']}] "
            f"{row['message']}"
        )
    return "\n".join(lines)


def build_alert_rows(
    trade_date: str,
    analytics_payload: Dict[str, Any],
    *,
    baseline_summary_rows: Optional[Iterable[Dict[str, Any]]] = None,
    baseline_attribution_rows: Optional[Iterable[Dict[str, Any]]] = None,
    parity_rows: Optional[Iterable[Dict[str, Any]]] = None,
    thresholds: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    resolved = resolve_alert_thresholds(thresholds)
    summary_rows = sorted(list(analytics_payload.get("summary_rows") or []), key=lambda item: str(item.get("strategy_tag") or ""))
    summary_baseline = _baseline_summary_map(baseline_summary_rows or [])
    reason_baseline = _baseline_reason_group_map(baseline_attribution_rows or [])
    alerts: List[Dict[str, Any]] = []

    parity_by_strategy: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in list(parity_rows or []):
        if bool(row.get("mismatch_flag")):
            parity_by_strategy[str(row.get("strategy_tag") or "")].append(dict(row))

    for summary in summary_rows:
        strategy_tag = str(summary.get("strategy_tag") or "")
        if not strategy_tag:
            continue
        current_fill_rate = float(summary.get("fill_rate", 0.0) or 0.0)
        submitted_count = int(summary.get("submitted_count", 0) or 0)
        filled_count = int(summary.get("filled_count", 0) or 0)
        candidate_count = int(summary.get("candidate_count", 0) or 0)
        baseline_fill_rate = _avg(
            [
                _safe_float(item.get("fill_rate"))
                for item in summary_baseline.get(strategy_tag, [])
                if _safe_float(item.get("fill_rate")) is not None
            ]
        )

        fill_rate_dropped = submitted_count >= int(resolved["min_submitted"]) and current_fill_rate <= float(
            resolved["fill_rate_drop_threshold"]
        )
        baseline_fill_dropped = baseline_fill_rate is not None and current_fill_rate < (
            float(baseline_fill_rate) * float(resolved["fill_rate_baseline_ratio"])
        )
        if fill_rate_dropped and (baseline_fill_rate is None or baseline_fill_dropped):
            alerts.append(
                _make_alert_row(
                    trade_date=trade_date,
                    strategy_tag=strategy_tag,
                    alert_type="fill_rate_drop",
                    alert_key="overall",
                    severity="critical" if submitted_count >= 3 and filled_count == 0 else "warn",
                    message=f"fill_rate={current_fill_rate:.2f} submitted={submitted_count} filled={filled_count}",
                    metric_name="fill_rate",
                    metric_value=current_fill_rate,
                    baseline_value=baseline_fill_rate,
                    threshold_value=float(resolved["fill_rate_drop_threshold"]),
                    payload={
                        "submitted_count": submitted_count,
                        "filled_count": filled_count,
                    },
                )
            )

        current_reason_counts = _current_reason_group_counts(analytics_payload, strategy_tag)
        for reason_group, count in sorted(current_reason_counts.items()):
            if reason_group in {"tie_break_applied"}:
                continue
            baseline_reason_avg = _avg(reason_baseline.get((strategy_tag, reason_group), []))
            spike = int(count) >= int(resolved["reject_spike_threshold"])
            baseline_spike = baseline_reason_avg is not None and int(count) >= max(
                int(resolved["reject_spike_threshold"]),
                int(float(baseline_reason_avg) * float(resolved["reject_spike_baseline_ratio"])),
            )
            if spike and (baseline_reason_avg is None or baseline_spike):
                alerts.append(
                    _make_alert_row(
                        trade_date=trade_date,
                        strategy_tag=strategy_tag,
                        alert_type="reject_reason_spike",
                        alert_key=reason_group,
                        severity="critical" if int(count) >= int(resolved["reject_spike_threshold"] * 2.0) else "warn",
                        message=f"{reason_group} count={count}",
                        metric_name="reason_group_count",
                        metric_value=float(count),
                        baseline_value=baseline_reason_avg,
                        threshold_value=float(resolved["reject_spike_threshold"]),
                        payload={"reason_group": reason_group},
                    )
                )

        degraded_count = int(summary.get("degraded_event_count", 0) or 0) + int(
            current_reason_counts.get("degraded_rejected", 0) or 0
        )
        degraded_denominator = max(candidate_count, submitted_count, 1)
        degraded_ratio = float(degraded_count) / float(degraded_denominator)
        if degraded_count > 0 and degraded_ratio >= float(resolved["degraded_ratio_threshold"]):
            alerts.append(
                _make_alert_row(
                    trade_date=trade_date,
                    strategy_tag=strategy_tag,
                    alert_type="degraded_mode_persistent",
                    alert_key="overall",
                    severity="critical" if degraded_ratio >= float(resolved["degraded_ratio_threshold"]) * 2.0 else "warn",
                    message=f"degraded_count={degraded_count} ratio={degraded_ratio:.2f}",
                    metric_name="degraded_ratio",
                    metric_value=degraded_ratio,
                    threshold_value=float(resolved["degraded_ratio_threshold"]),
                    payload={"degraded_count": degraded_count, "denominator": degraded_denominator},
                )
            )

        recovery_count = int(current_reason_counts.get("recovery_duplicate_prevented", 0) or 0) + int(
            summary.get("recovery_duplicate_prevented_count", 0) or 0
        )
        if recovery_count >= int(resolved["recovery_spike_threshold"]):
            alerts.append(
                _make_alert_row(
                    trade_date=trade_date,
                    strategy_tag=strategy_tag,
                    alert_type="recovery_duplicate_prevented_spike",
                    alert_key="overall",
                    severity="warn" if recovery_count < int(resolved["recovery_spike_threshold"] * 2.0) else "critical",
                    message=f"recovery_duplicate_prevented={recovery_count}",
                    metric_name="recovery_duplicate_prevented_count",
                    metric_value=float(recovery_count),
                    threshold_value=float(resolved["recovery_spike_threshold"]),
                    payload={"reason_group": "recovery_duplicate_prevented"},
                )
            )

        if strategy_tag == "opening_range_breakout":
            bad_source_count = sum(
                int(current_reason_counts.get(reason_group, 0) or 0)
                for reason_group in ("orb_source_unavailable", "orb_source_stale", "orb_source_insufficient")
            )
            bad_source_ratio = float(bad_source_count) / float(max(candidate_count, 1))
            if bad_source_count > 0 and bad_source_ratio >= float(resolved["orb_bad_source_ratio_threshold"]):
                alerts.append(
                    _make_alert_row(
                        trade_date=trade_date,
                        strategy_tag=strategy_tag,
                        alert_type="orb_source_quality_bad",
                        alert_key="overall",
                        severity="critical" if bad_source_ratio >= float(resolved["orb_bad_source_ratio_threshold"]) * 1.5 else "warn",
                        message=f"orb_bad_source_ratio={bad_source_ratio:.2f} count={bad_source_count}",
                        metric_name="orb_bad_source_ratio",
                        metric_value=bad_source_ratio,
                        threshold_value=float(resolved["orb_bad_source_ratio_threshold"]),
                        payload={"bad_source_count": bad_source_count, "candidate_count": candidate_count},
                    )
                )

        avg_markout_3m = _safe_float(summary.get("avg_markout_3m_bps"))
        if avg_markout_3m is not None and avg_markout_3m <= float(resolved["markout_3m_drop_bps"]):
            alerts.append(
                _make_alert_row(
                    trade_date=trade_date,
                    strategy_tag=strategy_tag,
                    alert_type="markout_quality_drop",
                    alert_key="avg_markout_3m_bps",
                    severity="warn" if avg_markout_3m > float(resolved["markout_3m_drop_bps"]) * 2.0 else "critical",
                    message=f"avg_markout_3m_bps={avg_markout_3m:.2f}",
                    metric_name="avg_markout_3m_bps",
                    metric_value=avg_markout_3m,
                    threshold_value=float(resolved["markout_3m_drop_bps"]),
                )
            )

        avg_markout_5m = _safe_float(summary.get("avg_markout_5m_bps"))
        if avg_markout_5m is not None and avg_markout_5m <= float(resolved["markout_5m_drop_bps"]):
            alerts.append(
                _make_alert_row(
                    trade_date=trade_date,
                    strategy_tag=strategy_tag,
                    alert_type="markout_quality_drop",
                    alert_key="avg_markout_5m_bps",
                    severity="warn" if avg_markout_5m > float(resolved["markout_5m_drop_bps"]) * 2.0 else "critical",
                    message=f"avg_markout_5m_bps={avg_markout_5m:.2f}",
                    metric_name="avg_markout_5m_bps",
                    metric_value=avg_markout_5m,
                    threshold_value=float(resolved["markout_5m_drop_bps"]),
                )
            )

        mismatches = list(parity_by_strategy.get(strategy_tag, []))
        if mismatches:
            overall_count_mismatches = [
                row
                for row in mismatches
                if str(row.get("slice_key") or "") == "overall"
                and str(row.get("slice_value") or "") == "all"
                and str(row.get("metric_name") or "") in {
                    "candidate_count",
                    "authoritative_ingress_count",
                    "submitted_count",
                    "filled_count",
                    "precheck_reject_count",
                    "native_handoff_reject_count",
                    "tie_break_count",
                }
            ]
            alerts.append(
                _make_alert_row(
                    trade_date=trade_date,
                    strategy_tag=strategy_tag,
                    alert_type="live_replay_parity_mismatch",
                    alert_key="overall",
                    severity="critical" if overall_count_mismatches else "warn",
                    message=f"parity mismatches={len(mismatches)}",
                    metric_name="parity_mismatch_count",
                    metric_value=float(len(mismatches)),
                    payload={"mismatches": mismatches[:5]},
                )
            )

    alerts.sort(
        key=lambda item: (
            str(item.get("strategy_tag") or ""),
            str(item.get("alert_type") or ""),
            str(item.get("alert_key") or ""),
            str(item.get("metric_name") or ""),
        )
    )
    return alerts
