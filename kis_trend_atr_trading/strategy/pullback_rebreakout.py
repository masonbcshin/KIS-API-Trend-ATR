from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional, Tuple

import pandas as pd

try:
    from config import settings
    from engine.pullback_pipeline_models import PullbackSetupCandidate, PullbackTimingDecision
    from utils.logger import get_logger
    from utils.market_hours import KST
    from utils.market_phase import (
        MarketPhaseContext,
        TradingVenue,
        VenueMarketPhase,
        phase_allowed_for_entry,
        resolve_market_phase_context,
    )
except ImportError:
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        PullbackSetupCandidate,
        PullbackTimingDecision,
    )
    from kis_trend_atr_trading.utils.logger import get_logger
    from kis_trend_atr_trading.utils.market_hours import KST
    from kis_trend_atr_trading.utils.market_phase import (
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

    def _resolve_phase_context(
        self,
        *,
        check_time: datetime,
        market_phase: Optional[object],
        market_venue: Optional[object],
    ) -> MarketPhaseContext:
        raw_phase = getattr(market_phase, "value", market_phase)
        phase_token = str(raw_phase or "").strip().upper()
        if phase_token in {item.value for item in VenueMarketPhase}:
            return MarketPhaseContext(
                venue=TradingVenue.KRX if phase_token.startswith("KRX_") else TradingVenue.NXT,
                phase=VenueMarketPhase(phase_token),
                source_session_state="provided_phase",
            )
        return resolve_market_phase_context(
            check_time=check_time,
            venue=market_venue or TradingVenue.KRX,
            session_state=market_phase,
        )

    @staticmethod
    def _setup_expiry_at(now_kst: datetime) -> datetime:
        refresh_sec = max(int(getattr(settings, "PULLBACK_SETUP_REFRESH_SEC", 60) or 60), 1)
        return now_kst + timedelta(seconds=max(refresh_sec * 2, 120))

    @staticmethod
    def _build_context_version(
        working: pd.DataFrame,
        *,
        phase_context: MarketPhaseContext,
    ) -> str:
        latest = working.iloc[-1]
        date_value = latest.get("date")
        if isinstance(date_value, pd.Timestamp):
            date_token = date_value.isoformat()
        elif isinstance(date_value, datetime):
            date_token = date_value.isoformat()
        else:
            date_token = str(date_value or "")
        payload = "|".join(
            [
                date_token,
                str(len(working)),
                f"{float(latest.get('close', 0.0) or 0.0):.6f}",
                str(phase_context.phase.value),
                str(phase_context.venue.value),
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _candidate_meta(candidate: PullbackSetupCandidate) -> Dict[str, Any]:
        return dict(candidate.extra_json or {})

    def _invalid_setup(self, reason: str, stock_code: str, detail: str) -> Tuple[None, PullbackCandidate]:
        self._entry_block(
            "pullback_invalid_setup",
            symbol=stock_code or "UNKNOWN",
            reason=detail,
        )
        return (
            None,
            PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason=reason,
                reason_code="pullback_invalid_setup",
            ),
        )

    def evaluate_setup_candidate(
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
    ) -> Tuple[Optional[PullbackSetupCandidate], Optional[PullbackCandidate]]:
        if not bool(getattr(settings, "ENABLE_PULLBACK_REBREAKOUT_STRATEGY", False)):
            return None, PullbackCandidate(decision=PullbackDecision.NOOP)

        now_kst = self._resolve_time(check_time)
        if has_existing_position and bool(getattr(settings, "PULLBACK_BLOCK_IF_EXISTING_POSITION", True)):
            self._entry_block(
                "pullback_existing_position",
                symbol=stock_code or "UNKNOWN",
                strategy_tag=self.strategy_tag,
            )
            return None, PullbackCandidate(
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
            return None, PullbackCandidate(
                decision=PullbackDecision.BLOCKED,
                reason="미종결 주문 존재 - Pullback 신규 진입 차단",
                reason_code="pullback_pending_order",
            )

        phase_context = self._resolve_phase_context(
            check_time=now_kst,
            market_phase=market_phase,
            market_venue=market_venue,
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
            return None, PullbackCandidate(
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
            return None, PullbackCandidate(
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
        required_bars = max(
            swing_lookback,
            pullback_lookback,
            20,
            int(getattr(settings, "TREND_MA_PERIOD", 50) or 50),
        )
        if working.empty or len(working) < required_bars:
            return self._invalid_setup("Pullback 판단용 데이터 부족", stock_code, "insufficient_history")

        latest = working.iloc[-1]
        atr = float(latest.get("atr", 0.0) or 0.0)
        adx = float(latest.get("adx", 0.0) or 0.0)
        trend_ma = float(latest.get("ma", 0.0) or 0.0)
        ma20 = float(latest.get("ma20", 0.0) or 0.0)
        if atr <= 0 or ma20 <= 0 or trend_ma <= 0:
            return self._invalid_setup("Pullback 판단 지표 부족", stock_code, "missing_indicator")

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
            return self._invalid_setup("눌림 구간이 아직 형성되지 않음", stock_code, "recent_swing_high")

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
            return self._invalid_setup("추세 필터 미충족", stock_code, "trend_filter_failed")
        if not adx_ok:
            return self._invalid_setup(
                f"ADX 기준 미충족 ({adx:.1f} < {min_adx:.1f})",
                stock_code,
                f"adx_below_min:{adx:.2f}",
            )

        min_pullback_pct = max(float(getattr(settings, "PULLBACK_MIN_PULLBACK_PCT", 0.015) or 0.015), 0.0)
        max_pullback_pct = max(
            float(getattr(settings, "PULLBACK_MAX_PULLBACK_PCT", 0.06) or 0.06),
            min_pullback_pct,
        )
        if pullback_pct < min_pullback_pct:
            return self._invalid_setup("눌림 부족", stock_code, f"pullback_too_shallow:{pullback_pct:.6f}")
        if pullback_pct > max_pullback_pct:
            return self._invalid_setup("눌림 과도", stock_code, f"pullback_too_deep:{pullback_pct:.6f}")

        if bool(getattr(settings, "PULLBACK_REQUIRE_ABOVE_MA20", True)) and current_price <= ma20:
            return self._invalid_setup("MA20 아래 - Pullback 진입 불가", stock_code, "below_ma20")

        if swing_low <= trend_ma:
            return self._invalid_setup("구조 저점 훼손", stock_code, "swing_low_below_trend_ma")

        self._log_setup_valid(
            symbol=stock_code or "UNKNOWN",
            reason="healthy_pullback_rebreakout",
            ma20=f"{ma20:.6f}",
            swing_high=f"{swing_high:.6f}",
            swing_low=f"{swing_low:.6f}",
            pullback_pct=f"{pullback_pct:.6f}",
            strategy_tag=self.strategy_tag,
        )

        prev_close = latest.get("prev_close", 0.0)
        if pd.isna(prev_close):
            prev_close = 0.0
        candidate = PullbackSetupCandidate(
            symbol=str(stock_code or "").zfill(6),
            strategy_tag=self.strategy_tag,
            created_at=now_kst,
            expires_at=self._setup_expiry_at(now_kst),
            context_version=self._build_context_version(working, phase_context=phase_context),
            swing_high=float(swing_high),
            swing_low=float(swing_low),
            micro_high=float(micro_high),
            atr=float(atr),
            source="daily_setup",
            extra_json={
                "stock_name": stock_name,
                "market_phase": phase_context.phase.value,
                "market_venue": phase_context.venue.value,
                "pullback_pct": float(pullback_pct),
                "ma20": float(ma20),
                "trend_ma": float(trend_ma),
                "adx": float(adx),
                "prev_close": float(prev_close or 0.0),
                "signal_time": now_kst.isoformat(),
            },
        )
        return candidate, None

    def evaluate_daily_timing(
        self,
        *,
        candidate: PullbackSetupCandidate,
        current_price: float,
        stock_code: str,
        timing_source: str,
    ) -> PullbackTimingDecision:
        if current_price <= float(candidate.micro_high or 0.0):
            self._entry_block(
                "pullback_invalid_setup",
                symbol=stock_code or "UNKNOWN",
                reason="rebreakout_not_confirmed",
            )
            return PullbackTimingDecision(
                should_emit_intent=False,
                reason="재돌파 미확인",
                reason_code="pullback_invalid_setup",
                timing_source=timing_source,
                entry_reference_price=float(candidate.micro_high or 0.0),
                meta={"micro_high": float(candidate.micro_high or 0.0)},
            )

        meta = self._candidate_meta(candidate)
        self._log_trigger_buy(
            symbol=stock_code or "UNKNOWN",
            trigger_price=f"{float(candidate.micro_high or 0.0):.6f}",
            current_price=f"{float(current_price):.6f}",
            micro_high=f"{float(candidate.micro_high or 0.0):.6f}",
            pullback_pct=f"{float(meta.get('pullback_pct', 0.0) or 0.0):.6f}",
            strategy_tag=self.strategy_tag,
            timing_source=timing_source,
        )
        return PullbackTimingDecision(
            should_emit_intent=True,
            reason="건강한 눌림 후 재돌파 진입",
            reason_code="",
            timing_source=timing_source,
            entry_reference_price=float(candidate.micro_high or 0.0),
            meta={
                "micro_high": float(candidate.micro_high or 0.0),
                "pullback_pct": float(meta.get("pullback_pct", 0.0) or 0.0),
            },
        )

    def confirm_timing(
        self,
        *,
        candidate: PullbackSetupCandidate,
        current_price: float,
        stock_code: str,
        check_time: Optional[datetime] = None,
        market_phase: Optional[object] = None,
        market_venue: Optional[object] = None,
        intraday_bars: Optional[list[dict]] = None,
        has_existing_position: bool = False,
        has_pending_order: bool = False,
        current_context_version: Optional[str] = None,
    ) -> PullbackTimingDecision:
        now_kst = self._resolve_time(check_time)
        if candidate.expires_at <= now_kst:
            return PullbackTimingDecision(
                should_emit_intent=False,
                reason="candidate_expired",
                reason_code="pullback_candidate_expired",
                timing_source="expired",
                invalidate_candidate=True,
                entry_reference_price=float(candidate.micro_high or 0.0),
            )

        if current_context_version and current_context_version != candidate.context_version:
            return PullbackTimingDecision(
                should_emit_intent=False,
                reason="context_version_mismatch",
                reason_code="pullback_context_version_mismatch",
                timing_source="stale_context",
                invalidate_candidate=True,
                entry_reference_price=float(candidate.micro_high or 0.0),
            )

        phase_context = self._resolve_phase_context(
            check_time=now_kst,
            market_phase=market_phase,
            market_venue=market_venue,
        )
        candidate_meta = self._candidate_meta(candidate)
        candidate_phase = str(candidate_meta.get("market_phase") or "").strip().upper()
        if candidate_phase and candidate_phase != phase_context.phase.value:
            return PullbackTimingDecision(
                should_emit_intent=False,
                reason="market_phase_mismatch",
                reason_code="pullback_market_phase_mismatch",
                timing_source="stale_phase",
                invalidate_candidate=True,
                entry_reference_price=float(candidate.micro_high or 0.0),
                meta={
                    "candidate_market_phase": candidate_phase,
                    "current_market_phase": phase_context.phase.value,
                },
            )

        if has_existing_position and bool(getattr(settings, "PULLBACK_BLOCK_IF_EXISTING_POSITION", True)):
            return PullbackTimingDecision(
                should_emit_intent=False,
                reason="existing_position",
                reason_code="pullback_existing_position",
                timing_source="precheck",
                entry_reference_price=float(candidate.micro_high or 0.0),
            )
        if has_pending_order and bool(getattr(settings, "PULLBACK_BLOCK_IF_PENDING_ORDER", True)):
            return PullbackTimingDecision(
                should_emit_intent=False,
                reason="pending_order",
                reason_code="pullback_pending_order",
                timing_source="precheck",
                entry_reference_price=float(candidate.micro_high or 0.0),
            )

        if intraday_bars:
            return self.evaluate_daily_timing(
                candidate=candidate,
                current_price=current_price,
                stock_code=stock_code,
                timing_source="intraday_quote",
            )

        return self.evaluate_daily_timing(
            candidate=candidate,
            current_price=current_price,
            stock_code=stock_code,
            timing_source="fallback_daily",
        )

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
        candidate, terminal = self.evaluate_setup_candidate(
            df=df,
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
        if terminal is not None:
            return terminal
        if candidate is None:
            return PullbackCandidate(decision=PullbackDecision.NOOP)

        timing = self.evaluate_daily_timing(
            candidate=candidate,
            current_price=current_price,
            stock_code=stock_code,
            timing_source="daily_rebreakout",
        )
        if not timing.should_emit_intent:
            return PullbackCandidate(
                decision=PullbackDecision.NOOP,
                reason=timing.reason,
                reason_code=timing.reason_code,
            )

        meta = self._candidate_meta(candidate)
        meta.update(
            {
                "strategy_tag": self.strategy_tag,
                "swing_high": float(candidate.swing_high or 0.0),
                "swing_low": float(candidate.swing_low or 0.0),
                "micro_high": float(candidate.micro_high or 0.0),
                "signal_time": self._resolve_time(check_time).isoformat(),
            }
        )
        return PullbackCandidate(
            decision=PullbackDecision.BUY,
            reason=timing.reason,
            reason_code="",
            atr=float(candidate.atr or 0.0),
            trigger_price=float(candidate.micro_high or 0.0),
            meta=meta,
        )
