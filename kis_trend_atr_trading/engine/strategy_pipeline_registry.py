from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

try:
    from engine.pullback_pipeline_models import (
        pullback_setup_candidate_from_strategy,
        strategy_setup_candidate_from_pullback,
        strategy_timing_decision_from_pullback,
    )
    from engine.strategy_pipeline_protocols import (
        StrategyCapabilities,
        StrategySetupEvaluation,
        StrategySetupEvaluator,
        StrategyTimingEvaluator,
    )
except ImportError:
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        pullback_setup_candidate_from_strategy,
        strategy_setup_candidate_from_pullback,
        strategy_timing_decision_from_pullback,
    )
    from kis_trend_atr_trading.engine.strategy_pipeline_protocols import (
        StrategyCapabilities,
        StrategySetupEvaluation,
        StrategySetupEvaluator,
        StrategyTimingEvaluator,
    )


@dataclass(frozen=True)
class StrategyRegistryEntry:
    strategy_tag: str
    setup_evaluator: StrategySetupEvaluator
    timing_evaluator: StrategyTimingEvaluator
    capabilities: StrategyCapabilities


class StrategyRegistry:
    def __init__(self) -> None:
        self._entries: Dict[str, StrategyRegistryEntry] = {}

    def register(
        self,
        *,
        strategy_tag: str,
        setup_evaluator: StrategySetupEvaluator,
        timing_evaluator: StrategyTimingEvaluator,
        capabilities: Optional[StrategyCapabilities] = None,
    ) -> None:
        self._entries[str(strategy_tag or "").strip()] = StrategyRegistryEntry(
            strategy_tag=str(strategy_tag or "").strip(),
            setup_evaluator=setup_evaluator,
            timing_evaluator=timing_evaluator,
            capabilities=capabilities or StrategyCapabilities(),
        )

    def get(self, strategy_tag: str) -> Optional[StrategyRegistryEntry]:
        return self._entries.get(str(strategy_tag or "").strip())

    def enabled_entries(self, strategy_tags: Iterable[str]) -> list[StrategyRegistryEntry]:
        enabled = []
        for raw_tag in strategy_tags:
            entry = self.get(raw_tag)
            if entry is not None:
                enabled.append(entry)
        return enabled

    def strategy_tags(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries.keys()))


class PullbackSetupEvaluatorAdapter:
    strategy_tag = "pullback_rebreakout"

    def __init__(self, pullback_strategy: Any) -> None:
        self._pullback_strategy = pullback_strategy

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
        daily_context: Optional[Any] = None,
        daily_df: Optional[Any] = None,
    ) -> StrategySetupEvaluation:
        if daily_context is not None:
            native_candidate, terminal = self._pullback_strategy.evaluate_setup_candidate_from_daily_context(
                daily_context=daily_context,
                current_price=current_price,
                stock_code=stock_code,
                stock_name=stock_name,
                check_time=check_time,
                market_phase=market_phase,
                market_venue=market_venue,
                has_existing_position=has_existing_position,
                has_pending_order=has_pending_order,
                market_regime_snapshot=market_regime_snapshot,
            )
        else:
            native_candidate, terminal = self._pullback_strategy.evaluate_setup_candidate(
                df=daily_df,
                current_price=current_price,
                stock_code=stock_code,
                stock_name=stock_name,
                check_time=check_time,
                market_phase=market_phase,
                market_venue=market_venue,
                has_existing_position=has_existing_position,
                has_pending_order=has_pending_order,
                market_regime_snapshot=market_regime_snapshot,
            )
        if native_candidate is None:
            return StrategySetupEvaluation(
                strategy_tag=self.strategy_tag,
                candidate=None,
                native_candidate=None,
                skip_reason=str(getattr(terminal, "reason", "") or ""),
                skip_code=str(getattr(terminal, "reason_code", "") or ""),
            )
        return StrategySetupEvaluation(
            strategy_tag=self.strategy_tag,
            candidate=strategy_setup_candidate_from_pullback(native_candidate),
            native_candidate=native_candidate,
            skip_reason="",
            skip_code="",
        )


class PullbackTimingEvaluatorAdapter:
    strategy_tag = "pullback_rebreakout"

    def __init__(self, pullback_strategy: Any) -> None:
        self._pullback_strategy = pullback_strategy

    def evaluate_timing(
        self,
        *,
        candidate: Any,
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
    ) -> Any:
        pullback_candidate = native_candidate
        if pullback_candidate is None:
            pullback_candidate = pullback_setup_candidate_from_strategy(candidate)
        decision = self._pullback_strategy.confirm_timing(
            candidate=pullback_candidate,
            current_price=current_price,
            stock_code=stock_code,
            check_time=check_time,
            market_phase=market_phase,
            market_venue=market_venue,
            intraday_bars=intraday_bars,
            has_existing_position=has_existing_position,
            has_pending_order=has_pending_order,
            current_context_version=current_context_version,
        )
        return strategy_timing_decision_from_pullback(
            candidate=pullback_candidate,
            decision=decision,
        )


def build_default_strategy_registry(*, pullback_strategy: Any) -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register(
        strategy_tag="pullback_rebreakout",
        setup_evaluator=PullbackSetupEvaluatorAdapter(pullback_strategy),
        timing_evaluator=PullbackTimingEvaluatorAdapter(pullback_strategy),
        capabilities=StrategyCapabilities(
            uses_daily_context=True,
            uses_intraday_timing=True,
            requires_single_writer_order=True,
            market_regime_mode="read_only",
        ),
    )
    return registry
