from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class DailyContext:
    symbol: str
    trade_date: str
    context_version: str
    recent_bars: Tuple[Dict[str, Any], ...]
    prev_high: float
    prev_close: float
    atr: float
    adx: float
    trend: str
    ma20: float
    ma50: float
    swing_high: float
    swing_low: float
    refreshed_at: datetime
    source: str


@dataclass(frozen=True)
class PullbackSetupCandidate:
    symbol: str
    strategy_tag: str
    created_at: datetime
    expires_at: datetime
    context_version: str
    swing_high: float
    swing_low: float
    micro_high: float
    atr: float
    source: str
    extra_json: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class StrategySetupCandidate:
    strategy_tag: str
    symbol: str
    created_at: datetime
    expires_at: datetime
    trade_date: str
    entry_reference_price: float
    entry_reference_label: str
    meta: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = "v1"


@dataclass(frozen=True)
class PullbackTimingDecision:
    should_emit_intent: bool = False
    reason: str = ""
    reason_code: str = ""
    timing_source: str = ""
    invalidate_candidate: bool = False
    entry_reference_price: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyTimingDecision:
    strategy_tag: str
    symbol: str
    created_at: datetime
    expires_at: datetime
    trade_date: str
    entry_reference_price: float
    entry_reference_label: str
    should_emit_intent: bool = False
    reason: str = ""
    reason_code: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = "v1"


@dataclass(frozen=True)
class PullbackEntryIntent:
    symbol: str
    strategy_tag: str
    created_at: datetime
    candidate_created_at: datetime
    expires_at: datetime
    context_version: str
    entry_reference_price: float
    source: str
    current_price: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def intent_key(self) -> str:
        return f"{self.strategy_tag}:{self.symbol}"


@dataclass(frozen=True)
class StrategyEntryIntent:
    strategy_tag: str
    symbol: str
    created_at: datetime
    expires_at: datetime
    trade_date: str
    entry_reference_price: float
    entry_reference_label: str
    meta: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = "v1"


def _trade_date_from_datetime(value: datetime) -> str:
    return value.date().isoformat() if isinstance(value, datetime) else ""


def strategy_setup_candidate_from_pullback(candidate: PullbackSetupCandidate) -> StrategySetupCandidate:
    extra_meta = dict(candidate.extra_json or {})
    signal_time = str(extra_meta.get("signal_time") or "").strip()
    trade_date = signal_time[:10] if len(signal_time) >= 10 else _trade_date_from_datetime(candidate.created_at)
    meta = {
        "native_type": "pullback_setup_candidate",
        "context_version": str(candidate.context_version or ""),
        "swing_high": float(candidate.swing_high or 0.0),
        "swing_low": float(candidate.swing_low or 0.0),
        "micro_high": float(candidate.micro_high or 0.0),
        "atr": float(candidate.atr or 0.0),
        "source": str(candidate.source or ""),
        "extra_json": extra_meta,
    }
    return StrategySetupCandidate(
        strategy_tag=str(candidate.strategy_tag or ""),
        symbol=str(candidate.symbol).zfill(6),
        created_at=candidate.created_at,
        expires_at=candidate.expires_at,
        trade_date=trade_date,
        entry_reference_price=float(candidate.micro_high or 0.0),
        entry_reference_label="pullback_micro_high",
        meta=meta,
    )


def pullback_setup_candidate_from_strategy(candidate: StrategySetupCandidate) -> PullbackSetupCandidate:
    meta = dict(candidate.meta or {})
    extra_json = dict(meta.get("extra_json") or {})
    return PullbackSetupCandidate(
        symbol=str(candidate.symbol).zfill(6),
        strategy_tag=str(candidate.strategy_tag or ""),
        created_at=candidate.created_at,
        expires_at=candidate.expires_at,
        context_version=str(meta.get("context_version") or ""),
        swing_high=float(meta.get("swing_high", 0.0) or 0.0),
        swing_low=float(meta.get("swing_low", 0.0) or 0.0),
        micro_high=float(meta.get("micro_high", candidate.entry_reference_price) or 0.0),
        atr=float(meta.get("atr", 0.0) or 0.0),
        source=str(meta.get("source") or "registry_adapter"),
        extra_json=extra_json,
    )


def strategy_timing_decision_from_pullback(
    *,
    candidate: PullbackSetupCandidate,
    decision: PullbackTimingDecision,
) -> StrategyTimingDecision:
    candidate_meta = dict(candidate.extra_json or {})
    decision_meta = dict(decision.meta or {})
    signal_time = str(candidate_meta.get("signal_time") or "").strip()
    trade_date = signal_time[:10] if len(signal_time) >= 10 else _trade_date_from_datetime(candidate.created_at)
    meta = {
        "native_type": "pullback_timing_decision",
        "timing_source": str(decision.timing_source or ""),
        "invalidate_candidate": bool(decision.invalidate_candidate),
        "candidate_context_version": str(candidate.context_version or ""),
        "candidate_extra_json": candidate_meta,
        "decision_meta": decision_meta,
    }
    return StrategyTimingDecision(
        strategy_tag=str(candidate.strategy_tag or ""),
        symbol=str(candidate.symbol).zfill(6),
        created_at=candidate.created_at,
        expires_at=candidate.expires_at,
        trade_date=trade_date,
        entry_reference_price=float(decision.entry_reference_price or candidate.micro_high or 0.0),
        entry_reference_label="pullback_intraday_high",
        should_emit_intent=bool(decision.should_emit_intent),
        reason=str(decision.reason or ""),
        reason_code=str(decision.reason_code or ""),
        meta=meta,
    )


def pullback_timing_decision_from_strategy(decision: StrategyTimingDecision) -> PullbackTimingDecision:
    meta = dict(decision.meta or {})
    return PullbackTimingDecision(
        should_emit_intent=bool(decision.should_emit_intent),
        reason=str(decision.reason or ""),
        reason_code=str(decision.reason_code or ""),
        timing_source=str(meta.get("timing_source") or ""),
        invalidate_candidate=bool(meta.get("invalidate_candidate", False)),
        entry_reference_price=float(decision.entry_reference_price or 0.0),
        meta=dict(meta.get("decision_meta") or {}),
    )


def strategy_entry_intent_from_pullback(intent: PullbackEntryIntent) -> StrategyEntryIntent:
    trade_date = _trade_date_from_datetime(intent.created_at)
    meta = {
        "native_type": "pullback_entry_intent",
        "context_version": str(intent.context_version or ""),
        "source": str(intent.source or ""),
        "candidate_created_at": intent.candidate_created_at.isoformat(),
        "current_price": float(intent.current_price or 0.0),
        "native_meta": dict(intent.meta or {}),
    }
    return StrategyEntryIntent(
        strategy_tag=str(intent.strategy_tag or ""),
        symbol=str(intent.symbol).zfill(6),
        created_at=intent.created_at,
        expires_at=intent.expires_at,
        trade_date=trade_date,
        entry_reference_price=float(intent.entry_reference_price or 0.0),
        entry_reference_label="pullback_intraday_high",
        meta=meta,
    )


def pullback_entry_intent_from_strategy(intent: StrategyEntryIntent) -> PullbackEntryIntent:
    meta = dict(intent.meta or {})
    candidate_created_at_raw = str(meta.get("candidate_created_at") or "").strip()
    candidate_created_at = intent.created_at
    if candidate_created_at_raw:
        try:
            candidate_created_at = datetime.fromisoformat(candidate_created_at_raw)
        except ValueError:
            candidate_created_at = intent.created_at
    return PullbackEntryIntent(
        symbol=str(intent.symbol).zfill(6),
        strategy_tag=str(intent.strategy_tag or ""),
        created_at=intent.created_at,
        candidate_created_at=candidate_created_at,
        expires_at=intent.expires_at,
        context_version=str(meta.get("context_version") or ""),
        entry_reference_price=float(intent.entry_reference_price or 0.0),
        source=str(meta.get("source") or "registry_adapter"),
        current_price=float(meta.get("current_price", 0.0) or 0.0),
        meta=dict(meta.get("native_meta") or {}),
    )


@dataclass(frozen=True)
class AccountRiskSnapshot:
    fetched_at: datetime
    total_eval: float
    cash_balance: float
    total_pnl: float
    holdings: Tuple[Dict[str, Any], ...] = ()
    source: str = ""
    success: bool = True
    stale: bool = False
    version: str = ""
    last_error: str = ""


@dataclass(frozen=True)
class HoldingsRiskSnapshot:
    fetched_at: datetime
    holdings: Tuple[Dict[str, Any], ...]
    source: str = ""
    success: bool = True
    stale: bool = False
    version: str = ""
    last_error: str = ""
