from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .summary_drilldown import derive_event_dimensions, event_sort_key, iter_slice_pairs, strategy_rank


def _normalized_reason(event: Dict[str, Any]) -> str:
    return str(event.get("ingress_reject_reason") or event.get("reject_reason") or "").strip()


def _orb_reason_group(reason: str, source_state: str) -> str:
    token = str(reason or "").strip().lower()
    state = str(source_state or "").strip().lower()
    if state in {"unsupported", "missing"} or token in {"orb_intraday_unsupported", "orb_intraday_missing"}:
        return "orb_source_unavailable"
    if state == "stale" or token == "orb_intraday_stale":
        return "orb_source_stale"
    if state == "insufficient" or token == "orb_intraday_insufficient":
        return "orb_source_insufficient"
    return ""


def _precheck_reason_group(reason: str) -> str:
    token = str(reason or "").strip().lower()
    if token in {"existing_position", "existing_position_snapshot"}:
        return "precheck_existing_position"
    if token in {"pending_order", "pullback_pending_order"}:
        return "precheck_pending_order"
    if token in {"existing_holding", "existing_holding_snapshot", "insufficient_cash_snapshot", "intent_expired"}:
        return "precheck_risk_or_holdings"
    return "precheck_other"


def _build_competition_context(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    accepted_by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    tie_break_applied_event_ids: set[str] = set()
    accepted_strategy_pairs: set[Tuple[str, str]] = set()

    for event in sorted(list(events or []), key=event_sort_key):
        if str(event.get("event_type") or "") != "intent_ingressed" or str(event.get("decision") or "") != "accepted":
            continue
        symbol = str(event.get("symbol") or "").zfill(6)
        strategy_tag = str(event.get("strategy_tag") or "")
        if not symbol or not strategy_tag:
            continue
        prior = [item for item in accepted_by_symbol[symbol] if str(item.get("strategy_tag") or "") != strategy_tag]
        if prior:
            tie_break_applied_event_ids.add(str(event.get("event_id") or ""))
        accepted_by_symbol[symbol].append(event)
        accepted_strategy_pairs.add((symbol, strategy_tag))

    winner_by_symbol: Dict[str, str] = {}
    contested_symbols: set[str] = set()
    for symbol, symbol_events in accepted_by_symbol.items():
        strategies = {str(item.get("strategy_tag") or "") for item in symbol_events if str(item.get("strategy_tag") or "")}
        if len(strategies) <= 1:
            continue
        contested_symbols.add(symbol)
        winner_event = min(
            symbol_events,
            key=lambda item: (
                event_sort_key(item)[0],
                strategy_rank(str(item.get("strategy_tag") or "")),
                event_sort_key(item)[1],
            ),
        )
        winner_by_symbol[symbol] = str(winner_event.get("strategy_tag") or "")

    return {
        "winner_by_symbol": winner_by_symbol,
        "tie_break_applied_event_ids": tie_break_applied_event_ids,
        "accepted_strategy_pairs": accepted_strategy_pairs,
        "contested_symbols": contested_symbols,
    }


def _event_weight(event: Dict[str, Any], reason_group: str) -> int:
    if reason_group == "recovery_duplicate_prevented":
        payload = dict(event.get("payload_json") or {})
        return max(int(payload.get("duplicate_prevented_count", 1) or 1), 1)
    return 1


def _classify_event(event: Dict[str, Any], context: Dict[str, Any]) -> List[Tuple[str, str, str, int]]:
    event_type = str(event.get("event_type") or "")
    reject_stage = str(event.get("stage") or "")
    reject_reason = _normalized_reason(event)
    source_state = str(derive_event_dimensions(event).get("source_state") or "")
    decision = str(event.get("decision") or "")
    symbol = str(event.get("symbol") or "").zfill(6)
    strategy_tag = str(event.get("strategy_tag") or "")
    rows: List[Tuple[str, str, str, int]] = []

    orb_group = _orb_reason_group(reject_reason, source_state)

    if event_type == "timing_rejected":
        reason_group = orb_group or "timing_rejected"
        rows.append((reject_stage, reject_reason or reason_group, reason_group, 1))
    elif event_type == "intent_ingressed" and decision == "rejected":
        reason_token = str(reject_reason or "").lower()
        if reason_token.startswith("degraded_mode"):
            rows.append((reject_stage, reject_reason or "degraded_mode", "degraded_rejected", 1))
        elif reason_token in {"duplicate", "pending_symbol_cap", "duplicate_or_queue_full"}:
            rows.append((reject_stage, reject_reason or "duplicate", "duplicate_blocked", 1))
        elif reason_token in {"queue_depth_limit", "queue_full"}:
            rows.append((reject_stage, reject_reason or "queue_full", "authoritative_queue_rejected", 1))
    elif event_type == "intent_ingressed" and decision == "accepted":
        if str(event.get("event_id") or "") in set(context.get("tie_break_applied_event_ids") or set()):
            rows.append((reject_stage or "ingress", "tie_break_applied", "tie_break_applied", 1))
    elif event_type == "precheck_rejected":
        reason_group = _precheck_reason_group(reject_reason)
        rows.append((reject_stage, reject_reason or reason_group, reason_group, 1))
        winner_by_symbol = dict(context.get("winner_by_symbol") or {})
        if (
            symbol in set(context.get("contested_symbols") or set())
            and (symbol, strategy_tag) in set(context.get("accepted_strategy_pairs") or set())
            and winner_by_symbol.get(symbol)
            and winner_by_symbol.get(symbol) != strategy_tag
            and str(reject_reason or "").lower() in {"existing_position", "pending_order"}
        ):
            rows.append((reject_stage, reject_reason or "tie_break_loser", "tie_break_loser", 1))
    elif event_type == "native_handoff_rejected":
        reason_group = orb_group or "native_handoff_rejected"
        rows.append((reject_stage, reject_reason or reason_group, reason_group, 1))
    elif event_type == "recovery_duplicate_prevented":
        rows.append(
            (
                reject_stage or "recovery",
                "recovery_duplicate_prevented",
                "recovery_duplicate_prevented",
                _event_weight(event, "recovery_duplicate_prevented"),
            )
        )
    return rows


def _outcome_class(reason_group: str) -> str:
    if reason_group in {"duplicate_blocked", "tie_break_loser", "authoritative_queue_rejected"}:
        return "block"
    if reason_group in {"degraded_rejected"}:
        return "degraded"
    if reason_group in {"recovery_duplicate_prevented"}:
        return "recovery"
    if reason_group in {"tie_break_applied"}:
        return "diagnostic"
    return "reject"


def build_attribution_rows(trade_date: str, events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    context = _build_competition_context(events)
    counts: Counter[Tuple[str, str, str, str, str, str, str]] = Counter()

    for event in sorted(list(events or []), key=event_sort_key):
        strategy_tag = str(event.get("strategy_tag") or "").strip()
        if not strategy_tag:
            continue
        for reject_stage, reject_reason, reason_group, weight in _classify_event(event, context):
            outcome_class = _outcome_class(reason_group)
            for slice_key, slice_value in iter_slice_pairs(event):
                counts[
                    (
                        strategy_tag,
                        slice_key,
                        slice_value,
                        str(reject_stage or ""),
                        str(reject_reason or ""),
                        str(reason_group or ""),
                        outcome_class,
                    )
                ] += int(weight)

    rows: List[Dict[str, Any]] = []
    for (strategy_tag, slice_key, slice_value, reject_stage, reject_reason, reason_group, outcome_class), count in sorted(
        counts.items(),
        key=lambda item: (
            str(item[0][0]),
            str(item[0][1]),
            str(item[0][2]),
            str(item[0][3]),
            -int(item[1]),
            str(item[0][5]),
            str(item[0][4]),
        ),
    ):
        rows.append(
            {
                "trade_date": trade_date,
                "strategy_tag": strategy_tag,
                "slice_key": slice_key,
                "slice_value": slice_value,
                "reject_stage": reject_stage,
                "reject_reason": reject_reason,
                "reason_group": reason_group,
                "outcome_class": outcome_class,
                "count": int(count),
            }
        )
    return rows
