from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import engine.multiday_executor as multiday_executor  # noqa: E402
from engine.multiday_executor import MultidayExecutor  # noqa: E402
from engine.pullback_pipeline_models import AuthoritativeEntryIntent, PullbackEntryIntent  # noqa: E402
from engine.pullback_pipeline_stores import ArmedCandidateStore, EntryIntentQueue  # noqa: E402
from engine.pullback_pipeline_workers import OrderExecutionWorker  # noqa: E402
from engine.strategy_pipeline_persistence import StrategyPipelinePersistenceManager  # noqa: E402
from utils.market_hours import KST  # noqa: E402


def _kst_dt(hour: int = 9, minute: int = 5) -> datetime:
    return datetime(2026, 3, 11, hour, minute, tzinfo=KST)


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


def _make_authoritative_intent(
    *,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> AuthoritativeEntryIntent:
    created_at = created_at or _kst_dt(9, 7)
    expires_at = expires_at or _kst_dt(9, 25)
    native_payload = PullbackEntryIntent(
        symbol="005930",
        strategy_tag="pullback_rebreakout",
        created_at=created_at,
        candidate_created_at=_kst_dt(9, 5),
        expires_at=expires_at,
        context_version="ctx-1",
        entry_reference_price=50100.0,
        source="fallback_daily",
        current_price=50150.0,
        meta={"entry_reference_label": "pullback_intraday_high"},
    )
    return AuthoritativeEntryIntent(
        strategy_tag="pullback_rebreakout",
        symbol="005930",
        created_at=created_at,
        expires_at=expires_at,
        trade_date="2026-03-11",
        entry_reference_price=50100.0,
        entry_reference_label="pullback_intraday_high",
        native_payload=native_payload,
        source="unit_test",
        meta={"entry_reference_label": "pullback_intraday_high"},
    )


def _make_fake_recovery_executor(manager: StrategyPipelinePersistenceManager):
    shadow_candidates = {}
    executor = SimpleNamespace()
    executor._pipeline_persistence_manager = manager
    executor._pullback_candidate_store = ArmedCandidateStore()
    executor._pipeline_recovered_pending_intents = []
    executor._pipeline_advisory_runtime_metadata = {}
    executor._pipeline_state_load_ms = 0.0
    executor._recovered_candidate_count = 0
    executor._recovered_intent_count = 0
    executor._dropped_stale_candidate_count = 0
    executor._dropped_stale_intent_count = 0
    executor._recovery_duplicate_prevented_count = 0
    executor._recovery_corrupt_record_skipped_count = 0
    executor._recovery_broker_reconciled_count = 0
    executor.stock_code = "005930"
    executor.strategy = SimpleNamespace(has_position=False, position=None)
    executor._has_active_pending_buy_order = lambda: False
    executor.cached_account_has_holding = lambda symbol: False
    executor._trade_date_key = lambda dt: dt.date().isoformat()
    executor._normalize_pullback_refresh_symbol = lambda raw: MultidayExecutor._normalize_pullback_refresh_symbol(raw)
    executor._pipeline_reconciled_symbols = lambda: set()
    executor.upsert_strategy_shadow_candidate = lambda strategy_tag, symbol, candidate: shadow_candidates.__setitem__(
        f"{strategy_tag}:{symbol}", candidate
    )
    executor.snapshot_strategy_shadow_candidates = lambda: dict(shadow_candidates)
    return executor


def test_restart_recovery_prevents_duplicates_via_reconciliation(tmp_path: Path):
    manager = _make_manager(tmp_path)
    intent = _make_authoritative_intent()
    manager.append_intent_state(intent=intent, journal_state="accepted", message="accepted", source="timing_worker")

    recovery = manager.load_recovery_state(
        current_trade_date="2026-03-11",
        now=_kst_dt(9, 9),
        reconciled_symbols={"005930"},
    )

    assert recovery.recovered_pending_intents == []
    assert recovery.duplicate_prevented_count == 1
    assert recovery.broker_reconciled_count == 1


def test_recovered_pending_intent_is_not_immediately_auto_requeued(tmp_path: Path):
    manager = _make_manager(tmp_path)
    live_now = datetime.now(KST).replace(second=0, microsecond=0)
    intent = _make_authoritative_intent(
        created_at=live_now,
        expires_at=live_now + multiday_executor.timedelta(minutes=10),
    )
    manager.append_intent_state(intent=intent, journal_state="accepted", message="accepted", source="timing_worker")

    executor = _make_fake_recovery_executor(manager)
    executor._pullback_entry_queue = EntryIntentQueue(maxsize=32)

    MultidayExecutor._restore_persisted_pipeline_state(executor)

    assert len(executor._pipeline_recovered_pending_intents) == 1
    assert executor._pullback_entry_queue.qsize() == 0


def test_strategy_native_expiry_authority_is_preserved_for_recovered_intents(tmp_path: Path):
    manager = _make_manager(tmp_path)
    intent = _make_authoritative_intent()
    expired_intent = AuthoritativeEntryIntent(
        strategy_tag=intent.strategy_tag,
        symbol=intent.symbol,
        created_at=intent.created_at,
        expires_at=_kst_dt(9, 6),
        trade_date=intent.trade_date,
        entry_reference_price=intent.entry_reference_price,
        entry_reference_label=intent.entry_reference_label,
        native_payload=intent.native_payload,
        source=intent.source,
        meta=intent.meta,
    )
    manager.append_intent_state(
        intent=expired_intent,
        journal_state="accepted",
        message="accepted",
        source="timing_worker",
    )

    recovery = manager.load_recovery_state(
        current_trade_date="2026-03-11",
        now=_kst_dt(9, 9),
        reconciled_symbols=(),
    )

    assert recovery.recovered_pending_intents == []
    assert recovery.dropped_stale_intent_count == 1


def test_single_writer_order_worker_writes_order_journal(tmp_path: Path):
    manager = _make_manager(tmp_path)
    intent = _make_authoritative_intent()
    manager.append_intent_state(intent=intent, journal_state="accepted", message="accepted", source="timing_worker")

    executor = SimpleNamespace(
        _pipeline_persistence_manager=manager,
        strategy=SimpleNamespace(has_position=False, position=None),
        _authoritative_intent_consumed_count_by_strategy={},
        _mixed_strategy_dedupe_count=0,
        _authoritative_intent_reject_reason="",
        _authoritative_order_handoff_path="",
        _strategy_end_to_end_latency_ms=0.0,
        _pullback_end_to_end_latency_ms=0.0,
        _has_active_pending_buy_order=lambda: False,
        cached_account_has_holding=lambda symbol: False,
    )
    candidate_store = ArmedCandidateStore()
    queue = EntryIntentQueue(maxsize=16)
    worker = OrderExecutionWorker(
        executor=executor,
        candidate_store=candidate_store,
        entry_queue=queue,
        stop_event=threading.Event(),
        on_error=lambda *_args: None,
    )
    worker._dispatch_intent = lambda _intent: {  # type: ignore[method-assign]
        "success": True,
        "order_no": "B-0001",
        "exec_qty": 1,
        "message": "filled",
    }
    worker._now = lambda: _kst_dt(9, 8)  # type: ignore[method-assign]

    worker._process_intent(intent)

    journal_lines = manager.order_journal_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(journal_lines) == 1
    assert "\"journal_state\":\"filled\"" in journal_lines[0]


def test_pipeline_state_persistence_flag_off_preserves_existing_behavior():
    executor = MultidayExecutor.__new__(MultidayExecutor)
    executor._threaded_pullback_pipeline_disabled = False

    with patch.object(multiday_executor.settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", True), \
         patch.object(multiday_executor.settings, "ENABLE_MULTI_STRATEGY_THREADED_PIPELINE", True), \
         patch.object(multiday_executor.settings, "THREADED_PIPELINE_ENABLED_STRATEGIES", "pullback_rebreakout"), \
         patch.object(multiday_executor.settings, "ENABLE_PIPELINE_STATE_PERSISTENCE", False):
        assert executor._is_multi_strategy_threaded_pipeline_enabled() is True
        assert executor._is_pipeline_state_persistence_enabled() is False
