"""REST market-data provider using existing KISApi implementation."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

from api.kis_api import KISApi
from core.market_data import BarCallback, MarketDataProvider
from utils.market_hours import KST


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
        self._daily_fetch_calls = 0
        self._quote_snapshot_calls = 0
        self._latest_price_calls = 0
        self._latest_price_with_open_calls = 0

    @staticmethod
    def _completed_minute_bar_ts() -> datetime:
        now_kst = datetime.now(KST)
        minute_floor = now_kst.replace(second=0, microsecond=0)
        return minute_floor - timedelta(minutes=1)

    def _build_synthetic_minute_bars(self, stock_code: str, n: int) -> List[dict]:
        count = max(int(n), 1)
        end_ts = self._completed_minute_bar_ts()
        try:
            last_price = float(self.get_latest_price(stock_code) or 0.0)
        except Exception:
            last_price = 0.0

        bars: List[dict] = []
        for idx in range(count):
            start_at = end_ts - timedelta(minutes=(count - idx - 1))
            bars.append(
                {
                    "stock_code": stock_code,
                    "timeframe": "1m",
                    "start_at": start_at,
                    "end_at": start_at + timedelta(minutes=1),
                    "date": start_at,
                    "open": last_price,
                    "high": last_price,
                    "low": last_price,
                    "close": last_price,
                    "volume": 0.0,
                }
            )
        return bars

    def get_recent_bars(self, stock_code: str, n: int, timeframe: str) -> List[dict]:
        tf = (timeframe or "").upper()
        if tf in ("1M", "1MIN", "MINUTE"):
            return self._build_synthetic_minute_bars(stock_code, n)

        if tf not in ("D", "1D", "DAY", "DAILY"):
            raise ValueError(f"REST provider currently supports daily bars only: timeframe={timeframe}")

        self._daily_fetch_calls += 1
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
        self._latest_price_calls += 1
        data = self._api.get_current_price(stock_code=stock_code)
        return float(data.get("current_price", 0.0) or 0.0)

    def get_quote_snapshot(self, stock_code: str) -> dict:
        self._quote_snapshot_calls += 1
        data = self._api.get_current_price(stock_code=stock_code)
        now_kst = datetime.now(KST)
        return {
            "stock_code": str(stock_code).zfill(6),
            "stock_name": data.get("stock_name"),
            "current_price": float(data.get("current_price", 0.0) or 0.0),
            "open_price": float(data.get("open_price", 0.0) or 0.0),
            "best_ask": None,
            "best_bid": None,
            "received_at": now_kst,
            "quote_age_sec": 0.0,
            "source": "rest_quote",
            "data_feed": "rest",
            "ws_connected": False,
        }

    def get_latest_price_with_open(self, stock_code: str) -> Tuple[float, float]:
        """Return `(current_price, open_price)` from a single quote API call."""
        self._latest_price_with_open_calls += 1
        data = self._api.get_current_price(stock_code=stock_code)
        current_price = float(data.get("current_price", 0.0) or 0.0)
        open_price = float(data.get("open_price", 0.0) or 0.0)
        return current_price, open_price

    def metrics(self) -> dict:
        return {
            "daily_fetch_calls": int(self._daily_fetch_calls),
            "quote_snapshot_calls": int(self._quote_snapshot_calls),
            "latest_price_calls": int(self._latest_price_calls),
            "latest_price_with_open_calls": int(self._latest_price_with_open_calls),
            "rest_quote_calls": int(
                self._quote_snapshot_calls
                + self._latest_price_calls
                + self._latest_price_with_open_calls
            ),
        }

    def subscribe_bars(
        self,
        stock_codes: List[str],
        timeframe: str,
        on_bar_callback: BarCallback,
    ) -> Optional[Callable[[], None]]:
        # REST adapter is polling-only. Subscription is not supported.
        return None
