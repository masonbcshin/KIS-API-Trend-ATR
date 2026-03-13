from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine.pullback_pipeline_models import (  # noqa: E402
    AuthoritativeEntryIntent,
    PullbackEntryIntent,
    PullbackSetupCandidate,
    StrategySetupCandidate,
)
from engine.pullback_pipeline_stores import ArmedCandidateStore  # noqa: E402
from engine.strategy_pipeline_persistence import (  # noqa: E402
    JournalWriteRequest,
    PipelinePersistenceThread,
    StrategyPipelinePersistenceManager,
)
from utils.market_hours import KST  # noqa: E402


def _kst_dt(hour: int = 9, minute: int = 5) -> datetime:
    return datetime(2026, 3, 11, hour, minute, tzinfo=KST)


def _make_pullback_candidate(symbol: str = "005930") -> PullbackSetupCandidate:
    created_at = _kst_dt(9, 5)
    return PullbackSetupCandidate(
        symbol=symbol,
        strategy_tag="pullback_rebreakout",
        created_at=created_at,
        expires_at=_kst_dt(9, 25),
        context_version="ctx-1",
        swing_high=50500.0,
        swing_low=49500.0,
        micro_high=50100.0,
        atr=1200.0,
        source="unit_test",
        extra_json={"signal_time": created_at.isoformat()},
    )


def _make_shadow_candidate(strategy_tag: str = "trend_atr", symbol: str = "005930") -> StrategySetupCandidate:
    created_at = _kst_dt(9, 6)
    return StrategySetupCandidate(
        strategy_tag=strategy_tag,
        symbol=symbol,
        created_at=created_at,
        expires_at=_kst_dt(9, 26),
        trade_date="2026-03-11",
        entry_reference_price=50100.0,
        entry_reference_label="prev_high",
        meta={"native_type": f"{strategy_tag}_setup_candidate"},
    )


def _make_authoritative_intent(strategy_tag: str = "pullback_rebreakout") -> AuthoritativeEntryIntent:
    created_at = _kst_dt(9, 7)
    native_payload = PullbackEntryIntent(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=created_at,
        candidate_created_at=_kst_dt(9, 5),
        expires_at=_kst_dt(9, 25),
        context_version="ctx-1",
        entry_reference_price=50100.0,
        source="fallback_daily",
        current_price=50150.0,
        meta={"entry_reference_label": "pullback_intraday_high"},
    )
    if strategy_tag != "pullback_rebreakout":
        native_payload = {
            "strategy_tag": strategy_tag,
            "symbol": "005930",
            "created_at": created_at.isoformat(),
        }
    return AuthoritativeEntryIntent(
        strategy_tag=strategy_tag,
        symbol="005930",
        created_at=created_at,
        expires_at=_kst_dt(9, 25),
        trade_date="2026-03-11",
        entry_reference_price=50100.0,
        entry_reference_label="prev_high",
        native_payload=native_payload,
        source="unit_test",
        meta={"entry_reference_label": "prev_high"},
    )


def _make_manager(tmp_path: Path) -> StrategyPipelinePersistenceManager:
    return StrategyPipelinePersistenceManager(
        state_dir=str(tmp_path),
        enabled=True,
        candidate_snapshot_interval_sec=15.0,
        intent_journal_enabled=True,
        intent_max_age_sec=120.0,
        candidate_max_recover_age_sec=300.0,
        recover_only_current_trade_date=True,
    )


def _flush_manager(manager: StrategyPipelinePersistenceManager) -> None:
    manager.drain_pending_writes()


def _make_runtime_source(symbol: str) -> tuple[SimpleNamespace, ArmedCandidateStore]:
    candidate_store = ArmedCandidateStore()
    candidate_store.upsert(_make_pullback_candidate(symbol=symbol))
    executor = SimpleNamespace(
        stock_code=symbol,
        snapshot_strategy_shadow_candidates=lambda: {},
        _trade_date_key=lambda dt: dt.date().isoformat(),
        _worker_health_state={},
        _worker_state_reason={},
        _worker_lag_sec={},
        _risk_snapshot_stale=False,
        _risk_snapshot_last_success_age_sec=-1.0,
        _strategy_regime_snapshot_state_used="absent",
        _pipeline_state_save_ms=0.0,
    )
    return executor, candidate_store


def test_candidate_snapshot_save_and_load_recovers_pullback_and_shadow_candidates(tmp_path: Path):
    manager = _make_manager(tmp_path)
    candidate_store = ArmedCandidateStore()
    pullback_candidate = _make_pullback_candidate()
    shadow_candidate = _make_shadow_candidate()
    candidate_store.upsert(pullback_candidate)
    executor = SimpleNamespace(
        snapshot_strategy_shadow_candidates=lambda: {"trend_atr:005930": shadow_candidate},
        _trade_date_key=lambda dt: dt.date().isoformat(),
    )

    assert manager.maybe_save_candidate_snapshot(
        executor=executor,
        candidate_store=candidate_store,
        now=_kst_dt(9, 10),
        force=True,
    ) is True

    recovery = manager.load_recovery_state(
        current_trade_date="2026-03-11",
        now=_kst_dt(9, 11),
        reconciled_symbols=(),
    )

    assert len(recovery.recovered_pullback_candidates) == 1
    assert len(recovery.recovered_shadow_candidates) == 1
    assert recovery.recovered_pullback_candidates[0].symbol == "005930"
    assert recovery.recovered_shadow_candidates[0].strategy_tag == "trend_atr"


def test_intent_and_order_journal_append_and_load_latest_state(tmp_path: Path):
    manager = _make_manager(tmp_path)
    intent = _make_authoritative_intent()
    manager.append_intent_state(intent=intent, journal_state="accepted", message="accepted", source="timing_worker")
    manager.append_order_state(
        intent=intent,
        journal_state="filled",
        message="filled",
        broker_order_id="A0001",
        source="order_worker",
    )
    _flush_manager(manager)

    recovery = manager.load_recovery_state(
        current_trade_date="2026-03-11",
        now=_kst_dt(9, 8),
        reconciled_symbols=(),
    )

    assert recovery.recovered_pending_intents == []
    assert manager.compute_intent_id(intent) in recovery.finalized_or_submitted_intent_ids


def test_stale_candidate_is_discarded_on_recovery(tmp_path: Path):
    manager = _make_manager(tmp_path)
    candidate_store = ArmedCandidateStore()
    stale_candidate = _make_pullback_candidate()
    candidate_store.upsert(stale_candidate)
    executor = SimpleNamespace(
        snapshot_strategy_shadow_candidates=lambda: {},
        _trade_date_key=lambda dt: dt.date().isoformat(),
    )
    manager.maybe_save_candidate_snapshot(
        executor=executor,
        candidate_store=candidate_store,
        now=_kst_dt(9, 10),
        force=True,
    )

    payload = manager.candidate_snapshot_path.read_text(encoding="utf-8")
    mutated = payload.replace(_kst_dt(9, 10).isoformat(), _kst_dt(8, 0).isoformat(), 1)
    manager.candidate_snapshot_path.write_text(mutated, encoding="utf-8")

    recovery = manager.load_recovery_state(
        current_trade_date="2026-03-11",
        now=_kst_dt(14, 30),
        reconciled_symbols=(),
    )

    assert recovery.recovered_pullback_candidates == []
    assert recovery.dropped_stale_candidate_count == 1


def test_current_trade_date_mismatch_candidate_is_discarded(tmp_path: Path):
    manager = _make_manager(tmp_path)
    candidate_store = ArmedCandidateStore()
    prior_day_candidate = PullbackSetupCandidate(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=datetime(2026, 3, 10, 15, 0, tzinfo=KST),
        expires_at=datetime(2026, 3, 10, 15, 30, tzinfo=KST),
        context_version="ctx-prior",
        swing_high=50500.0,
        swing_low=49500.0,
        micro_high=50100.0,
        atr=1200.0,
        source="unit_test",
        extra_json={"signal_time": "2026-03-10T15:00:00+09:00"},
    )
    candidate_store.upsert(prior_day_candidate)
    executor = SimpleNamespace(
        snapshot_strategy_shadow_candidates=lambda: {},
        _trade_date_key=lambda dt: dt.date().isoformat(),
    )
    manager.maybe_save_candidate_snapshot(
        executor=executor,
        candidate_store=candidate_store,
        now=datetime(2026, 3, 10, 15, 1, tzinfo=KST),
        force=True,
    )

    recovery = manager.load_recovery_state(
        current_trade_date="2026-03-11",
        now=_kst_dt(9, 10),
        reconciled_symbols=(),
    )

    assert recovery.recovered_pullback_candidates == []
    assert recovery.dropped_stale_candidate_count == 1


def test_partial_corrupt_tail_journal_recovery_skips_tail(tmp_path: Path):
    manager = _make_manager(tmp_path)
    intent = _make_authoritative_intent()
    manager.append_intent_state(intent=intent, journal_state="accepted", message="accepted", source="timing_worker")
    _flush_manager(manager)
    with manager.intent_journal_path.open("a", encoding="utf-8") as fh:
        fh.write("{broken-json-tail\n")

    recovery = manager.load_recovery_state(
        current_trade_date="2026-03-11",
        now=_kst_dt(9, 9),
        reconciled_symbols=(),
    )

    assert len(recovery.recovered_pending_intents) == 1
    assert recovery.corrupt_record_skipped_count == 1


def test_shared_persistence_writer_handles_multiple_runtime_sources_with_one_thread(tmp_path: Path):
    manager = _make_manager(tmp_path)
    first_executor, first_store = _make_runtime_source("005930")
    second_executor, second_store = _make_runtime_source("000660")
    manager.register_runtime_source(owner_key="executor:005930", executor=first_executor, candidate_store=first_store)
    manager.register_runtime_source(owner_key="executor:000660", executor=second_executor, candidate_store=second_store)

    stop_event = threading.Event()
    worker = PipelinePersistenceThread(
        persistence_manager=manager,
        stop_event=stop_event,
    )
    worker.start()
    deadline = time.time() + 2.0
    while not manager.writer_started and time.time() < deadline:
        time.sleep(0.05)

    assert manager.writer_started is True
    assert manager.registered_source_count() == 2

    stop_event.set()
    worker.join(timeout=5.0)

    snapshot_payload = json.loads(manager.candidate_snapshot_path.read_text(encoding="utf-8"))
    recovered_symbols = {str(record.get("symbol") or "") for record in list(snapshot_payload.get("records") or [])}
    assert recovered_symbols == {"000660", "005930"}


def test_load_recovery_state_once_only_reads_disk_once(tmp_path: Path):
    manager = _make_manager(tmp_path)
    candidate_store = ArmedCandidateStore()
    candidate_store.upsert(_make_pullback_candidate())
    executor = SimpleNamespace(
        snapshot_strategy_shadow_candidates=lambda: {},
        _trade_date_key=lambda dt: dt.date().isoformat(),
    )
    manager.maybe_save_candidate_snapshot(
        executor=executor,
        candidate_store=candidate_store,
        now=_kst_dt(9, 10),
        force=True,
    )

    with patch.object(manager, "load_recovery_state", wraps=manager.load_recovery_state) as wrapped:
        first = manager.load_recovery_state_once(
            current_trade_date="2026-03-11",
            now=_kst_dt(9, 11),
            reconciled_symbols=(),
        )
        second = manager.load_recovery_state_once(
            current_trade_date="2026-03-11",
            now=_kst_dt(9, 12),
            reconciled_symbols=(),
        )

    assert wrapped.call_count == 1
    assert first == second


def test_atomic_snapshot_write_uses_unique_tmp_names_under_concurrent_calls(tmp_path: Path):
    manager = _make_manager(tmp_path)
    target_path = tmp_path / "candidate_snapshot.json"
    seen_tmp_paths: list[str] = []
    original_replace = Path.replace

    def _capturing_replace(self: Path, target: Path):
        seen_tmp_paths.append(str(self))
        return original_replace(self, target)

    payloads = [{"index": index, "saved_at": _kst_dt(9, 10).isoformat()} for index in range(4)]
    with patch.object(Path, "replace", autospec=True, side_effect=_capturing_replace):
        threads = [
            threading.Thread(target=manager._atomic_write_json, args=(target_path, payload))
            for payload in payloads
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

    assert len(seen_tmp_paths) == len(payloads)
    assert len(set(seen_tmp_paths)) == len(payloads)
    assert not list(tmp_path.glob("candidate_snapshot.json.*.tmp"))


def test_journal_append_writes_jsonl_line_and_newline_in_single_write(tmp_path: Path):
    manager = _make_manager(tmp_path)
    intent = _make_authoritative_intent()
    request = JournalWriteRequest(
        journal_kind="intent",
        record=manager._journal_record(
            intent=intent,
            journal_state="accepted",
            message="accepted",
            source="timing_worker",
        ),
        flush=False,
    )

    class _FakeFile:
        def __init__(self) -> None:
            self.writes: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write(self, value: str) -> int:
            self.writes.append(value)
            return len(value)

        def flush(self) -> None:
            return None

    fake_file = _FakeFile()
    with patch.object(type(manager.intent_journal_path), "open", autospec=True, return_value=fake_file):
        manager._process_journal_request(request)

    assert len(fake_file.writes) == 1
    assert fake_file.writes[0].endswith("\n")


def test_startup_logging_reports_effective_state_dir_and_writer_started(tmp_path: Path):
    manager = _make_manager(tmp_path)

    with patch("engine.strategy_pipeline_persistence.logger.info") as info_log:
        assert manager.prepare_process_global_writer() is True
        manager.mark_process_global_writer_started(thread_name="PipelinePersistenceThread")

    messages = [str(call.args[0]) for call in info_log.call_args_list]
    assert any("startup enabled=%s state_dir=%s" in message for message in messages)
    assert any("process_global_writer started" in message for message in messages)
