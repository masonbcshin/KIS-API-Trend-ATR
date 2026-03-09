from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

import pandas as pd

from config import settings
from utils.logger import get_logger
from utils.market_hours import KST
from utils.market_phase import (
    MarketPhaseContext,
    TradingVenue,
    VenueMarketPhase,
    phase_allowed_for_entry,
    resolve_market_phase_context,
)


logger = get_logger("pullback_rebreakout")


class PullbackDecision(str, Enum):
    NOOP = "NOOP"
    BLOCKED = "BLOCKED"
    BUY = "BUY"


@dataclass(frozen=True)
class PullbackCandidate:
    decision: PullbackDecision
    reason: str = ""
    reason_code: str = ""
    atr: float = 0.0
    trigger_price: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


class PullbackRebreakoutStrategy:
    strategy_tag = "pullback_rebreakout"

    @staticmethod
    def _resolve_time(check_time: Optional[datetime]) -> datetime:
        if check_time is None:
            return datetime.now(KST)
        if check_time.tzinfo is None:
            return KST.localize(check_time)
        return check_time.astimezone(KST)

    @staticmethod
    def _resolve_venues(raw_value: object) -> list[TradingVenue]:
        if isinstance(raw_value, str):
            tokens = [token.strip() for token in raw_value.split(",") if token.strip()]
        else:
            tokens = [str(token).strip() for token in list(raw_value or []) if str(token).strip()]

        venues: list[TradingVenue] = []
        for token in tokens:
            normalized = str(token or "").strip().upper()
            if normalized == TradingVenue.NXT.value:
                venues.append(TradingVenue.NXT)
            elif normalized == TradingVenue.KRX.value:
                venues.append(TradingVenue.KRX)
        return venues or [TradingVenue.KRX]

    @staticmethod
    def _entry_block(reason_code: str, **payload: Any) -> None:
        details = " ".join(
            f"{key}={value}"
            for key, value in payload.items()
            if value is not None
        )
        logger.info(f"[ENTRY_BLOCK] reason={reason_code} {details}".rstrip())

    @staticmethod
    def _log_candidate(**payload: Any) -> None:
        details = " ".join(
            f"{key}={value}"
            for key, value in payload.items()
            if value is not None
        )
        logger.info(f"[PULLBACK] candidate {details}".rstrip())

    @staticmethod
    def _log_setup_valid(**payload: Any) -> None:
        details = " ".join(
            f"{key}={value}"
            for key, value in payload.items()
            if value is not None
        )
        logger.info(f"[PULLBACK] setup_valid {details}".rstrip())

    @staticmethod
    def _log_trigger_buy(**payload: Any) -> None:
        details = " ".join(
            f"{key}={value}"
            for key, value in payload.items()
            if value is not None
        )
        logger.info(f"[PULLBACK] trigger_buy {details}".rstrip())

    def evaluate(
        self,
        *,
        df: pd.DataFrame,
        current_price: float,
        stock_code: str,
        stock_name: str = "",
        check_time: Optional[datetime] = None,
        market_phase: Optional[object] = None,
        market_venue: Optional[object] = None,
        has_existing_position: bool = False,
        has_pending_order: bool = False,
        market_regime_snapshot: Optional[object] = None,
    ) -> PullbackCandidate:
        if not bool(getattr(settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", False)):
            return PullbackCandidate(decision=PullbackDecision.NOOP)

        now_kst = self._resolve_time(check_time)
        if has_existing_position and bool(getattr(settings, "PULLBACK_BLOCK_IF_EXISTING_POSITION", True)):
            self._entry_block(
                "pullback_existing_position",
                symbol=stock_code or "UNKNOWN",
                strategy_tag=self.strategy_tag,
            )
            return PullbackCandidate(
                decision=PullbackDecision.BLOCKED,
                reason="기존 포지션 보유 중 - Pullback 신규 진입 차단",
                reason_code="pullback_existing_position",
            )

        if has_pending_order and bool(getattr(settings, "PULLBACK_BLOCK_IF_PENDING_ORDER", True)):
            self._entry_block(
                "pullback_pending_order",
                symbol=stock_code or "UNKNOWN",
                strategy_tag=self.strategy_tag,
            )
            return PullbackCandidate(
                decision=PullbackDecision.BLOCKED,
                reason="미종결 주문 존재 - Pullback 신규 진입 차단",
                reason_code="pullback_pending_order",
            )

        raw_phase = getattr(market_phase, "value", market_phase)
        phase_token = str(raw_phase or "").strip().upper()
        if phase_token in {item.value for item in VenueMarketPhase}:
            phase_context = MarketPhaseContext(
                venue=TradingVenue.KRX if phase_token.startswith("KRX_") else TradingVenue.NXT,
                phase=VenueMarketPhase(phase_token),
                source_session_state="provided_phase",
            )
        else:
            phase_context = resolve_market_phase_context(
                check_time=now_kst,
                venue=market_venue or TradingVenue.KRX,
                session_state=market_phase,
            )
        allowed_venues = self._resolve_venues(
            getattr(settings, "PULLBACK_ALLOWED_ENTRY_VENUES", TradingVenue.KRX.value)
        )
        if bool(getattr(settings, "PULLBACK_ONLY_MAIN_MARKET", True)) and not phase_allowed_for_entry(
            phase_context.phase,
            allowed_venues=allowed_venues,
        ):
            self._entry_block(
                "pullback_not_main_market",
                symbol=stock_code or "UNKNOWN",
                market_phase=phase_context.phase.value,
                venue=phase_context.venue.value,
            )
            return PullbackCandidate(
                decision=PullbackDecision.BLOCKED,
                reason="메인마켓 phase가 아니어서 Pullback 신규 진입 차단",
                reason_code="pullback_not_main_market",
                meta={
                    "market_phase": phase_context.phase.value,
                    "market_venue": phase_context.venue.value,
                },
            )

        regime = getattr(market_regime_snapshot, "regime", None)
        regime_is_stale = bool(getattr(market_regime_snapshot, "is_stale", False))
        if regime is not None and not regime_is_stale and str(getattr(regime, "value", regime)).upper() == "BAD":
            regime_value = str(getattr(regime, "value", regime))
            self._entry_block(
                "pullback_regime_bad",
                symbol=stock_code or "UNKNOWN",
                regime=regime_value,
            )
            return PullbackCandidate(
                decision=PullbackDecision.BLOCKED,
                reason="시장 레짐 BAD - Pullback 신규 진입 차단",
                reason_code="pullback_regime_bad",
                meta={"market_regime": regime_value},
            )

        working = df.copy().reset_index(drop=True)
        swing_lookback = max(int(getattr(settings, "PULLBACK_SWING_LOOKBACK_BARS", 15) or 15), 5)
        pullback_lookback = max(int(getattr(settings, "PULLBACK_LOOKBACK_BARS", 12) or 12), 5)
        rebreakout_lookback = max(
            int(getattr(settings, "PULLBACK_REBREAKOUT_LOOKBACK_BARS", 3) or 3),
            1,
        )
        required_bars = max(swing_lookback, pullback_lookback, 20, int(getattr(settings, "TREND_MA_PERIOD", 50) or 50))
        if working.empty or len(working) < required_bars:
            self._entry_block(
                "pullback_invalid_setup",
                symbol=stock_code or "UNKNOWN",
                reason="insufficient_history",
            )
            return PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason="Pullback 판단용 데이터 부족",
                reason_code="pullback_invalid_setup",
            )

        latest = working.iloc[-1]
        atr = float(latest.get("atr", 0.0) or 0.0)
        adx = float(latest.get("adx", 0.0) or 0.0)
        trend_ma = float(latest.get("ma", 0.0) or 0.0)
        ma20 = float(latest.get("ma20", 0.0) or 0.0)
        if atr <= 0 or ma20 <= 0 or trend_ma <= 0:
            self._entry_block(
                "pullback_invalid_setup",
                symbol=stock_code or "UNKNOWN",
                reason="missing_indicator",
            )
            return PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason="Pullback 판단 지표 부족",
                reason_code="pullback_invalid_setup",
            )

        latest_close = float(latest.get("close", 0.0) or 0.0)
        trend_ok = bool(latest_close > trend_ma and ma20 > trend_ma)
        use_adx_filter = bool(getattr(settings, "PULLBACK_USE_ADX_FILTER", True))
        min_adx = max(float(getattr(settings, "PULLBACK_MIN_ADX", 20.0) or 20.0), 0.0)
        adx_ok = (not use_adx_filter) or adx >= min_adx

        swing_window = working.tail(swing_lookback).reset_index(drop=True)
        swing_high_idx = int(swing_window["high"].astype(float).idxmax())
        swing_high = float(swing_window.iloc[swing_high_idx]["high"])
        pullback_tail = swing_window.iloc[swing_high_idx + 1 :].reset_index(drop=True)
        if pullback_tail.empty or swing_high_idx >= max(len(swing_window) - rebreakout_lookback, 0):
            self._entry_block(
                "pullback_invalid_setup",
                symbol=stock_code or "UNKNOWN",
                reason="recent_swing_high",
            )
            return PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason="눌림 구간이 아직 형성되지 않음",
                reason_code="pullback_invalid_setup",
            )

        pullback_window = pullback_tail.tail(max(pullback_lookback, rebreakout_lookback)).reset_index(drop=True)
        swing_low = float(pullback_window["low"].astype(float).min())
        micro_window = pullback_window.tail(rebreakout_lookback)
        micro_high = float(micro_window["high"].astype(float).max())
        pullback_pct = (
            (float(swing_high) - float(current_price)) / float(swing_high)
            if float(swing_high) > 0
            else 0.0
        )

        self._log_candidate(
            symbol=stock_code or "UNKNOWN",
            trend_ok=str(bool(trend_ok)).lower(),
            adx=f"{adx:.2f}",
            swing_high=f"{swing_high:.6f}",
            current_price=f"{float(current_price):.6f}",
            pullback_pct=f"{pullback_pct:.6f}",
            market_phase=phase_context.phase.value,
            strategy_tag=self.strategy_tag,
        )

        if not trend_ok:
            self._entry_block(
                "pullback_invalid_setup",
                symbol=stock_code or "UNKNOWN",
                reason="trend_filter_failed",
            )
            return PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason="추세 필터 미충족",
                reason_code="pullback_invalid_setup",
            )
        if not adx_ok:
            self._entry_block(
                "pullback_invalid_setup",
                symbol=stock_code or "UNKNOWN",
                reason=f"adx_below_min:{adx:.2f}",
            )
            return PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason=f"ADX 기준 미충족 ({adx:.1f} < {min_adx:.1f})",
                reason_code="pullback_invalid_setup",
            )

        min_pullback_pct = max(float(getattr(settings, "PULLBACK_MIN_PULLBACK_PCT", 0.015) or 0.015), 0.0)
        max_pullback_pct = max(
            float(getattr(settings, "PULLBACK_MAX_PULLBACK_PCT", 0.06) or 0.06),
            min_pullback_pct,
        )
        if pullback_pct < min_pullback_pct:
            self._entry_block(
                "pullback_invalid_setup",
                symbol=stock_code or "UNKNOWN",
                reason=f"pullback_too_shallow:{pullback_pct:.6f}",
            )
            return PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason="눌림 부족",
                reason_code="pullback_invalid_setup",
            )
        if pullback_pct > max_pullback_pct:
            self._entry_block(
                "pullback_invalid_setup",
                symbol=stock_code or "UNKNOWN",
                reason=f"pullback_too_deep:{pullback_pct:.6f}",
            )
            return PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason="눌림 과도",
                reason_code="pullback_invalid_setup",
            )

        if bool(getattr(settings, "PULLBACK_REQUIRE_ABOVE_MA20", True)) and current_price <= ma20:
            self._entry_block(
                "pullback_invalid_setup",
                symbol=stock_code or "UNKNOWN",
                reason="below_ma20",
            )
            return PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason="MA20 아래 - Pullback 진입 불가",
                reason_code="pullback_invalid_setup",
            )

        if swing_low <= trend_ma:
            self._entry_block(
                "pullback_invalid_setup",
                symbol=stock_code or "UNKNOWN",
                reason="swing_low_below_trend_ma",
            )
            return PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason="구조 저점 훼손",
                reason_code="pullback_invalid_setup",
            )

        if current_price <= micro_high:
            self._entry_block(
                "pullback_invalid_setup",
                symbol=stock_code or "UNKNOWN",
                reason="rebreakout_not_confirmed",
            )
            return PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason="재돌파 미확인",
                reason_code="pullback_invalid_setup",
            )

        self._log_setup_valid(
            symbol=stock_code or "UNKNOWN",
            reason="healthy_pullback_rebreakout",
            ma20=f"{ma20:.6f}",
            swing_high=f"{swing_high:.6f}",
            swing_low=f"{swing_low:.6f}",
            pullback_pct=f"{pullback_pct:.6f}",
            strategy_tag=self.strategy_tag,
        )
        self._log_trigger_buy(
            symbol=stock_code or "UNKNOWN",
            trigger_price=f"{micro_high:.6f}",
            current_price=f"{float(current_price):.6f}",
            micro_high=f"{micro_high:.6f}",
            pullback_pct=f"{pullback_pct:.6f}",
            strategy_tag=self.strategy_tag,
        )

        return PullbackCandidate(
            decision=PullbackDecision.BUY,
            reason="건강한 눌림 후 재돌파 진입",
            reason_code="",
            atr=atr,
            trigger_price=micro_high,
            meta={
                "strategy_tag": self.strategy_tag,
                "market_phase": phase_context.phase.value,
                "market_venue": phase_context.venue.value,
                "pullback_pct": float(pullback_pct),
                "swing_high": float(swing_high),
                "swing_low": float(swing_low),
                "micro_high": float(micro_high),
                "ma20": float(ma20),
                "trend_ma": float(trend_ma),
                "adx": float(adx),
                "stock_name": stock_name,
                "signal_time": now_kst.isoformat(),
            },
        )
