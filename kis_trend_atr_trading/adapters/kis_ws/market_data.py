"""WS market-data provider with completed-bar callback semantics."""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict, deque
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
    ):
        self._ws_client = ws_client or KISWSClient(
            max_reconnect_attempts=5,
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

        completed = self._aggregator.add_tick(tick)
        if completed is None:
            return

        with self._lock:
            self._bars[code].append(completed)

        cb = self._on_bar_callback
        if cb is None:
            return
        if asyncio.iscoroutinefunction(cb):
            asyncio.create_task(cb(completed))
        else:
            cb(completed)

    def _run_ws(self, stock_codes: List[str]) -> None:
        self._ws_running = True
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
            return self.stop

        self._thread = threading.Thread(
            target=self._run_ws,
            args=(codes,),
            daemon=True,
            name="kis-ws-market-data",
        )
        self._thread.start()
        return self.stop

    def stop(self) -> None:
        self._ws_client.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

    @property
    def ws_failed(self) -> bool:
        return self._ws_failed

    @property
    def ws_running(self) -> bool:
        return self._ws_running

