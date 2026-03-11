from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


FUNNEL_STAGES: Tuple[str, ...] = (
    "candidate_created",
    "timing_confirmed",
    "authoritative_ingress",
    "precheck_pass",
    "precheck_reject",
    "native_handoff_pass",
    "native_handoff_reject",
    "submitted",
    "filled",
    "exit",
)

FUNNEL_STAGE_ORDER: Dict[str, int] = {stage_name: index for index, stage_name in enumerate(FUNNEL_STAGES, start=1)}

FUNNEL_PREVIOUS_STAGE: Dict[str, Optional[str]] = {
    "candidate_created": None,
    "timing_confirmed": "candidate_created",
    "authoritative_ingress": "timing_confirmed",
    "precheck_pass": "authoritative_ingress",
    "precheck_reject": "authoritative_ingress",
    "native_handoff_pass": "precheck_pass",
    "native_handoff_reject": "precheck_pass",
    "submitted": "native_handoff_pass",
    "filled": "submitted",
    "exit": "filled",
}

SLICE_KEY_ORDER: Dict[str, int] = {
    "overall": 0,
    "regime_state": 1,
    "session_bucket": 2,
    "source_state": 3,
    "degraded_mode": 4,
}

STRATEGY_RANK: Dict[str, int] = {
    "pullback_rebreakout": 0,
    "trend_atr": 1,
    "opening_range_breakout": 2,
}


def _parse_ts(raw: Any) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


def strategy_rank(strategy_tag: str) -> int:
    return int(STRATEGY_RANK.get(str(strategy_tag or "").strip(), 99))


def event_sort_key(event: Dict[str, Any]) -> Tuple[Any, ...]:
    ts = _parse_ts(event.get("event_ts"))
    if ts is None:
        ts = datetime.max
    return (
        ts,
        int(event.get("_line_index", 0) or 0),
        strategy_rank(str(event.get("strategy_tag") or "")),
        str(event.get("event_id") or ""),
    )


def derive_session_bucket(value: Any) -> str:
    ts = value if isinstance(value, datetime) else _parse_ts(value)
    if ts is None:
        return "unknown"
    hour = int(ts.hour)
    minute = int(ts.minute)
    minutes = (hour * 60) + minute
    if minutes < (10 * 60 + 30):
        return "opening"
    if minutes < (14 * 60 + 30):
        return "mid"
    return "late"


def _lookup_nested(payload: Any, *paths: Sequence[str]) -> str:
    if not isinstance(payload, dict):
        return ""
    for path in paths:
        current: Any = payload
        for token in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(token)
        if current not in (None, ""):
            return str(current)
    return ""


def normalize_source_state(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    if not token:
        return "na"
    alias_map = {
        "ready": "fresh",
        "supported": "fresh",
        "unavailable": "unsupported",
        "fallback_daily": "fallback",
        "intraday_unavailable": "unsupported",
        "n/a": "na",
        "none": "na",
    }
    normalized = alias_map.get(token, token)
    if normalized in {"fresh", "unsupported", "missing", "insufficient", "stale", "fallback", "na", "unknown"}:
        return normalized
    return normalized


def resolve_source_state(
    *,
    strategy_tag: str,
    event_type: str,
    reject_reason: str = "",
    source_state: str = "",
    payload_json: Optional[Dict[str, Any]] = None,
) -> str:
    explicit = normalize_source_state(source_state)
    if explicit != "na":
        return explicit

    payload = dict(payload_json or {})
    nested = _lookup_nested(
        payload,
        ("source_state",),
        ("intraday_source_state",),
        ("entry_meta", "intraday_source_state"),
        ("decision_meta", "intraday_source_state"),
        ("replay_row", "source_state"),
        ("replay_row", "intraday_source_state"),
        ("replay_row", "entry_meta", "intraday_source_state"),
        ("replay_row", "decision_meta", "intraday_source_state"),
    )
    if nested:
        return normalize_source_state(nested)

    reason = str(reject_reason or "").strip().lower()
    if reason.startswith("orb_intraday_"):
        return normalize_source_state(reason.replace("orb_intraday_", "", 1))
    if str(strategy_tag or "") == "opening_range_breakout":
        if event_type in {
            "candidate_created",
            "timing_confirmed",
            "intent_ingressed",
            "precheck_rejected",
            "native_handoff_started",
            "native_handoff_rejected",
            "order_submitted",
            "order_filled",
            "order_cancelled",
            "exit_decision",
        } and not reason.startswith("orb_intraday_"):
            return "fresh"
    return "na"


def normalize_regime_state(raw: Any) -> str:
    token = str(raw or "").strip()
    return token if token else "unknown"


def derive_event_dimensions(event: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(event.get("payload_json") or {})
    return {
        "regime_state": normalize_regime_state(event.get("regime_state")),
        "session_bucket": str(event.get("session_bucket") or derive_session_bucket(event.get("event_ts")) or "unknown"),
        "source_state": resolve_source_state(
            strategy_tag=str(event.get("strategy_tag") or ""),
            event_type=str(event.get("event_type") or ""),
            reject_reason=str(event.get("reject_reason") or ""),
            source_state=str(event.get("source_state") or ""),
            payload_json=payload,
        ),
        "degraded_mode": bool(event.get("degraded_mode")),
    }


def iter_slice_pairs(event: Dict[str, Any]) -> Iterator[Tuple[str, str]]:
    dims = derive_event_dimensions(event)
    yield ("overall", "all")
    yield ("regime_state", str(dims["regime_state"]))
    yield ("session_bucket", str(dims["session_bucket"]))
    yield ("source_state", str(dims["source_state"]))
    yield ("degraded_mode", "degraded" if bool(dims["degraded_mode"]) else "normal")


def _entity_id(event: Dict[str, Any], *, prefer: Sequence[str]) -> str:
    for key in prefer:
        token = str(event.get(key) or "").strip()
        if token:
            return token
    return str(event.get("event_id") or "")


def funnel_stage_entities(event: Dict[str, Any]) -> Dict[str, str]:
    event_type = str(event.get("event_type") or "")
    decision = str(event.get("decision") or "")
    stages: Dict[str, str] = {}
    if event_type == "candidate_created":
        stages["candidate_created"] = _entity_id(event, prefer=("candidate_id", "event_id"))
    elif event_type == "timing_confirmed":
        stages["timing_confirmed"] = _entity_id(event, prefer=("candidate_id", "intent_id", "event_id"))
    elif event_type == "intent_ingressed" and decision == "accepted":
        stages["authoritative_ingress"] = _entity_id(event, prefer=("intent_id", "event_id"))
    elif event_type == "precheck_rejected":
        stages["precheck_reject"] = _entity_id(event, prefer=("intent_id", "event_id"))
    elif event_type == "native_handoff_rejected":
        entity = _entity_id(event, prefer=("intent_id", "event_id"))
        stages["precheck_pass"] = entity
        stages["native_handoff_reject"] = entity
    elif event_type in {"native_handoff_started", "order_submitted", "order_filled", "order_cancelled"}:
        entity = _entity_id(event, prefer=("intent_id", "event_id"))
        stages["precheck_pass"] = entity
        if event_type in {"order_submitted", "order_filled", "order_cancelled"}:
            stages["native_handoff_pass"] = entity
    if event_type == "order_submitted":
        stages["submitted"] = _entity_id(event, prefer=("intent_id", "broker_order_id", "event_id"))
    elif event_type == "order_filled":
        stages["filled"] = _entity_id(event, prefer=("broker_order_id", "intent_id", "event_id"))
    elif event_type == "exit_decision":
        stages["exit"] = _entity_id(event, prefer=("broker_order_id", "intent_id", "event_id"))
    return stages


def build_funnel_rows(trade_date: str, events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[Tuple[str, str, str, str], set[str]] = defaultdict(set)
    slice_keys: set[Tuple[str, str, str]] = set()

    for event in sorted(list(events or []), key=event_sort_key):
        strategy_tag = str(event.get("strategy_tag") or "").strip()
        if not strategy_tag:
            continue
        stage_entities = funnel_stage_entities(event)
        if not stage_entities:
            continue
        slices = tuple(iter_slice_pairs(event))
        for slice_key, slice_value in slices:
            slice_keys.add((strategy_tag, slice_key, slice_value))
            for stage_name, entity_id in stage_entities.items():
                counts[(strategy_tag, slice_key, slice_value, stage_name)].add(str(entity_id or event.get("event_id") or ""))

    rows: List[Dict[str, Any]] = []
    for strategy_tag, slice_key, slice_value in sorted(
        slice_keys,
        key=lambda item: (
            str(item[0]),
            int(SLICE_KEY_ORDER.get(str(item[1]), 99)),
            str(item[2]),
        ),
    ):
        for stage_name in FUNNEL_STAGES:
            stage_count = len(counts.get((strategy_tag, slice_key, slice_value, stage_name), set()))
            previous_stage = FUNNEL_PREVIOUS_STAGE.get(stage_name)
            previous_stage_count = (
                len(counts.get((strategy_tag, slice_key, slice_value, previous_stage), set()))
                if previous_stage
                else stage_count
            )
            if previous_stage is None:
                conversion_rate = 1.0 if stage_count > 0 else 0.0
            elif previous_stage_count > 0:
                conversion_rate = float(stage_count) / float(previous_stage_count)
            else:
                conversion_rate = 0.0
            rows.append(
                {
                    "trade_date": trade_date,
                    "strategy_tag": strategy_tag,
                    "slice_key": slice_key,
                    "slice_value": slice_value,
                    "stage_name": stage_name,
                    "stage_order": int(FUNNEL_STAGE_ORDER[stage_name]),
                    "stage_count": int(stage_count),
                    "prev_stage_count": int(previous_stage_count),
                    "conversion_rate": float(conversion_rate),
                }
            )
    return rows
