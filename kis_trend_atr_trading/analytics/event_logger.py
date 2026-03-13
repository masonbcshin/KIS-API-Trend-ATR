from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from analytics.summary_drilldown import derive_session_bucket, resolve_source_state
    from config import settings
    from utils.logger import get_logger
    from utils.market_hours import KST
except ImportError:
    from kis_trend_atr_trading.analytics.summary_drilldown import derive_session_bucket, resolve_source_state
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.utils.logger import get_logger
    from kis_trend_atr_trading.utils.market_hours import KST


logger = get_logger("strategy_analytics")

ANALYTICS_SCHEMA_VERSION = "v1"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_strategy_event_dir(event_dir: Optional[str] = None) -> Path:
    configured_dir = str(
        event_dir
        or getattr(settings, "STRATEGY_ANALYTICS_EVENT_DIR", "data/analytics")
        or "data/analytics"
    ).strip()
    base_dir = Path(configured_dir)
    if not base_dir.is_absolute():
        base_dir = _project_root() / base_dir
    return base_dir


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(val) for key, val in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, (str, bytes)):
        return _json_ready(vars(value))
    return value


def _stable_hash(value: Any) -> str:
    import hashlib

    payload = json.dumps(_json_ready(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _normalize_symbol(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw.zfill(6)


def _iso_trade_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    token = str(value or "").strip()
    return token[:10] if len(token) >= 10 else token


def _payload_hash(value: Any) -> str:
    return _stable_hash(value if value is not None else {})


def compute_candidate_id(candidate: Any) -> str:
    payload = {
        "strategy_tag": str(getattr(candidate, "strategy_tag", "") or ""),
        "symbol": _normalize_symbol(getattr(candidate, "symbol", "")),
        "created_at": getattr(candidate, "created_at", None).isoformat()
        if isinstance(getattr(candidate, "created_at", None), datetime)
        else "",
        "expires_at": getattr(candidate, "expires_at", None).isoformat()
        if isinstance(getattr(candidate, "expires_at", None), datetime)
        else "",
        "payload_hash": _payload_hash(getattr(candidate, "extra_json", None) or getattr(candidate, "meta", None) or vars(candidate)),
    }
    return _stable_hash(payload)


def compute_intent_id(intent: Any) -> str:
    native_payload = getattr(intent, "native_payload", None)
    payload_hash = _payload_hash(native_payload if native_payload is not None else getattr(intent, "meta", None) or vars(intent))
    payload = {
        "strategy_tag": str(getattr(intent, "strategy_tag", "") or ""),
        "symbol": _normalize_symbol(getattr(intent, "symbol", "")),
        "trade_date": str(getattr(intent, "trade_date", "") or ""),
        "created_at": getattr(intent, "created_at", None).isoformat()
        if isinstance(getattr(intent, "created_at", None), datetime)
        else "",
        "expires_at": getattr(intent, "expires_at", None).isoformat()
        if isinstance(getattr(intent, "expires_at", None), datetime)
        else "",
        "entry_reference_price": float(getattr(intent, "entry_reference_price", 0.0) or 0.0),
        "entry_reference_label": str(getattr(intent, "entry_reference_label", "") or ""),
        "payload_hash": payload_hash,
    }
    return _stable_hash(payload)


def _compute_event_id(record: Dict[str, Any]) -> str:
    identity = {
        "trade_date": str(record.get("trade_date") or ""),
        "event_ts": str(record.get("event_ts") or ""),
        "strategy_tag": str(record.get("strategy_tag") or ""),
        "symbol": str(record.get("symbol") or ""),
        "intent_id": str(record.get("intent_id") or ""),
        "candidate_id": str(record.get("candidate_id") or ""),
        "broker_order_id": str(record.get("broker_order_id") or ""),
        "event_type": str(record.get("event_type") or ""),
        "stage": str(record.get("stage") or ""),
        "decision": str(record.get("decision") or ""),
        "source_component": str(record.get("source_component") or ""),
        "payload_hash": _payload_hash(record.get("payload_json") or {}),
    }
    return _stable_hash(identity)


class StrategyAnalyticsEventLogger:
    def __init__(
        self,
        *,
        event_dir: Optional[str] = None,
        enabled: bool = False,
        flush_each_write: bool = False,
    ) -> None:
        self._event_dir = resolve_strategy_event_dir(event_dir)
        self._enabled = bool(enabled)
        self._flush_each_write = bool(flush_each_write)
        self._lock = threading.Lock()
        self._current_trade_date: str = ""
        self._handle = None

    @property
    def enabled(self) -> bool:
        return bool(self._enabled)

    @property
    def event_dir(self) -> Path:
        return self._event_dir

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.flush()
                finally:
                    self._handle.close()
                self._handle = None
                self._current_trade_date = ""

    def _ensure_handle(self, trade_date: str):
        self._event_dir.mkdir(parents=True, exist_ok=True)
        if self._handle is not None and self._current_trade_date == trade_date:
            return self._handle
        if self._handle is not None:
            try:
                self._handle.flush()
            finally:
                self._handle.close()
        path = self._event_dir / f"strategy_events_{trade_date}.jsonl"
        self._handle = path.open("a", encoding="utf-8")
        self._current_trade_date = trade_date
        return self._handle

    def build_event(
        self,
        *,
        event_ts: datetime,
        strategy_tag: str,
        symbol: str,
        event_type: str,
        stage: str,
        decision: str = "",
        trade_date: str = "",
        intent_id: str = "",
        candidate_id: str = "",
        broker_order_id: str = "",
        reject_reason: str = "",
        regime_state: str = "",
        degraded_mode: bool = False,
        queue_depth: Optional[int] = None,
        payload_schema_version: str = ANALYTICS_SCHEMA_VERSION,
        source_component: str = "",
        payload_json: Optional[Dict[str, Any]] = None,
        session_bucket: str = "",
        source_state: str = "",
        tie_break_applied: bool = False,
        tie_break_winner_strategy: str = "",
        ingress_reject_reason: str = "",
        recovery_flag: Optional[bool] = None,
    ) -> Dict[str, Any]:
        normalized_trade_date = str(trade_date or _iso_trade_date(event_ts) or "").strip()
        normalized_payload = _json_ready(payload_json or {})
        resolved_session_bucket = str(session_bucket or derive_session_bucket(event_ts) or "unknown")
        resolved_source_state = resolve_source_state(
            strategy_tag=str(strategy_tag or ""),
            event_type=str(event_type or ""),
            reject_reason=str(reject_reason or ""),
            source_state=str(source_state or ""),
            payload_json=normalized_payload if isinstance(normalized_payload, dict) else {},
        )
        resolved_ingress_reject_reason = str(
            ingress_reject_reason
            or (reject_reason if str(stage or "") == "ingress" and str(decision or "") == "rejected" else "")
            or ""
        )
        payload = {
            "schema_version": ANALYTICS_SCHEMA_VERSION,
            "trade_date": normalized_trade_date,
            "event_ts": event_ts.isoformat(),
            "strategy_tag": str(strategy_tag or ""),
            "symbol": _normalize_symbol(symbol),
            "intent_id": str(intent_id or ""),
            "candidate_id": str(candidate_id or ""),
            "broker_order_id": str(broker_order_id or ""),
            "event_type": str(event_type or ""),
            "stage": str(stage or ""),
            "decision": str(decision or ""),
            "reject_reason": str(reject_reason or ""),
            "regime_state": str(regime_state or ""),
            "degraded_mode": bool(degraded_mode),
            "queue_depth": int(queue_depth) if queue_depth is not None else None,
            "payload_schema_version": str(payload_schema_version or ANALYTICS_SCHEMA_VERSION),
            "source_component": str(source_component or ""),
            "payload_json": normalized_payload,
            "session_bucket": resolved_session_bucket,
            "source_state": resolved_source_state,
            "tie_break_applied": bool(tie_break_applied),
            "tie_break_winner_strategy": str(tie_break_winner_strategy or ""),
            "ingress_reject_reason": resolved_ingress_reject_reason,
            "recovery_flag": (
                bool(recovery_flag)
                if recovery_flag is not None
                else bool(str(stage or "") == "recovery" or str(event_type or "").startswith("recovery_"))
            ),
        }
        payload["event_id"] = _compute_event_id(payload)
        return payload

    def append(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        trade_date = str(event.get("trade_date") or "").strip()
        if not trade_date:
            return None
        line = json.dumps(_json_ready(event), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        with self._lock:
            handle = self._ensure_handle(trade_date)
            handle.write(line)
            handle.write("\n")
            if self._flush_each_write:
                handle.flush()
        return event

    def log_event(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        event = self.build_event(**kwargs)
        return self.append(event)


def load_strategy_events(
    *,
    event_dir: Optional[str] = None,
    trade_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    base_dir = resolve_strategy_event_dir(event_dir)
    if not base_dir.exists():
        return []
    paths: Iterable[Path]
    if trade_date:
        paths = [base_dir / f"strategy_events_{str(trade_date).strip()}.jsonl"]
    else:
        paths = sorted(base_dir.glob("strategy_events_*.jsonl"))
    events: List[Dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line_index, raw_line in enumerate(fh):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[STRATEGY_ANALYTICS] corrupt tail ignored file=%s line=%s", path, line_index + 1)
                    break
                if not isinstance(payload, dict):
                    continue
                payload["_line_index"] = line_index
                events.append(payload)
    events.sort(key=lambda item: (str(item.get("event_ts") or ""), int(item.get("_line_index", 0) or 0)))
    return events


def inspect_strategy_event_input(
    *,
    event_dir: Optional[str] = None,
    trade_date: Optional[str] = None,
) -> Dict[str, Any]:
    base_dir = resolve_strategy_event_dir(event_dir)
    diagnostics: Dict[str, Any] = {
        "configured_event_dir": str(
            event_dir
            or getattr(settings, "STRATEGY_ANALYTICS_EVENT_DIR", "data/analytics")
            or "data/analytics"
        ).strip(),
        "resolved_event_dir": str(base_dir),
        "trade_date": str(trade_date or ""),
        "event_dir_exists": bool(base_dir.exists()),
        "event_file": "",
        "event_file_exists": False,
        "event_file_size_bytes": 0,
        "available_event_file_count": 0,
        "missing_input_state": "ok",
    }
    if base_dir.exists():
        diagnostics["available_event_file_count"] = len(list(base_dir.glob("strategy_events_*.jsonl")))
    if trade_date:
        target_file = base_dir / f"strategy_events_{str(trade_date).strip()}.jsonl"
        diagnostics["event_file"] = str(target_file)
        diagnostics["event_file_exists"] = bool(target_file.exists())
        if target_file.exists():
            try:
                diagnostics["event_file_size_bytes"] = int(target_file.stat().st_size)
            except OSError:
                diagnostics["event_file_size_bytes"] = 0

    if not diagnostics["event_dir_exists"]:
        diagnostics["missing_input_state"] = "event_dir_missing"
    elif trade_date:
        if not diagnostics["event_file_exists"]:
            diagnostics["missing_input_state"] = "trade_date_file_missing"
        elif int(diagnostics["event_file_size_bytes"] or 0) <= 0:
            diagnostics["missing_input_state"] = "trade_date_file_empty"
    elif int(diagnostics["available_event_file_count"] or 0) <= 0:
        diagnostics["missing_input_state"] = "event_dir_empty"

    return diagnostics


def analytics_events_from_replay_report(
    report: Dict[str, Any],
    *,
    trade_date: Optional[str] = None,
    source_component: str = "threaded_pipeline_replay",
) -> List[Dict[str, Any]]:
    emitted: List[Dict[str, Any]] = []
    builder = StrategyAnalyticsEventLogger(enabled=False)

    def emit(
        *,
        event_ts: str,
        strategy_tag: str,
        symbol: str,
        event_type: str,
        stage: str,
        decision: str = "",
        reject_reason: str = "",
        payload_json: Optional[Dict[str, Any]] = None,
    ) -> None:
        ts = datetime.fromisoformat(str(event_ts))
        emitted.append(
            builder.build_event(
                event_ts=ts,
                trade_date=str(trade_date or _iso_trade_date(ts) or ""),
                strategy_tag=strategy_tag,
                symbol=symbol,
                event_type=event_type,
                stage=stage,
                decision=decision,
                reject_reason=reject_reason,
                regime_state="replay",
                degraded_mode=False,
                queue_depth=int((payload_json or {}).get("queue_depth", 0) or 0),
                source_component=source_component,
                payload_json=payload_json or {},
            )
        )

    for row in list(report.get("candidate_timeline") or []):
        if bool(row.get("setup_candidate_created")):
            emit(
                event_ts=str(row.get("event_ts")),
                strategy_tag=str(row.get("strategy_tag") or ""),
                symbol=str(row.get("symbol") or ""),
                event_type="candidate_created",
                stage="setup",
                decision="accepted",
                payload_json={"replay_row": dict(row)},
            )
    for row in list(report.get("intent_timeline") or []):
        if bool(row.get("timing_confirmed")):
            emit(
                event_ts=str(row.get("event_ts")),
                strategy_tag=str(row.get("strategy_tag") or ""),
                symbol=str(row.get("symbol") or ""),
                event_type="timing_confirmed",
                stage="timing",
                decision="accepted",
                payload_json={"replay_row": dict(row)},
            )
        elif str(row.get("reject_reason") or "").strip():
            emit(
                event_ts=str(row.get("event_ts")),
                strategy_tag=str(row.get("strategy_tag") or ""),
                symbol=str(row.get("symbol") or ""),
                event_type="timing_rejected",
                stage="timing",
                decision="rejected",
                reject_reason=str(row.get("reject_reason") or ""),
                payload_json={"replay_row": dict(row)},
            )
        emit(
            event_ts=str(row.get("event_ts")),
            strategy_tag=str(row.get("strategy_tag") or ""),
            symbol=str(row.get("symbol") or ""),
            event_type="intent_ingressed",
            stage="ingress",
            decision="accepted" if bool(row.get("intent_emitted")) else "rejected",
            reject_reason="" if bool(row.get("intent_emitted")) else str(row.get("reject_reason") or ""),
            payload_json={"replay_row": dict(row)},
        )
    decision_map = {
        "precheck_rejected": ("precheck_rejected", "precheck", "rejected"),
        "native_handoff_started": ("native_handoff_started", "handoff", "started"),
        "native_handoff_rejected": ("native_handoff_rejected", "handoff", "rejected"),
        "order_would_submit": ("order_submitted", "order", "submitted"),
        "order_blocked": ("order_cancelled", "order", "blocked"),
    }
    for row in list(report.get("order_timeline") or []):
        decision = str(row.get("order_decision") or "")
        mapped = decision_map.get(decision)
        if mapped is None:
            continue
        event_type, stage, mapped_decision = mapped
        emit(
            event_ts=str(row.get("event_ts")),
            strategy_tag=str(row.get("strategy_tag") or ""),
            symbol=str(row.get("symbol") or ""),
            event_type=event_type,
            stage=stage,
            decision=mapped_decision,
            reject_reason=str(row.get("reject_reason") or ""),
            payload_json={"replay_row": dict(row)},
        )
    return emitted
