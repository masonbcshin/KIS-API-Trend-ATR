from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from time import monotonic
from typing import Any, Callable, Optional

import pandas as pd

try:
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.utils.logger import get_logger
    from kis_trend_atr_trading.utils.market_hours import KST, MARKET_OPEN
except ImportError:
    from config import settings
    from utils.logger import get_logger
    from utils.market_hours import KST, MARKET_OPEN


logger = get_logger("market_regime")


class MarketRegime(str, Enum):
    GOOD = "GOOD"
    NEUTRAL = "NEUTRAL"
    BAD = "BAD"


@dataclass(frozen=True)
class MarketRegimeProbe:
    symbol: str
    close: float
    ma: float
    return_pct: float
    above_ma: bool
    current_price: Optional[float] = None
    open_price: Optional[float] = None
    intraday_open_return_pct: Optional[float] = None


@dataclass(frozen=True)
class MarketRegimeSnapshot:
    regime: MarketRegime
    reason: str
    as_of: datetime
    expires_at: datetime
    stale_after: datetime
    is_stale: bool
    kospi_symbol: str
    kosdaq_symbol: str
    kospi_close: float
    kospi_ma: float
    kosdaq_close: float
    kosdaq_ma: float
    kospi_3d_return_pct: float
    kosdaq_3d_return_pct: float
    intraday_guard_active: bool
    intraday_guard_reason: Optional[str]
    source: str = "main_loop_cache"

    def age_sec(self, now: Optional[datetime] = None) -> float:
        return max((ensure_kst(now) - self.as_of).total_seconds(), 0.0)

    def stale_age_sec(self, now: Optional[datetime] = None) -> float:
        return max((ensure_kst(now) - self.stale_after).total_seconds(), 0.0)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return ensure_kst(now) >= self.expires_at

    def with_runtime_status(self, now: Optional[datetime] = None) -> "MarketRegimeSnapshot":
        now_kst = ensure_kst(now)
        return replace(self, is_stale=(now_kst >= self.stale_after))


@dataclass(frozen=True)
class MarketRegimeProbeLoadResult:
    probe: MarketRegimeProbe
    daily_fetch_elapsed_sec: float = 0.0
    intraday_fetch_elapsed_sec: float = 0.0


@dataclass(frozen=True)
class DailyRegimeContext:
    trade_date: str
    refreshed_at: datetime
    context_version: str
    source: str
    success: bool
    stale: bool
    kospi_probe: MarketRegimeProbe
    kosdaq_probe: MarketRegimeProbe
    regime: MarketRegime
    reason: str


@dataclass(frozen=True)
class DailyRegimeContextBuildResult:
    context: DailyRegimeContext
    daily_fetch_elapsed_sec: float = 0.0
    classify_elapsed_sec: float = 0.0
    total_refresh_elapsed_sec: float = 0.0


@dataclass(frozen=True)
class MarketRegimeGuardResult:
    snapshot: MarketRegimeSnapshot
    intraday_fetch_elapsed_sec: float = 0.0
    intraday_guard_elapsed_sec: float = 0.0
    total_refresh_elapsed_sec: float = 0.0
    quote_source: str = ""
    quote_state: str = ""
    intraday_guard_reason: Optional[str] = None


@dataclass(frozen=True)
class MarketRegimeBuildResult:
    snapshot: MarketRegimeSnapshot
    daily_fetch_elapsed_sec: float = 0.0
    intraday_fetch_elapsed_sec: float = 0.0
    classify_elapsed_sec: float = 0.0
    total_refresh_elapsed_sec: float = 0.0
    daily_context_refresh_elapsed_sec: float = 0.0
    intraday_guard_elapsed_sec: float = 0.0
    quote_source: str = ""
    quote_state: str = ""
    daily_context_state: str = ""


@dataclass
class MarketRegimeLoopContext:
    refresh_attempted: bool = False


@dataclass
class MarketRegimeObservationState:
    startup_monotonic: float
    first_snapshot_logged: bool = False
    session_first_snapshot_logged_date: Optional[str] = None
    last_no_snapshot_warning_key: Optional[str] = None


@dataclass(frozen=True)
class MarketRegimeRefreshOutcome:
    snapshot: Optional[MarketRegimeSnapshot]
    refreshed: bool
    refresh_attempted: bool
    elapsed_sec: float = 0.0
    budget_exceeded: bool = False
    error: Optional[str] = None
    refresh_skipped_reason: str = ""
    daily_fetch_elapsed_sec: float = 0.0
    intraday_fetch_elapsed_sec: float = 0.0
    classify_elapsed_sec: float = 0.0
    effective_budget_sec: float = 0.0
    bootstrap_budget_sec: float = 0.0
    used_bootstrap_budget: bool = False
    previous_as_of: Optional[str] = None
    using_previous_snapshot: bool = False

    @property
    def total_refresh_elapsed_sec(self) -> float:
        return max(float(self.elapsed_sec or 0.0), 0.0)


def ensure_kst(check_time: Optional[datetime]) -> datetime:
    if check_time is None:
        return datetime.now(KST)
    if check_time.tzinfo is None:
        return KST.localize(check_time)
    return check_time.astimezone(KST)


def get_market_regime_cache_ttl_sec(check_time: Optional[datetime] = None) -> int:
    default_ttl = max(int(getattr(settings, "MARKET_REGIME_CACHE_TTL_SEC", 60) or 60), 1)
    opening_ttl = max(
        int(getattr(settings, "MARKET_REGIME_OPENING_CACHE_TTL_SEC", default_ttl) or default_ttl),
        1,
    )
    return opening_ttl if is_opening_guard_window(check_time) else default_ttl


def get_market_regime_refresh_budget_sec() -> float:
    return max(float(getattr(settings, "MARKET_REGIME_REFRESH_BUDGET_SEC", 1.5) or 1.5), 0.0)


def get_market_regime_bootstrap_budget_sec() -> float:
    return max(float(getattr(settings, "MARKET_REGIME_BOOTSTRAP_BUDGET_SEC", 3.0) or 3.0), 0.0)


def get_market_regime_daily_context_refresh_sec() -> float:
    return max(
        float(getattr(settings, "MARKET_REGIME_DAILY_CONTEXT_REFRESH_SEC", 300) or 300.0),
        1.0,
    )


def get_market_regime_background_stale_grace_sec() -> float:
    return max(
        float(getattr(settings, "MARKET_REGIME_BACKGROUND_STALE_GRACE_SEC", 180) or 180.0),
        1.0,
    )


def get_market_regime_quote_max_age_sec() -> float:
    return max(float(getattr(settings, "MARKET_REGIME_QUOTE_MAX_AGE_SEC", 15) or 15.0), 0.0)


def get_market_regime_quote_fallback_mode() -> str:
    mode = str(getattr(settings, "MARKET_REGIME_QUOTE_FALLBACK_MODE", "skip") or "skip").strip().lower()
    return mode if mode in ("skip", "rest") else "skip"


def get_market_regime_fail_mode() -> str:
    fail_mode = str(getattr(settings, "MARKET_REGIME_FAIL_MODE", "closed") or "closed").strip().lower()
    return fail_mode if fail_mode in ("open", "closed") else "closed"


def materialize_market_regime_snapshot(
    snapshot: Optional[MarketRegimeSnapshot],
    check_time: Optional[datetime] = None,
) -> Optional[MarketRegimeSnapshot]:
    if snapshot is None:
        return None
    return snapshot.with_runtime_status(ensure_kst(check_time))


def is_market_regime_snapshot_expired(
    snapshot: Optional[MarketRegimeSnapshot],
    check_time: Optional[datetime] = None,
) -> bool:
    if snapshot is None:
        return True
    return materialize_market_regime_snapshot(snapshot, check_time).is_expired(check_time)


def get_daily_regime_context_state(
    context: Optional[DailyRegimeContext],
    *,
    check_time: Optional[datetime] = None,
    stale_after_sec: Optional[float] = None,
    expected_trade_date: Optional[str] = None,
    expected_context_version_prefix: Optional[str] = None,
) -> tuple[Optional[DailyRegimeContext], str]:
    if context is None:
        return None, "absent"

    now_kst = ensure_kst(check_time)
    if expected_trade_date and str(context.trade_date) != str(expected_trade_date):
        return replace(context, stale=True), "trade_date_mismatch"
    if expected_context_version_prefix and not str(context.context_version or "").startswith(
        str(expected_context_version_prefix)
    ):
        return replace(context, stale=True), "version_mismatch"
    if not bool(context.success):
        return replace(context, stale=True), "absent"

    effective_stale_after_sec = (
        get_market_regime_daily_context_refresh_sec()
        if stale_after_sec is None
        else max(float(stale_after_sec or 0.0), 0.0)
    )
    age_sec = max((now_kst - ensure_kst(context.refreshed_at)).total_seconds(), 0.0)
    if effective_stale_after_sec > 0 and age_sec > effective_stale_after_sec:
        return replace(context, stale=True), "stale"
    return replace(context, stale=False), "fresh"


def refresh_shared_market_regime_snapshot(
    current_snapshot: Optional[MarketRegimeSnapshot],
    refresh_fn: Callable[[datetime], Any],
    check_time: Optional[datetime] = None,
    loop_context: Optional[MarketRegimeLoopContext] = None,
    budget_sec: Optional[float] = None,
) -> MarketRegimeRefreshOutcome:
    now_kst = ensure_kst(check_time)
    current_snapshot = materialize_market_regime_snapshot(current_snapshot, now_kst)

    if current_snapshot is not None and not current_snapshot.is_expired(now_kst):
        return MarketRegimeRefreshOutcome(
            snapshot=current_snapshot,
            refreshed=False,
            refresh_attempted=False,
            refresh_skipped_reason="fresh_snapshot",
            previous_as_of=(current_snapshot.as_of.isoformat() if current_snapshot is not None else None),
        )

    if loop_context is not None and loop_context.refresh_attempted:
        return MarketRegimeRefreshOutcome(
            snapshot=current_snapshot,
            refreshed=False,
            refresh_attempted=False,
            refresh_skipped_reason="loop_refresh_already_attempted",
            previous_as_of=(current_snapshot.as_of.isoformat() if current_snapshot is not None else None),
        )

    if loop_context is not None:
        loop_context.refresh_attempted = True

    started_at = monotonic()
    try:
        refreshed_result = refresh_fn(now_kst)
    except Exception as exc:
        return MarketRegimeRefreshOutcome(
            snapshot=current_snapshot,
            refreshed=False,
            refresh_attempted=True,
            elapsed_sec=(monotonic() - started_at),
            error=str(exc),
            previous_as_of=(current_snapshot.as_of.isoformat() if current_snapshot is not None else None),
            using_previous_snapshot=(current_snapshot is not None),
        )

    elapsed_sec = monotonic() - started_at
    try:
        build_result = coerce_market_regime_build_result(
            refreshed_result,
            fallback_total_elapsed_sec=elapsed_sec,
        )
        next_snapshot = materialize_market_regime_snapshot(build_result.snapshot, now_kst)
    except Exception as exc:
        return MarketRegimeRefreshOutcome(
            snapshot=current_snapshot,
            refreshed=False,
            refresh_attempted=True,
            elapsed_sec=elapsed_sec,
            error=str(exc),
            previous_as_of=(current_snapshot.as_of.isoformat() if current_snapshot is not None else None),
            using_previous_snapshot=(current_snapshot is not None),
        )
    total_refresh_elapsed_sec = max(
        elapsed_sec,
        float(build_result.total_refresh_elapsed_sec or 0.0),
    )
    general_budget_sec = (
        get_market_regime_refresh_budget_sec()
        if budget_sec is None
        else max(float(budget_sec or 0.0), 0.0)
    )
    bootstrap_budget_sec = get_market_regime_bootstrap_budget_sec()
    used_bootstrap_budget = current_snapshot is None and bootstrap_budget_sec > 0
    effective_budget_sec = bootstrap_budget_sec if used_bootstrap_budget else general_budget_sec
    previous_as_of = current_snapshot.as_of.isoformat() if current_snapshot is not None else None
    using_previous_snapshot = current_snapshot is not None
    if effective_budget_sec > 0 and total_refresh_elapsed_sec > effective_budget_sec:
        return MarketRegimeRefreshOutcome(
            snapshot=next_snapshot,
            refreshed=(next_snapshot is not None),
            refresh_attempted=True,
            elapsed_sec=total_refresh_elapsed_sec,
            budget_exceeded=True,
            daily_fetch_elapsed_sec=build_result.daily_fetch_elapsed_sec,
            intraday_fetch_elapsed_sec=build_result.intraday_fetch_elapsed_sec,
            classify_elapsed_sec=build_result.classify_elapsed_sec,
            effective_budget_sec=effective_budget_sec,
            bootstrap_budget_sec=bootstrap_budget_sec,
            used_bootstrap_budget=used_bootstrap_budget,
            previous_as_of=previous_as_of,
            using_previous_snapshot=(next_snapshot is None and using_previous_snapshot),
        )

    return MarketRegimeRefreshOutcome(
        snapshot=next_snapshot,
        refreshed=True,
        refresh_attempted=True,
        elapsed_sec=total_refresh_elapsed_sec,
        daily_fetch_elapsed_sec=build_result.daily_fetch_elapsed_sec,
        intraday_fetch_elapsed_sec=build_result.intraday_fetch_elapsed_sec,
        classify_elapsed_sec=build_result.classify_elapsed_sec,
        effective_budget_sec=effective_budget_sec,
        bootstrap_budget_sec=bootstrap_budget_sec,
        used_bootstrap_budget=used_bootstrap_budget,
        previous_as_of=previous_as_of,
        using_previous_snapshot=using_previous_snapshot,
    )


class MarketRegimeService:
    """Builds a market regime snapshot for the shared main-loop cache."""

    def __init__(self, api: Any):
        self.api = api

    @staticmethod
    def _regime_symbols() -> tuple[str, str]:
        return (
            str(getattr(settings, "MARKET_REGIME_KOSPI_SYMBOL", "069500") or "069500"),
            str(getattr(settings, "MARKET_REGIME_KOSDAQ_SYMBOL", "229200") or "229200"),
        )

    @staticmethod
    def _regime_daily_settings() -> tuple[int, int, float]:
        return (
            max(int(getattr(settings, "MARKET_REGIME_MA_PERIOD", 20) or 20), 1),
            max(int(getattr(settings, "MARKET_REGIME_LOOKBACK_DAYS", 3) or 3), 1),
            float(getattr(settings, "MARKET_REGIME_BAD_3D_RETURN_PCT", -0.03) or -0.03),
        )

    def daily_context_version_prefix(self, trade_date: str) -> str:
        ma_period, lookback_days, bad_3d_return_pct = self._regime_daily_settings()
        kospi_symbol, kosdaq_symbol = self._regime_symbols()
        payload = "|".join(
            [
                str(trade_date or ""),
                str(ma_period),
                str(lookback_days),
                f"{bad_3d_return_pct:.6f}",
                kospi_symbol,
                kosdaq_symbol,
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]

    def _build_daily_context_version(
        self,
        *,
        trade_date: str,
        kospi: MarketRegimeProbe,
        kosdaq: MarketRegimeProbe,
        regime: MarketRegime,
        reason: str,
    ) -> str:
        prefix = self.daily_context_version_prefix(trade_date)
        payload = "|".join(
            [
                prefix,
                regime.value,
                str(reason or ""),
                f"{float(kospi.close or 0.0):.6f}",
                f"{float(kospi.ma or 0.0):.6f}",
                f"{float(kospi.return_pct or 0.0):.6f}",
                f"{float(kosdaq.close or 0.0):.6f}",
                f"{float(kosdaq.ma or 0.0):.6f}",
                f"{float(kosdaq.return_pct or 0.0):.6f}",
            ]
        )
        return f"{prefix}:{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:10]}"

    def build_daily_context(
        self,
        *,
        check_time: Optional[datetime] = None,
        include_metrics: bool = False,
        source: str = "main_loop_cache",
    ) -> Any:
        started_at = monotonic()
        now_kst = ensure_kst(check_time)
        trade_date = now_kst.date().isoformat()
        ma_period, lookback_days, bad_3d_return_pct = self._regime_daily_settings()
        kospi_symbol, kosdaq_symbol = self._regime_symbols()

        kospi_result = self._load_probe(
            symbol=kospi_symbol,
            ma_period=ma_period,
            lookback_days=lookback_days,
            as_of=now_kst,
            load_intraday=False,
        )
        kosdaq_result = self._load_probe(
            symbol=kosdaq_symbol,
            ma_period=ma_period,
            lookback_days=lookback_days,
            as_of=now_kst,
            load_intraday=False,
        )
        classify_started_at = monotonic()
        regime, reason = self._classify_daily(
            kospi=kospi_result.probe,
            kosdaq=kosdaq_result.probe,
            bad_3d_return_pct=bad_3d_return_pct,
        )
        classify_elapsed_sec = monotonic() - classify_started_at
        context = DailyRegimeContext(
            trade_date=trade_date,
            refreshed_at=now_kst,
            context_version=self._build_daily_context_version(
                trade_date=trade_date,
                kospi=kospi_result.probe,
                kosdaq=kosdaq_result.probe,
                regime=regime,
                reason=reason,
            ),
            source=source,
            success=True,
            stale=False,
            kospi_probe=kospi_result.probe,
            kosdaq_probe=kosdaq_result.probe,
            regime=regime,
            reason=reason,
        )
        if not include_metrics:
            return context
        return DailyRegimeContextBuildResult(
            context=context,
            daily_fetch_elapsed_sec=(
                kospi_result.daily_fetch_elapsed_sec + kosdaq_result.daily_fetch_elapsed_sec
            ),
            classify_elapsed_sec=classify_elapsed_sec,
            total_refresh_elapsed_sec=(monotonic() - started_at),
        )

    def _build_snapshot_from_context(
        self,
        *,
        daily_context: DailyRegimeContext,
        check_time: datetime,
        regime: MarketRegime,
        reason: str,
        intraday_guard_reason: Optional[str],
        snapshot_source: str,
        intraday_guard_active: bool,
        stale_after_sec_override: Optional[float] = None,
    ) -> MarketRegimeSnapshot:
        ttl_sec = get_market_regime_cache_ttl_sec(check_time)
        stale_max_sec = max(
            float(getattr(settings, "MARKET_REGIME_STALE_MAX_SEC", 180) or 180),
            float(ttl_sec),
        )
        if stale_after_sec_override is not None:
            stale_max_sec = max(stale_max_sec, float(stale_after_sec_override or 0.0))
        return MarketRegimeSnapshot(
            regime=regime,
            reason=reason,
            as_of=check_time,
            expires_at=check_time + timedelta(seconds=ttl_sec),
            stale_after=check_time + timedelta(seconds=stale_max_sec),
            is_stale=False,
            kospi_symbol=daily_context.kospi_probe.symbol,
            kosdaq_symbol=daily_context.kosdaq_probe.symbol,
            kospi_close=daily_context.kospi_probe.close,
            kospi_ma=daily_context.kospi_probe.ma,
            kosdaq_close=daily_context.kosdaq_probe.close,
            kosdaq_ma=daily_context.kosdaq_probe.ma,
            kospi_3d_return_pct=daily_context.kospi_probe.return_pct,
            kosdaq_3d_return_pct=daily_context.kosdaq_probe.return_pct,
            intraday_guard_active=intraday_guard_active,
            intraday_guard_reason=intraday_guard_reason,
            source=snapshot_source,
        )

    @staticmethod
    def _quote_snapshot_to_probe(
        probe: MarketRegimeProbe,
        quote_snapshot: Optional[dict[str, Any]],
    ) -> MarketRegimeProbe:
        if not isinstance(quote_snapshot, dict) or not quote_snapshot:
            return probe
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        open_price = float(quote_snapshot.get("open_price", 0.0) or 0.0)
        intraday_open_return_pct = None
        if current_price > 0 and open_price > 0:
            intraday_open_return_pct = (current_price / open_price) - 1.0
        return replace(
            probe,
            current_price=(current_price if current_price > 0 else None),
            open_price=(open_price if open_price > 0 else None),
            intraday_open_return_pct=intraday_open_return_pct,
        )

    def apply_intraday_guard(
        self,
        daily_context: DailyRegimeContext,
        *,
        check_time: Optional[datetime] = None,
        include_metrics: bool = False,
        quote_snapshot_loader: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
        use_ws_cache_only: bool = False,
        quote_fallback_mode: str = "rest",
        quote_max_age_sec: float = 15.0,
        snapshot_source: str = "main_loop_cache",
        stale_after_sec_override: Optional[float] = None,
        daily_context_state: str = "fresh",
    ) -> Any:
        started_at = monotonic()
        now_kst = ensure_kst(check_time)
        intraday_guard_active = is_opening_guard_window(now_kst)
        regime = daily_context.regime
        reason = daily_context.reason
        intraday_guard_reason = None
        quote_source = "skip"
        quote_state = "absent"
        intraday_fetch_elapsed_sec = 0.0

        kospi = daily_context.kospi_probe
        kosdaq = daily_context.kosdaq_probe

        if intraday_guard_active:
            quote_states: list[str] = []
            quote_sources: list[str] = []
            loader = quote_snapshot_loader if callable(quote_snapshot_loader) else None

            def _load_quote(symbol: str) -> tuple[Optional[dict[str, Any]], str, str]:
                if loader is not None:
                    snapshot = loader(symbol)
                    if isinstance(snapshot, dict) and snapshot:
                        raw_age_sec = snapshot.get("quote_age_sec")
                        age_sec = (
                            float(raw_age_sec)
                            if raw_age_sec is not None
                            else float("inf")
                        )
                        received_at = snapshot.get("received_at")
                        if age_sec == float("inf") and isinstance(received_at, datetime):
                            current_now = datetime.now(received_at.tzinfo) if received_at.tzinfo else datetime.now()
                            age_sec = max((current_now - received_at).total_seconds(), 0.0)
                        if age_sec <= max(float(quote_max_age_sec or 0.0), 0.0):
                            return snapshot, "ws_cache", "fresh"
                        if use_ws_cache_only or str(quote_fallback_mode or "skip").lower() != "rest":
                            return None, "skip", "stale"
                    else:
                        if use_ws_cache_only or str(quote_fallback_mode or "skip").lower() != "rest":
                            return None, "skip", "absent"
                if use_ws_cache_only:
                    return None, "skip", "absent"
                if str(quote_fallback_mode or "rest").lower() != "rest":
                    return None, "skip", "absent"
                intraday = self._load_intraday_probe(symbol)
                if intraday.get("current_price") is None or intraday.get("open_price") is None:
                    return None, "rest", "absent"
                return (
                    {
                        "current_price": intraday.get("current_price"),
                        "open_price": intraday.get("open_price"),
                        "received_at": now_kst,
                        "quote_age_sec": 0.0,
                        "source": "rest_quote",
                    },
                    "rest",
                    "fresh",
                )

            intraday_fetch_started_at = monotonic()
            kospi_quote, kospi_source, kospi_state = _load_quote(daily_context.kospi_probe.symbol)
            kosdaq_quote, kosdaq_source, kosdaq_state = _load_quote(daily_context.kosdaq_probe.symbol)
            intraday_fetch_elapsed_sec = monotonic() - intraday_fetch_started_at

            quote_states.extend([kospi_state, kosdaq_state])
            quote_sources.extend([kospi_source, kosdaq_source])
            if "rest" in quote_sources:
                quote_source = "rest"
            elif "ws_cache" in quote_sources:
                quote_source = "ws_cache"
            else:
                quote_source = "skip"
            if "fresh" in quote_states:
                quote_state = "fresh"
            elif "stale" in quote_states:
                quote_state = "stale"
            else:
                quote_state = "absent"

            if kospi_quote is not None:
                kospi = self._quote_snapshot_to_probe(kospi, kospi_quote)
            if kosdaq_quote is not None:
                kosdaq = self._quote_snapshot_to_probe(kosdaq, kosdaq_quote)

            regime, reason, intraday_guard_reason = self._apply_intraday_guard(
                regime=regime,
                reason=reason,
                kospi=kospi,
                kosdaq=kosdaq,
                check_time=now_kst,
            )

        guard_elapsed_sec = monotonic() - started_at
        snapshot = self._build_snapshot_from_context(
            daily_context=daily_context,
            check_time=now_kst,
            regime=regime,
            reason=reason,
            intraday_guard_reason=intraday_guard_reason,
            snapshot_source=snapshot_source,
            intraday_guard_active=intraday_guard_active,
            stale_after_sec_override=stale_after_sec_override,
        )
        self._log_snapshot(snapshot)
        if not include_metrics:
            return snapshot
        return MarketRegimeBuildResult(
            snapshot=snapshot,
            daily_fetch_elapsed_sec=0.0,
            intraday_fetch_elapsed_sec=intraday_fetch_elapsed_sec,
            classify_elapsed_sec=0.0,
            total_refresh_elapsed_sec=guard_elapsed_sec,
            daily_context_refresh_elapsed_sec=0.0,
            intraday_guard_elapsed_sec=guard_elapsed_sec,
            quote_source=quote_source,
            quote_state=quote_state,
            daily_context_state=daily_context_state,
        )

    def build_snapshot(
        self,
        check_time: Optional[datetime] = None,
        include_metrics: bool = False,
    ) -> Any:
        started_at = monotonic()
        now_kst = ensure_kst(check_time)
        daily_result = self.build_daily_context(
            check_time=now_kst,
            include_metrics=True,
            source="main_loop_cache",
        )
        build_result = self.apply_intraday_guard(
            daily_result.context,
            check_time=now_kst,
            include_metrics=True,
            quote_snapshot_loader=None,
            use_ws_cache_only=False,
            quote_fallback_mode="rest",
            quote_max_age_sec=get_market_regime_quote_max_age_sec(),
            snapshot_source="main_loop_cache",
            daily_context_state="fresh",
        )
        if not include_metrics:
            return build_result.snapshot
        return MarketRegimeBuildResult(
            snapshot=build_result.snapshot,
            daily_fetch_elapsed_sec=daily_result.daily_fetch_elapsed_sec,
            intraday_fetch_elapsed_sec=build_result.intraday_fetch_elapsed_sec,
            classify_elapsed_sec=daily_result.classify_elapsed_sec,
            total_refresh_elapsed_sec=max(
                float(monotonic() - started_at),
                float(daily_result.total_refresh_elapsed_sec + build_result.total_refresh_elapsed_sec),
            ),
            daily_context_refresh_elapsed_sec=daily_result.total_refresh_elapsed_sec,
            intraday_guard_elapsed_sec=build_result.intraday_guard_elapsed_sec,
            quote_source=build_result.quote_source,
            quote_state=build_result.quote_state,
            daily_context_state="fresh",
        )

    def _load_probe(
        self,
        symbol: str,
        ma_period: int,
        lookback_days: int,
        as_of: datetime,
        load_intraday: bool,
    ) -> MarketRegimeProbeLoadResult:
        daily_fetch_started_at = monotonic()
        bars = self.api.get_daily_ohlcv(stock_code=symbol, period_type="D")
        daily_fetch_elapsed_sec = monotonic() - daily_fetch_started_at
        if not isinstance(bars, pd.DataFrame) or bars.empty:
            raise ValueError(f"market regime daily data missing: symbol={symbol}")

        df = bars.copy()
        if "date" not in df.columns or "close" not in df.columns:
            raise ValueError(f"market regime required columns missing: symbol={symbol}")

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
        df = df[df["date"].dt.date < as_of.date()].reset_index(drop=True)

        required_rows = max(ma_period, lookback_days + 1)
        if len(df) < required_rows:
            raise ValueError(
                f"market regime insufficient history: symbol={symbol} "
                f"rows={len(df)} required={required_rows}"
            )

        close_series = pd.to_numeric(df["close"], errors="coerce")
        ma_series = close_series.rolling(window=ma_period).mean()

        close = float(close_series.iloc[-1])
        ma = float(ma_series.iloc[-1])
        prev_close = float(close_series.iloc[-(lookback_days + 1)])
        if prev_close <= 0:
            raise ValueError(f"market regime invalid previous close: symbol={symbol}")

        intraday_fetch_elapsed_sec = 0.0
        if load_intraday:
            intraday_fetch_started_at = monotonic()
            intraday = self._load_intraday_probe(symbol)
            intraday_fetch_elapsed_sec = monotonic() - intraday_fetch_started_at
        else:
            intraday = {
                "current_price": None,
                "open_price": None,
                "intraday_open_return_pct": None,
            }
        return MarketRegimeProbeLoadResult(
            probe=MarketRegimeProbe(
                symbol=symbol,
                close=close,
                ma=ma,
                return_pct=(close / prev_close) - 1.0,
                above_ma=(close > ma),
                current_price=intraday.get("current_price"),
                open_price=intraday.get("open_price"),
                intraday_open_return_pct=intraday.get("intraday_open_return_pct"),
            ),
            daily_fetch_elapsed_sec=daily_fetch_elapsed_sec,
            intraday_fetch_elapsed_sec=intraday_fetch_elapsed_sec,
        )

    def _load_intraday_probe(self, symbol: str) -> dict[str, Optional[float]]:
        try:
            price_data = self.api.get_current_price(symbol) or {}
        except Exception as exc:
            logger.warning(
                "[MARKET_REGIME] intraday_quote_unavailable symbol=%s error=%s",
                symbol,
                exc,
            )
            return {
                "current_price": None,
                "open_price": None,
                "intraday_open_return_pct": None,
            }

        current_price = float(price_data.get("current_price", 0.0) or 0.0)
        open_price = float(price_data.get("open_price", 0.0) or 0.0)
        intraday_open_return_pct = None
        if current_price > 0 and open_price > 0:
            intraday_open_return_pct = (current_price / open_price) - 1.0
        return {
            "current_price": current_price if current_price > 0 else None,
            "open_price": open_price if open_price > 0 else None,
            "intraday_open_return_pct": intraday_open_return_pct,
        }

    @staticmethod
    def _classify_daily(
        kospi: MarketRegimeProbe,
        kosdaq: MarketRegimeProbe,
        bad_3d_return_pct: float,
    ) -> tuple[MarketRegime, str]:
        if (not kospi.above_ma) and (not kosdaq.above_ma):
            return MarketRegime.BAD, "both_below_ma"

        if kospi.above_ma and kosdaq.above_ma:
            if kospi.return_pct > bad_3d_return_pct and kosdaq.return_pct > bad_3d_return_pct:
                return MarketRegime.GOOD, "both_above_ma_and_stable_3d"
            return MarketRegime.NEUTRAL, "above_ma_but_weak_recent_return"

        return MarketRegime.NEUTRAL, "mixed_ma_trend"

    def _apply_intraday_guard(
        self,
        regime: MarketRegime,
        reason: str,
        kospi: MarketRegimeProbe,
        kosdaq: MarketRegimeProbe,
        check_time: datetime,
    ) -> tuple[MarketRegime, str, Optional[str]]:
        intraday_drop_pct = float(
            getattr(settings, "MARKET_REGIME_INTRADAY_DROP_PCT", -0.015) or -0.015
        )
        if intraday_drop_pct >= 0 or not is_opening_guard_window(check_time):
            return regime, reason, None

        for probe in (kospi, kosdaq):
            drop_pct = probe.intraday_open_return_pct
            if drop_pct is None:
                continue
            if drop_pct <= intraday_drop_pct:
                logger.info(
                    "[MARKET_REGIME] regime_changed=BAD reason=intraday_drop "
                    "symbol=%s intraday_open_return_pct=%.6f threshold=%.6f",
                    probe.symbol,
                    drop_pct,
                    intraday_drop_pct,
                )
                return MarketRegime.BAD, "intraday_drop_guard", f"intraday_drop:{probe.symbol}"

        return regime, reason, None

    def _log_snapshot(self, snapshot: MarketRegimeSnapshot) -> None:
        logger.info(
            "[MARKET_REGIME] snapshot_built regime=%s reason=%s as_of=%s "
            "kospi_symbol=%s kosdaq_symbol=%s kospi_close=%.6f kospi_ma20=%.6f "
            "kosdaq_close=%.6f kosdaq_ma20=%.6f kospi_3d_return_pct=%.6f "
            "kosdaq_3d_return_pct=%.6f",
            snapshot.regime.value,
            snapshot.reason,
            snapshot.as_of.isoformat(),
            snapshot.kospi_symbol,
            snapshot.kosdaq_symbol,
            snapshot.kospi_close,
            snapshot.kospi_ma,
            snapshot.kosdaq_close,
            snapshot.kosdaq_ma,
            snapshot.kospi_3d_return_pct,
            snapshot.kosdaq_3d_return_pct,
        )


def is_opening_guard_window(check_time: Optional[datetime] = None) -> bool:
    now_kst = ensure_kst(check_time)
    guard_minutes = max(
        int(getattr(settings, "MARKET_REGIME_OPENING_GUARD_MINUTES", 30) or 0),
        0,
    )
    if guard_minutes <= 0:
        return False

    market_open_dt = now_kst.replace(
        hour=MARKET_OPEN.hour,
        minute=MARKET_OPEN.minute,
        second=0,
        microsecond=0,
    )
    elapsed_sec = (now_kst - market_open_dt).total_seconds()
    return 0 <= elapsed_sec <= (guard_minutes * 60)


def coerce_market_regime_build_result(
    raw_result: Any,
    *,
    fallback_total_elapsed_sec: float = 0.0,
) -> MarketRegimeBuildResult:
    if isinstance(raw_result, MarketRegimeBuildResult):
        total_refresh_elapsed_sec = max(
            float(raw_result.total_refresh_elapsed_sec or 0.0),
            float(fallback_total_elapsed_sec or 0.0),
        )
        return replace(raw_result, total_refresh_elapsed_sec=total_refresh_elapsed_sec)
    if isinstance(raw_result, MarketRegimeSnapshot):
        return MarketRegimeBuildResult(
            snapshot=raw_result,
            total_refresh_elapsed_sec=max(float(fallback_total_elapsed_sec or 0.0), 0.0),
        )
    raise TypeError(
        "refresh_fn must return MarketRegimeSnapshot or MarketRegimeBuildResult: "
        f"type={type(raw_result).__name__}"
    )


def log_market_regime_refresh_outcome(
    outcome: MarketRegimeRefreshOutcome,
    snapshot: Optional[MarketRegimeSnapshot],
) -> None:
    if outcome.refreshed and snapshot is not None:
        ttl_sec = max((snapshot.expires_at - snapshot.as_of).total_seconds(), 0.0)
        logger.info(
                "[MARKET_REGIME] snapshot_updated regime=%s reason=%s as_of=%s "
            "expires_at=%s ttl_sec=%.1f source=%s budget_exceeded=%s "
            "kospi_symbol=%s kosdaq_symbol=%s kospi_close=%.6f kospi_ma=%.6f "
            "kosdaq_close=%.6f kosdaq_ma=%.6f total_refresh_elapsed_sec=%.3f "
            "daily_fetch_elapsed_sec=%.3f intraday_fetch_elapsed_sec=%.3f "
            "classify_elapsed_sec=%.3f",
            snapshot.regime.value,
            snapshot.reason,
            snapshot.as_of.isoformat(),
            snapshot.expires_at.isoformat(),
            ttl_sec,
            snapshot.source,
            str(bool(outcome.budget_exceeded)).lower(),
            snapshot.kospi_symbol,
            snapshot.kosdaq_symbol,
            snapshot.kospi_close,
            snapshot.kospi_ma,
            snapshot.kosdaq_close,
            snapshot.kosdaq_ma,
            outcome.total_refresh_elapsed_sec,
            max(float(outcome.daily_fetch_elapsed_sec or 0.0), 0.0),
            max(float(outcome.intraday_fetch_elapsed_sec or 0.0), 0.0),
            max(float(outcome.classify_elapsed_sec or 0.0), 0.0),
        )

    if outcome.budget_exceeded:
        logger.warning(
            "[MARKET_REGIME] snapshot_refresh_budget_exceeded budget_sec=%.3f "
            "total_refresh_elapsed_sec=%.3f daily_fetch_elapsed_sec=%.3f "
            "intraday_fetch_elapsed_sec=%.3f classify_elapsed_sec=%.3f "
            "using_previous_snapshot=%s previous_as_of=%s adopted_as_of=%s",
            max(float(outcome.effective_budget_sec or 0.0), 0.0),
            outcome.total_refresh_elapsed_sec,
            max(float(outcome.daily_fetch_elapsed_sec or 0.0), 0.0),
            max(float(outcome.intraday_fetch_elapsed_sec or 0.0), 0.0),
            max(float(outcome.classify_elapsed_sec or 0.0), 0.0),
            str(bool(outcome.using_previous_snapshot)).lower(),
            outcome.previous_as_of or "none",
            snapshot.as_of.isoformat() if snapshot is not None else "none",
        )


def observe_market_regime_snapshot(
    *,
    observation_state: MarketRegimeObservationState,
    snapshot: Optional[MarketRegimeSnapshot],
    now_kst: Optional[datetime] = None,
    in_session: bool = False,
    filter_enabled: bool = False,
) -> None:
    now_kst = ensure_kst(now_kst)
    if snapshot is not None and not observation_state.first_snapshot_logged:
        logger.info(
            "[MARKET_REGIME] first_snapshot_created as_of=%s startup_to_first_snapshot_sec=%.3f",
            snapshot.as_of.isoformat(),
            max(monotonic() - observation_state.startup_monotonic, 0.0),
        )
        observation_state.first_snapshot_logged = True

    market_open_dt = now_kst.replace(
        hour=MARKET_OPEN.hour,
        minute=MARKET_OPEN.minute,
        second=0,
        microsecond=0,
    )
    session_key = market_open_dt.strftime("%Y-%m-%d")
    if in_session and snapshot is not None and observation_state.session_first_snapshot_logged_date != session_key:
        logger.info(
            "[MARKET_REGIME] session_first_snapshot_created as_of=%s session_start_to_first_snapshot_sec=%.3f",
            snapshot.as_of.isoformat(),
            max((snapshot.as_of - market_open_dt).total_seconds(), 0.0),
        )
        observation_state.session_first_snapshot_logged_date = session_key

    if snapshot is not None or not in_session:
        observation_state.last_no_snapshot_warning_key = None

    if not in_session or not filter_enabled or snapshot is not None:
        return

    elapsed_since_session_start_sec = max((now_kst - market_open_dt).total_seconds(), 0.0)
    if elapsed_since_session_start_sec > 1800:
        return

    warning_key = f"{session_key}:{int(elapsed_since_session_start_sec // 60)}"
    if observation_state.last_no_snapshot_warning_key == warning_key:
        return
    observation_state.last_no_snapshot_warning_key = warning_key
    logger.warning(
        "[MARKET_REGIME] no_snapshot_yet elapsed_since_session_start_sec=%.1f fail_mode=%s",
        elapsed_since_session_start_sec,
        get_market_regime_fail_mode(),
    )
