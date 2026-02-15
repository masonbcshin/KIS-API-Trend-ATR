"""Market data provider protocol for core engine integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class OHLCVBar:
    """Normalized OHLCV bar payload."""

    stock_code: str
    timeframe: str
    start_at: datetime
    end_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_dict(self) -> dict:
        return {
            "stock_code": self.stock_code,
            "timeframe": self.timeframe,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "date": self.end_at,
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": float(self.volume),
        }


BarCallback = Callable[[OHLCVBar], None]


@runtime_checkable
class MarketDataProvider(Protocol):
    """
    Core market-data provider contract.

    - Engines must use this interface instead of directly binding to REST/WS implementation.
    - Strategy/order/risk behavior must remain unchanged.
    """

    def get_recent_bars(self, stock_code: str, n: int, timeframe: str) -> List[dict]:
        """Return recent bars in ascending chronological order."""

    def get_latest_price(self, stock_code: str) -> float:
        """Return latest tradable price for the stock code."""

    def subscribe_bars(
        self,
        stock_codes: List[str],
        timeframe: str,
        on_bar_callback: BarCallback,
    ) -> Optional[Callable[[], None]]:
        """
        Optional streaming subscription for completed bars.

        Returns:
            Optional[Callable[[], None]]: stop function for the subscription.
        """

