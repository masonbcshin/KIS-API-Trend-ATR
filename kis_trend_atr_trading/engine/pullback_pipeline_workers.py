from __future__ import annotations

import queue
import threading
import time
import hashlib
from datetime import datetime
from typing import Any, Callable, Optional

import pandas as pd

try:
    from config import settings
    from engine.pullback_pipeline_models import (
        AuthoritativeEntryIntent,
        DailyContext,
        PullbackEntryIntent,
        PullbackSetupCandidate,
        StrategyEntryIntent,
        pullback_timing_decision_from_strategy,
        strategy_setup_candidate_from_pullback,
    )
    from engine.pullback_pipeline_stores import (
        AccountRiskStore,
        ArmedCandidateStore,
        DailyContextStore,
        DirtySymbolSet,
        EntryIntentQueue,
    )
    from engine.strategy_pipeline_health import (
        DegradedModeController,
        PipelineHealthMonitorThread,
        WorkerHealthStore,
    )
    from engine.strategy_pipeline_registry import StrategyRegistry
    from strategy.multiday_trend_atr import SignalType
    from strategy.opening_range_breakout import ORBDecision, ORBCandidate
    from strategy.pullback_rebreakout import PullbackDecision
    from utils.logger import get_logger
    from utils.market_hours import KST
except ImportError:
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        AuthoritativeEntryIntent,
        DailyContext,
        PullbackEntryIntent,
        PullbackSetupCandidate,
        StrategyEntryIntent,
        pullback_timing_decision_from_strategy,
        strategy_setup_candidate_from_pullback,
    )
    from kis_trend_atr_trading.engine.pullback_pipeline_stores import (
        AccountRiskStore,
        ArmedCandidateStore,
        DailyContextStore,
        DirtySymbolSet,
        EntryIntentQueue,
    )
    from kis_trend_atr_trading.engine.strategy_pipeline_health import (
        DegradedModeController,
        PipelineHealthMonitorThread,
        WorkerHealthStore,
    )
    from kis_trend_atr_trading.engine.strategy_pipeline_registry import StrategyRegistry
    from kis_trend_atr_trading.strategy.opening_range_breakout import ORBDecision, ORBCandidate
    from kis_trend_atr_trading.strategy.pullback_rebreakout import PullbackDecision
    from kis_trend_atr_trading.strategy.multiday_trend_atr import SignalType
    from kis_trend_atr_trading.utils.logger import get_logger
    from kis_trend_atr_trading.utils.market_hours import KST


logger = get_logger("pullback_pipeline")


def _strategy_queue_counts(entry_queue: EntryIntentQueue) -> dict[str, int]:
    counts = {}
    if entry_queue is None:
        return counts
    strategy_counts = getattr(entry_queue, "strategy_counts", None)
    if callable(strategy_counts):
        counts.update(dict(strategy_counts() or {}))
    return counts


def _worker_health_queue_depth(queue_like: Any) -> int:
    qsize = getattr(queue_like, "qsize", None)
    if callable(qsize):
        try:
            return int(qsize() or 0)
        except Exception:
            return 0
    return 0


def _worker_is_degraded(controller: Optional[DegradedModeController]) -> bool:
    return bool(controller is not None and controller.is_degraded())


def _persistence_manager(executor: Any) -> Optional[Any]:
    return getattr(executor, "_pipeline_persistence_manager", None)


class RiskSnapshotThread(threading.Thread):
    def __init__(
        self,
        *,
        executor: Any,
        account_risk_store: AccountRiskStore,
        stop_event: threading.Event,
        on_error: Callable[[str, Exception], None],
        health_store: Optional[WorkerHealthStore] = None,
    ) -> None:
        super().__init__(name="RiskSnapshotThread", daemon=True)
        self._executor = executor
        self._account_risk_store = account_risk_store
        self._health_store = health_store
        self._stop_event = stop_event
        self._on_error = on_error
        self.error_state: str = ""

    def run(self) -> None:
        interval_sec = max(float(getattr(settings, "RISK_SNAPSHOT_REFRESH_SEC", 30) or 30.0), 1.0)
        if self._health_store is not None:
            stall_after = max(float(getattr(self._executor, "_pipeline_worker_stall_sec", 20.0) or 20.0), interval_sec * 2.0)
            self._health_store.ensure_worker(self.name, stall_after_sec=stall_after)
        while not self._stop_event.is_set():
            started = time.perf_counter()
            try:
                self._run_cycle()
            except Exception as exc:
                self.error_state = str(exc)
                if self._health_store is not None:
                    self._health_store.mark_error(self.name, exc)
                logger.error("[PULLBACK_RISK] worker_error=%s", exc)
                self._on_error(self.name, exc)
                return
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            setattr(self._executor, "_risk_snapshot_refresh_ms", elapsed_ms)
            if self._health_store is not None:
                self._health_store.mark_success(
                    self.name,
                    processed_delta=1,
                    avg_eval_ms=elapsed_ms,
                    state_reason="risk_snapshot_cycle",
                )
            self._stop_event.wait(interval_sec)
        if self._health_store is not None:
            self._health_store.mark_stopped(self.name, state_reason="stop_event_set")

    def _run_cycle(self) -> None:
        now_kst = datetime.now(KST)
        account_ttl = max(float(getattr(settings, "RISK_SNAPSHOT_TTL_SEC", 60) or 60.0), 1.0)
        holdings_ttl = max(float(getattr(settings, "HOLDINGS_SNAPSHOT_TTL_SEC", 30) or 30.0), 1.0)
        _, account_state = self._account_risk_store.get_account_state(ttl_sec=account_ttl, now=now_kst)
        _, holdings_state = self._account_risk_store.get_holdings_state(ttl_sec=holdings_ttl, now=now_kst)

        if account_state != "fresh":
            self._refresh_account_snapshot()
        if holdings_state != "fresh":
            self._refresh_holdings_snapshot()

        account_snapshot, account_state = self._account_risk_store.get_account_state(
            ttl_sec=account_ttl,
            now=now_kst,
        )
        holdings_snapshot, holdings_state = self._account_risk_store.get_holdings_state(
            ttl_sec=holdings_ttl,
            now=now_kst,
        )
        stale = account_state != "fresh" or holdings_state != "fresh"
        setattr(self._executor, "_risk_snapshot_stale", stale)
        last_success_age = self._account_risk_store.get_last_account_success_age_sec(now=now_kst)
        setattr(
            self._executor,
            "_risk_snapshot_last_success_age_sec",
            float(last_success_age) if last_success_age is not None else -1.0,
        )
        logger.debug(
            "[PULLBACK_RISK] cycle account_state=%s holdings_state=%s last_success_age_sec=%.2f",
            account_state,
            holdings_state,
            float(last_success_age) if last_success_age is not None else -1.0,
        )
        if account_snapshot is not None or holdings_snapshot is not None:
            logger.info(
                "[PULLBACK_RISK] refreshed account_state=%s holdings_state=%s",
                account_state,
                holdings_state,
            )

    def _refresh_account_snapshot(self) -> None:
        snapshot = self._executor.refresh_account_risk_snapshot_sync(source="background_refresh")
        if snapshot is None:
            setattr(
                self._executor,
                "_risk_snapshot_refresh_fail_count",
                int(getattr(self._executor, "_risk_snapshot_refresh_fail_count", 0) or 0) + 1,
            )
            logger.warning("[PULLBACK_RISK] account_refresh_failed mode=%s", getattr(self._executor, "_report_mode", "PAPER"))
            return
        self._account_risk_store.replace_account_snapshot(snapshot)
        setattr(
            self._executor,
            "_risk_snapshot_refresh_count",
            int(getattr(self._executor, "_risk_snapshot_refresh_count", 0) or 0) + 1,
        )
        logger.info(
            "[PULLBACK_RISK] account_refreshed source=%s total_eval=%.2f cash_balance=%.2f",
            snapshot.source,
            float(snapshot.total_eval or 0.0),
            float(snapshot.cash_balance or 0.0),
        )

    def _refresh_holdings_snapshot(self) -> None:
        snapshot = self._executor.refresh_holdings_risk_snapshot_sync(source="background_refresh")
        if snapshot is None:
            setattr(
                self._executor,
                "_risk_snapshot_refresh_fail_count",
                int(getattr(self._executor, "_risk_snapshot_refresh_fail_count", 0) or 0) + 1,
            )
            logger.warning("[PULLBACK_RISK] holdings_refresh_failed mode=%s", getattr(self._executor, "_report_mode", "PAPER"))
            return
        self._account_risk_store.replace_holdings_snapshot(snapshot)
        setattr(
            self._executor,
            "_holdings_snapshot_refresh_count",
            int(getattr(self._executor, "_holdings_snapshot_refresh_count", 0) or 0) + 1,
        )
        logger.info(
            "[PULLBACK_RISK] holdings_refreshed source=%s count=%s",
            snapshot.source,
            len(snapshot.holdings),
        )


class DailyRefreshThread(threading.Thread):
    def __init__(
        self,
        *,
        executor: Any,
        daily_context_store: DailyContextStore,
        stop_event: threading.Event,
        on_error: Callable[[str, Exception], None],
        health_store: Optional[WorkerHealthStore] = None,
    ) -> None:
        super().__init__(name="DailyRefreshThread", daemon=True)
        self._executor = executor
        self._daily_context_store = daily_context_store
        self._health_store = health_store
        self._stop_event = stop_event
        self._on_error = on_error
        self._last_trade_date: str = ""

    def _now(self) -> datetime:
        return datetime.now(KST)

    def _required_daily_bars(self) -> int:
        return max(
            int(getattr(settings, "PULLBACK_SWING_LOOKBACK_BARS", 15) or 15),
            int(getattr(settings, "PULLBACK_LOOKBACK_BARS", 12) or 12),
            int(getattr(settings, "TREND_MA_PERIOD", 50) or 50),
            20,
        )

    def _build_context_version(self, df_slice: pd.DataFrame, trade_date: str) -> str:
        latest = df_slice.iloc[-1] if not df_slice.empty else {}
        payload = "|".join(
            [
                str(trade_date or ""),
                str(len(df_slice)),
                f"{float(latest.get('close', 0.0) or 0.0):.6f}",
                f"{float(latest.get('atr', 0.0) or 0.0):.6f}",
                f"{float(latest.get('adx', 0.0) or 0.0):.6f}",
                f"{float(latest.get('ma20', 0.0) or 0.0):.6f}",
                f"{float(latest.get('ma', 0.0) or 0.0):.6f}",
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def _build_daily_context(
        self,
        *,
        symbol: str,
        indicator_df: pd.DataFrame,
        refreshed_at: datetime,
    ) -> Optional[DailyContext]:
        if indicator_df is None or getattr(indicator_df, "empty", True):
            return None

        normalized = indicator_df.copy().reset_index(drop=True)
        required_bars = self._required_daily_bars()
        minimal = normalized.tail(required_bars).copy().reset_index(drop=True)
        if minimal.empty:
            return None

        latest = minimal.iloc[-1]
        latest_date = self._executor._extract_market_data_trade_date(latest.get("date"))
        trade_date = latest_date or self._executor._trade_date_key(refreshed_at)
        swing_window = minimal.tail(max(int(getattr(settings, "PULLBACK_SWING_LOOKBACK_BARS", 15) or 15), 5))
        pullback_window = minimal.tail(max(int(getattr(settings, "PULLBACK_LOOKBACK_BARS", 12) or 12), 5))
        keep_columns = [
            column
            for column in (
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "atr",
                "adx",
                "ma",
                "ma20",
                "trend",
                "prev_high",
                "prev_close",
            )
            if column in minimal.columns
        ]
        minimal_records = tuple(minimal[keep_columns].to_dict(orient="records"))
        return DailyContext(
            symbol=str(symbol).zfill(6),
            trade_date=str(trade_date),
            context_version=self._build_context_version(minimal, str(trade_date)),
            recent_bars=minimal_records,
            prev_high=float(latest.get("prev_high", 0.0) or 0.0),
            prev_close=float(latest.get("prev_close", 0.0) or 0.0),
            atr=float(latest.get("atr", 0.0) or 0.0),
            adx=float(latest.get("adx", 0.0) or 0.0),
            trend=str(latest.get("trend", "") or ""),
            ma20=float(latest.get("ma20", 0.0) or 0.0),
            ma50=float(latest.get("ma", 0.0) or 0.0),
            swing_high=float(swing_window["high"].astype(float).max() if not swing_window.empty else 0.0),
            swing_low=float(pullback_window["low"].astype(float).min() if not pullback_window.empty else 0.0),
            refreshed_at=refreshed_at,
            source="daily_refresh_thread",
        )

    def run(self) -> None:
        interval_sec = max(float(getattr(settings, "DAILY_CONTEXT_REFRESH_SEC", 60) or 60.0), 1.0)
        if self._health_store is not None:
            stall_after = max(float(getattr(self._executor, "_pipeline_worker_stall_sec", 20.0) or 20.0), interval_sec * 2.0)
            self._health_store.ensure_worker(self.name, stall_after_sec=stall_after)
        while not self._stop_event.is_set():
            started = time.perf_counter()
            try:
                self._run_cycle()
            except Exception as exc:
                if self._health_store is not None:
                    self._health_store.mark_error(self.name, exc)
                self._on_error(self.name, exc)
                return
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            setattr(self._executor, "_daily_context_refresh_ms", elapsed_ms)
            if self._health_store is not None:
                self._health_store.mark_success(
                    self.name,
                    processed_delta=1,
                    avg_eval_ms=elapsed_ms,
                    queue_depth=int(self._daily_context_store.size()),
                    state_reason="daily_refresh_cycle",
                )
            self._stop_event.wait(interval_sec)
        if self._health_store is not None:
            self._health_store.mark_stopped(self.name, state_reason="stop_event_set")

    def _run_cycle(self) -> None:
        now_kst = self._now()
        trade_date = self._executor._trade_date_key(now_kst)
        force_refresh = bool(
            getattr(settings, "DAILY_CONTEXT_FORCE_REFRESH_ON_TRADE_DATE_CHANGE", True)
        ) and trade_date != self._last_trade_date
        symbols = list(self._executor.get_pullback_daily_refresh_symbols() or [])
        for raw_symbol in symbols:
            symbol = str(raw_symbol).zfill(6)
            self._refresh_symbol(symbol=symbol, now_kst=now_kst, force_refresh=force_refresh)
        self._last_trade_date = trade_date
        setattr(self._executor, "_daily_context_store_size", self._daily_context_store.size())
        logger.info("[PULLBACK_DAILY] store_size=%s", self._daily_context_store.size())

    def _refresh_symbol(self, *, symbol: str, now_kst: datetime, force_refresh: bool) -> None:
        started = time.perf_counter()
        raw_df = self._executor.fetch_market_data_for_symbol(symbol)
        if raw_df is None or getattr(raw_df, "empty", True):
            logger.info("[PULLBACK_DAILY] context_skip symbol=%s reason=missing_daily_data", symbol)
            return
        indicator_df = self._executor.strategy.add_indicators(raw_df)
        context = self._build_daily_context(symbol=symbol, indicator_df=indicator_df, refreshed_at=now_kst)
        if context is None:
            logger.info("[PULLBACK_DAILY] context_skip symbol=%s reason=empty_context", symbol)
            return
        self._daily_context_store.upsert(context)
        setattr(self._executor, "_pullback_daily_context_version", context.context_version)
        setattr(self._executor, "_daily_context_refresh_count", int(getattr(self._executor, "_daily_context_refresh_count", 0) or 0) + 1)
        logger.info(
            "[PULLBACK_DAILY] context_refreshed symbol=%s trade_date=%s elapsed_ms=%.2f force=%s",
            symbol,
            context.trade_date,
            (time.perf_counter() - started) * 1000.0,
            str(bool(force_refresh)).lower(),
        )


class PullbackSetupWorker(threading.Thread):
    def __init__(
        self,
        *,
        executor: Any,
        candidate_store: ArmedCandidateStore,
        daily_context_store: Optional[DailyContextStore],
        dirty_symbols: DirtySymbolSet,
        strategy_registry: Optional[StrategyRegistry],
        enabled_strategy_tags: tuple[str, ...],
        stop_event: threading.Event,
        on_error: Callable[[str, Exception], None],
        health_store: Optional[WorkerHealthStore] = None,
        degraded_controller: Optional[DegradedModeController] = None,
    ) -> None:
        super().__init__(name="PullbackSetupWorker", daemon=True)
        self._executor = executor
        self._candidate_store = candidate_store
        self._daily_context_store = daily_context_store
        self._dirty_symbols = dirty_symbols
        self._strategy_registry = strategy_registry
        self._enabled_strategy_tags = tuple(enabled_strategy_tags or ())
        self._health_store = health_store
        self._degraded_controller = degraded_controller
        self._stop_event = stop_event
        self._on_error = on_error

    def _resolve_regime_snapshot_state(self) -> str:
        snapshot = getattr(self._executor, "market_regime_snapshot", None)
        error_state = str(getattr(self._executor, "_market_regime_worker_error_state", "") or "").strip()
        if error_state:
            return "error_state"
        if snapshot is None:
            return "absent"
        return "stale" if bool(getattr(snapshot, "is_stale", False)) else "fresh"

    def _registry_entry(self):
        if self._strategy_registry is None:
            return None
        if "pullback_rebreakout" not in self._enabled_strategy_tags:
            return None
        return self._strategy_registry.get("pullback_rebreakout")

    def _process_shadow_strategy_setup(
        self,
        *,
        strategy_tag: str,
        daily_df: Optional[pd.DataFrame],
        daily_context: Optional[DailyContext],
        current_price: float,
        open_price: Optional[float],
        stock_name: str,
        decision_time: datetime,
        has_pending_order: bool,
    ) -> None:
        if self._strategy_registry is None or strategy_tag not in self._enabled_strategy_tags:
            return
        if strategy_tag == "pullback_rebreakout":
            return
        upsert_shadow_candidate = getattr(self._executor, "upsert_strategy_shadow_candidate", None)
        remove_shadow_candidate = getattr(self._executor, "remove_strategy_shadow_candidate", None)
        if not callable(upsert_shadow_candidate) or not callable(remove_shadow_candidate):
            return
        registry_entry = self._strategy_registry.get(strategy_tag)
        if registry_entry is None:
            return
        intraday_provider_ready = bool(
            getattr(self._executor, "is_cached_intraday_provider_ready", lambda: False)()
        )
        intraday_bars = []
        if strategy_tag == "opening_range_breakout":
            fetch_cached_intraday = getattr(self._executor, "fetch_cached_intraday_bars_if_available", None)
            if callable(fetch_cached_intraday):
                intraday_bars = list(fetch_cached_intraday(n=120) or [])
        evaluation = registry_entry.setup_evaluator.evaluate_setup(
            daily_df=daily_df,
            daily_context=daily_context,
            current_price=current_price,
            open_price=open_price,
            intraday_bars=intraday_bars,
            intraday_provider_ready=intraday_provider_ready,
            stock_code=self._executor.stock_code,
            stock_name=stock_name,
            check_time=decision_time,
            market_phase=getattr(self._executor, "market_phase_context", None),
            market_venue=getattr(self._executor, "market_venue_context", "KRX"),
            has_existing_position=self._executor.strategy.has_position,
            has_pending_order=has_pending_order,
            market_regime_snapshot=getattr(self._executor, "market_regime_snapshot", None),
        )
        if strategy_tag == "trend_atr":
            setattr(self._executor, "_trend_atr_adapter_path_used", True)
        if strategy_tag == "opening_range_breakout":
            setattr(self._executor, "_orb_adapter_path_used", True)
            orb_state = str(
                (evaluation.meta or {}).get(
                    "intraday_source_state",
                    (getattr(evaluation.candidate, "meta", {}) or {}).get("intraday_source_state", "missing"),
                )
                or "missing"
            )
            setattr(self._executor, "_orb_intraday_source_state", orb_state)
        if evaluation.candidate is None:
            remove_shadow_candidate(strategy_tag, self._executor.stock_code)
            remove_shadow_intent = getattr(self._executor, "remove_strategy_shadow_intent", None)
            if callable(remove_shadow_intent):
                remove_shadow_intent(strategy_tag, self._executor.stock_code)
            return
        upsert_shadow_candidate(
            strategy_tag,
            self._executor.stock_code,
            evaluation.candidate,
        )

    def _record_setup_metrics(self, elapsed_ms: float) -> None:
        setattr(self._executor, "_strategy_setup_eval_ms", elapsed_ms)
        get_shadow_counts = getattr(self._executor, "get_strategy_shadow_counts", None)
        shadow_counts = get_shadow_counts() if callable(get_shadow_counts) else {"candidates": {}, "intents": {}}
        candidate_counts = {"pullback_rebreakout": self._candidate_store.size()}
        candidate_counts.update(dict(shadow_counts.get("candidates") or {}))
        setattr(self._executor, "_candidate_store_size_by_strategy", candidate_counts)
        setattr(self._executor, "_strategy_regime_snapshot_state_used", self._resolve_regime_snapshot_state())

    def run(self) -> None:
        interval_sec = max(float(getattr(settings, "PULLBACK_SETUP_REFRESH_SEC", 60) or 60.0), 1.0)
        if self._health_store is not None:
            stall_after = max(float(getattr(self._executor, "_pipeline_worker_stall_sec", 20.0) or 20.0), interval_sec * 2.0)
            self._health_store.ensure_worker(self.name, stall_after_sec=stall_after)
        while not self._stop_event.is_set():
            started = time.perf_counter()
            try:
                if _worker_is_degraded(self._degraded_controller):
                    setattr(self._executor, "_pullback_setup_skip_reason", "degraded_mode")
                else:
                    self._run_cycle()
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                setattr(self._executor, "_pullback_setup_eval_ms", elapsed_ms)
                self._record_setup_metrics(elapsed_ms)
                if self._health_store is not None:
                    self._health_store.mark_success(
                        self.name,
                        processed_delta=1,
                        avg_eval_ms=elapsed_ms,
                        queue_depth=self._candidate_store.size(),
                        state_reason="degraded_mode" if _worker_is_degraded(self._degraded_controller) else "setup_cycle",
                    )
                logger.debug(
                    "[PULLBACK_PIPELINE] pullback_setup_eval_ms=%.2f pullback_candidate_store_size=%s",
                    elapsed_ms,
                    self._candidate_store.size(),
                )
            except Exception as exc:
                if self._health_store is not None:
                    self._health_store.mark_error(self.name, exc)
                self._on_error(self.name, exc)
                return
            self._stop_event.wait(interval_sec)
        if self._health_store is not None:
            self._health_store.mark_stopped(self.name, state_reason="stop_event_set")

    def _run_cycle(self) -> None:
        if bool(getattr(settings, "ENABLE_PULLBACK_DAILY_REFRESH_THREAD", False)) and self._daily_context_store is not None:
            self._run_cycle_from_daily_context()
            return

        quote_snapshot = self._executor.fetch_quote_snapshot()
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        if current_price <= 0:
            setattr(self._executor, "_pullback_setup_skip_reason", "invalid_current_price")
            return

        df = self._executor.fetch_market_data()
        if df is None or getattr(df, "empty", True):
            self._candidate_store.remove(self._executor.stock_code)
            setattr(self._executor, "_pullback_setup_skip_reason", "missing_daily_data")
            return

        has_pending_order = (
            self._executor._has_active_pending_buy_order()
            if not self._executor.strategy.has_position
            else False
        )
        registry_entry = self._registry_entry()
        if registry_entry is not None:
            evaluation = registry_entry.setup_evaluator.evaluate_setup(
                daily_df=df,
                daily_context=None,
                current_price=current_price,
                open_price=float(quote_snapshot.get("open_price", 0.0) or 0.0),
                stock_code=self._executor.stock_code,
                stock_name=str(quote_snapshot.get("stock_name") or ""),
                check_time=datetime.now(KST),
                market_phase=getattr(self._executor, "market_phase_context", None),
                market_venue=getattr(self._executor, "market_venue_context", "KRX"),
                has_existing_position=self._executor.strategy.has_position,
                has_pending_order=has_pending_order,
                market_regime_snapshot=getattr(self._executor, "market_regime_snapshot", None),
            )
            candidate = evaluation.native_candidate
            terminal = evaluation
            if candidate is None:
                setattr(
                    self._executor,
                    "_pullback_setup_skip_reason",
                    str(evaluation.skip_code or evaluation.skip_reason or ""),
                )
        else:
            candidate, terminal = self._executor.strategy.pullback_strategy.evaluate_setup_candidate(
                df=df,
                current_price=current_price,
                stock_code=self._executor.stock_code,
                stock_name=str(quote_snapshot.get("stock_name") or ""),
                check_time=datetime.now(KST),
                market_phase=getattr(self._executor, "market_phase_context", None),
                market_venue=getattr(self._executor, "market_venue_context", "KRX"),
                has_existing_position=self._executor.strategy.has_position,
                has_pending_order=has_pending_order,
                market_regime_snapshot=getattr(self._executor, "market_regime_snapshot", None),
            )
        if candidate is None:
            self._process_shadow_strategy_setup(
                strategy_tag="trend_atr",
                daily_df=df,
                daily_context=None,
                current_price=current_price,
                open_price=float(quote_snapshot.get("open_price", 0.0) or 0.0),
                stock_name=str(quote_snapshot.get("stock_name") or ""),
                decision_time=datetime.now(KST),
                has_pending_order=has_pending_order,
            )
            self._process_shadow_strategy_setup(
                strategy_tag="opening_range_breakout",
                daily_df=df,
                daily_context=None,
                current_price=current_price,
                open_price=float(quote_snapshot.get("open_price", 0.0) or 0.0),
                stock_name=str(quote_snapshot.get("stock_name") or ""),
                decision_time=datetime.now(KST),
                has_pending_order=has_pending_order,
            )
            get_shadow_candidate = getattr(self._executor, "get_strategy_shadow_candidate", None)
            if callable(get_shadow_candidate):
                for strategy_tag in ("trend_atr", "opening_range_breakout"):
                    if strategy_tag in self._enabled_strategy_tags and get_shadow_candidate(strategy_tag, self._executor.stock_code) is not None:
                        self._dirty_symbols.mark(self._executor.stock_code)
                        break
            self._candidate_store.remove(self._executor.stock_code)
            if terminal is not None:
                setattr(self._executor, "_pullback_threaded_context_version", "")
            return

        setattr(self._executor, "_pullback_threaded_context_version", candidate.context_version)
        setattr(self._executor, "_pullback_setup_skip_reason", "")
        self._candidate_store.upsert(candidate)
        self._process_shadow_strategy_setup(
            strategy_tag="trend_atr",
            daily_df=df,
            daily_context=None,
            current_price=current_price,
            open_price=float(quote_snapshot.get("open_price", 0.0) or 0.0),
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            decision_time=datetime.now(KST),
            has_pending_order=has_pending_order,
        )
        self._process_shadow_strategy_setup(
            strategy_tag="opening_range_breakout",
            daily_df=df,
            daily_context=None,
            current_price=current_price,
            open_price=float(quote_snapshot.get("open_price", 0.0) or 0.0),
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            decision_time=datetime.now(KST),
            has_pending_order=has_pending_order,
        )
        self._dirty_symbols.mark(candidate.symbol)

    def _run_cycle_from_daily_context(self) -> None:
        decision_time = datetime.now(KST)
        quote_snapshot = self._executor.get_cached_pullback_quote_snapshot()
        if not quote_snapshot:
            self._candidate_store.remove(self._executor.stock_code)
            setattr(self._executor, "_pullback_setup_skip_reason", "missing_quote")
            logger.info("[PULLBACK_DAILY] context_skip symbol=%s reason=missing_quote", self._executor.stock_code)
            return
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        if current_price <= 0:
            self._candidate_store.remove(self._executor.stock_code)
            setattr(self._executor, "_pullback_setup_skip_reason", "invalid_current_price")
            logger.info("[PULLBACK_DAILY] context_skip symbol=%s reason=invalid_current_price", self._executor.stock_code)
            return

        expected_trade_date = self._executor._trade_date_key(decision_time)
        expected_context_version = str(getattr(self._executor, "_pullback_daily_context_version", "") or "") or None
        context, reason = self._daily_context_store.get_validated(
            self._executor.stock_code,
            expected_trade_date=expected_trade_date,
            stale_after_sec=float(getattr(settings, "DAILY_CONTEXT_STALE_SEC", 180) or 180.0),
            expected_context_version=expected_context_version,
            now=decision_time,
        )
        if context is None:
            self._candidate_store.remove(self._executor.stock_code)
            setattr(self._executor, "_pullback_setup_skip_reason", reason)
            logger.info(
                "[PULLBACK_DAILY] context_skip symbol=%s reason=%s",
                self._executor.stock_code,
                reason or "missing",
            )
            return

        has_pending_order = (
            self._executor._has_active_pending_buy_order()
            if not self._executor.strategy.has_position
            else False
        )
        registry_entry = self._registry_entry()
        if registry_entry is not None:
            evaluation = registry_entry.setup_evaluator.evaluate_setup(
                daily_df=None,
                daily_context=context,
                current_price=current_price,
                open_price=float(quote_snapshot.get("open_price", 0.0) or 0.0),
                stock_code=self._executor.stock_code,
                stock_name=str(quote_snapshot.get("stock_name") or ""),
                check_time=decision_time,
                market_phase=getattr(self._executor, "market_phase_context", None),
                market_venue=getattr(self._executor, "market_venue_context", "KRX"),
                has_existing_position=self._executor.strategy.has_position,
                has_pending_order=has_pending_order,
                market_regime_snapshot=getattr(self._executor, "market_regime_snapshot", None),
            )
            candidate = evaluation.native_candidate
            terminal = evaluation
            if candidate is None:
                setattr(
                    self._executor,
                    "_pullback_setup_skip_reason",
                    str(evaluation.skip_code or evaluation.skip_reason or ""),
                )
        else:
            candidate, terminal = self._executor.strategy.pullback_strategy.evaluate_setup_candidate_from_daily_context(
                daily_context=context,
                current_price=current_price,
                stock_code=self._executor.stock_code,
                stock_name=str(quote_snapshot.get("stock_name") or ""),
                check_time=decision_time,
                market_phase=getattr(self._executor, "market_phase_context", None),
                market_venue=getattr(self._executor, "market_venue_context", "KRX"),
                has_existing_position=self._executor.strategy.has_position,
                has_pending_order=has_pending_order,
                market_regime_snapshot=getattr(self._executor, "market_regime_snapshot", None),
            )
        if candidate is None:
            self._process_shadow_strategy_setup(
                strategy_tag="trend_atr",
                daily_df=None,
                daily_context=context,
                current_price=current_price,
                open_price=float(quote_snapshot.get("open_price", 0.0) or 0.0),
                stock_name=str(quote_snapshot.get("stock_name") or ""),
                decision_time=decision_time,
                has_pending_order=has_pending_order,
            )
            self._process_shadow_strategy_setup(
                strategy_tag="opening_range_breakout",
                daily_df=None,
                daily_context=context,
                current_price=current_price,
                open_price=float(quote_snapshot.get("open_price", 0.0) or 0.0),
                stock_name=str(quote_snapshot.get("stock_name") or ""),
                decision_time=decision_time,
                has_pending_order=has_pending_order,
            )
            get_shadow_candidate = getattr(self._executor, "get_strategy_shadow_candidate", None)
            if callable(get_shadow_candidate):
                for strategy_tag in ("trend_atr", "opening_range_breakout"):
                    if strategy_tag in self._enabled_strategy_tags and get_shadow_candidate(strategy_tag, self._executor.stock_code) is not None:
                        self._dirty_symbols.mark(self._executor.stock_code)
                        break
            self._candidate_store.remove(self._executor.stock_code)
            if terminal is not None:
                setattr(self._executor, "_pullback_threaded_context_version", "")
            return

        setattr(self._executor, "_pullback_threaded_context_version", context.context_version)
        setattr(self._executor, "_pullback_setup_skip_reason", "")
        self._candidate_store.upsert(candidate)
        self._process_shadow_strategy_setup(
            strategy_tag="trend_atr",
            daily_df=None,
            daily_context=context,
            current_price=current_price,
            open_price=float(quote_snapshot.get("open_price", 0.0) or 0.0),
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            decision_time=decision_time,
            has_pending_order=has_pending_order,
        )
        self._process_shadow_strategy_setup(
            strategy_tag="opening_range_breakout",
            daily_df=None,
            daily_context=context,
            current_price=current_price,
            open_price=float(quote_snapshot.get("open_price", 0.0) or 0.0),
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            decision_time=decision_time,
            has_pending_order=has_pending_order,
        )
        self._dirty_symbols.mark(candidate.symbol)


class PullbackTimingWorker(threading.Thread):
    def __init__(
        self,
        *,
        executor: Any,
        candidate_store: ArmedCandidateStore,
        dirty_symbols: DirtySymbolSet,
        entry_queue: EntryIntentQueue,
        strategy_registry: Optional[StrategyRegistry],
        enabled_strategy_tags: tuple[str, ...],
        stop_event: threading.Event,
        on_error: Callable[[str, Exception], None],
        health_store: Optional[WorkerHealthStore] = None,
        degraded_controller: Optional[DegradedModeController] = None,
    ) -> None:
        super().__init__(name="PullbackTimingWorker", daemon=True)
        self._executor = executor
        self._candidate_store = candidate_store
        self._dirty_symbols = dirty_symbols
        self._entry_queue = entry_queue
        self._strategy_registry = strategy_registry
        self._enabled_strategy_tags = tuple(enabled_strategy_tags or ())
        self._health_store = health_store
        self._degraded_controller = degraded_controller
        self._stop_event = stop_event
        self._on_error = on_error

    def run(self) -> None:
        poll_sec = max(float(getattr(settings, "PULLBACK_TIMING_DIRTY_POLL_SEC", 0.5) or 0.5), 0.05)
        max_batch = max(int(getattr(settings, "MAX_DIRTY_SYMBOL_BATCH", 50) or 50), 1)
        if self._health_store is not None:
            self._health_store.ensure_worker(
                self.name,
                stall_after_sec=max(float(getattr(self._executor, "_pipeline_worker_stall_sec", 20.0) or 20.0), poll_sec * 8.0),
            )
        while not self._stop_event.is_set():
            try:
                symbols = self._dirty_symbols.drain(max_items=max_batch)
                if self._health_store is not None:
                    self._health_store.heartbeat(
                        self.name,
                        queue_depth=self._entry_queue.qsize(),
                        state_reason="idle" if not symbols else "timing_batch",
                    )
                if not symbols:
                    self._stop_event.wait(poll_sec)
                    continue
                if _worker_is_degraded(self._degraded_controller):
                    setattr(self._executor, "_pullback_timing_skip_reason", "degraded_mode")
                    if self._health_store is not None:
                        self._health_store.mark_success(
                            self.name,
                            processed_delta=0,
                            avg_eval_ms=0.0,
                            queue_depth=self._entry_queue.qsize(),
                            state_reason="degraded_mode",
                        )
                    continue
                processed = 0
                batch_elapsed_ms = 0.0
                for symbol in symbols:
                    if self._stop_event.is_set():
                        return
                    started = time.perf_counter()
                    self._process_symbol(symbol)
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    batch_elapsed_ms += elapsed_ms
                    processed += 1
                    setattr(self._executor, "_pullback_timing_eval_ms", elapsed_ms)
                    setattr(self._executor, "_strategy_timing_eval_ms", elapsed_ms)
                if self._health_store is not None:
                    self._health_store.mark_success(
                        self.name,
                        processed_delta=max(processed, 1),
                        avg_eval_ms=(batch_elapsed_ms / max(processed, 1)),
                        queue_depth=self._entry_queue.qsize(),
                        state_reason="timing_batch",
                    )
            except Exception as exc:
                if self._health_store is not None:
                    self._health_store.mark_error(self.name, exc)
                self._on_error(self.name, exc)
                return
        if self._health_store is not None:
            self._health_store.mark_stopped(self.name, state_reason="stop_event_set")

    def _registry_entry(self):
        if self._strategy_registry is None:
            return None
        if "pullback_rebreakout" not in self._enabled_strategy_tags:
            return None
        return self._strategy_registry.get("pullback_rebreakout")

    def _should_use_authoritative_multi_strategy_queue(self) -> bool:
        return bool(getattr(self._executor, "_is_multi_strategy_threaded_pipeline_enabled", lambda: False)())

    def _update_queue_metrics(self) -> None:
        strategy_counts = _strategy_queue_counts(self._entry_queue)
        setattr(self._executor, "_pullback_intent_queue_depth", int(strategy_counts.get("pullback_rebreakout", 0) or 0))
        setattr(self._executor, "_intent_queue_depth_by_strategy", strategy_counts)
        setattr(self._executor, "_authoritative_intent_queue_depth", int(self._entry_queue.qsize()))
        setattr(self._executor, "_authoritative_intent_queue_depth_by_strategy", strategy_counts)
        mixed_tiebreak = getattr(self._entry_queue, "mixed_strategy_tiebreak_count", None)
        if callable(mixed_tiebreak):
            setattr(self._executor, "_mixed_strategy_tiebreak_count", int(mixed_tiebreak() or 0))

    def _enqueue_authoritative_intent(self, intent: Any) -> bool:
        queue_depth_limit = max(int(getattr(settings, "MAX_INTENT_QUEUE_DEPTH", 1024) or 1024), 1)
        strategy_tag = str(getattr(intent, "strategy_tag", "") or "")
        queue_depth = int(self._entry_queue.qsize() or 0)
        if _worker_is_degraded(self._degraded_controller):
            self._record_ingress_reject(strategy_tag=strategy_tag, reason="degraded_mode")
            manager = _persistence_manager(self._executor)
            if manager is not None:
                manager.append_intent_state(
                    intent=intent,
                    journal_state="rejected",
                    reason="degraded_mode",
                    message="authoritative ingress rejected by degraded mode",
                    source="timing_worker",
                )
            return False
        if queue_depth >= queue_depth_limit:
            self._record_ingress_reject(strategy_tag=strategy_tag, reason="queue_depth_limit")
            manager = _persistence_manager(self._executor)
            if manager is not None:
                manager.append_intent_state(
                    intent=intent,
                    journal_state="rejected",
                    reason="queue_depth_limit",
                    message="authoritative ingress rejected by queue depth limit",
                    source="timing_worker",
                )
            return False
        queued = self._entry_queue.put_if_absent(intent)
        self._update_queue_metrics()
        manager = _persistence_manager(self._executor)
        if not queued:
            queue_reason = str(getattr(self._entry_queue, "last_reject_reason", lambda: "")() or "")
            self._record_ingress_reject(
                strategy_tag=strategy_tag,
                reason=queue_reason or "duplicate_or_queue_full",
                dropped_delta=int(getattr(self._entry_queue, "dropped_count", lambda: 0)() or 0),
            )
            if manager is not None:
                manager.append_intent_state(
                    intent=intent,
                    journal_state=(
                        "duplicate_blocked"
                        if str(queue_reason or "") in {"duplicate", "pending_symbol_cap"}
                        else "rejected"
                    ),
                    reason=queue_reason or "duplicate_or_queue_full",
                    message="authoritative ingress rejected",
                    source="timing_worker",
                )
        elif manager is not None:
            manager.append_intent_state(
                intent=intent,
                journal_state="accepted",
                message="authoritative ingress accepted",
                source="timing_worker",
            )
        return queued

    def _record_ingress_reject(self, *, strategy_tag: str, reason: str, dropped_delta: int = 0) -> None:
        setattr(self._executor, "_authoritative_intent_reject_reason", str(reason or ""))
        setattr(self._executor, "_authoritative_queue_reject_reason", str(reason or ""))
        if str(reason or "") in {"degraded_mode", "queue_depth_limit"}:
            counts = dict(getattr(self._executor, "_degraded_ingress_reject_count_by_strategy", {}) or {})
            counts[str(strategy_tag or "")] = int(counts.get(str(strategy_tag or ""), 0) or 0) + 1
            setattr(self._executor, "_degraded_ingress_reject_count_by_strategy", counts)
        if self._health_store is not None:
            self._health_store.heartbeat(
                self.name,
                queue_depth=self._entry_queue.qsize(),
                dropped_delta=max(int(dropped_delta or 0), 0),
                state_reason=f"ingress_reject:{reason}",
            )

    def _process_shadow_strategy_timing(
        self,
        *,
        strategy_tag: str,
        symbol: str,
        current_price: float,
        quote_snapshot: dict,
        intraday_bars: list[dict],
        has_pending_order: bool,
    ) -> None:
        if self._strategy_registry is None or strategy_tag not in self._enabled_strategy_tags:
            return
        if strategy_tag == "pullback_rebreakout":
            return
        get_shadow_candidate = getattr(self._executor, "get_strategy_shadow_candidate", None)
        upsert_shadow_intent = getattr(self._executor, "upsert_strategy_shadow_intent", None)
        remove_shadow_intent = getattr(self._executor, "remove_strategy_shadow_intent", None)
        if not callable(get_shadow_candidate) or not callable(upsert_shadow_intent) or not callable(remove_shadow_intent):
            return
        candidate = get_shadow_candidate(strategy_tag, symbol)
        if candidate is None:
            remove_shadow_intent(strategy_tag, symbol)
            return
        if self._executor.strategy.has_position or has_pending_order:
            remove_shadow_intent(strategy_tag, symbol)
            return
        registry_entry = self._strategy_registry.get(strategy_tag)
        if registry_entry is None:
            return
        decision = registry_entry.timing_evaluator.evaluate_timing(
            candidate=candidate,
            native_candidate=None,
            current_price=current_price,
            stock_code=symbol,
            check_time=quote_snapshot.get("received_at") if isinstance(quote_snapshot.get("received_at"), datetime) else datetime.now(KST),
            market_phase=getattr(self._executor, "market_phase_context", None),
            market_venue=getattr(self._executor, "market_venue_context", "KRX"),
            intraday_bars=intraday_bars,
            has_existing_position=False,
            has_pending_order=has_pending_order,
            current_context_version=None,
        )
        if strategy_tag == "opening_range_breakout":
            setattr(self._executor, "_orb_adapter_path_used", True)
            orb_state = str(
                (decision.meta or {}).get(
                    "decision_meta",
                    {},
                ).get(
                    "intraday_source_state",
                    (decision.meta or {}).get("entry_meta", {}).get("intraday_source_state", "missing"),
                )
                or "missing"
            )
            setattr(self._executor, "_orb_intraday_source_state", orb_state)
        if not bool(decision.should_emit_intent):
            remove_shadow_intent(strategy_tag, symbol)
            return
        intent = StrategyEntryIntent(
            strategy_tag=strategy_tag,
            symbol=str(symbol).zfill(6),
            created_at=datetime.now(KST),
            expires_at=decision.expires_at,
            trade_date=str(decision.trade_date or ""),
            entry_reference_price=float(decision.entry_reference_price or 0.0),
            entry_reference_label=str(decision.entry_reference_label or "prev_high"),
            meta=dict(decision.meta or {}),
        )
        upsert_shadow_intent(strategy_tag, symbol, intent)
        if not self._should_use_authoritative_multi_strategy_queue():
            return
        authoritative_payload = {
            "strategy_entry_intent": intent,
            "shadow_candidate": candidate,
            "decision": decision,
            "quote_snapshot": dict(quote_snapshot or {}),
            "intraday_bars": list(intraday_bars or []),
        }
        authoritative_intent = AuthoritativeEntryIntent(
            strategy_tag=strategy_tag,
            symbol=str(symbol).zfill(6),
            created_at=intent.created_at,
            expires_at=intent.expires_at,
            trade_date=str(intent.trade_date or ""),
            entry_reference_price=float(intent.entry_reference_price or 0.0),
            entry_reference_label=str(intent.entry_reference_label or "prev_high"),
            native_payload=authoritative_payload,
            source=str((decision.meta or {}).get("timing_source") or ""),
            meta=dict(intent.meta or {}),
        )
        self._enqueue_authoritative_intent(authoritative_intent)

    def _process_symbol(self, symbol: str) -> None:
        candidate = self._candidate_store.get(symbol)
        quote_snapshot = self._executor.get_cached_pullback_quote_snapshot()
        if candidate is None:
            if quote_snapshot:
                current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
                if current_price > 0.0:
                    intraday_bars = self._executor.fetch_cached_intraday_bars_if_available(
                        n=max(int(getattr(settings, "PULLBACK_REBREAKOUT_LOOKBACK_BARS", 3) or 3) + 2, 5)
                    )
                    self._process_shadow_strategy_timing(
                        strategy_tag="trend_atr",
                        symbol=symbol,
                        current_price=current_price,
                        quote_snapshot=quote_snapshot,
                        intraday_bars=intraday_bars,
                        has_pending_order=self._executor._has_active_pending_buy_order(),
                    )
                    self._process_shadow_strategy_timing(
                        strategy_tag="opening_range_breakout",
                        symbol=symbol,
                        current_price=current_price,
                        quote_snapshot=quote_snapshot,
                        intraday_bars=intraday_bars,
                        has_pending_order=self._executor._has_active_pending_buy_order(),
                    )
                    self._update_queue_metrics()
            setattr(self._executor, "_pullback_timing_skip_reason", "no_candidate")
            return

        if self._executor.strategy.has_position:
            setattr(self._executor, "_pullback_timing_skip_reason", "existing_position_precheck")
            return

        has_pending_order = self._executor._has_active_pending_buy_order()
        if has_pending_order:
            setattr(self._executor, "_pullback_timing_skip_reason", "pending_order_precheck")
            return

        if not quote_snapshot:
            setattr(self._executor, "_pullback_timing_skip_reason", "missing_quote")
            return
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        if current_price <= 0:
            setattr(self._executor, "_pullback_timing_skip_reason", "invalid_current_price")
            return

        intraday_bars = self._executor.fetch_cached_intraday_bars_if_available(
            n=max(int(getattr(settings, "PULLBACK_REBREAKOUT_LOOKBACK_BARS", 3) or 3) + 2, 5)
        )
        registry_entry = self._registry_entry()
        if registry_entry is not None:
            strategy_decision = registry_entry.timing_evaluator.evaluate_timing(
                candidate=strategy_setup_candidate_from_pullback(candidate),
                native_candidate=candidate,
                current_price=current_price,
                stock_code=symbol,
                check_time=quote_snapshot.get("received_at") if isinstance(quote_snapshot.get("received_at"), datetime) else datetime.now(KST),
                market_phase=getattr(self._executor, "market_phase_context", None),
                market_venue=getattr(self._executor, "market_venue_context", "KRX"),
                intraday_bars=intraday_bars,
                has_existing_position=False,
                has_pending_order=has_pending_order,
                current_context_version=str(getattr(self._executor, "_pullback_threaded_context_version", "") or ""),
            )
            decision = pullback_timing_decision_from_strategy(strategy_decision)
        else:
            decision = self._executor.strategy.pullback_strategy.confirm_timing(
                candidate=candidate,
                current_price=current_price,
                stock_code=symbol,
                check_time=quote_snapshot.get("received_at") if isinstance(quote_snapshot.get("received_at"), datetime) else datetime.now(KST),
                market_phase=getattr(self._executor, "market_phase_context", None),
                market_venue=getattr(self._executor, "market_venue_context", "KRX"),
                intraday_bars=intraday_bars,
                has_existing_position=False,
                has_pending_order=has_pending_order,
                current_context_version=str(getattr(self._executor, "_pullback_threaded_context_version", "") or ""),
            )
        if decision.invalidate_candidate:
            self._candidate_store.remove(symbol)
        if not decision.should_emit_intent:
            setattr(self._executor, "_pullback_timing_skip_reason", decision.reason_code or decision.reason or "not_confirmed")
            logger.debug(
                "[PULLBACK_PIPELINE] pullback_timing_skip_reason=%s symbol=%s",
                decision.reason_code or decision.reason,
                symbol,
            )
            return

        intent = PullbackEntryIntent(
            symbol=symbol,
            strategy_tag=candidate.strategy_tag,
            created_at=datetime.now(KST),
            candidate_created_at=candidate.created_at,
            expires_at=candidate.expires_at,
            context_version=candidate.context_version,
            entry_reference_price=float(decision.entry_reference_price or candidate.micro_high or 0.0),
            source=str(decision.timing_source or "fallback_daily"),
            current_price=float(current_price),
            meta={
                **dict(candidate.extra_json or {}),
                **dict(decision.meta or {}),
            },
        )
        if self._should_use_authoritative_multi_strategy_queue():
            authoritative_intent = AuthoritativeEntryIntent(
                strategy_tag=intent.strategy_tag,
                symbol=intent.symbol,
                created_at=intent.created_at,
                expires_at=intent.expires_at,
                trade_date=intent.created_at.date().isoformat(),
                entry_reference_price=float(intent.entry_reference_price or 0.0),
                entry_reference_label=str((intent.meta or {}).get("entry_reference_label") or "pullback_intraday_high"),
                native_payload=intent,
                source=str(intent.source or ""),
                meta=dict(intent.meta or {}),
            )
            queued = self._enqueue_authoritative_intent(authoritative_intent)
        else:
            queued = self._entry_queue.put_if_absent(intent)
            self._update_queue_metrics()
            manager = _persistence_manager(self._executor)
            if queued and manager is not None:
                manager.append_intent_state(
                    intent=intent,
                    journal_state="accepted",
                    message="authoritative ingress accepted",
                    source="timing_worker",
                )
            elif manager is not None:
                queue_reason = str(getattr(self._entry_queue, "last_reject_reason", lambda: "")() or "")
                manager.append_intent_state(
                    intent=intent,
                    journal_state=(
                        "duplicate_blocked"
                        if str(queue_reason or "") in {"duplicate", "pending_symbol_cap"}
                        else "rejected"
                    ),
                    reason=queue_reason or "duplicate_or_queue_full",
                    message="authoritative ingress rejected",
                    source="timing_worker",
                )
        self._process_shadow_strategy_timing(
            strategy_tag="trend_atr",
            symbol=symbol,
            current_price=current_price,
            quote_snapshot=quote_snapshot,
            intraday_bars=intraday_bars,
            has_pending_order=has_pending_order,
        )
        self._process_shadow_strategy_timing(
            strategy_tag="opening_range_breakout",
            symbol=symbol,
            current_price=current_price,
            quote_snapshot=quote_snapshot,
            intraday_bars=intraday_bars,
            has_pending_order=has_pending_order,
        )
        self._update_queue_metrics()
        if not queued:
            setattr(self._executor, "_pullback_timing_skip_reason", "duplicate_or_queue_full")
            return
        logger.debug(
            "[PULLBACK_PIPELINE] intent_enqueued symbol=%s source=%s pullback_intent_queue_depth=%s",
            symbol,
            intent.source,
            self._entry_queue.qsize(),
        )


class OrderExecutionWorker(threading.Thread):
    def __init__(
        self,
        *,
        executor: Any,
        candidate_store: ArmedCandidateStore,
        entry_queue: EntryIntentQueue,
        stop_event: threading.Event,
        on_error: Callable[[str, Exception], None],
        health_store: Optional[WorkerHealthStore] = None,
    ) -> None:
        super().__init__(name="OrderExecutionWorker", daemon=True)
        self._executor = executor
        self._candidate_store = candidate_store
        self._entry_queue = entry_queue
        self._health_store = health_store
        self._stop_event = stop_event
        self._on_error = on_error

    def run(self) -> None:
        if self._health_store is not None:
            self._health_store.ensure_worker(
                self.name,
                stall_after_sec=max(float(getattr(self._executor, "_pipeline_worker_stall_sec", 20.0) or 20.0), 5.0),
            )
        while not self._stop_event.is_set():
            intent: Optional[Any] = None
            try:
                if self._health_store is not None:
                    self._health_store.heartbeat(self.name, queue_depth=self._entry_queue.qsize(), state_reason="waiting")
                intent = self._entry_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                started = time.perf_counter()
                self._process_intent(intent)
                if self._health_store is not None:
                    self._health_store.mark_success(
                        self.name,
                        processed_delta=1,
                        avg_eval_ms=(time.perf_counter() - started) * 1000.0,
                        queue_depth=self._entry_queue.qsize(),
                        state_reason="order_drain",
                    )
            except Exception as exc:
                if self._health_store is not None:
                    self._health_store.mark_error(self.name, exc)
                self._on_error(self.name, exc)
                return
            finally:
                if intent is not None:
                    self._entry_queue.complete(intent)
                    strategy_counts = _strategy_queue_counts(self._entry_queue)
                    setattr(self._executor, "_pullback_intent_queue_depth", int(strategy_counts.get("pullback_rebreakout", 0) or 0))
                    setattr(self._executor, "_intent_queue_depth_by_strategy", strategy_counts)
                    setattr(self._executor, "_authoritative_intent_queue_depth", int(self._entry_queue.qsize()))
                    setattr(self._executor, "_authoritative_intent_queue_depth_by_strategy", strategy_counts)
        if self._health_store is not None:
            self._health_store.mark_stopped(self.name, state_reason="stop_event_set")

    def _now(self) -> datetime:
        return datetime.now(KST)

    def _strategy_consumed(self, strategy_tag: str) -> None:
        consumed = dict(getattr(self._executor, "_authoritative_intent_consumed_count_by_strategy", {}) or {})
        consumed[strategy_tag] = int(consumed.get(strategy_tag, 0) or 0) + 1
        setattr(self._executor, "_authoritative_intent_consumed_count_by_strategy", consumed)

    def _journal_reject(self, intent: Any, reason: str) -> None:
        manager = _persistence_manager(self._executor)
        if manager is None:
            return
        manager.append_intent_state(
            intent=intent,
            journal_state=manager.classify_reject_state(reason),
            reason=reason,
            message=reason,
            source="order_worker",
        )

    def _journal_order_result(self, intent: Any, order_result: dict) -> None:
        manager = _persistence_manager(self._executor)
        if manager is None:
            return
        state = manager.classify_order_result(order_result)
        broker_order_id = str(order_result.get("order_no") or "")
        manager.append_order_state(
            intent=intent,
            journal_state=state,
            reason=str(order_result.get("reason") or ""),
            message=str(order_result.get("message") or ""),
            broker_order_id=broker_order_id,
            source="order_worker",
        )

    def _record_reject(self, *, strategy_tag: str, symbol: str, reason: str, dedupe: bool = False) -> None:
        setattr(self._executor, "_authoritative_intent_reject_reason", str(reason or ""))
        if dedupe:
            setattr(
                self._executor,
                "_mixed_strategy_dedupe_count",
                int(getattr(self._executor, "_mixed_strategy_dedupe_count", 0) or 0) + 1,
            )
        logger.info(
            "[ENTRY_REJECT] strategy_tag=%s symbol=%s reason=%s",
            strategy_tag,
            symbol,
            reason,
        )

    def _remove_strategy_shadow_state(self, strategy_tag: str, symbol: str) -> None:
        remove_candidate = getattr(self._executor, "remove_strategy_shadow_candidate", None)
        remove_intent = getattr(self._executor, "remove_strategy_shadow_intent", None)
        if callable(remove_candidate):
            remove_candidate(strategy_tag, symbol)
        if callable(remove_intent):
            remove_intent(strategy_tag, symbol)

    def _mark_completed(self, intent: Any, *, order_result: dict) -> None:
        strategy_tag = str(getattr(intent, "strategy_tag", "") or "")
        symbol = str(getattr(intent, "symbol", "") or "").zfill(6)
        if strategy_tag == "pullback_rebreakout":
            self._candidate_store.remove(symbol)
        else:
            self._remove_strategy_shadow_state(strategy_tag, symbol)
        if order_result.get("success") or order_result.get("skipped"):
            created_at = getattr(intent, "created_at", None)
            native_payload = getattr(intent, "native_payload", None)
            if isinstance(native_payload, PullbackEntryIntent):
                created_at = native_payload.candidate_created_at
            start_time = created_at if isinstance(created_at, datetime) else self._now()
            elapsed_ms = max((self._now() - start_time).total_seconds() * 1000.0, 0.0)
            setattr(self._executor, "_strategy_end_to_end_latency_ms", float(elapsed_ms))
            if strategy_tag == "pullback_rebreakout":
                setattr(self._executor, "_pullback_end_to_end_latency_ms", float(elapsed_ms))

    def _common_precheck(self, intent: Any) -> tuple[bool, str]:
        now = self._now()
        expires_at = getattr(intent, "expires_at", None)
        if isinstance(expires_at, datetime) and expires_at <= now:
            return False, "intent_expired"
        if self._executor.strategy.has_position:
            return False, "existing_position"
        if self._executor._has_active_pending_buy_order():
            return False, "pending_order"
        if bool(getattr(self._executor, "cached_account_has_holding", lambda *_args, **_kwargs: False)(intent.symbol)):
            return False, "existing_holding"
        return True, ""

    def _extract_pullback_native_intent(self, intent: Any) -> PullbackEntryIntent:
        if isinstance(intent, PullbackEntryIntent):
            return intent
        payload = getattr(intent, "native_payload", None)
        if isinstance(payload, PullbackEntryIntent):
            return payload
        raise TypeError("pullback authoritative intent missing native payload")

    def _execute_pullback_entry_intent(self, intent: Any) -> dict:
        native_intent = self._extract_pullback_native_intent(intent)
        quote_snapshot = self._executor.fetch_quote_snapshot()
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        open_price = float(quote_snapshot.get("open_price", 0.0) or 0.0)
        if current_price <= 0:
            return {"success": False, "reason": "invalid_current_price"}
        df = self._executor.fetch_market_data()
        if df is None or getattr(df, "empty", True):
            return {"success": False, "reason": "missing_daily_data"}
        df_with_indicators = self._executor.strategy.add_indicators(df)

        pullback_candidate = self._executor.strategy.pullback_strategy.evaluate(
            df=df_with_indicators,
            current_price=current_price,
            stock_code=native_intent.symbol,
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            check_time=self._now(),
            market_phase=getattr(self._executor, "market_phase_context", None),
            market_venue=getattr(self._executor, "market_venue_context", "KRX"),
            has_existing_position=self._executor.strategy.has_position,
            has_pending_order=self._executor._has_active_pending_buy_order(),
            market_regime_snapshot=getattr(self._executor, "market_regime_snapshot", None),
        )
        if pullback_candidate.decision != PullbackDecision.BUY:
            return {
                "success": False,
                "reason": str(getattr(pullback_candidate, "reason_code", "") or getattr(pullback_candidate, "reason", "") or "pullback_not_buy"),
                "blocked": pullback_candidate.decision == PullbackDecision.BLOCKED,
            }

        signal = self._executor.strategy.build_pullback_buy_signal(
            pullback_candidate=pullback_candidate,
            df_with_indicators=df_with_indicators,
            current_price=current_price,
            open_price=open_price,
            stock_code=native_intent.symbol,
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            check_time=self._now(),
        )
        if getattr(signal.signal_type, "value", signal.signal_type) != SignalType.BUY.value:
            return {"success": False, "reason": str(getattr(signal, "reason_code", "") or getattr(signal, "reason", "") or "pullback_guard_block")}

        signal.meta = dict(getattr(signal, "meta", {}) or {})
        signal.meta.setdefault("strategy_tag", "pullback_rebreakout")
        signal.meta["timing_source"] = native_intent.source
        signal.meta["entry_reference_price"] = float(native_intent.entry_reference_price or 0.0)
        signal.meta["entry_reference_label"] = "pullback_intraday_high"
        signal.meta["pipeline_intent_created_at"] = native_intent.created_at.isoformat()
        setattr(self._executor, "_authoritative_order_handoff_path", "pullback")
        return dict(self._executor.execute_buy(signal) or {})

    def _execute_trend_atr_entry_intent(self, intent: AuthoritativeEntryIntent) -> dict:
        quote_snapshot = self._executor.fetch_quote_snapshot()
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        open_price = float(quote_snapshot.get("open_price", 0.0) or 0.0)
        if current_price <= 0:
            return {"success": False, "reason": "invalid_current_price"}
        df = self._executor.fetch_market_data()
        if df is None or getattr(df, "empty", True):
            return {"success": False, "reason": "missing_daily_data"}
        setup_result = self._executor.strategy.evaluate_trend_atr_setup_candidate(
            df=df,
            current_price=current_price,
            open_price=open_price,
            stock_code=intent.symbol,
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            check_time=self._now(),
            market_regime_snapshot=getattr(self._executor, "market_regime_snapshot", None),
        )
        if not bool(setup_result.get("can_enter", False)):
            return {
                "success": False,
                "reason": str(setup_result.get("meta", {}).get("reason_code") or setup_result.get("reason") or "trend_atr_not_buy"),
            }
        signal = self._executor.strategy.build_trend_atr_buy_signal(
            current_price=current_price,
            entry_reason=str(setup_result.get("reason") or ""),
            entry_atr=float(setup_result.get("atr", 0.0) or 0.0),
            entry_meta=dict(setup_result.get("meta") or {}),
            df_with_indicators=setup_result.get("df_with_indicators"),
        )
        if getattr(signal.signal_type, "value", signal.signal_type) != SignalType.BUY.value:
            return {"success": False, "reason": str(getattr(signal, "reason_code", "") or getattr(signal, "reason", "") or "trend_atr_guard_block")}
        signal.meta = dict(getattr(signal, "meta", {}) or {})
        signal.meta["pipeline_intent_created_at"] = intent.created_at.isoformat()
        setattr(self._executor, "_authoritative_order_handoff_path", "trend_atr")
        return dict(self._executor.execute_buy(signal) or {})

    def _execute_orb_entry_intent(self, intent: AuthoritativeEntryIntent) -> dict:
        quote_snapshot = self._executor.fetch_quote_snapshot()
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        open_price = float(quote_snapshot.get("open_price", 0.0) or 0.0)
        if current_price <= 0:
            return {"success": False, "reason": "invalid_current_price"}
        df = self._executor.fetch_market_data()
        if df is None or getattr(df, "empty", True):
            return {"success": False, "reason": "missing_daily_data"}
        df_with_indicators = self._executor.strategy.add_indicators(df)
        intraday_provider_ready = bool(
            getattr(self._executor, "is_cached_intraday_provider_ready", lambda: False)()
        )
        intraday_bars = list(getattr(self._executor, "fetch_cached_intraday_bars_if_available", lambda n=120: [])(120) or [])
        orb_candidate, terminal = self._executor.strategy.orb_strategy.evaluate_setup_candidate(
            df=df_with_indicators,
            current_price=current_price,
            open_price=open_price,
            intraday_bars=intraday_bars,
            stock_code=intent.symbol,
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            check_time=self._now(),
            market_phase=getattr(self._executor, "market_phase_context", None),
            market_venue=getattr(self._executor, "market_venue_context", "KRX"),
            has_existing_position=self._executor.strategy.has_position,
            has_pending_order=self._executor._has_active_pending_buy_order(),
            market_regime_snapshot=getattr(self._executor, "market_regime_snapshot", None),
            intraday_provider_ready=intraday_provider_ready,
        )
        if orb_candidate is None:
            return {
                "success": False,
                "reason": str(getattr(terminal, "reason_code", "") or getattr(terminal, "reason", "") or "orb_not_buy"),
            }
        timing_decision = self._executor.strategy.orb_strategy.confirm_timing(
            candidate=orb_candidate,
            current_price=current_price,
            intraday_bars=intraday_bars,
            stock_code=intent.symbol,
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            check_time=self._now(),
            market_phase=getattr(self._executor, "market_phase_context", None),
            market_venue=getattr(self._executor, "market_venue_context", "KRX"),
            has_existing_position=self._executor.strategy.has_position,
            has_pending_order=self._executor._has_active_pending_buy_order(),
            intraday_provider_ready=intraday_provider_ready,
        )
        orb_state = str(
            (timing_decision.meta or {}).get("intraday_source_state", (orb_candidate.meta or {}).get("intraday_source_state", "missing"))
            or "missing"
        )
        setattr(self._executor, "_orb_intraday_source_state", orb_state)
        if not bool(timing_decision.should_emit_intent):
            return {"success": False, "reason": str(timing_decision.reason_code or timing_decision.reason or "orb_timing_block")}
        orb_buy_candidate = ORBCandidate(
            decision=ORBDecision.BUY,
            reason=str(timing_decision.reason or ""),
            reason_code=str(timing_decision.reason_code or ""),
            atr=float(orb_candidate.atr or 0.0),
            trigger_price=float(timing_decision.entry_reference_price or orb_candidate.opening_range_high or 0.0),
            meta={**dict(orb_candidate.meta or {}), **dict(timing_decision.meta or {})},
        )
        signal = self._executor.strategy.build_orb_buy_signal(
            orb_candidate=orb_buy_candidate,
            df_with_indicators=df_with_indicators,
            current_price=current_price,
            open_price=open_price,
            stock_code=intent.symbol,
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            check_time=self._now(),
        )
        if getattr(signal.signal_type, "value", signal.signal_type) != SignalType.BUY.value:
            return {"success": False, "reason": str(getattr(signal, "reason_code", "") or getattr(signal, "reason", "") or "orb_guard_block")}
        signal.meta = dict(getattr(signal, "meta", {}) or {})
        signal.meta["pipeline_intent_created_at"] = intent.created_at.isoformat()
        setattr(self._executor, "_authoritative_order_handoff_path", "opening_range_breakout")
        return dict(self._executor.execute_buy(signal) or {})

    def _dispatch_intent(self, intent: Any) -> dict:
        strategy_tag = str(getattr(intent, "strategy_tag", "") or "")
        if strategy_tag == "pullback_rebreakout":
            return self._execute_pullback_entry_intent(intent)
        if isinstance(intent, AuthoritativeEntryIntent) and strategy_tag == "trend_atr":
            return self._execute_trend_atr_entry_intent(intent)
        if isinstance(intent, AuthoritativeEntryIntent) and strategy_tag == "opening_range_breakout":
            return self._execute_orb_entry_intent(intent)
        return {"success": False, "reason": "unsupported_strategy"}

    def _process_intent(self, intent: Any) -> None:
        strategy_tag = str(getattr(intent, "strategy_tag", "") or "")
        symbol = str(getattr(intent, "symbol", "") or "").zfill(6)
        allowed, reason = self._common_precheck(intent)
        if not allowed:
            self._record_reject(
                strategy_tag=strategy_tag,
                symbol=symbol,
                reason=reason,
                dedupe=reason in {"existing_position", "pending_order", "existing_holding"},
            )
            self._journal_reject(intent, reason)
            if strategy_tag == "pullback_rebreakout":
                self._candidate_store.remove(symbol)
            else:
                self._remove_strategy_shadow_state(strategy_tag, symbol)
            return

        self._strategy_consumed(strategy_tag)
        order_result = self._dispatch_intent(intent)
        if not bool(order_result.get("success") or order_result.get("skipped")):
            self._record_reject(
                strategy_tag=strategy_tag,
                symbol=symbol,
                reason=str(order_result.get("reason") or order_result.get("message") or "handoff_rejected"),
            )
        self._journal_order_result(intent, order_result)
        self._mark_completed(intent, order_result=order_result)
