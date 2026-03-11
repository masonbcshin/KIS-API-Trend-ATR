from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine.pullback_pipeline_models import (  # noqa: E402
    AuthoritativeEntryIntent,
    PullbackEntryIntent,
    PullbackSetupCandidate,
    StrategySetupCandidate,
)
from engine.pullback_pipeline_stores import ArmedCandidateStore  # noqa: E402
from engine.strategy_pipeline_persistence import StrategyPipelinePersistenceManager  # noqa: E402
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
    with manager.intent_journal_path.open("a", encoding="utf-8") as fh:
        fh.write("{broken-json-tail\n")

    recovery = manager.load_recovery_state(
        current_trade_date="2026-03-11",
        now=_kst_dt(9, 9),
        reconciled_symbols=(),
    )

    assert len(recovery.recovered_pending_intents) == 1
    assert recovery.corrupt_record_skipped_count == 1
