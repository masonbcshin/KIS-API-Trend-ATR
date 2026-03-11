from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import threading
import time
from typing import Any, Callable, Dict, Optional

try:
    from utils.logger import get_logger
    from utils.market_hours import KST
except ImportError:
    from kis_trend_atr_trading.utils.logger import get_logger
    from kis_trend_atr_trading.utils.market_hours import KST


logger = get_logger("strategy_pipeline_health")


class WorkerState:
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALLED = "STALLED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class WorkerHealthSnapshot:
    worker_name: str
    state: str = WorkerState.HEALTHY
    state_reason: str = ""
    last_heartbeat_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error_at: Optional[datetime] = None
    last_error: str = ""
    processed_count: int = 0
    dropped_count: int = 0
    avg_eval_ms: float = 0.0
    queue_depth_seen: int = 0
    lag_sec: float = 0.0
    stall_after_sec: float = 20.0


class WorkerHealthStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: Dict[str, WorkerHealthSnapshot] = {}

    def ensure_worker(self, worker_name: str, *, stall_after_sec: float = 20.0) -> None:
        now = datetime.now(KST)
        with self._lock:
            if worker_name in self._snapshots:
                current = self._snapshots[worker_name]
                self._snapshots[worker_name] = replace(
                    current,
                    stall_after_sec=max(float(stall_after_sec or 0.0), 1.0),
                )
                return
            self._snapshots[worker_name] = WorkerHealthSnapshot(
                worker_name=str(worker_name or "").strip(),
                last_heartbeat_at=now,
                last_success_at=None,
                stall_after_sec=max(float(stall_after_sec or 0.0), 1.0),
            )

    def _update_locked(
        self,
        worker_name: str,
        *,
        now: datetime,
        state: Optional[str] = None,
        state_reason: Optional[str] = None,
        queue_depth: Optional[int] = None,
        processed_delta: int = 0,
        dropped_delta: int = 0,
        avg_eval_ms: Optional[float] = None,
        success: bool = False,
        error: Optional[str] = None,
    ) -> WorkerHealthSnapshot:
        current = self._snapshots.get(worker_name)
        if current is None:
            current = WorkerHealthSnapshot(
                worker_name=str(worker_name or "").strip(),
                last_heartbeat_at=now,
                stall_after_sec=20.0,
            )
        processed_count = int(current.processed_count or 0) + max(int(processed_delta or 0), 0)
        dropped_count = int(current.dropped_count or 0) + max(int(dropped_delta or 0), 0)
        new_avg_eval_ms = float(current.avg_eval_ms or 0.0)
        if avg_eval_ms is not None:
            sample = max(float(avg_eval_ms or 0.0), 0.0)
            prior_count = max(int(current.processed_count or 0), 0)
            sample_weight = max(int(processed_delta or 0), 1)
            new_avg_eval_ms = (
                ((float(current.avg_eval_ms or 0.0) * prior_count) + (sample * sample_weight))
                / max(prior_count + sample_weight, 1)
            )
        next_state = state or current.state
        next_reason = state_reason if state_reason is not None else current.state_reason
        last_success_at = now if success else current.last_success_at
        last_error_at = now if error else current.last_error_at
        last_error = str(error or current.last_error or "")
        updated = replace(
            current,
            state=str(next_state or WorkerState.HEALTHY),
            state_reason=str(next_reason or ""),
            last_heartbeat_at=now,
            last_success_at=last_success_at,
            last_error_at=last_error_at,
            last_error=last_error,
            processed_count=processed_count,
            dropped_count=dropped_count,
            avg_eval_ms=new_avg_eval_ms,
            queue_depth_seen=int(queue_depth if queue_depth is not None else current.queue_depth_seen or 0),
        )
        self._snapshots[worker_name] = updated
        return updated

    def heartbeat(
        self,
        worker_name: str,
        *,
        queue_depth: Optional[int] = None,
        processed_delta: int = 0,
        dropped_delta: int = 0,
        avg_eval_ms: Optional[float] = None,
        state_reason: Optional[str] = None,
    ) -> WorkerHealthSnapshot:
        now = datetime.now(KST)
        with self._lock:
            current = self._snapshots.get(worker_name)
            state = None
            if current is not None and current.state == WorkerState.STALLED:
                state = WorkerState.HEALTHY
                if state_reason is None:
                    state_reason = "heartbeat_recovered"
            return self._update_locked(
                worker_name,
                now=now,
                state=state,
                state_reason=state_reason,
                queue_depth=queue_depth,
                processed_delta=processed_delta,
                dropped_delta=dropped_delta,
                avg_eval_ms=avg_eval_ms,
            )

    def mark_success(
        self,
        worker_name: str,
        *,
        queue_depth: Optional[int] = None,
        processed_delta: int = 1,
        avg_eval_ms: Optional[float] = None,
        state_reason: str = "ok",
    ) -> WorkerHealthSnapshot:
        now = datetime.now(KST)
        with self._lock:
            return self._update_locked(
                worker_name,
                now=now,
                state=WorkerState.HEALTHY,
                state_reason=state_reason,
                queue_depth=queue_depth,
                processed_delta=processed_delta,
                avg_eval_ms=avg_eval_ms,
                success=True,
            )

    def mark_error(self, worker_name: str, error: Any, *, state_reason: str = "worker_error") -> WorkerHealthSnapshot:
        now = datetime.now(KST)
        with self._lock:
            return self._update_locked(
                worker_name,
                now=now,
                state=WorkerState.DEGRADED,
                state_reason=state_reason,
                error=str(error or ""),
            )

    def mark_stopped(self, worker_name: str, *, state_reason: str = "stopped") -> WorkerHealthSnapshot:
        now = datetime.now(KST)
        with self._lock:
            return self._update_locked(
                worker_name,
                now=now,
                state=WorkerState.STOPPED,
                state_reason=state_reason,
            )

    def evaluate(self, *, now: Optional[datetime] = None) -> Dict[str, WorkerHealthSnapshot]:
        current_now = now or datetime.now(KST)
        with self._lock:
            evaluated: Dict[str, WorkerHealthSnapshot] = {}
            for worker_name, snapshot in list(self._snapshots.items()):
                lag_sec = 0.0
                if isinstance(snapshot.last_heartbeat_at, datetime):
                    lag_sec = max((current_now - snapshot.last_heartbeat_at).total_seconds(), 0.0)
                state = snapshot.state
                state_reason = snapshot.state_reason
                if state != WorkerState.STOPPED:
                    if lag_sec > max(float(snapshot.stall_after_sec or 0.0), 1.0):
                        state = WorkerState.STALLED
                        state_reason = f"stall_lag>{float(snapshot.stall_after_sec or 0.0):.1f}s"
                    elif state == WorkerState.STALLED:
                        state = WorkerState.HEALTHY
                        state_reason = "recovered"
                    elif snapshot.last_error and snapshot.last_error_at is not None:
                        state = WorkerState.DEGRADED
                        if not state_reason:
                            state_reason = "last_error_present"
                    else:
                        state = WorkerState.HEALTHY
                        if not state_reason:
                            state_reason = "ok"
                updated = replace(
                    snapshot,
                    state=state,
                    state_reason=state_reason,
                    lag_sec=float(lag_sec),
                )
                self._snapshots[worker_name] = updated
                evaluated[worker_name] = updated
            return evaluated

    def snapshot(self, worker_name: str) -> Optional[WorkerHealthSnapshot]:
        with self._lock:
            return self._snapshots.get(worker_name)


@dataclass(frozen=True)
class DegradedModeSnapshot:
    is_degraded: bool = False
    entered_at: Optional[datetime] = None
    last_transition_at: Optional[datetime] = None
    state_reason: str = ""
    transitions: int = 0


class DegradedModeController:
    def __init__(
        self,
        *,
        enabled: bool,
        enter_queue_depth: int,
        exit_queue_depth: int,
        min_hold_sec: float,
    ) -> None:
        self._enabled = bool(enabled)
        self._enter_queue_depth = max(int(enter_queue_depth or 0), 1)
        self._exit_queue_depth = max(int(exit_queue_depth or 0), 0)
        self._min_hold_sec = max(float(min_hold_sec or 0.0), 0.0)
        self._lock = threading.Lock()
        self._snapshot = DegradedModeSnapshot()

    def current(self) -> DegradedModeSnapshot:
        with self._lock:
            return self._snapshot

    def is_degraded(self) -> bool:
        with self._lock:
            return bool(self._snapshot.is_degraded)

    @staticmethod
    def _stalled_workers(worker_snapshots: Dict[str, WorkerHealthSnapshot]) -> list[str]:
        names = []
        for worker_name, snapshot in dict(worker_snapshots or {}).items():
            if str(getattr(snapshot, "state", "") or "") == WorkerState.STALLED:
                names.append(str(worker_name))
        return sorted(names)

    def evaluate(
        self,
        *,
        queue_depth: int,
        worker_snapshots: Dict[str, WorkerHealthSnapshot],
        now: Optional[datetime] = None,
    ) -> DegradedModeSnapshot:
        current_now = now or datetime.now(KST)
        stalled_workers = self._stalled_workers(worker_snapshots)
        enter_reason = ""
        if stalled_workers:
            enter_reason = f"worker_stalled:{','.join(stalled_workers)}"
        elif int(queue_depth or 0) >= self._enter_queue_depth:
            enter_reason = f"queue_depth={int(queue_depth or 0)}"

        with self._lock:
            snapshot = self._snapshot
            if not self._enabled:
                if snapshot.is_degraded:
                    self._snapshot = DegradedModeSnapshot(
                        is_degraded=False,
                        entered_at=None,
                        last_transition_at=current_now,
                        state_reason="disabled",
                        transitions=int(snapshot.transitions or 0) + 1,
                    )
                return self._snapshot

            if not snapshot.is_degraded and enter_reason:
                self._snapshot = DegradedModeSnapshot(
                    is_degraded=True,
                    entered_at=current_now,
                    last_transition_at=current_now,
                    state_reason=enter_reason,
                    transitions=int(snapshot.transitions or 0) + 1,
                )
                return self._snapshot

            if not snapshot.is_degraded:
                return snapshot

            hold_elapsed_sec = 0.0
            if isinstance(snapshot.entered_at, datetime):
                hold_elapsed_sec = max((current_now - snapshot.entered_at).total_seconds(), 0.0)
            if enter_reason:
                if snapshot.state_reason != enter_reason:
                    self._snapshot = replace(snapshot, state_reason=enter_reason)
                return self._snapshot
            if hold_elapsed_sec < self._min_hold_sec:
                return self._snapshot
            if int(queue_depth or 0) > self._exit_queue_depth:
                return self._snapshot

            self._snapshot = DegradedModeSnapshot(
                is_degraded=False,
                entered_at=None,
                last_transition_at=current_now,
                state_reason="recovered",
                transitions=int(snapshot.transitions or 0) + 1,
            )
            return self._snapshot


class PipelineHealthMonitorThread(threading.Thread):
    def __init__(
        self,
        *,
        executor: Any,
        health_store: WorkerHealthStore,
        degraded_controller: Optional[DegradedModeController],
        entry_queue: Any,
        dirty_symbols: Any,
        candidate_cleanup: Optional[Callable[[datetime], Dict[str, int]]],
        stop_event: threading.Event,
        on_error: Callable[[str, Exception], None],
    ) -> None:
        super().__init__(name="PipelineHealthMonitorThread", daemon=True)
        self._executor = executor
        self._health_store = health_store
        self._degraded_controller = degraded_controller
        self._entry_queue = entry_queue
        self._dirty_symbols = dirty_symbols
        self._candidate_cleanup = candidate_cleanup
        self._stop_event = stop_event
        self._on_error = on_error
        self._last_cleanup_mono: float = 0.0

    def run(self) -> None:
        interval_sec = max(float(getattr(self._executor, "_pipeline_worker_heartbeat_sec", 5.0) or 5.0), 1.0)
        while not self._stop_event.is_set():
            started = time.perf_counter()
            try:
                self._run_cycle()
                self._health_store.mark_success(
                    self.name,
                    queue_depth=int(getattr(self._entry_queue, "qsize", lambda: 0)() or 0),
                    processed_delta=1,
                    avg_eval_ms=(time.perf_counter() - started) * 1000.0,
                    state_reason="monitor_cycle",
                )
            except Exception as exc:
                self._health_store.mark_error(self.name, exc)
                logger.error("[PIPELINE_HEALTH] monitor_error=%s", exc)
                self._on_error(self.name, exc)
                return
            self._stop_event.wait(interval_sec)
        self._health_store.mark_stopped(self.name, state_reason="stop_event_set")

    def _maybe_cleanup_candidates(self, *, now: datetime) -> Dict[str, int]:
        interval_sec = max(float(getattr(self._executor, "_candidate_cleanup_interval_sec", 30.0) or 30.0), 1.0)
        current_mono = time.monotonic()
        if self._candidate_cleanup is None:
            return {}
        if self._last_cleanup_mono and (current_mono - self._last_cleanup_mono) < interval_sec:
            return {}
        self._last_cleanup_mono = current_mono
        return dict(self._candidate_cleanup(now) or {})

    def _publish_metrics(
        self,
        *,
        queue_depth: int,
        dirty_count: int,
        worker_snapshots: Dict[str, WorkerHealthSnapshot],
        degraded_snapshot: Optional[DegradedModeSnapshot],
        cleanup_stats: Dict[str, int],
    ) -> None:
        setattr(
            self._executor,
            "_worker_health_state",
            {name: snapshot.state for name, snapshot in dict(worker_snapshots or {}).items()},
        )
        setattr(
            self._executor,
            "_worker_state_reason",
            {name: snapshot.state_reason for name, snapshot in dict(worker_snapshots or {}).items()},
        )
        setattr(
            self._executor,
            "_worker_lag_sec",
            {name: float(snapshot.lag_sec or 0.0) for name, snapshot in dict(worker_snapshots or {}).items()},
        )
        setattr(self._executor, "_dirty_symbol_count", int(dirty_count or 0))
        dropped_count = int(getattr(self._entry_queue, "dropped_count", lambda: 0)() or 0)
        setattr(self._executor, "_dropped_intent_count", dropped_count)
        setattr(self._executor, "_candidate_cleanup_stats", dict(cleanup_stats or {}))
        if degraded_snapshot is not None:
            setattr(self._executor, "_degraded_mode_current", bool(degraded_snapshot.is_degraded))
            setattr(self._executor, "_degraded_mode_transitions", int(degraded_snapshot.transitions or 0))
            setattr(self._executor, "_degraded_mode_reason", str(degraded_snapshot.state_reason or ""))
        else:
            setattr(self._executor, "_degraded_mode_current", False)
            setattr(self._executor, "_degraded_mode_transitions", 0)
            setattr(self._executor, "_degraded_mode_reason", "")
        setattr(self._executor, "_authoritative_intent_queue_depth", int(queue_depth or 0))
        queue_strategy_counts = getattr(self._entry_queue, "strategy_counts", None)
        if callable(queue_strategy_counts):
            setattr(
                self._executor,
                "_authoritative_intent_queue_depth_by_strategy",
                dict(queue_strategy_counts() or {}),
            )

    def _run_cycle(self) -> None:
        now = datetime.now(KST)
        queue_depth = int(getattr(self._entry_queue, "qsize", lambda: 0)() or 0)
        dirty_count = int(getattr(self._dirty_symbols, "size", lambda: 0)() or 0)
        worker_snapshots = self._health_store.evaluate(now=now)
        degraded_snapshot = None
        if self._degraded_controller is not None:
            degraded_snapshot = self._degraded_controller.evaluate(
                queue_depth=queue_depth,
                worker_snapshots=worker_snapshots,
                now=now,
            )
        cleanup_stats = self._maybe_cleanup_candidates(now=now)
        self._publish_metrics(
            queue_depth=queue_depth,
            dirty_count=dirty_count,
            worker_snapshots=worker_snapshots,
            degraded_snapshot=degraded_snapshot,
            cleanup_stats=cleanup_stats,
        )
        logger.debug(
            "[PIPELINE_HEALTH] queue_depth=%s dirty=%s degraded=%s worker_states=%s",
            queue_depth,
            dirty_count,
            bool(getattr(degraded_snapshot, "is_degraded", False)),
            {name: snapshot.state for name, snapshot in worker_snapshots.items()},
        )
