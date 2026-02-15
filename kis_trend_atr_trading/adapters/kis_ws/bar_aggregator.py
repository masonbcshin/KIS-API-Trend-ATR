"""Tick-to-bar aggregation utilities for WS market-data adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional

from core.market_data import OHLCVBar


@dataclass
class MarketTick:
    """Normalized market tick for adapter-internal aggregation."""

    stock_code: str
    price: float
    volume: float
    timestamp: datetime


class MinuteBarAggregator:
    """
    Aggregates ticks into 1-minute completed bars.

    Rule:
    - A bar is emitted only when the next minute tick arrives (or force flush on stop).
    - This prevents strategy evaluation on incomplete minute bars.
    """

    def __init__(self, timeframe: str = "1m"):
        if timeframe != "1m":
            raise ValueError("MinuteBarAggregator supports timeframe='1m' only.")
        self._timeframe = timeframe
        self._current: Dict[str, OHLCVBar] = {}

    @staticmethod
    def _minute_floor(ts: datetime) -> datetime:
        return ts.replace(second=0, microsecond=0)

    def add_tick(self, tick: MarketTick) -> Optional[OHLCVBar]:
        code = str(tick.stock_code).zfill(6)
        price = float(tick.price)
        volume = float(tick.volume or 0.0)
        ts = tick.timestamp

        minute_start = self._minute_floor(ts)
        minute_end = minute_start + timedelta(minutes=1)
        current = self._current.get(code)

        if current is None:
            self._current[code] = OHLCVBar(
                stock_code=code,
                timeframe=self._timeframe,
                start_at=minute_start,
                end_at=minute_end,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
            return None

        if minute_start == current.start_at:
            self._current[code] = OHLCVBar(
                stock_code=code,
                timeframe=current.timeframe,
                start_at=current.start_at,
                end_at=current.end_at,
                open=current.open,
                high=max(current.high, price),
                low=min(current.low, price),
                close=price,
                volume=current.volume + volume,
            )
            return None

        completed = current
        self._current[code] = OHLCVBar(
            stock_code=code,
            timeframe=self._timeframe,
            start_at=minute_start,
            end_at=minute_end,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
        )
        return completed

    def flush(self, stock_code: Optional[str] = None) -> Optional[OHLCVBar]:
        if stock_code is None:
            return None
        code = str(stock_code).zfill(6)
        return self._current.pop(code, None)

    def flush_all(self) -> Dict[str, OHLCVBar]:
        bars = dict(self._current)
        self._current.clear()
        return bars

