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
        DailyContext,
        PullbackEntryIntent,
        PullbackSetupCandidate,
    )
    from engine.pullback_pipeline_stores import (
        AccountRiskStore,
        ArmedCandidateStore,
        DailyContextStore,
        DirtySymbolSet,
        EntryIntentQueue,
    )
    from strategy.multiday_trend_atr import SignalType
    from strategy.pullback_rebreakout import PullbackDecision
    from utils.logger import get_logger
    from utils.market_hours import KST
except ImportError:
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        DailyContext,
        PullbackEntryIntent,
        PullbackSetupCandidate,
    )
    from kis_trend_atr_trading.engine.pullback_pipeline_stores import (
        AccountRiskStore,
        ArmedCandidateStore,
        DailyContextStore,
        DirtySymbolSet,
        EntryIntentQueue,
    )
    from kis_trend_atr_trading.strategy.pullback_rebreakout import PullbackDecision
    from kis_trend_atr_trading.strategy.multiday_trend_atr import SignalType
    from kis_trend_atr_trading.utils.logger import get_logger
    from kis_trend_atr_trading.utils.market_hours import KST


logger = get_logger("pullback_pipeline")


class RiskSnapshotThread(threading.Thread):
    def __init__(
        self,
        *,
        executor: Any,
        account_risk_store: AccountRiskStore,
        stop_event: threading.Event,
        on_error: Callable[[str, Exception], None],
    ) -> None:
        super().__init__(name="RiskSnapshotThread", daemon=True)
        self._executor = executor
        self._account_risk_store = account_risk_store
        self._stop_event = stop_event
        self._on_error = on_error
        self.error_state: str = ""

    def run(self) -> None:
        interval_sec = max(float(getattr(settings, "RISK_SNAPSHOT_REFRESH_SEC", 30) or 30.0), 1.0)
        while not self._stop_event.is_set():
            started = time.perf_counter()
            try:
                self._run_cycle()
            except Exception as exc:
                self.error_state = str(exc)
                logger.error("[PULLBACK_RISK] worker_error=%s", exc)
                self._on_error(self.name, exc)
                return
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            setattr(self._executor, "_risk_snapshot_refresh_ms", elapsed_ms)
            self._stop_event.wait(interval_sec)

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
    ) -> None:
        super().__init__(name="DailyRefreshThread", daemon=True)
        self._executor = executor
        self._daily_context_store = daily_context_store
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
        while not self._stop_event.is_set():
            started = time.perf_counter()
            try:
                self._run_cycle()
            except Exception as exc:
                self._on_error(self.name, exc)
                return
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            setattr(self._executor, "_daily_context_refresh_ms", elapsed_ms)
            self._stop_event.wait(interval_sec)

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
        stop_event: threading.Event,
        on_error: Callable[[str, Exception], None],
    ) -> None:
        super().__init__(name="PullbackSetupWorker", daemon=True)
        self._executor = executor
        self._candidate_store = candidate_store
        self._daily_context_store = daily_context_store
        self._dirty_symbols = dirty_symbols
        self._stop_event = stop_event
        self._on_error = on_error

    def run(self) -> None:
        interval_sec = max(float(getattr(settings, "PULLBACK_SETUP_REFRESH_SEC", 60) or 60.0), 1.0)
        while not self._stop_event.is_set():
            started = time.perf_counter()
            try:
                self._run_cycle()
            except Exception as exc:
                self._on_error(self.name, exc)
                return
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            setattr(self._executor, "_pullback_setup_eval_ms", elapsed_ms)
            logger.debug(
                "[PULLBACK_PIPELINE] pullback_setup_eval_ms=%.2f pullback_candidate_store_size=%s",
                elapsed_ms,
                self._candidate_store.size(),
            )
            self._stop_event.wait(interval_sec)

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
            self._candidate_store.remove(self._executor.stock_code)
            if terminal is not None:
                setattr(self._executor, "_pullback_threaded_context_version", "")
            return

        setattr(self._executor, "_pullback_threaded_context_version", candidate.context_version)
        setattr(self._executor, "_pullback_setup_skip_reason", "")
        self._candidate_store.upsert(candidate)
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
            self._candidate_store.remove(self._executor.stock_code)
            if terminal is not None:
                setattr(self._executor, "_pullback_threaded_context_version", "")
            return

        setattr(self._executor, "_pullback_threaded_context_version", context.context_version)
        setattr(self._executor, "_pullback_setup_skip_reason", "")
        self._candidate_store.upsert(candidate)
        self._dirty_symbols.mark(candidate.symbol)


class PullbackTimingWorker(threading.Thread):
    def __init__(
        self,
        *,
        executor: Any,
        candidate_store: ArmedCandidateStore,
        dirty_symbols: DirtySymbolSet,
        entry_queue: EntryIntentQueue,
        stop_event: threading.Event,
        on_error: Callable[[str, Exception], None],
    ) -> None:
        super().__init__(name="PullbackTimingWorker", daemon=True)
        self._executor = executor
        self._candidate_store = candidate_store
        self._dirty_symbols = dirty_symbols
        self._entry_queue = entry_queue
        self._stop_event = stop_event
        self._on_error = on_error

    def run(self) -> None:
        poll_sec = max(float(getattr(settings, "PULLBACK_TIMING_DIRTY_POLL_SEC", 0.5) or 0.5), 0.05)
        while not self._stop_event.is_set():
            try:
                symbols = self._dirty_symbols.drain()
                if not symbols:
                    self._stop_event.wait(poll_sec)
                    continue
                for symbol in symbols:
                    if self._stop_event.is_set():
                        return
                    started = time.perf_counter()
                    self._process_symbol(symbol)
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    setattr(self._executor, "_pullback_timing_eval_ms", elapsed_ms)
            except Exception as exc:
                self._on_error(self.name, exc)
                return

    def _process_symbol(self, symbol: str) -> None:
        candidate = self._candidate_store.get(symbol)
        if candidate is None:
            setattr(self._executor, "_pullback_timing_skip_reason", "no_candidate")
            return

        if self._executor.strategy.has_position:
            setattr(self._executor, "_pullback_timing_skip_reason", "existing_position_precheck")
            return

        has_pending_order = self._executor._has_active_pending_buy_order()
        if has_pending_order:
            setattr(self._executor, "_pullback_timing_skip_reason", "pending_order_precheck")
            return

        quote_snapshot = self._executor.get_cached_pullback_quote_snapshot()
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
        queued = self._entry_queue.put_if_absent(intent)
        setattr(self._executor, "_pullback_intent_queue_depth", self._entry_queue.qsize())
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
    ) -> None:
        super().__init__(name="OrderExecutionWorker", daemon=True)
        self._executor = executor
        self._candidate_store = candidate_store
        self._entry_queue = entry_queue
        self._stop_event = stop_event
        self._on_error = on_error

    def run(self) -> None:
        while not self._stop_event.is_set():
            intent: Optional[PullbackEntryIntent] = None
            try:
                intent = self._entry_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process_intent(intent)
            except Exception as exc:
                self._on_error(self.name, exc)
                return
            finally:
                if intent is not None:
                    self._entry_queue.complete(intent)
                    setattr(self._executor, "_pullback_intent_queue_depth", self._entry_queue.qsize())

    def _process_intent(self, intent: PullbackEntryIntent) -> None:
        if self._executor.strategy.has_position:
            self._candidate_store.remove(intent.symbol)
            return

        if self._executor._has_active_pending_buy_order():
            self._candidate_store.remove(intent.symbol)
            return

        if bool(getattr(self._executor, "cached_account_has_holding", lambda *_args, **_kwargs: False)(intent.symbol)):
            self._candidate_store.remove(intent.symbol)
            return

        quote_snapshot = self._executor.fetch_quote_snapshot()
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        open_price = float(quote_snapshot.get("open_price", 0.0) or 0.0)
        if current_price <= 0:
            return

        df = self._executor.fetch_market_data()
        if df is None or getattr(df, "empty", True):
            return

        pullback_candidate = self._executor.strategy.pullback_strategy.evaluate(
            df=df,
            current_price=current_price,
            stock_code=intent.symbol,
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            check_time=datetime.now(KST),
            market_phase=getattr(self._executor, "market_phase_context", None),
            market_venue=getattr(self._executor, "market_venue_context", "KRX"),
            has_existing_position=self._executor.strategy.has_position,
            has_pending_order=self._executor._has_active_pending_buy_order(),
            market_regime_snapshot=getattr(self._executor, "market_regime_snapshot", None),
        )
        if pullback_candidate.decision != PullbackDecision.BUY:
            if pullback_candidate.decision == PullbackDecision.BLOCKED:
                self._candidate_store.remove(intent.symbol)
            return

        signal = self._executor.strategy.build_pullback_buy_signal(
            pullback_candidate=pullback_candidate,
            df_with_indicators=self._executor.strategy.add_indicators(df),
            current_price=current_price,
            open_price=open_price,
            stock_code=intent.symbol,
            stock_name=str(quote_snapshot.get("stock_name") or ""),
            check_time=datetime.now(KST),
        )
        if getattr(signal.signal_type, "value", signal.signal_type) != SignalType.BUY.value:
            return

        signal.meta = dict(getattr(signal, "meta", {}) or {})
        signal.meta.setdefault("strategy_tag", "pullback_rebreakout")
        signal.meta["timing_source"] = intent.source
        signal.meta["entry_reference_price"] = float(intent.entry_reference_price or 0.0)
        signal.meta["entry_reference_label"] = "pullback_intraday_high"
        signal.meta["pipeline_intent_created_at"] = intent.created_at.isoformat()
        started = time.perf_counter()
        order_result = self._executor.execute_buy(signal)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        setattr(
            self._executor,
            "_pullback_end_to_end_latency_ms",
            max((datetime.now(KST) - intent.candidate_created_at).total_seconds() * 1000.0, elapsed_ms),
        )
        if order_result.get("success") or order_result.get("skipped"):
            self._candidate_store.remove(intent.symbol)
