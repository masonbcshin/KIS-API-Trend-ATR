from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable, Optional

try:
    from kis_trend_atr_trading.utils.market_hours import MarketSessionState, get_market_session_state
except ImportError:
    from utils.market_hours import MarketSessionState, get_market_session_state


class TradingVenue(str, Enum):
    KRX = "KRX"
    NXT = "NXT"


class VenueMarketPhase(str, Enum):
    OFF_SESSION = "OFF_SESSION"
    KRX_PREOPEN = "KRX_PREOPEN"
    KRX_CONTINUOUS = "KRX_CONTINUOUS"
    KRX_AUCTION = "KRX_AUCTION"
    KRX_POSTCLOSE = "KRX_POSTCLOSE"
    NXT_PREMARKET = "NXT_PREMARKET"
    NXT_MAIN_MARKET = "NXT_MAIN_MARKET"
    NXT_AUCTION_PAUSE = "NXT_AUCTION_PAUSE"
    NXT_AFTERMARKET = "NXT_AFTERMARKET"


@dataclass(frozen=True)
class MarketPhaseContext:
    venue: TradingVenue
    phase: VenueMarketPhase
    source_session_state: str


def _normalize_venue(venue: Optional[object]) -> TradingVenue:
    raw = getattr(venue, "value", venue)
    token = str(raw or TradingVenue.KRX.value).strip().upper()
    return TradingVenue.NXT if token == TradingVenue.NXT.value else TradingVenue.KRX


def _normalize_session_state(session_state: Optional[object]) -> Optional[MarketSessionState]:
    if session_state is None:
        return None
    if isinstance(session_state, MarketSessionState):
        return session_state
    raw = getattr(session_state, "value", session_state)
    token = str(raw or "").strip().upper()
    for state in MarketSessionState:
        if token == state.value:
            return state
    return None


def resolve_market_phase_context(
    *,
    check_time: Optional[datetime] = None,
    venue: Optional[object] = None,
    session_state: Optional[object] = None,
) -> MarketPhaseContext:
    normalized_venue = _normalize_venue(venue)
    normalized_state = _normalize_session_state(session_state)
    if normalized_state is None:
        normalized_state, _ = get_market_session_state(now=check_time)

    if normalized_state == MarketSessionState.IN_SESSION:
        phase = (
            VenueMarketPhase.NXT_MAIN_MARKET
            if normalized_venue == TradingVenue.NXT
            else VenueMarketPhase.KRX_CONTINUOUS
        )
    elif normalized_state == MarketSessionState.PREOPEN_WARMUP:
        phase = (
            VenueMarketPhase.NXT_PREMARKET
            if normalized_venue == TradingVenue.NXT
            else VenueMarketPhase.KRX_PREOPEN
        )
    elif normalized_state == MarketSessionState.AUCTION_GUARD:
        phase = (
            VenueMarketPhase.NXT_AUCTION_PAUSE
            if normalized_venue == TradingVenue.NXT
            else VenueMarketPhase.KRX_AUCTION
        )
    elif normalized_state == MarketSessionState.POSTCLOSE:
        phase = (
            VenueMarketPhase.NXT_AFTERMARKET
            if normalized_venue == TradingVenue.NXT
            else VenueMarketPhase.KRX_POSTCLOSE
        )
    else:
        phase = VenueMarketPhase.OFF_SESSION

    return MarketPhaseContext(
        venue=normalized_venue,
        phase=phase,
        source_session_state=normalized_state.value,
    )


def is_main_market_phase(phase: Optional[object]) -> bool:
    raw = getattr(phase, "value", phase)
    token = str(raw or "").strip().upper()
    return token in {
        VenueMarketPhase.KRX_CONTINUOUS.value,
        VenueMarketPhase.NXT_MAIN_MARKET.value,
    }


def phase_allowed_for_entry(
    phase: Optional[object],
    *,
    allowed_venues: Optional[Iterable[object]] = None,
) -> bool:
    if not is_main_market_phase(phase):
        return False

    if allowed_venues is None:
        return True

    allowed_tokens = {
        _normalize_venue(item).value
        for item in list(allowed_venues or [])
    }
    if not allowed_tokens:
        return True

    raw = getattr(phase, "value", phase)
    token = str(raw or "").strip().upper()
    if token == VenueMarketPhase.KRX_CONTINUOUS.value:
        return TradingVenue.KRX.value in allowed_tokens
    if token == VenueMarketPhase.NXT_MAIN_MARKET.value:
        return TradingVenue.NXT.value in allowed_tokens
    return False
