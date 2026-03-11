from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Iterable, Optional

try:
    from config import settings
    from engine.pullback_pipeline_models import (
        StrategySetupCandidate,
        StrategyTimingDecision,
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
    from strategy.opening_range_breakout import ORBSetupCandidate, ORBTimingDecision
except ImportError:
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        StrategySetupCandidate,
        StrategyTimingDecision,
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
    from kis_trend_atr_trading.strategy.opening_range_breakout import ORBSetupCandidate, ORBTimingDecision


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
        open_price: Optional[float],
        intraday_bars: Optional[list[dict]] = None,
        intraday_provider_ready: bool = True,
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


class TrendATRSetupEvaluatorAdapter:
    strategy_tag = "trend_atr"

    def __init__(self, trend_atr_strategy: Any) -> None:
        self._trend_atr_strategy = trend_atr_strategy

    def evaluate_setup(
        self,
        *,
        stock_code: str,
        stock_name: str,
        current_price: float,
        open_price: Optional[float],
        intraday_bars: Optional[list[dict]] = None,
        intraday_provider_ready: bool = True,
        check_time: Any,
        market_phase: Optional[Any],
        market_venue: Optional[Any],
        has_existing_position: bool,
        has_pending_order: bool,
        market_regime_snapshot: Optional[Any],
        daily_context: Optional[Any] = None,
        daily_df: Optional[Any] = None,
    ) -> StrategySetupEvaluation:
        if daily_df is None and daily_context is not None:
            import pandas as pd

            daily_df = pd.DataFrame(list(getattr(daily_context, "recent_bars", ()) or []))
        setup_result = self._trend_atr_strategy.evaluate_trend_atr_setup_candidate(
            df=daily_df,
            current_price=current_price,
            open_price=open_price,
            stock_code=stock_code,
            stock_name=stock_name,
            check_time=check_time,
            market_regime_snapshot=market_regime_snapshot,
        )
        entry_meta = dict(setup_result.get("meta") or {})
        if not bool(setup_result.get("can_enter", False)):
            return StrategySetupEvaluation(
                strategy_tag=self.strategy_tag,
                candidate=None,
                native_candidate=dict(setup_result),
                skip_reason=str(setup_result.get("reason") or ""),
                skip_code=str(entry_meta.get("reason_code") or ""),
                meta=entry_meta,
            )

        decision_time = self._trend_atr_strategy._resolve_entry_time(check_time)
        trigger_price = float(
            entry_meta.get("entry_reference_price", entry_meta.get("prev_high", 0.0)) or 0.0
        )
        entry_label = str(entry_meta.get("entry_reference_label") or "prev_high")
        candidate = StrategySetupCandidate(
            strategy_tag=self.strategy_tag,
            symbol=str(stock_code).zfill(6),
            created_at=decision_time,
            expires_at=decision_time
            + timedelta(seconds=max(int(getattr(settings, "STRATEGY_CANDIDATE_MAX_AGE_SEC", 300) or 300), 1)),
            trade_date=decision_time.date().isoformat(),
            entry_reference_price=trigger_price,
            entry_reference_label=entry_label,
            meta={
                "native_type": "trend_atr_setup_candidate",
                "entry_reason": str(setup_result.get("reason") or ""),
                "entry_atr": float(setup_result.get("atr", 0.0) or 0.0),
                "entry_meta": entry_meta,
                "current_price": float(current_price or 0.0),
                "open_price": float(open_price or 0.0) if open_price is not None else None,
                "expiry_authority": "registry_shadow_only",
            },
        )
        return StrategySetupEvaluation(
            strategy_tag=self.strategy_tag,
            candidate=candidate,
            native_candidate=dict(setup_result),
        )


def _strategy_setup_candidate_from_orb(candidate: ORBSetupCandidate) -> StrategySetupCandidate:
    meta = dict(candidate.meta or {})
    return StrategySetupCandidate(
        strategy_tag="opening_range_breakout",
        symbol=str(candidate.symbol).zfill(6),
        created_at=candidate.created_at,
        expires_at=candidate.expires_at,
        trade_date=candidate.created_at.date().isoformat(),
        entry_reference_price=float(candidate.opening_range_high or 0.0),
        entry_reference_label=str(meta.get("entry_reference_label") or "opening_range_high"),
        meta={
            "native_type": "orb_setup_candidate",
            "opening_range_high": float(candidate.opening_range_high or 0.0),
            "opening_range_low": float(candidate.opening_range_low or 0.0),
            "entry_atr": float(candidate.atr or 0.0),
            "source": str(candidate.source or ""),
            "entry_meta": meta,
        },
    )


def _orb_setup_candidate_from_strategy(candidate: StrategySetupCandidate) -> ORBSetupCandidate:
    meta = dict(candidate.meta or {})
    entry_meta = dict(meta.get("entry_meta") or {})
    return ORBSetupCandidate(
        symbol=str(candidate.symbol).zfill(6),
        strategy_tag="opening_range_breakout",
        created_at=candidate.created_at,
        expires_at=candidate.expires_at,
        opening_range_high=float(meta.get("opening_range_high", candidate.entry_reference_price) or 0.0),
        opening_range_low=float(meta.get("opening_range_low", 0.0) or 0.0),
        atr=float(meta.get("entry_atr", 0.0) or 0.0),
        source=str(meta.get("source") or "registry_adapter"),
        meta=entry_meta,
    )


class OpeningRangeBreakoutSetupEvaluatorAdapter:
    strategy_tag = "opening_range_breakout"

    def __init__(self, orb_strategy: Any, indicator_strategy: Optional[Any] = None) -> None:
        self._orb_strategy = orb_strategy
        self._indicator_strategy = indicator_strategy

    def evaluate_setup(
        self,
        *,
        stock_code: str,
        stock_name: str,
        current_price: float,
        open_price: Optional[float],
        intraday_bars: Optional[list[dict]] = None,
        intraday_provider_ready: bool = True,
        check_time: Any,
        market_phase: Optional[Any],
        market_venue: Optional[Any],
        has_existing_position: bool,
        has_pending_order: bool,
        market_regime_snapshot: Optional[Any],
        daily_context: Optional[Any] = None,
        daily_df: Optional[Any] = None,
    ) -> StrategySetupEvaluation:
        if daily_df is None and daily_context is not None:
            import pandas as pd

            daily_df = pd.DataFrame(list(getattr(daily_context, "recent_bars", ()) or []))
        if daily_df is None:
            return StrategySetupEvaluation(
                strategy_tag=self.strategy_tag,
                candidate=None,
                native_candidate=None,
                skip_reason="missing_daily_data",
                skip_code="missing_daily_data",
                meta={"intraday_source_state": "missing"},
            )
        if daily_df is not None and not {
            "atr",
            "ma",
            "ma20",
            "adx",
            "trend",
            "prev_high",
            "prev_close",
        }.issubset(set(getattr(daily_df, "columns", []))):
            add_indicators = getattr(self._indicator_strategy, "add_indicators", None)
            if callable(add_indicators):
                daily_df = add_indicators(daily_df)
        native_candidate, terminal = self._orb_strategy.evaluate_setup_candidate(
            df=daily_df,
            current_price=current_price,
            open_price=open_price,
            intraday_bars=intraday_bars,
            stock_code=stock_code,
            stock_name=stock_name,
            check_time=check_time,
            market_phase=market_phase,
            market_venue=market_venue,
            has_existing_position=has_existing_position,
            has_pending_order=has_pending_order,
            market_regime_snapshot=market_regime_snapshot,
            intraday_provider_ready=intraday_provider_ready,
        )
        if native_candidate is None:
            return StrategySetupEvaluation(
                strategy_tag=self.strategy_tag,
                candidate=None,
                native_candidate=terminal,
                skip_reason=str(getattr(terminal, "reason", "") or ""),
                skip_code=str(getattr(terminal, "reason_code", "") or ""),
                meta=dict(getattr(terminal, "meta", {}) or {}),
            )
        return StrategySetupEvaluation(
            strategy_tag=self.strategy_tag,
            candidate=_strategy_setup_candidate_from_orb(native_candidate),
            native_candidate=native_candidate,
            meta=dict(native_candidate.meta or {}),
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


class TrendATRTimingEvaluatorAdapter:
    strategy_tag = "trend_atr"

    def __init__(self, trend_atr_strategy: Any) -> None:
        self._trend_atr_strategy = trend_atr_strategy

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
        entry_meta = dict((candidate.meta or {}).get("entry_meta") or {})
        trigger_price = float(
            candidate.entry_reference_price or entry_meta.get("entry_reference_price", 0.0) or 0.0
        )
        should_emit = bool(current_price > 0.0 and trigger_price > 0.0 and current_price >= trigger_price)
        return StrategyTimingDecision(
            strategy_tag=self.strategy_tag,
            symbol=str(stock_code).zfill(6),
            created_at=self._trend_atr_strategy._resolve_entry_time(check_time),
            expires_at=candidate.expires_at,
            trade_date=str(candidate.trade_date or ""),
            entry_reference_price=trigger_price,
            entry_reference_label=str(candidate.entry_reference_label or "prev_high"),
            should_emit_intent=should_emit,
            reason="" if should_emit else "trend_breakout_not_confirmed",
            reason_code="" if should_emit else "trend_atr_breakout_not_confirmed",
            meta={
                "timing_mode": "immediate",
                "entry_meta": entry_meta,
                "extension_pct": float(entry_meta.get("extension_pct", 0.0) or 0.0),
                "expiry_authority": "registry_shadow_only",
            },
        )


class OpeningRangeBreakoutTimingEvaluatorAdapter:
    strategy_tag = "opening_range_breakout"

    def __init__(self, orb_strategy: Any) -> None:
        self._orb_strategy = orb_strategy

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
        orb_candidate = native_candidate if isinstance(native_candidate, ORBSetupCandidate) else None
        if orb_candidate is None:
            orb_candidate = _orb_setup_candidate_from_strategy(candidate)
        decision = self._orb_strategy.confirm_timing(
            candidate=orb_candidate,
            current_price=current_price,
            intraday_bars=intraday_bars,
            stock_code=stock_code,
            stock_name=str((orb_candidate.meta or {}).get("stock_name") or ""),
            check_time=check_time,
            market_phase=market_phase,
            market_venue=market_venue,
            has_existing_position=has_existing_position,
            has_pending_order=has_pending_order,
            intraday_provider_ready=True,
        )
        return StrategyTimingDecision(
            strategy_tag=self.strategy_tag,
            symbol=str(stock_code).zfill(6),
            created_at=orb_candidate.created_at,
            expires_at=orb_candidate.expires_at,
            trade_date=str(candidate.trade_date or orb_candidate.created_at.date().isoformat()),
            entry_reference_price=float(
                decision.entry_reference_price or orb_candidate.opening_range_high or 0.0
            ),
            entry_reference_label=str(candidate.entry_reference_label or "opening_range_high"),
            should_emit_intent=bool(decision.should_emit_intent),
            reason=str(decision.reason or ""),
            reason_code=str(decision.reason_code or ""),
            meta={
                "timing_source": str(decision.timing_source or ""),
                "invalidate_candidate": bool(decision.invalidate_candidate),
                "decision_meta": dict(decision.meta or {}),
                "entry_meta": dict(orb_candidate.meta or {}),
            },
        )


def build_default_strategy_registry(
    *,
    pullback_strategy: Any,
    trend_atr_strategy: Optional[Any] = None,
    orb_strategy: Optional[Any] = None,
) -> StrategyRegistry:
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
    if trend_atr_strategy is not None:
        registry.register(
            strategy_tag="trend_atr",
            setup_evaluator=TrendATRSetupEvaluatorAdapter(trend_atr_strategy),
            timing_evaluator=TrendATRTimingEvaluatorAdapter(trend_atr_strategy),
            capabilities=StrategyCapabilities(
                uses_daily_context=True,
                uses_intraday_timing=False,
                requires_single_writer_order=True,
                market_regime_mode="read_only",
            ),
        )
    if orb_strategy is not None:
        registry.register(
            strategy_tag="opening_range_breakout",
            setup_evaluator=OpeningRangeBreakoutSetupEvaluatorAdapter(
                orb_strategy,
                indicator_strategy=trend_atr_strategy,
            ),
            timing_evaluator=OpeningRangeBreakoutTimingEvaluatorAdapter(orb_strategy),
            capabilities=StrategyCapabilities(
                uses_daily_context=True,
                uses_intraday_timing=True,
                requires_single_writer_order=True,
                market_regime_mode="read_only",
            ),
        )
    return registry
