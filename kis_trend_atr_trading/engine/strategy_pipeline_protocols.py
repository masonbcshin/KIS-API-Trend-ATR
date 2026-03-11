from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

try:
    from engine.pullback_pipeline_models import (
        DailyContext,
        StrategySetupCandidate,
        StrategyTimingDecision,
    )
except ImportError:
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        DailyContext,
        StrategySetupCandidate,
        StrategyTimingDecision,
    )


@dataclass(frozen=True)
class StrategyCapabilities:
    uses_daily_context: bool = False
    uses_intraday_timing: bool = False
    requires_single_writer_order: bool = True
    market_regime_mode: str = "read_only"


@dataclass(frozen=True)
class StrategySetupEvaluation:
    strategy_tag: str
    candidate: Optional[StrategySetupCandidate] = None
    native_candidate: Optional[Any] = None
    skip_reason: str = ""
    skip_code: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class StrategySetupEvaluator(Protocol):
    strategy_tag: str

    def evaluate_setup(
        self,
        *,
        stock_code: str,
        stock_name: str,
        current_price: float,
        check_time: Any,
        market_phase: Optional[Any],
        market_venue: Optional[Any],
        has_existing_position: bool,
        has_pending_order: bool,
        market_regime_snapshot: Optional[Any],
        daily_context: Optional[DailyContext] = None,
        daily_df: Optional[Any] = None,
    ) -> StrategySetupEvaluation:
        ...


class StrategyTimingEvaluator(Protocol):
    strategy_tag: str

    def evaluate_timing(
        self,
        *,
        candidate: StrategySetupCandidate,
        native_candidate: Optional[Any],
        current_price: float,
        stock_code: str,
        check_time: Any,
        market_phase: Optional[Any],
        market_venue: Optional[Any],
        intraday_bars: Optional[list[dict]],
        has_existing_position: bool,
        has_pending_order: bool,
        current_context_version: Optional[str],
    ) -> StrategyTimingDecision:
        ...
