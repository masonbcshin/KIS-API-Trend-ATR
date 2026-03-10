from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

try:
    from config import settings
    from engine.pullback_pipeline_models import PullbackEntryIntent, PullbackSetupCandidate
    from engine.pullback_pipeline_stores import ArmedCandidateStore, DirtySymbolSet, EntryIntentQueue
    from strategy.multiday_trend_atr import SignalType
    from strategy.pullback_rebreakout import PullbackDecision
    from utils.logger import get_logger
    from utils.market_hours import KST
except ImportError:
    from kis_trend_atr_trading.config import settings
    from kis_trend_atr_trading.engine.pullback_pipeline_models import (
        PullbackEntryIntent,
        PullbackSetupCandidate,
    )
    from kis_trend_atr_trading.engine.pullback_pipeline_stores import (
        ArmedCandidateStore,
        DirtySymbolSet,
        EntryIntentQueue,
    )
    from kis_trend_atr_trading.strategy.pullback_rebreakout import PullbackDecision
    from kis_trend_atr_trading.strategy.multiday_trend_atr import SignalType
    from kis_trend_atr_trading.utils.logger import get_logger
    from kis_trend_atr_trading.utils.market_hours import KST


logger = get_logger("pullback_pipeline")


class PullbackSetupWorker(threading.Thread):
    def __init__(
        self,
        *,
        executor: Any,
        candidate_store: ArmedCandidateStore,
        dirty_symbols: DirtySymbolSet,
        stop_event: threading.Event,
        on_error: Callable[[str, Exception], None],
    ) -> None:
        super().__init__(name="PullbackSetupWorker", daemon=True)
        self._executor = executor
        self._candidate_store = candidate_store
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
        quote_snapshot = self._executor.fetch_quote_snapshot()
        current_price = float(quote_snapshot.get("current_price", 0.0) or 0.0)
        if current_price <= 0:
            return

        df = self._executor.fetch_market_data()
        if df is None or getattr(df, "empty", True):
            self._candidate_store.remove(self._executor.stock_code)
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

        quote_snapshot = self._executor.fetch_quote_snapshot()
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
