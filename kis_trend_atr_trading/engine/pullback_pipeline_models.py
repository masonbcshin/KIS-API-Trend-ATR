from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


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
