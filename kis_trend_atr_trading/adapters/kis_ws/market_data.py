"""WS market-data provider with completed-bar callback semantics."""

from __future__ import annotations

import asyncio
import math
import threading
from collections import defaultdict, deque
from datetime import datetime
from typing import Callable, Deque, Dict, List, Optional

from adapters.kis_rest.market_data import KISRestMarketDataProvider
from adapters.kis_ws.bar_aggregator import MarketTick, MinuteBarAggregator
from adapters.kis_ws.ws_client import KISWSClient
from core.market_data import BarCallback, MarketDataProvider, OHLCVBar
from utils.logger import get_logger

logger = get_logger("kis_ws_market_data")


class KISWSMarketDataProvider(MarketDataProvider):
    """
    WebSocket-backed market-data provider.

    - Emits only completed 1m bars.
    - Keeps in-memory recent bars for `get_recent_bars(..., timeframe='1m')`.
    - On WS failure follows fixed policy (`rest_fallback` by default).
    """

    def __init__(
        self,
        *,
        ws_client: Optional[KISWSClient] = None,
        rest_fallback_provider: Optional[KISRestMarketDataProvider] = None,
        max_bar_history: int = 2048,
        failure_policy: str = "rest_fallback",
        max_reconnect_attempts: int = 5,
        reconnect_base_delay: float = 1.0,
        missing_gap_required: int = 2,
    ):
        self._ws_client = ws_client or KISWSClient(
            max_reconnect_attempts=max_reconnect_attempts,
            reconnect_base_delay=reconnect_base_delay,
            failure_policy=failure_policy,
        )
        self._rest_fallback = rest_fallback_provider or KISRestMarketDataProvider()
        self._failure_policy = failure_policy
        self._aggregator = MinuteBarAggregator(timeframe="1m")
        self._bars: Dict[str, Deque[OHLCVBar]] = defaultdict(
            lambda: deque(maxlen=max(int(max_bar_history), 100))
        )
        self._latest_price: Dict[str, float] = {}
        self._on_bar_callback: Optional[BarCallback] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._ws_running = False
        self._ws_failed = False
        self._subscribed_codes: List[str] = []
        self._last_message_ts: Optional[datetime] = None
        self._last_completed_bar_ts: Dict[str, datetime] = {}
        self._missing_gap_required = max(int(missing_gap_required), 2)
        self._missing_gap_detected: bool = False

    def get_recent_bars(self, stock_code: str, n: int, timeframe: str) -> List[dict]:
        code = str(stock_code).zfill(6)
        tf = (timeframe or "").lower()
        count = max(int(n), 1)

        if tf in ("1m", "1min", "minute"):
            with self._lock:
                bars = list(self._bars.get(code, []))[-count:]
            return [bar.to_dict() for bar in bars]

        # Keep compatibility with existing daily-bar strategy path.
        return self._rest_fallback.get_recent_bars(code, count, timeframe)

    def get_latest_price(self, stock_code: str) -> float:
        code = str(stock_code).zfill(6)
        with self._lock:
            latest = self._latest_price.get(code)
        if latest is not None:
            return float(latest)
        return self._rest_fallback.get_latest_price(code)

    def _handle_tick(self, tick: MarketTick) -> None:
        code = str(tick.stock_code).zfill(6)
        with self._lock:
            self._latest_price[code] = float(tick.price)
            self._last_message_ts = datetime.now()

        completed = self._aggregator.add_tick(tick)
        if completed is None:
            return

        missing_count = 0
        with self._lock:
            prev_ts = self._last_completed_bar_ts.get(code)
            if prev_ts is not None:
                jump_min = int((completed.start_at - prev_ts).total_seconds() // 60)
                missing_count = max(jump_min - 1, 0)
                if missing_count >= self._missing_gap_required:
                    self._missing_gap_detected = True
            self._last_completed_bar_ts[code] = completed.start_at
            self._bars[code].append(completed)

        if missing_count >= self._missing_gap_required:
            logger.warning(
                "[WS] missing completed bars detected stock=%s missing=%s (>= %s)",
                code,
                missing_count,
                self._missing_gap_required,
            )
            self._attempt_backfill(code, missing_count)

        cb = self._on_bar_callback
        if cb is None:
            return
        if asyncio.iscoroutinefunction(cb):
            asyncio.create_task(cb(completed))
        else:
            cb(completed)

    def _run_ws(self, stock_codes: List[str]) -> None:
        self._ws_running = True
        self._ws_failed = False
        try:
            result = asyncio.run(self._ws_client.run(stock_codes, self._handle_tick))
            if not result.success:
                self._ws_failed = True
                logger.error(
                    "[WS] feed ended with failure policy=%s reason=%s",
                    result.failure_policy,
                    result.reason,
                )
        finally:
            self._ws_running = False

    def subscribe_bars(
        self,
        stock_codes: List[str],
        timeframe: str,
        on_bar_callback: BarCallback,
    ) -> Optional[Callable[[], None]]:
        tf = (timeframe or "").lower()
        if tf not in ("1m", "1min", "minute"):
            raise ValueError(f"WS provider supports 1m subscription only: timeframe={timeframe}")

        self._on_bar_callback = on_bar_callback
        codes = [str(code).zfill(6) for code in stock_codes]

        if self._thread and self._thread.is_alive():
            with self._lock:
                if codes == self._subscribed_codes:
                    return self.stop
            self.stop()

        self._thread = threading.Thread(
            target=self._run_ws,
            args=(codes,),
            daemon=True,
            name="kis-ws-market-data",
        )
        with self._lock:
            self._subscribed_codes = list(codes)
            self._ws_failed = False
        self._thread.start()
        return self.stop

    def stop(self) -> None:
        self._ws_client.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

    def _attempt_backfill(self, stock_code: str, missing_count: int) -> None:
        try:
            self._rest_fallback.get_recent_bars(
                stock_code=stock_code,
                n=max(missing_count + 2, 3),
                timeframe="1m",
            )
            logger.info("[WS] REST backfill attempted stock=%s missing=%s", stock_code, missing_count)
        except Exception as err:
            logger.warning(
                "[WS] REST backfill failed stock=%s missing=%s err=%s",
                stock_code,
                missing_count,
                err,
            )

    def is_ws_connected(self) -> bool:
        with self._lock:
            running = self._ws_running
            failed = self._ws_failed
        return bool(running) and not bool(failed)

    def last_message_age_sec(self) -> float:
        with self._lock:
            ts = self._last_message_ts
        if ts is None:
            return math.inf
        return max((datetime.now() - ts).total_seconds(), 0.0)

    def get_last_completed_bar_ts(self, stock_code: Optional[str] = None) -> Optional[datetime]:
        with self._lock:
            if stock_code:
                return self._last_completed_bar_ts.get(str(stock_code).zfill(6))
            if not self._last_completed_bar_ts:
                return None
            return max(self._last_completed_bar_ts.values())

    def health(self) -> Dict[str, object]:
        return {
            "ws_running": bool(self.ws_running),
            "ws_failed": bool(self.ws_failed),
            "ws_connected": bool(self.is_ws_connected()),
            "last_message_age_sec": float(self.last_message_age_sec()),
            "subscribed_count": len(self._subscribed_codes),
            "missing_gap_detected": bool(self._missing_gap_detected),
        }

    @property
    def ws_failed(self) -> bool:
        return self._ws_failed

    @property
    def ws_running(self) -> bool:
        return self._ws_running
