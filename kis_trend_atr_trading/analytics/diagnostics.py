from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from .summary_drilldown import FUNNEL_STAGES, SLICE_KEY_ORDER


def _funnel_index(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Dict[str, Any]]]]:
    index: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
    for row in list(rows or []):
        strategy_tag = str(row.get("strategy_tag") or "")
        slice_key = str(row.get("slice_key") or "")
        slice_value = str(row.get("slice_value") or "")
        stage_name = str(row.get("stage_name") or "")
        index.setdefault(strategy_tag, {}).setdefault(slice_key, {}).setdefault(slice_value, {})[stage_name] = dict(row)
    return index


def _overall_reason_group_counts(rows: Iterable[Dict[str, Any]], strategy_tag: str) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = defaultdict(int)
    for row in list(rows or []):
        if str(row.get("strategy_tag") or "") != strategy_tag:
            continue
        if str(row.get("slice_key") or "") != "overall" or str(row.get("slice_value") or "") != "all":
            continue
        counts[str(row.get("reason_group") or "")] += int(row.get("count", 0) or 0)
    return [
        {"reason_group": reason_group, "count": int(count)}
        for reason_group, count in sorted(counts.items(), key=lambda item: (-int(item[1]), str(item[0])))
    ]


def _diagnostic_counts(reason_group_counts: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    wanted = {
        "tie_break_applied": 0,
        "tie_break_loser": 0,
        "degraded_rejected": 0,
        "recovery_duplicate_prevented": 0,
        "authoritative_queue_rejected": 0,
    }
    for row in list(reason_group_counts or []):
        reason_group = str(row.get("reason_group") or "")
        if reason_group in wanted:
            wanted[reason_group] = int(row.get("count", 0) or 0)
    return wanted


def _slice_summary(strategy_funnel: Dict[str, Dict[str, Dict[str, Any]]], slice_key: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for slice_value, stages in sorted((strategy_funnel.get(slice_key) or {}).items()):
        candidate_count = int((stages.get("candidate_created") or {}).get("stage_count", 0) or 0)
        ingress_count = int((stages.get("authoritative_ingress") or {}).get("stage_count", 0) or 0)
        submitted_count = int((stages.get("submitted") or {}).get("stage_count", 0) or 0)
        filled_count = int((stages.get("filled") or {}).get("stage_count", 0) or 0)
        if not any((candidate_count, ingress_count, submitted_count, filled_count)):
            continue
        rows.append(
            {
                "slice_value": slice_value,
                "candidate_count": candidate_count,
                "authoritative_ingress_count": ingress_count,
                "submitted_count": submitted_count,
                "filled_count": filled_count,
            }
        )
    return rows


def _orb_source_quality(strategy_funnel: Dict[str, Dict[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for source_state, stages in sorted((strategy_funnel.get("source_state") or {}).items()):
        candidate_count = int((stages.get("candidate_created") or {}).get("stage_count", 0) or 0)
        filled_count = int((stages.get("filled") or {}).get("stage_count", 0) or 0)
        rejected_count = int((stages.get("precheck_reject") or {}).get("stage_count", 0) or 0) + int(
            (stages.get("native_handoff_reject") or {}).get("stage_count", 0) or 0
        )
        if not any((candidate_count, filled_count, rejected_count)):
            continue
        rows.append(
            {
                "source_state": source_state,
                "candidate_count": candidate_count,
                "filled_count": filled_count,
                "rejected_count": rejected_count,
            }
        )
    return rows


def build_diagnostics_report(
    *,
    trade_date: str,
    analytics_payload: Dict[str, Any],
    alert_rows: Optional[Iterable[Dict[str, Any]]] = None,
    parity_rows: Optional[Iterable[Dict[str, Any]]] = None,
    top_n: int = 5,
) -> Dict[str, Any]:
    funnel_index = _funnel_index(analytics_payload.get("funnel_rows") or [])
    alert_by_strategy: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    parity_by_strategy: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in list(alert_rows or []):
        alert_by_strategy[str(row.get("strategy_tag") or "")].append(dict(row))
    for row in list(parity_rows or []):
        if bool(row.get("mismatch_flag")):
            parity_by_strategy[str(row.get("strategy_tag") or "")].append(dict(row))

    strategies: List[Dict[str, Any]] = []
    for summary in sorted(list(analytics_payload.get("summary_rows") or []), key=lambda item: str(item.get("strategy_tag") or "")):
        strategy_tag = str(summary.get("strategy_tag") or "")
        strategy_funnel = funnel_index.get(strategy_tag, {})
        overall = ((strategy_funnel.get("overall") or {}).get("all") or {})
        reason_group_counts = _overall_reason_group_counts(analytics_payload.get("attribution_rows") or [], strategy_tag)
        strategies.append(
            {
                "strategy_tag": strategy_tag,
                "summary": dict(summary),
                "funnel": [
                    {
                        "stage_name": stage_name,
                        "stage_count": int((overall.get(stage_name) or {}).get("stage_count", 0) or 0),
                        "conversion_rate": float((overall.get(stage_name) or {}).get("conversion_rate", 0.0) or 0.0),
                    }
                    for stage_name in FUNNEL_STAGES
                ],
                "top_reject_reasons": list((summary.get("top_reject_reason_json") or [])[:top_n]),
                "top_reason_groups": reason_group_counts[:top_n],
                "diagnostics": _diagnostic_counts(reason_group_counts),
                "orb_source_quality": _orb_source_quality(strategy_funnel) if strategy_tag == "opening_range_breakout" else [],
                "slice_summary": {
                    slice_key: _slice_summary(strategy_funnel, slice_key)
                    for slice_key in sorted(
                        ("regime_state", "session_bucket", "source_state", "degraded_mode"),
                        key=lambda item: int(SLICE_KEY_ORDER.get(item, 99)),
                    )
                },
                "alerts": sorted(alert_by_strategy.get(strategy_tag, []), key=lambda item: (str(item.get("alert_type") or ""), str(item.get("alert_key") or ""))),
                "parity_mismatches": sorted(
                    parity_by_strategy.get(strategy_tag, []),
                    key=lambda item: (
                        int(SLICE_KEY_ORDER.get(str(item.get("slice_key") or ""), 99)),
                        str(item.get("slice_value") or ""),
                        str(item.get("metric_name") or ""),
                    ),
                )[:top_n],
            }
        )

    return {
        "trade_date": trade_date,
        "event_count": int(analytics_payload.get("event_count", 0) or 0),
        "strategy_count": len(strategies),
        "strategies": strategies,
    }


def render_diagnostics_text(report: Dict[str, Any]) -> str:
    lines = [
        f"[STRATEGY_DIAGNOSTICS] trade_date={report.get('trade_date')} events={report.get('event_count')} strategies={report.get('strategy_count')}"
    ]
    for strategy in list(report.get("strategies") or []):
        summary = dict(strategy.get("summary") or {})
        funnel = {str(item.get("stage_name") or ""): dict(item) for item in list(strategy.get("funnel") or [])}
        lines.append(f"- {strategy['strategy_tag']}")
        lines.append(
            f"  summary candidates={summary.get('candidate_count')} ingress={summary.get('authoritative_ingress_count')} "
            f"submitted={summary.get('submitted_count')} filled={summary.get('filled_count')} "
            f"fill_rate={float(summary.get('fill_rate', 0.0) or 0.0):.2f} "
            f"avg_3m={summary.get('avg_markout_3m_bps')} avg_5m={summary.get('avg_markout_5m_bps')}"
        )
        lines.append(
            "  funnel "
            + " ".join(
                [
                    f"{stage_name}={int((funnel.get(stage_name) or {}).get('stage_count', 0) or 0)}"
                    for stage_name in ("candidate_created", "timing_confirmed", "authoritative_ingress", "submitted", "filled", "exit")
                ]
            )
        )
        if strategy.get("top_reject_reasons"):
            lines.append(f"  top_rejects={strategy['top_reject_reasons']}")
        if strategy.get("top_reason_groups"):
            lines.append(f"  top_reason_groups={strategy['top_reason_groups']}")
        lines.append(f"  diagnostics={strategy.get('diagnostics')}")
        if strategy.get("orb_source_quality"):
            lines.append(f"  orb_source_quality={strategy['orb_source_quality']}")
        for slice_key in ("regime_state", "session_bucket", "source_state", "degraded_mode"):
            slice_rows = list((strategy.get("slice_summary") or {}).get(slice_key) or [])
            if slice_rows:
                lines.append(f"  {slice_key}={slice_rows}")
        if strategy.get("alerts"):
            lines.append(
                "  alerts="
                + str(
                    [
                        {
                            "type": row.get("alert_type"),
                            "severity": row.get("severity"),
                            "key": row.get("alert_key"),
                        }
                        for row in list(strategy.get("alerts") or [])
                    ]
                )
            )
        if strategy.get("parity_mismatches"):
            lines.append(f"  parity_mismatches={strategy['parity_mismatches']}")
    return "\n".join(lines)
