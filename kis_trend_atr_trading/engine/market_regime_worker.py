from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

try:
    from config import settings
    from utils.logger import get_logger
    from utils.market_hours import KST
    from utils.market_regime import (
        DailyRegimeContext,
        MarketRegimeService,
        MarketRegimeSnapshot,
        ensure_kst,
        get_daily_regime_context_state,
        get_market_regime_background_stale_grace_sec,
        get_market_regime_daily_context_refresh_sec,
        get_market_regime_quote_fallback_mode,
        get_market_regime_quote_max_age_sec,
        materialize_market_regime_snapshot,
    )
except ImportError:
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.utils.logger import get_logger
    from kis_trend_atr_trading.utils.market_hours import KST
    from kis_trend_atr_trading.utils.market_regime import (
        DailyRegimeContext,
        MarketRegimeService,
        MarketRegimeSnapshot,
        ensure_kst,
        get_daily_regime_context_state,
        get_market_regime_background_stale_grace_sec,
        get_market_regime_daily_context_refresh_sec,
        get_market_regime_quote_fallback_mode,
        get_market_regime_quote_max_age_sec,
        materialize_market_regime_snapshot,
    )


logger = get_logger("market_regime_worker")


class MarketRegimeRefreshThread(threading.Thread):
    def __init__(
        self,
        *,
        service: MarketRegimeService,
        quote_snapshot_loader: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
        stop_event: Optional[threading.Event] = None,
        on_error: Optional[Callable[[str, Exception], None]] = None,
    ) -> None:
        super().__init__(name="MarketRegimeRefreshThread", daemon=True)
        self._service = service
        self._quote_snapshot_loader = quote_snapshot_loader
        self._stop_event = stop_event or threading.Event()
        self._on_error = on_error
        self._lock = threading.Lock()
        self._snapshot: Optional[MarketRegimeSnapshot] = None
        self._daily_context: Optional[DailyRegimeContext] = None
        self._last_trade_date: str = ""
        self._bootstrap_attempted: bool = False
        self._refresh_fail_count: int = 0
        self._background_refresh_ms: float = 0.0
        self._daily_context_refresh_ms: float = 0.0
        self._intraday_guard_ms: float = 0.0
        self._quote_source: str = "skip"
        self._quote_state: str = "absent"
        self._daily_context_state: str = "absent"
        self._last_success_at: Optional[datetime] = None
        self._error_state: str = ""
        self._refresh_state: str = "bootstrap_pending"

    def get_snapshot(self, check_time: Optional[datetime] = None) -> Optional[MarketRegimeSnapshot]:
        with self._lock:
            snapshot = self._snapshot
        return materialize_market_regime_snapshot(snapshot, check_time)

    def get_status(self, now: Optional[datetime] = None) -> dict[str, Any]:
        now_kst = ensure_kst(now)
        with self._lock:
            snapshot = self._snapshot
            daily_context = self._daily_context
            last_success_at = self._last_success_at
            refresh_state = self._refresh_state
            error_state = self._error_state
            fail_count = self._refresh_fail_count
            background_refresh_ms = self._background_refresh_ms
            daily_context_refresh_ms = self._daily_context_refresh_ms
            intraday_guard_ms = self._intraday_guard_ms
            quote_source = self._quote_source
            quote_state = self._quote_state
            daily_context_state = self._daily_context_state
            bootstrap_attempted = self._bootstrap_attempted
        snapshot = materialize_market_regime_snapshot(snapshot, now_kst)
        last_success_age_sec = -1.0
        if last_success_at is not None:
            last_success_age_sec = max((now_kst - ensure_kst(last_success_at)).total_seconds(), 0.0)
        if snapshot is not None and snapshot.is_stale:
            refresh_state = "background_stale"
        elif snapshot is None and bootstrap_attempted:
            refresh_state = "bootstrap_pending" if not error_state else "refresh_fail"
        elif error_state:
            refresh_state = "refresh_fail"
        return {
            "snapshot": snapshot,
            "daily_context": daily_context,
            "refresh_state": refresh_state,
            "market_regime_background_refresh_ms": float(background_refresh_ms or 0.0),
            "market_regime_daily_context_refresh_ms": float(daily_context_refresh_ms or 0.0),
            "market_regime_intraday_guard_ms": float(intraday_guard_ms or 0.0),
            "market_regime_quote_source": quote_source or "skip",
            "market_regime_quote_state": quote_state or "absent",
            "market_regime_daily_context_state": daily_context_state or "absent",
            "market_regime_background_last_success_age_sec": float(last_success_age_sec),
            "market_regime_background_refresh_fail_count": int(fail_count or 0),
            "market_regime_worker_error_state": error_state or "",
        }

    def run(self) -> None:
        try:
            interval_sec = max(
                float(getattr(settings, "MARKET_REGIME_REFRESH_INTERVAL_SEC", 30) or 30.0),
                1.0,
            )
            force_on_trade_date_change = bool(
                getattr(settings, "MARKET_REGIME_FORCE_DAILY_REFRESH_ON_TRADE_DATE_CHANGE", True)
            )
            force_daily = True
            while not self._stop_event.is_set():
                self._bootstrap_attempted = True
                self._run_cycle(force_daily=force_daily)
                force_daily = False
                if self._stop_event.is_set():
                    break
                force_daily = self._wait_until_next_cycle(
                    interval_sec=interval_sec,
                    force_on_trade_date_change=force_on_trade_date_change,
                )
        except Exception as exc:
            self._record_failure(
                exc=exc,
                now_kst=ensure_kst(datetime.now(KST)),
                refresh_state="refresh_fail",
                daily_context_state="absent",
            )

    def _wait_until_next_cycle(self, *, interval_sec: float, force_on_trade_date_change: bool) -> bool:
        deadline = time.monotonic() + max(float(interval_sec or 0.0), 0.0)
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if force_on_trade_date_change:
                current_trade_date = datetime.now(KST).date().isoformat()
                with self._lock:
                    previous_trade_date = self._last_trade_date
                if previous_trade_date and current_trade_date != previous_trade_date:
                    logger.info(
                        "[MARKET_REGIME_BG] trade_date_changed previous=%s current=%s force_daily_refresh=true",
                        previous_trade_date,
                        current_trade_date,
                    )
                    return True
            self._stop_event.wait(min(1.0, remaining))
        return False

    def _run_cycle(self, *, force_daily: bool = False, now_kst: Optional[datetime] = None) -> None:
        started_at = time.perf_counter()
        now_kst = ensure_kst(now_kst)
        trade_date = now_kst.date().isoformat()
        with self._lock:
            current_context = self._daily_context
            self._last_trade_date = trade_date

        context_prefix = self._service.daily_context_version_prefix(trade_date)
        current_context, current_context_state = get_daily_regime_context_state(
            current_context,
            check_time=now_kst,
            stale_after_sec=get_market_regime_daily_context_refresh_sec(),
            expected_trade_date=trade_date,
            expected_context_version_prefix=context_prefix,
        )

        daily_context_refresh_ms = 0.0
        needs_daily_refresh = force_daily or current_context is None or current_context_state in {
            "absent",
            "stale",
            "trade_date_mismatch",
            "version_mismatch",
        }
        if needs_daily_refresh:
            try:
                daily_started_at = time.perf_counter()
                daily_result = self._service.build_daily_context(
                    check_time=now_kst,
                    include_metrics=True,
                    source="background_refresh",
                )
                daily_context_refresh_ms = (time.perf_counter() - daily_started_at) * 1000.0
                current_context = daily_result.context
                current_context_state = "fresh"
                logger.info(
                    "[MARKET_REGIME_BG] daily_context_refreshed trade_date=%s context_version=%s "
                    "market_regime_daily_context_refresh_ms=%.3f",
                    current_context.trade_date,
                    current_context.context_version,
                    daily_context_refresh_ms,
                )
            except Exception as exc:
                self._record_failure(
                    exc=exc,
                    now_kst=now_kst,
                    refresh_state="refresh_fail",
                    daily_context_state=current_context_state or "absent",
                )
                return

        if current_context is None:
            self._update_metrics(
                snapshot=self.get_snapshot(now_kst),
                daily_context=None,
                refresh_state="bootstrap_pending" if not self._error_state else "refresh_fail",
                background_refresh_ms=(time.perf_counter() - started_at) * 1000.0,
                daily_context_refresh_ms=daily_context_refresh_ms,
                intraday_guard_ms=0.0,
                quote_source="skip",
                quote_state="absent",
                daily_context_state=current_context_state or "absent",
                error_state=self._error_state,
            )
            return

        try:
            guard_started_at = time.perf_counter()
            build_result = self._service.apply_intraday_guard(
                current_context,
                check_time=now_kst,
                include_metrics=True,
                quote_snapshot_loader=self._quote_snapshot_loader,
                use_ws_cache_only=bool(
                    getattr(settings, "MARKET_REGIME_INTRADAY_USE_WS_CACHE_ONLY", True)
                ),
                quote_fallback_mode=get_market_regime_quote_fallback_mode(),
                quote_max_age_sec=get_market_regime_quote_max_age_sec(),
                snapshot_source="background_refresh",
                stale_after_sec_override=get_market_regime_background_stale_grace_sec(),
                daily_context_state=current_context_state or "fresh",
            )
            intraday_guard_ms = max(
                float(build_result.intraday_guard_elapsed_sec or 0.0) * 1000.0,
                (time.perf_counter() - guard_started_at) * 1000.0,
            )
        except Exception as exc:
            self._record_failure(
                exc=exc,
                now_kst=now_kst,
                refresh_state="refresh_fail",
                daily_context_state=current_context_state or "fresh",
            )
            return

        snapshot = build_result.snapshot
        refresh_state = "refreshed"
        self._update_metrics(
            snapshot=snapshot,
            daily_context=current_context,
            refresh_state=refresh_state,
            background_refresh_ms=(time.perf_counter() - started_at) * 1000.0,
            daily_context_refresh_ms=daily_context_refresh_ms,
            intraday_guard_ms=intraday_guard_ms,
            quote_source=build_result.quote_source or "skip",
            quote_state=build_result.quote_state or "absent",
            daily_context_state=current_context_state or "fresh",
            error_state="",
            success_time=now_kst,
        )
        logger.info(
            "[MARKET_REGIME_BG] snapshot_refreshed as_of=%s regime=%s reason=%s "
            "market_regime_background_refresh_ms=%.3f market_regime_daily_context_refresh_ms=%.3f "
            "market_regime_intraday_guard_ms=%.3f market_regime_quote_source=%s "
            "market_regime_quote_state=%s market_regime_daily_context_state=%s "
            "market_regime_background_last_success_age_sec=0.000 market_regime_background_refresh_fail_count=%s",
            snapshot.as_of.isoformat(),
            snapshot.regime.value,
            snapshot.reason,
            float((time.perf_counter() - started_at) * 1000.0),
            float(daily_context_refresh_ms),
            float(intraday_guard_ms),
            build_result.quote_source or "skip",
            build_result.quote_state or "absent",
            current_context_state or "fresh",
            int(getattr(self, "_refresh_fail_count", 0) or 0),
        )

    def _update_metrics(
        self,
        *,
        snapshot: Optional[MarketRegimeSnapshot],
        daily_context: Optional[DailyRegimeContext],
        refresh_state: str,
        background_refresh_ms: float,
        daily_context_refresh_ms: float,
        intraday_guard_ms: float,
        quote_source: str,
        quote_state: str,
        daily_context_state: str,
        error_state: str,
        success_time: Optional[datetime] = None,
    ) -> None:
        with self._lock:
            self._snapshot = snapshot
            self._daily_context = daily_context
            self._refresh_state = refresh_state
            self._background_refresh_ms = float(background_refresh_ms or 0.0)
            self._daily_context_refresh_ms = float(daily_context_refresh_ms or 0.0)
            self._intraday_guard_ms = float(intraday_guard_ms or 0.0)
            self._quote_source = str(quote_source or "skip")
            self._quote_state = str(quote_state or "absent")
            self._daily_context_state = str(daily_context_state or "absent")
            self._error_state = str(error_state or "")
            if success_time is not None:
                self._last_success_at = ensure_kst(success_time)

    def _record_failure(
        self,
        *,
        exc: Exception,
        now_kst: datetime,
        refresh_state: str,
        daily_context_state: str,
    ) -> None:
        with self._lock:
            self._refresh_fail_count += 1
        error_state = str(exc)
        self._update_metrics(
            snapshot=self.get_snapshot(now_kst),
            daily_context=self._daily_context,
            refresh_state=refresh_state,
            background_refresh_ms=0.0,
            daily_context_refresh_ms=self._daily_context_refresh_ms,
            intraday_guard_ms=self._intraday_guard_ms,
            quote_source=self._quote_source,
            quote_state=self._quote_state,
            daily_context_state=daily_context_state,
            error_state=error_state,
        )
        logger.error(
            "[MARKET_REGIME_BG] refresh_failed err=%s market_regime_daily_context_state=%s "
            "market_regime_background_last_success_age_sec=%.3f market_regime_background_refresh_fail_count=%s",
            error_state,
            daily_context_state,
            float(self.get_status(now_kst).get("market_regime_background_last_success_age_sec", -1.0)),
            int(self.get_status(now_kst).get("market_regime_background_refresh_fail_count", 0)),
        )
        if callable(self._on_error):
            self._on_error(self.name, exc)
