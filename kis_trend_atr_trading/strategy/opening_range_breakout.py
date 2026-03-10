from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Iterable, Optional

import pandas as pd

from config import settings
from utils.entry_utils import ASSET_TYPE_ETF, ASSET_TYPE_STOCK, compute_extension_pct, detect_asset_type
from utils.logger import get_logger
from utils.market_hours import KST, MARKET_OPEN
from utils.market_phase import (
    MarketPhaseContext,
    TradingVenue,
    VenueMarketPhase,
    phase_allowed_for_entry,
    resolve_market_phase_context,
)


logger = get_logger("opening_range_breakout")


class ORBDecision(str, Enum):
    NOOP = "NOOP"
    BLOCKED = "BLOCKED"
    BUY = "BUY"


@dataclass(frozen=True)
class ORBCandidate:
    decision: ORBDecision
    reason: str = ""
    reason_code: str = ""
    atr: float = 0.0
    trigger_price: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


class OpeningRangeBreakoutStrategy:
    strategy_tag = "opening_range_breakout"

    @staticmethod
    def _resolve_time(check_time: Optional[datetime]) -> datetime:
        if check_time is None:
            return datetime.now(KST)
        if check_time.tzinfo is None:
            return KST.localize(check_time)
        return check_time.astimezone(KST)

    @staticmethod
    def _asset_type_max_pct(asset_type: str, etf_value: float, stock_value: float) -> float:
        if str(asset_type or ASSET_TYPE_STOCK).upper() == ASSET_TYPE_ETF:
            return max(float(etf_value or 0.0), 0.0)
        return max(float(stock_value or 0.0), 0.0)

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
    def _parse_bar_time(raw_value: object, fallback_tz=KST) -> Optional[datetime]:
        if raw_value is None:
            return None
        if isinstance(raw_value, pd.Timestamp):
            dt_value = raw_value.to_pydatetime()
        elif isinstance(raw_value, datetime):
            dt_value = raw_value
        elif isinstance(raw_value, str):
            token = raw_value.strip()
            if not token:
                return None
            try:
                dt_value = datetime.fromisoformat(token)
            except ValueError:
                return None
        else:
            return None

        if dt_value.tzinfo is None:
            return fallback_tz.localize(dt_value)
        return dt_value.astimezone(fallback_tz)

    def _normalize_intraday_bars(
        self,
        intraday_bars: Iterable[dict],
        *,
        decision_time: datetime,
    ) -> list[dict]:
        normalized: list[dict] = []
        trade_date = decision_time.date()
        market_open_at = decision_time.replace(
            hour=MARKET_OPEN.hour,
            minute=MARKET_OPEN.minute,
            second=0,
            microsecond=0,
        )
        for raw_bar in list(intraday_bars or []):
            if not isinstance(raw_bar, dict):
                continue
            start_at = self._parse_bar_time(raw_bar.get("start_at") or raw_bar.get("date"))
            if start_at is None:
                continue
            if start_at.date() != trade_date or start_at < market_open_at:
                continue
            try:
                normalized.append(
                    {
                        "start_at": start_at,
                        "open": float(raw_bar.get("open", 0.0) or 0.0),
                        "high": float(raw_bar.get("high", 0.0) or 0.0),
                        "low": float(raw_bar.get("low", 0.0) or 0.0),
                        "close": float(raw_bar.get("close", 0.0) or 0.0),
                        "volume": float(raw_bar.get("volume", 0.0) or 0.0),
                    }
                )
            except (TypeError, ValueError):
                continue
        normalized.sort(key=lambda item: item["start_at"])
        return normalized

    @staticmethod
    def _compute_vwap(bars: Iterable[dict]) -> float:
        total_turnover = 0.0
        total_volume = 0.0
        for bar in list(bars or []):
            volume = float(bar.get("volume", 0.0) or 0.0)
            if volume <= 0:
                continue
            typical_price = (
                float(bar.get("high", 0.0) or 0.0)
                + float(bar.get("low", 0.0) or 0.0)
                + float(bar.get("close", 0.0) or 0.0)
            ) / 3.0
            total_turnover += typical_price * volume
            total_volume += volume
        if total_volume <= 0:
            return 0.0
        return total_turnover / total_volume

    def evaluate(
        self,
        *,
        df: pd.DataFrame,
        current_price: float,
        open_price: Optional[float],
        intraday_bars: Optional[list[dict]],
        stock_code: str,
        stock_name: str = "",
        check_time: Optional[datetime] = None,
        market_phase: Optional[object] = None,
        market_venue: Optional[object] = None,
        has_existing_position: bool = False,
        has_pending_order: bool = False,
        market_regime_snapshot: Optional[object] = None,
    ) -> ORBCandidate:
        if not bool(getattr(settings, "ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY", False)):
            return ORBCandidate(decision=ORBDecision.NOOP)

        now_kst = self._resolve_time(check_time)
        if has_existing_position:
            self._entry_block("orb_existing_position", symbol=stock_code or "UNKNOWN")
            return ORBCandidate(
                decision=ORBDecision.BLOCKED,
                reason="기존 포지션 보유 중 - ORB 신규 진입 차단",
                reason_code="orb_existing_position",
            )

        if has_pending_order and bool(getattr(settings, "ORB_BLOCK_IF_PENDING_ORDER", True)):
            self._entry_block("orb_pending_order", symbol=stock_code or "UNKNOWN")
            return ORBCandidate(
                decision=ORBDecision.BLOCKED,
                reason="미종결 주문 존재 - ORB 신규 진입 차단",
                reason_code="orb_pending_order",
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
            getattr(settings, "ORB_ALLOWED_ENTRY_VENUES", TradingVenue.KRX.value)
        )
        if bool(getattr(settings, "ORB_ONLY_MAIN_MARKET", True)) and not phase_allowed_for_entry(
            phase_context.phase,
            allowed_venues=allowed_venues,
        ):
            self._entry_block(
                "orb_not_main_market",
                symbol=stock_code or "UNKNOWN",
                market_phase=phase_context.phase.value,
                venue=phase_context.venue.value,
            )
            return ORBCandidate(
                decision=ORBDecision.BLOCKED,
                reason="메인마켓 phase가 아니어서 ORB 신규 진입 차단",
                reason_code="orb_not_main_market",
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
                "orb_regime_bad",
                symbol=stock_code or "UNKNOWN",
                regime=regime_value,
            )
            return ORBCandidate(
                decision=ORBDecision.BLOCKED,
                reason="시장 레짐 BAD - ORB 신규 진입 차단",
                reason_code="orb_regime_bad",
                meta={"market_regime": regime_value},
            )

        if df.empty:
            return ORBCandidate(decision=ORBDecision.NOOP)
        latest = df.iloc[-1]
        prev_high = float(latest.get("prev_high", 0.0) or 0.0)
        prev_close = float(latest.get("prev_close", 0.0) or 0.0)
        atr = float(latest.get("atr", 0.0) or 0.0)
        adx = float(latest.get("adx", 0.0) or 0.0)
        trend_ma = float(latest.get("ma", 0.0) or 0.0)
        latest_close = float(latest.get("close", 0.0) or 0.0)
        if prev_high <= 0 or atr <= 0 or trend_ma <= 0:
            return ORBCandidate(decision=ORBDecision.NOOP)

        if current_price <= prev_high or not open_price or open_price <= prev_high:
            return ORBCandidate(decision=ORBDecision.NOOP)

        if latest_close <= trend_ma:
            return ORBCandidate(decision=ORBDecision.NOOP)

        if bool(getattr(settings, "ORB_USE_ADX_FILTER", True)) and adx < float(
            getattr(settings, "ORB_MIN_ADX", 20.0) or 20.0
        ):
            return ORBCandidate(decision=ORBDecision.NOOP)

        asset_type = detect_asset_type(stock_code=stock_code, stock_name=stock_name)
        open_vs_prev_high_pct = compute_extension_pct(open_price, prev_high)
        min_open_gap_pct = max(float(getattr(settings, "ORB_MIN_OPEN_ABOVE_PREV_HIGH_PCT", 0.0) or 0.0), 0.0)
        max_open_gap_pct = self._asset_type_max_pct(
            asset_type,
            getattr(settings, "ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_ETF", 0.0),
            getattr(settings, "ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_STOCK", 0.0),
        )
        if open_vs_prev_high_pct < min_open_gap_pct:
            return ORBCandidate(decision=ORBDecision.NOOP)
        if max_open_gap_pct > 0 and open_vs_prev_high_pct > max_open_gap_pct:
            self._entry_block(
                "orb_gap_too_large",
                symbol=stock_code or "UNKNOWN",
                open_vs_prev_high_pct=f"{open_vs_prev_high_pct:.6f}",
                max_open_gap_pct=f"{max_open_gap_pct:.6f}",
            )
            return ORBCandidate(
                decision=ORBDecision.BLOCKED,
                reason="ORB 시가 갭이 허용 범위를 초과",
                reason_code="orb_gap_too_large",
            )

        bars = self._normalize_intraday_bars(intraday_bars or [], decision_time=now_kst)
        range_minutes = max(int(getattr(settings, "ORB_OPENING_RANGE_MINUTES", 5) or 5), 3)
        if len(bars) < range_minutes:
            return ORBCandidate(decision=ORBDecision.NOOP)

        market_open_at = now_kst.replace(
            hour=MARKET_OPEN.hour,
            minute=MARKET_OPEN.minute,
            second=0,
            microsecond=0,
        )
        minutes_since_open = max((now_kst - market_open_at).total_seconds() / 60.0, 0.0)
        opening_guard_minutes = 0
        if bool(getattr(settings, "ENABLE_OPENING_NO_ENTRY_GUARD", False)):
            opening_guard_minutes = max(int(getattr(settings, "OPENING_NO_ENTRY_MINUTES", 0) or 0), 0)
        entry_start_minutes = max(
            range_minutes,
            max(int(getattr(settings, "ORB_ENTRY_START_MINUTES", 0) or 0), 0),
            opening_guard_minutes,
        )
        entry_cutoff_minutes = max(
            int(getattr(settings, "ORB_ENTRY_CUTOFF_MINUTES", 90) or 90),
            entry_start_minutes,
        )
        if minutes_since_open < entry_start_minutes or minutes_since_open > entry_cutoff_minutes:
            return ORBCandidate(decision=ORBDecision.NOOP)

        opening_range_bars = bars[:range_minutes]
        opening_range_high = max(float(bar["high"]) for bar in opening_range_bars)
        opening_range_low = min(float(bar["low"]) for bar in opening_range_bars)
        if current_price <= opening_range_high:
            return ORBCandidate(decision=ORBDecision.NOOP)

        orb_extension_pct = compute_extension_pct(current_price, opening_range_high)
        max_orb_extension_pct = self._asset_type_max_pct(
            asset_type,
            getattr(settings, "ORB_MAX_EXTENSION_PCT_ETF", 0.0),
            getattr(settings, "ORB_MAX_EXTENSION_PCT_STOCK", 0.0),
        )
        if max_orb_extension_pct > 0 and orb_extension_pct > max_orb_extension_pct:
            self._entry_block(
                "orb_extension_exceeded",
                symbol=stock_code or "UNKNOWN",
                opening_range_high=f"{opening_range_high:.6f}",
                current_price=f"{float(current_price):.6f}",
                extension_pct=f"{orb_extension_pct:.6f}",
                max_allowed_pct=f"{max_orb_extension_pct:.6f}",
            )
            return ORBCandidate(
                decision=ORBDecision.BLOCKED,
                reason="ORB 돌파 추격폭 상한 초과",
                reason_code="orb_extension_exceeded",
                meta={
                    "entry_reference_price": opening_range_high,
                    "entry_reference_label": "opening_range_high",
                    "max_allowed_pct": max_orb_extension_pct,
                },
            )

        recent_lookback = max(int(getattr(settings, "ORB_RECENT_BREAKOUT_LOOKBACK_BARS", 3) or 3), 1)
        rearm_band_pct = max(float(getattr(settings, "ORB_REARM_BAND_PCT", 0.002) or 0.0), 0.0)
        recent_bars = bars[-recent_lookback:]
        rearm_threshold_price = opening_range_high * (1.0 + rearm_band_pct)
        breakout_is_fresh = any(float(bar.get("close", 0.0) or 0.0) <= rearm_threshold_price for bar in recent_bars)
        if not breakout_is_fresh:
            self._entry_block(
                "orb_breakout_not_fresh",
                symbol=stock_code or "UNKNOWN",
                opening_range_high=f"{opening_range_high:.6f}",
                rearm_threshold_price=f"{rearm_threshold_price:.6f}",
            )
            return ORBCandidate(
                decision=ORBDecision.BLOCKED,
                reason="ORB 돌파가 이미 진행되어 fresh breakout 구간이 아님",
                reason_code="orb_breakout_not_fresh",
            )

        vwap = self._compute_vwap(bars)
        if bool(getattr(settings, "ORB_REQUIRE_ABOVE_VWAP", True)) and vwap > 0 and current_price < vwap:
            self._entry_block(
                "orb_below_vwap",
                symbol=stock_code or "UNKNOWN",
                current_price=f"{float(current_price):.6f}",
                vwap=f"{vwap:.6f}",
            )
            return ORBCandidate(
                decision=ORBDecision.BLOCKED,
                reason="ORB 진입 시점이 VWAP 아래라서 차단",
                reason_code="orb_below_vwap",
            )

        meta = {
            "strategy_tag": self.strategy_tag,
            "asset_type": asset_type,
            "prev_high": prev_high,
            "prev_close": prev_close,
            "current_price": float(current_price or 0.0),
            "current_price_at_signal": float(current_price or 0.0),
            "open_price": float(open_price or 0.0),
            "entry_reference_price": opening_range_high,
            "entry_reference_label": "opening_range_high",
            "opening_range_high": opening_range_high,
            "opening_range_low": opening_range_low,
            "opening_range_minutes": range_minutes,
            "extension_pct": orb_extension_pct,
            "open_vs_prev_high_pct": open_vs_prev_high_pct,
            "max_allowed_pct": max_orb_extension_pct,
            "signal_time": now_kst.isoformat(),
            "adx": adx,
            "orb_vwap": vwap,
            "orb_breakout_fresh": True,
            "reason_code": "",
        }

        logger.info(
            "[ORB] trigger_buy symbol=%s opening_range_high=%.6f current_price=%.6f "
            "open_vs_prev_high_pct=%.6f extension_pct=%.6f vwap=%.6f",
            stock_code or "UNKNOWN",
            opening_range_high,
            float(current_price or 0.0),
            open_vs_prev_high_pct,
            orb_extension_pct,
            vwap,
        )
        return ORBCandidate(
            decision=ORBDecision.BUY,
            reason=f"장초 ORB 돌파 ({range_minutes}분 range high {opening_range_high:,.0f})",
            atr=atr,
            trigger_price=opening_range_high,
            meta=meta,
        )
