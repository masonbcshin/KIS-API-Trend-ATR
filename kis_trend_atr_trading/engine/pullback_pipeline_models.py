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
class PullbackTimingDecision:
    should_emit_intent: bool = False
    reason: str = ""
    reason_code: str = ""
    timing_source: str = ""
    invalidate_candidate: bool = False
    entry_reference_price: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


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
