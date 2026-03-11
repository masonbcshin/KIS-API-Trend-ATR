from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from config import settings
    from engine.pullback_pipeline_models import PullbackSetupCandidate, StrategySetupCandidate
    from utils.logger import get_logger
    from utils.market_hours import KST
except ImportError:
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.engine.pullback_pipeline_models import PullbackSetupCandidate, StrategySetupCandidate
    from kis_trend_atr_trading.utils.logger import get_logger
    from kis_trend_atr_trading.utils.market_hours import KST


logger = get_logger("strategy_pipeline_persistence")

SCHEMA_VERSION = "v1"
FINALIZED_JOURNAL_STATES = {"rejected", "submitted", "filled", "cancelled", "expired", "duplicate_blocked"}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _stable_hash(value: Any) -> str:
    payload = json.dumps(_json_ready(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return __import__("hashlib").sha1(payload.encode("utf-8")).hexdigest()


def _parse_datetime(raw: Any) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


@dataclass(frozen=True)
class RecoveredPendingIntent:
    intent_id: str
    strategy_tag: str
    symbol: str
    trade_date: str
    created_at: datetime
    expires_at: Optional[datetime]
    journal_state: str
    payload_hash: str
    payload_schema_version: str


@dataclass(frozen=True)
class RecoveryResult:
    recovered_pullback_candidates: List[PullbackSetupCandidate]
    recovered_shadow_candidates: List[StrategySetupCandidate]
    recovered_pending_intents: List[RecoveredPendingIntent]
    finalized_or_submitted_intent_ids: set[str]
    dropped_stale_candidate_count: int = 0
    dropped_stale_intent_count: int = 0
    duplicate_prevented_count: int = 0
    corrupt_record_skipped_count: int = 0
    broker_reconciled_count: int = 0
    advisory_runtime_metadata: Optional[Dict[str, Any]] = None
    load_ms: float = 0.0


class StrategyPipelinePersistenceManager:
    def __init__(
        self,
        *,
        state_dir: Optional[str] = None,
        enabled: bool = False,
        candidate_snapshot_interval_sec: float = 15.0,
        intent_journal_enabled: bool = True,
        intent_max_age_sec: float = 120.0,
        candidate_max_recover_age_sec: float = 300.0,
        recover_only_current_trade_date: bool = True,
    ) -> None:
        configured = str(state_dir or getattr(settings, "PIPELINE_STATE_DIR", "data/pipeline_state") or "data/pipeline_state").strip()
        base_dir = Path(configured)
        if not base_dir.is_absolute():
            base_dir = _project_root() / base_dir
        self._state_dir = base_dir
        self._enabled = bool(enabled)
        self._candidate_snapshot_interval_sec = max(float(candidate_snapshot_interval_sec or 15.0), 1.0)
        self._intent_journal_enabled = bool(intent_journal_enabled)
        self._intent_max_age_sec = max(float(intent_max_age_sec or 120.0), 0.0)
        self._candidate_max_recover_age_sec = max(float(candidate_max_recover_age_sec or 300.0), 0.0)
        self._recover_only_current_trade_date = bool(recover_only_current_trade_date)
        self._write_lock = threading.Lock()
        self._last_candidate_snapshot_at: Optional[datetime] = None

    @property
    def enabled(self) -> bool:
        return bool(self._enabled)

    @property
    def candidate_snapshot_path(self) -> Path:
        return self._state_dir / "candidate_snapshot.json"

    @property
    def intent_journal_path(self) -> Path:
        return self._state_dir / "intent_journal.jsonl"

    @property
    def order_journal_path(self) -> Path:
        return self._state_dir / "order_journal.jsonl"

    @property
    def runtime_metadata_path(self) -> Path:
        return self._state_dir / "runtime_metadata.json"

    def ensure_state_dir(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)

    def compute_intent_id(self, intent: Any) -> str:
        native_payload = getattr(intent, "native_payload", None)
        payload_hash = _stable_hash(native_payload if native_payload is not None else getattr(intent, "meta", {}))
        payload = {
            "strategy_tag": str(getattr(intent, "strategy_tag", "") or ""),
            "symbol": str(getattr(intent, "symbol", "") or "").zfill(6),
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

    def _payload_hash(self, intent: Any) -> str:
        native_payload = getattr(intent, "native_payload", None)
        return _stable_hash(native_payload if native_payload is not None else getattr(intent, "meta", {}))

    def _journal_record(
        self,
        *,
        intent: Any,
        journal_state: str,
        reason: str = "",
        message: str = "",
        broker_order_id: str = "",
        source: str = "",
    ) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "recorded_at": datetime.now(KST).isoformat(),
            "intent_id": self.compute_intent_id(intent),
            "strategy_tag": str(getattr(intent, "strategy_tag", "") or ""),
            "symbol": str(getattr(intent, "symbol", "") or "").zfill(6),
            "trade_date": str(getattr(intent, "trade_date", "") or ""),
            "created_at": getattr(intent, "created_at", None).isoformat()
            if isinstance(getattr(intent, "created_at", None), datetime)
            else "",
            "expires_at": getattr(intent, "expires_at", None).isoformat()
            if isinstance(getattr(intent, "expires_at", None), datetime)
            else "",
            "journal_state": str(journal_state or ""),
            "payload_schema_version": str(getattr(intent, "schema_version", SCHEMA_VERSION) or SCHEMA_VERSION),
            "payload_hash": self._payload_hash(intent),
            "broker_order_id": str(broker_order_id or ""),
            "reason": str(reason or ""),
            "message": str(message or ""),
            "source": str(source or ""),
        }

    def _append_jsonl(self, path: Path, record: Dict[str, Any], *, flush: bool = False) -> None:
        self.ensure_state_dir()
        line = json.dumps(_json_ready(record), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        with self._write_lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.write("\n")
                fh.flush()
                if flush:
                    os.fsync(fh.fileno())

    def _atomic_write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        self.ensure_state_dir()
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        body = json.dumps(_json_ready(payload), ensure_ascii=True, indent=2, sort_keys=True)
        with self._write_lock:
            with tmp_path.open("w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            tmp_path.replace(path)

    def _load_jsonl(self, path: Path) -> tuple[list[Dict[str, Any]], int]:
        if not path.exists():
            return [], 0
        records: list[Dict[str, Any]] = []
        corrupt_count = 0
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    corrupt_count += 1
                    break
                if isinstance(parsed, dict):
                    records.append(parsed)
        return records, corrupt_count

    def _serialize_candidate(self, candidate: Any, *, source_kind: str) -> Dict[str, Any]:
        if isinstance(candidate, PullbackSetupCandidate):
            payload = {
                "candidate_type": "pullback_setup_candidate",
                "symbol": str(candidate.symbol).zfill(6),
                "strategy_tag": str(candidate.strategy_tag or ""),
                "created_at": candidate.created_at.isoformat(),
                "expires_at": candidate.expires_at.isoformat(),
                "context_version": str(candidate.context_version or ""),
                "swing_high": float(candidate.swing_high or 0.0),
                "swing_low": float(candidate.swing_low or 0.0),
                "micro_high": float(candidate.micro_high or 0.0),
                "atr": float(candidate.atr or 0.0),
                "source": str(candidate.source or ""),
                "extra_json": _json_ready(candidate.extra_json or {}),
            }
        elif isinstance(candidate, StrategySetupCandidate):
            payload = {
                "candidate_type": "strategy_setup_candidate",
                "strategy_tag": str(candidate.strategy_tag or ""),
                "symbol": str(candidate.symbol).zfill(6),
                "created_at": candidate.created_at.isoformat(),
                "expires_at": candidate.expires_at.isoformat(),
                "trade_date": str(candidate.trade_date or ""),
                "entry_reference_price": float(candidate.entry_reference_price or 0.0),
                "entry_reference_label": str(candidate.entry_reference_label or ""),
                "meta": _json_ready(candidate.meta or {}),
                "schema_version": str(candidate.schema_version or SCHEMA_VERSION),
            }
        else:
            raise TypeError(f"unsupported candidate type for persistence: {type(candidate)!r}")
        payload["schema_version"] = SCHEMA_VERSION
        payload["source_kind"] = str(source_kind or "")
        return payload

    def _deserialize_candidate(self, record: Dict[str, Any]) -> Optional[Any]:
        candidate_type = str(record.get("candidate_type") or "").strip()
        if candidate_type == "pullback_setup_candidate":
            created_at = _parse_datetime(record.get("created_at"))
            expires_at = _parse_datetime(record.get("expires_at"))
            if created_at is None or expires_at is None:
                return None
            return PullbackSetupCandidate(
                symbol=str(record.get("symbol") or "").zfill(6),
                strategy_tag=str(record.get("strategy_tag") or ""),
                created_at=created_at,
                expires_at=expires_at,
                context_version=str(record.get("context_version") or ""),
                swing_high=float(record.get("swing_high", 0.0) or 0.0),
                swing_low=float(record.get("swing_low", 0.0) or 0.0),
                micro_high=float(record.get("micro_high", 0.0) or 0.0),
                atr=float(record.get("atr", 0.0) or 0.0),
                source=str(record.get("source") or ""),
                extra_json=dict(record.get("extra_json") or {}),
            )
        if candidate_type == "strategy_setup_candidate":
            created_at = _parse_datetime(record.get("created_at"))
            expires_at = _parse_datetime(record.get("expires_at"))
            if created_at is None or expires_at is None:
                return None
            return StrategySetupCandidate(
                strategy_tag=str(record.get("strategy_tag") or ""),
                symbol=str(record.get("symbol") or "").zfill(6),
                created_at=created_at,
                expires_at=expires_at,
                trade_date=str(record.get("trade_date") or ""),
                entry_reference_price=float(record.get("entry_reference_price", 0.0) or 0.0),
                entry_reference_label=str(record.get("entry_reference_label") or ""),
                meta=dict(record.get("meta") or {}),
                schema_version=str(record.get("schema_version") or SCHEMA_VERSION),
            )
        return None

    def maybe_save_candidate_snapshot(
        self,
        *,
        executor: Any,
        candidate_store: Any,
        now: Optional[datetime] = None,
        force: bool = False,
    ) -> bool:
        if not self.enabled:
            return False
        current_now = now or datetime.now(KST)
        if not force and self._last_candidate_snapshot_at is not None:
            age_sec = max((current_now - self._last_candidate_snapshot_at).total_seconds(), 0.0)
            if age_sec < self._candidate_snapshot_interval_sec:
                return False
        pullback_candidates = list(getattr(candidate_store, "snapshot", lambda: {})().values())
        shadow_candidates = []
        snapshot_shadow_candidates = getattr(executor, "snapshot_strategy_shadow_candidates", None)
        if callable(snapshot_shadow_candidates):
            shadow_candidates = list(snapshot_shadow_candidates().values())
        records = []
        for candidate in pullback_candidates:
            records.append(self._serialize_candidate(candidate, source_kind="pullback_candidate_store"))
        for candidate in shadow_candidates:
            try:
                records.append(self._serialize_candidate(candidate, source_kind="strategy_shadow_candidate"))
            except TypeError:
                continue
        payload = {
            "schema_version": SCHEMA_VERSION,
            "saved_at": current_now.isoformat(),
            "trade_date": str(getattr(executor, "_trade_date_key", lambda dt: dt.date().isoformat())(current_now) or ""),
            "records": records,
        }
        started = time.perf_counter()
        self._atomic_write_json(self.candidate_snapshot_path, payload)
        self._last_candidate_snapshot_at = current_now
        setattr(executor, "_pipeline_state_save_ms", (time.perf_counter() - started) * 1000.0)
        return True

    def save_runtime_metadata(self, *, executor: Any, now: Optional[datetime] = None) -> None:
        if not self.enabled:
            return
        current_now = now or datetime.now(KST)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "saved_at": current_now.isoformat(),
            "worker_health_state": dict(getattr(executor, "_worker_health_state", {}) or {}),
            "worker_state_reason": dict(getattr(executor, "_worker_state_reason", {}) or {}),
            "worker_lag_sec": dict(getattr(executor, "_worker_lag_sec", {}) or {}),
            "risk_snapshot_stale": bool(getattr(executor, "_risk_snapshot_stale", False)),
            "risk_snapshot_last_success_age_sec": float(
                getattr(executor, "_risk_snapshot_last_success_age_sec", -1.0) or -1.0
            ),
            "market_regime_snapshot_state": str(
                getattr(executor, "_strategy_regime_snapshot_state_used", "absent") or "absent"
            ),
        }
        self._atomic_write_json(self.runtime_metadata_path, payload)

    def append_intent_state(
        self,
        *,
        intent: Any,
        journal_state: str,
        reason: str = "",
        message: str = "",
        broker_order_id: str = "",
        source: str = "",
    ) -> None:
        if not self.enabled or not self._intent_journal_enabled:
            return
        record = self._journal_record(
            intent=intent,
            journal_state=journal_state,
            reason=reason,
            message=message,
            broker_order_id=broker_order_id,
            source=source,
        )
        flush = str(journal_state or "") in {"submitted", "filled", "cancelled"}
        self._append_jsonl(self.intent_journal_path, record, flush=flush)

    def append_order_state(
        self,
        *,
        intent: Any,
        journal_state: str,
        reason: str = "",
        message: str = "",
        broker_order_id: str = "",
        source: str = "",
    ) -> None:
        if not self.enabled:
            return
        record = self._journal_record(
            intent=intent,
            journal_state=journal_state,
            reason=reason,
            message=message,
            broker_order_id=broker_order_id,
            source=source,
        )
        flush = str(journal_state or "") in {"submitted", "filled", "cancelled"}
        self._append_jsonl(self.order_journal_path, record, flush=flush)

    def classify_reject_state(self, reason: str) -> str:
        normalized = str(reason or "").strip()
        if normalized == "intent_expired":
            return "expired"
        if normalized in {"existing_position", "pending_order", "existing_holding", "duplicate"}:
            return "duplicate_blocked"
        return "rejected"

    def classify_order_result(self, order_result: Dict[str, Any]) -> str:
        if bool(order_result.get("success")):
            if bool(order_result.get("reconciled")):
                return "filled"
            if int(order_result.get("exec_qty", 0) or 0) > 0:
                return "filled"
            if str(order_result.get("status") or "").lower() == "cancelled":
                return "cancelled"
            return "submitted"
        if str(order_result.get("status") or "").lower() == "cancelled":
            return "cancelled"
        reason = str(order_result.get("reason") or order_result.get("message") or "")
        return self.classify_reject_state(reason)

    def _load_runtime_metadata(self) -> Optional[Dict[str, Any]]:
        if not self.runtime_metadata_path.exists():
            return None
        try:
            return json.loads(self.runtime_metadata_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def load_recovery_state(
        self,
        *,
        current_trade_date: str,
        now: Optional[datetime] = None,
        reconciled_symbols: Optional[Iterable[str]] = None,
    ) -> RecoveryResult:
        current_now = now or datetime.now(KST)
        reconciled = {str(symbol or "").zfill(6) for symbol in (reconciled_symbols or []) if str(symbol or "").strip()}
        started = time.perf_counter()

        pullback_candidates: list[PullbackSetupCandidate] = []
        shadow_candidates: list[StrategySetupCandidate] = []
        recovered_pending_intents: list[RecoveredPendingIntent] = []
        dropped_stale_candidate_count = 0
        dropped_stale_intent_count = 0
        duplicate_prevented_count = 0
        broker_reconciled_count = 0

        snapshot_payload: Dict[str, Any] = {}
        if self.candidate_snapshot_path.exists():
            try:
                snapshot_payload = json.loads(self.candidate_snapshot_path.read_text(encoding="utf-8"))
            except Exception:
                snapshot_payload = {}

        snapshot_trade_date = str(snapshot_payload.get("trade_date") or "")
        snapshot_saved_at = _parse_datetime(snapshot_payload.get("saved_at"))
        snapshot_records = list(snapshot_payload.get("records") or [])
        snapshot_mismatch = self._recover_only_current_trade_date and snapshot_trade_date and snapshot_trade_date != str(current_trade_date or "")

        if not snapshot_mismatch:
            snapshot_age_sec = (
                max((current_now - snapshot_saved_at).total_seconds(), 0.0)
                if isinstance(snapshot_saved_at, datetime)
                else 0.0
            )
            for raw_record in snapshot_records:
                record = dict(raw_record or {})
                candidate = self._deserialize_candidate(record)
                if candidate is None:
                    continue
                expires_at = getattr(candidate, "expires_at", None)
                trade_date = ""
                if isinstance(candidate, StrategySetupCandidate):
                    trade_date = str(candidate.trade_date or "")
                elif isinstance(candidate, PullbackSetupCandidate):
                    signal_time = str((candidate.extra_json or {}).get("signal_time") or "")
                    trade_date = signal_time[:10] if len(signal_time) >= 10 else candidate.created_at.date().isoformat()
                if self._recover_only_current_trade_date and trade_date and trade_date != str(current_trade_date or ""):
                    dropped_stale_candidate_count += 1
                    continue
                if isinstance(expires_at, datetime) and expires_at <= current_now:
                    dropped_stale_candidate_count += 1
                    continue
                if self._candidate_max_recover_age_sec > 0.0 and snapshot_age_sec > self._candidate_max_recover_age_sec:
                    dropped_stale_candidate_count += 1
                    continue
                if str(getattr(candidate, "symbol", "") or "").zfill(6) in reconciled:
                    duplicate_prevented_count += 1
                    broker_reconciled_count += 1
                    continue
                if isinstance(candidate, PullbackSetupCandidate):
                    pullback_candidates.append(candidate)
                elif isinstance(candidate, StrategySetupCandidate):
                    shadow_candidates.append(candidate)
        else:
            dropped_stale_candidate_count += len(snapshot_records)

        intent_records, corrupt_intents = self._load_jsonl(self.intent_journal_path)
        order_records, corrupt_orders = self._load_jsonl(self.order_journal_path)
        latest_by_intent: Dict[str, Dict[str, Any]] = {}
        for record in intent_records + order_records:
            intent_id = str(record.get("intent_id") or "").strip()
            if not intent_id:
                continue
            latest_by_intent[intent_id] = dict(record)

        finalized_or_submitted: set[str] = set()
        for intent_id, record in latest_by_intent.items():
            state = str(record.get("journal_state") or "")
            if state in FINALIZED_JOURNAL_STATES:
                finalized_or_submitted.add(intent_id)
            created_at = _parse_datetime(record.get("created_at"))
            expires_at = _parse_datetime(record.get("expires_at"))
            trade_date = str(record.get("trade_date") or "")
            symbol = str(record.get("symbol") or "").zfill(6)
            if state != "accepted":
                continue
            if self._recover_only_current_trade_date and trade_date and trade_date != str(current_trade_date or ""):
                dropped_stale_intent_count += 1
                continue
            if self._intent_max_age_sec > 0.0 and isinstance(created_at, datetime):
                if max((current_now - created_at).total_seconds(), 0.0) > self._intent_max_age_sec:
                    dropped_stale_intent_count += 1
                    continue
            if isinstance(expires_at, datetime) and expires_at <= current_now:
                dropped_stale_intent_count += 1
                continue
            if symbol in reconciled:
                duplicate_prevented_count += 1
                broker_reconciled_count += 1
                continue
            recovered_pending_intents.append(
                RecoveredPendingIntent(
                    intent_id=intent_id,
                    strategy_tag=str(record.get("strategy_tag") or ""),
                    symbol=symbol,
                    trade_date=trade_date,
                    created_at=created_at or current_now,
                    expires_at=expires_at,
                    journal_state=state,
                    payload_hash=str(record.get("payload_hash") or ""),
                    payload_schema_version=str(record.get("payload_schema_version") or SCHEMA_VERSION),
                )
            )

        load_ms = (time.perf_counter() - started) * 1000.0
        result = RecoveryResult(
            recovered_pullback_candidates=pullback_candidates,
            recovered_shadow_candidates=shadow_candidates,
            recovered_pending_intents=recovered_pending_intents,
            finalized_or_submitted_intent_ids=finalized_or_submitted,
            dropped_stale_candidate_count=dropped_stale_candidate_count,
            dropped_stale_intent_count=dropped_stale_intent_count,
            duplicate_prevented_count=duplicate_prevented_count,
            corrupt_record_skipped_count=int(corrupt_intents + corrupt_orders),
            broker_reconciled_count=broker_reconciled_count,
            advisory_runtime_metadata=self._load_runtime_metadata(),
            load_ms=load_ms,
        )
        logger.info(
            "[PIPELINE_PERSIST] recovered pullback=%s shadow=%s intents=%s dropped_candidates=%s dropped_intents=%s duplicate_prevented=%s corrupt_skipped=%s load_ms=%.2f",
            len(result.recovered_pullback_candidates),
            len(result.recovered_shadow_candidates),
            len(result.recovered_pending_intents),
            result.dropped_stale_candidate_count,
            result.dropped_stale_intent_count,
            result.duplicate_prevented_count,
            result.corrupt_record_skipped_count,
            load_ms,
        )
        return result


class PipelinePersistenceThread(threading.Thread):
    def __init__(
        self,
        *,
        executor: Any,
        persistence_manager: StrategyPipelinePersistenceManager,
        candidate_store: Any,
        stop_event: threading.Event,
        on_error: Any,
    ) -> None:
        super().__init__(name="PipelinePersistenceThread", daemon=True)
        self._executor = executor
        self._persistence_manager = persistence_manager
        self._candidate_store = candidate_store
        self._stop_event = stop_event
        self._on_error = on_error

    def run(self) -> None:
        interval_sec = max(
            float(getattr(settings, "PIPELINE_CANDIDATE_SNAPSHOT_INTERVAL_SEC", 15) or 15.0),
            1.0,
        )
        while not self._stop_event.is_set():
            started = time.perf_counter()
            try:
                self._persistence_manager.maybe_save_candidate_snapshot(
                    executor=self._executor,
                    candidate_store=self._candidate_store,
                )
                self._persistence_manager.save_runtime_metadata(executor=self._executor)
                setattr(self._executor, "_pipeline_state_save_ms", (time.perf_counter() - started) * 1000.0)
            except Exception as exc:
                logger.error("[PIPELINE_PERSIST] worker_error=%s", exc)
                self._on_error(self.name, exc)
                return
            self._stop_event.wait(interval_sec)
        try:
            self._persistence_manager.maybe_save_candidate_snapshot(
                executor=self._executor,
                candidate_store=self._candidate_store,
                force=True,
            )
            self._persistence_manager.save_runtime_metadata(executor=self._executor)
        except Exception:
            logger.exception("[PIPELINE_PERSIST] final_flush_failed")
