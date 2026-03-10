from __future__ import annotations

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
class MarketRegimeBuildResult:
    snapshot: MarketRegimeSnapshot
    daily_fetch_elapsed_sec: float = 0.0
    intraday_fetch_elapsed_sec: float = 0.0
    classify_elapsed_sec: float = 0.0
    total_refresh_elapsed_sec: float = 0.0


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
    build_result = coerce_market_regime_build_result(
        refreshed_result,
        fallback_total_elapsed_sec=elapsed_sec,
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
            snapshot=current_snapshot,
            refreshed=False,
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
            using_previous_snapshot=using_previous_snapshot,
        )

    return MarketRegimeRefreshOutcome(
        snapshot=materialize_market_regime_snapshot(build_result.snapshot, now_kst),
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

    def build_snapshot(
        self,
        check_time: Optional[datetime] = None,
        include_metrics: bool = False,
    ) -> Any:
        started_at = monotonic()
        now_kst = ensure_kst(check_time)
        ma_period = max(int(getattr(settings, "MARKET_REGIME_MA_PERIOD", 20) or 20), 1)
        lookback_days = max(int(getattr(settings, "MARKET_REGIME_LOOKBACK_DAYS", 3) or 3), 1)
        bad_3d_return_pct = float(
            getattr(settings, "MARKET_REGIME_BAD_3D_RETURN_PCT", -0.03) or -0.03
        )
        intraday_guard_active = is_opening_guard_window(now_kst)

        kospi_result = self._load_probe(
            symbol=str(getattr(settings, "MARKET_REGIME_KOSPI_SYMBOL", "069500") or "069500"),
            ma_period=ma_period,
            lookback_days=lookback_days,
            as_of=now_kst,
            load_intraday=intraday_guard_active,
        )
        kosdaq_result = self._load_probe(
            symbol=str(getattr(settings, "MARKET_REGIME_KOSDAQ_SYMBOL", "229200") or "229200"),
            ma_period=ma_period,
            lookback_days=lookback_days,
            as_of=now_kst,
            load_intraday=intraday_guard_active,
        )
        kospi = kospi_result.probe
        kosdaq = kosdaq_result.probe

        classify_started_at = monotonic()
        regime, reason = self._classify_daily(
            kospi=kospi,
            kosdaq=kosdaq,
            bad_3d_return_pct=bad_3d_return_pct,
        )
        regime, reason, intraday_guard_reason = self._apply_intraday_guard(
            regime=regime,
            reason=reason,
            kospi=kospi,
            kosdaq=kosdaq,
            check_time=now_kst,
        )
        classify_elapsed_sec = monotonic() - classify_started_at

        ttl_sec = get_market_regime_cache_ttl_sec(now_kst)
        stale_max_sec = max(
            float(getattr(settings, "MARKET_REGIME_STALE_MAX_SEC", 180) or 180),
            float(ttl_sec),
        )
        snapshot = MarketRegimeSnapshot(
            regime=regime,
            reason=reason,
            as_of=now_kst,
            expires_at=now_kst + timedelta(seconds=ttl_sec),
            stale_after=now_kst + timedelta(seconds=stale_max_sec),
            is_stale=False,
            kospi_symbol=kospi.symbol,
            kosdaq_symbol=kosdaq.symbol,
            kospi_close=kospi.close,
            kospi_ma=kospi.ma,
            kosdaq_close=kosdaq.close,
            kosdaq_ma=kosdaq.ma,
            kospi_3d_return_pct=kospi.return_pct,
            kosdaq_3d_return_pct=kosdaq.return_pct,
            intraday_guard_active=intraday_guard_active,
            intraday_guard_reason=intraday_guard_reason,
            source="main_loop_cache",
        )
        self._log_snapshot(snapshot)
        if not include_metrics:
            return snapshot
        return MarketRegimeBuildResult(
            snapshot=snapshot,
            daily_fetch_elapsed_sec=(
                kospi_result.daily_fetch_elapsed_sec + kosdaq_result.daily_fetch_elapsed_sec
            ),
            intraday_fetch_elapsed_sec=(
                kospi_result.intraday_fetch_elapsed_sec + kosdaq_result.intraday_fetch_elapsed_sec
            ),
            classify_elapsed_sec=classify_elapsed_sec,
            total_refresh_elapsed_sec=(monotonic() - started_at),
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
            "[MARKET_REGIME] regime=%s reason=%s kospi_symbol=%s kosdaq_symbol=%s "
            "kospi_close=%.6f kospi_ma20=%.6f kosdaq_close=%.6f kosdaq_ma20=%.6f "
            "kospi_3d_return_pct=%.6f kosdaq_3d_return_pct=%.6f",
            snapshot.regime.value,
            snapshot.reason,
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
            "expires_at=%s ttl_sec=%.1f source=%s kospi_symbol=%s kosdaq_symbol=%s "
            "kospi_close=%.6f kospi_ma=%.6f kosdaq_close=%.6f kosdaq_ma=%.6f "
            "total_refresh_elapsed_sec=%.3f daily_fetch_elapsed_sec=%.3f "
            "intraday_fetch_elapsed_sec=%.3f classify_elapsed_sec=%.3f",
            snapshot.regime.value,
            snapshot.reason,
            snapshot.as_of.isoformat(),
            snapshot.expires_at.isoformat(),
            ttl_sec,
            snapshot.source,
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
        return

    if outcome.budget_exceeded:
        logger.warning(
            "[MARKET_REGIME] snapshot_refresh_budget_exceeded budget_sec=%.3f "
            "total_refresh_elapsed_sec=%.3f daily_fetch_elapsed_sec=%.3f "
            "intraday_fetch_elapsed_sec=%.3f classify_elapsed_sec=%.3f "
            "using_previous_snapshot=%s previous_as_of=%s",
            max(float(outcome.effective_budget_sec or 0.0), 0.0),
            outcome.total_refresh_elapsed_sec,
            max(float(outcome.daily_fetch_elapsed_sec or 0.0), 0.0),
            max(float(outcome.intraday_fetch_elapsed_sec or 0.0), 0.0),
            max(float(outcome.classify_elapsed_sec or 0.0), 0.0),
            str(bool(outcome.using_previous_snapshot)).lower(),
            outcome.previous_as_of or "none",
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
