"""REST market-data provider using existing KISApi implementation."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, List, Optional

from api.kis_api import KISApi
from core.market_data import BarCallback, MarketDataProvider


class KISRestMarketDataProvider(MarketDataProvider):
    """
    MarketDataProvider backed by REST polling.

    This adapter intentionally preserves existing behavior:
    - daily OHLCV retrieval path is unchanged (delegates to KISApi.get_daily_ohlcv)
    - latest price retrieval path is unchanged (delegates to KISApi.get_current_price)
    """

    def __init__(self, api: Optional[KISApi] = None, period_type: str = "D"):
        self._api = api or KISApi(is_paper_trading=True)
        self._period_type = period_type

    def get_recent_bars(self, stock_code: str, n: int, timeframe: str) -> List[dict]:
        tf = (timeframe or "").upper()
        if tf not in ("D", "1D", "DAY", "DAILY"):
            raise ValueError(f"REST provider currently supports daily bars only: timeframe={timeframe}")

        df = self._api.get_daily_ohlcv(stock_code=stock_code, period_type=self._period_type)
        if df is None or df.empty:
            return []

        bars: List[dict] = []
        for _, row in df.tail(max(int(n), 1)).iterrows():
            date_value = row.get("date")
            if isinstance(date_value, str):
                try:
                    date_value = datetime.fromisoformat(date_value)
                except ValueError:
                    date_value = None
            bars.append(
                {
                    "stock_code": stock_code,
                    "timeframe": "D",
                    "date": date_value,
                    "open": float(row.get("open", 0.0) or 0.0),
                    "high": float(row.get("high", 0.0) or 0.0),
                    "low": float(row.get("low", 0.0) or 0.0),
                    "close": float(row.get("close", 0.0) or 0.0),
                    "volume": float(row.get("volume", 0.0) or 0.0),
                }
            )
        return bars

    def get_latest_price(self, stock_code: str) -> float:
        data = self._api.get_current_price(stock_code=stock_code)
        return float(data.get("current_price", 0.0) or 0.0)

    def subscribe_bars(
        self,
        stock_codes: List[str],
        timeframe: str,
        on_bar_callback: BarCallback,
    ) -> Optional[Callable[[], None]]:
        # REST adapter is polling-only. Subscription is not supported.
        return None

