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


@dataclass
class MarketRegimeLoopContext:
    refresh_attempted: bool = False


@dataclass(frozen=True)
class MarketRegimeRefreshOutcome:
    snapshot: Optional[MarketRegimeSnapshot]
    refreshed: bool
    refresh_attempted: bool
    elapsed_sec: float = 0.0
    budget_exceeded: bool = False
    error: Optional[str] = None
    refresh_skipped_reason: str = ""


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
    refresh_fn: Callable[[datetime], MarketRegimeSnapshot],
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
        )

    if loop_context is not None and loop_context.refresh_attempted:
        return MarketRegimeRefreshOutcome(
            snapshot=current_snapshot,
            refreshed=False,
            refresh_attempted=False,
            refresh_skipped_reason="loop_refresh_already_attempted",
        )

    if loop_context is not None:
        loop_context.refresh_attempted = True

    started_at = monotonic()
    try:
        refreshed_snapshot = refresh_fn(now_kst)
    except Exception as exc:
        return MarketRegimeRefreshOutcome(
            snapshot=current_snapshot,
            refreshed=False,
            refresh_attempted=True,
            elapsed_sec=(monotonic() - started_at),
            error=str(exc),
        )

    elapsed_sec = monotonic() - started_at
    effective_budget_sec = (
        get_market_regime_refresh_budget_sec()
        if budget_sec is None
        else max(float(budget_sec or 0.0), 0.0)
    )
    if effective_budget_sec > 0 and elapsed_sec > effective_budget_sec:
        return MarketRegimeRefreshOutcome(
            snapshot=current_snapshot,
            refreshed=False,
            refresh_attempted=True,
            elapsed_sec=elapsed_sec,
            budget_exceeded=True,
        )

    return MarketRegimeRefreshOutcome(
        snapshot=materialize_market_regime_snapshot(refreshed_snapshot, now_kst),
        refreshed=True,
        refresh_attempted=True,
        elapsed_sec=elapsed_sec,
    )


class MarketRegimeService:
    """Builds a market regime snapshot for the shared main-loop cache."""

    def __init__(self, api: Any):
        self.api = api

    def build_snapshot(self, check_time: Optional[datetime] = None) -> MarketRegimeSnapshot:
        now_kst = ensure_kst(check_time)
        ma_period = max(int(getattr(settings, "MARKET_REGIME_MA_PERIOD", 20) or 20), 1)
        lookback_days = max(int(getattr(settings, "MARKET_REGIME_LOOKBACK_DAYS", 3) or 3), 1)
        bad_3d_return_pct = float(
            getattr(settings, "MARKET_REGIME_BAD_3D_RETURN_PCT", -0.03) or -0.03
        )
        intraday_guard_active = is_opening_guard_window(now_kst)

        kospi = self._load_probe(
            symbol=str(getattr(settings, "MARKET_REGIME_KOSPI_SYMBOL", "069500") or "069500"),
            ma_period=ma_period,
            lookback_days=lookback_days,
            as_of=now_kst,
            load_intraday=intraday_guard_active,
        )
        kosdaq = self._load_probe(
            symbol=str(getattr(settings, "MARKET_REGIME_KOSDAQ_SYMBOL", "229200") or "229200"),
            ma_period=ma_period,
            lookback_days=lookback_days,
            as_of=now_kst,
            load_intraday=intraday_guard_active,
        )

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
        return snapshot

    def _load_probe(
        self,
        symbol: str,
        ma_period: int,
        lookback_days: int,
        as_of: datetime,
        load_intraday: bool,
    ) -> MarketRegimeProbe:
        bars = self.api.get_daily_ohlcv(stock_code=symbol, period_type="D")
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

        intraday = (
            self._load_intraday_probe(symbol)
            if load_intraday
            else {
                "current_price": None,
                "open_price": None,
                "intraday_open_return_pct": None,
            }
        )
        return MarketRegimeProbe(
            symbol=symbol,
            close=close,
            ma=ma,
            return_pct=(close / prev_close) - 1.0,
            above_ma=(close > ma),
            current_price=intraday.get("current_price"),
            open_price=intraday.get("open_price"),
            intraday_open_return_pct=intraday.get("intraday_open_return_pct"),
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
