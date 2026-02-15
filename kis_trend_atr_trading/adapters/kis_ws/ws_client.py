"""Minimal KIS WebSocket client for market-data adapter usage."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional, Sequence

import requests

from adapters.kis_ws.bar_aggregator import MarketTick
from utils.logger import get_logger

logger = get_logger("kis_ws_client")

WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"
TR_SUBSCRIBE = "H0STCNT0"


TickCallback = Callable[[MarketTick], Awaitable[None] | None]


@dataclass
class WSRunResult:
    success: bool
    failure_policy: str
    reason: str = ""


class KISWSClient:
    """
    WS client with minimal reconnection policy.

    Policy:
    - disconnect detected -> exponential backoff reconnect (max_reconnect_attempts)
    - reconnect exhaustion -> fixed failure policy (`rest_fallback` or `safe_exit`)
    """

    def __init__(
        self,
        *,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        is_paper_trading: bool = True,
        base_url: Optional[str] = None,
        max_reconnect_attempts: int = 5,
        reconnect_base_delay: float = 1.0,
        failure_policy: str = "rest_fallback",
    ):
        self.app_key = app_key or os.getenv("KIS_APP_KEY", "")
        self.app_secret = app_secret or os.getenv("KIS_APP_SECRET", "")
        self.is_paper_trading = is_paper_trading
        self.base_url = base_url or (
            "https://openapivts.koreainvestment.com:29443"
            if is_paper_trading
            else "https://openapi.koreainvestment.com:9443"
        )
        self.ws_url = WS_URL_PAPER if is_paper_trading else WS_URL_REAL
        self.max_reconnect_attempts = max(int(max_reconnect_attempts), 1)
        self.reconnect_base_delay = max(float(reconnect_base_delay), 0.2)
        self.failure_policy = failure_policy
        self._approval_key: Optional[str] = None
        self._running: bool = False
        self._ws = None

    def stop(self) -> None:
        self._running = False

    def _get_approval_key(self) -> str:
        if self._approval_key:
            return self._approval_key

        if not self.app_key or not self.app_secret:
            raise RuntimeError("KIS_APP_KEY/KIS_APP_SECRET is required for WS.")

        url = f"{self.base_url}/oauth2/Approval"
        headers = {"content-type": "application/json; charset=utf-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret,
        }
        response = requests.post(url, headers=headers, json=body, timeout=10)
        response.raise_for_status()
        data = response.json()
        approval_key = str(data.get("approval_key", "")).strip()
        if not approval_key:
            raise RuntimeError(f"approval_key is missing: {data}")
        self._approval_key = approval_key
        return approval_key

    async def _send_subscribe(self, stock_code: str) -> None:
        code = str(stock_code).zfill(6)
        payload = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": TR_SUBSCRIBE, "tr_key": code}},
        }
        await self._ws.send(json.dumps(payload))

    @staticmethod
    def _parse_tick(message: str) -> Optional[MarketTick]:
        if not message or message.startswith("{") or "|" not in message:
            return None

        parts = message.split("|")
        if len(parts) < 3 or parts[1] != TR_SUBSCRIBE:
            return None

        raw = parts[2] if len(parts) == 3 else "|".join(parts[2:])
        fields = raw.split("^")
        if len(fields) < 13:
            return None

        stock_code = str(fields[0]).zfill(6)
        hhmmss = str(fields[1] or "000000")
        price = float(fields[2] or 0.0)
        volume = float(fields[12] or 0.0)
        now = datetime.now()
        try:
            ts = now.replace(
                hour=int(hhmmss[0:2]),
                minute=int(hhmmss[2:4]),
                second=int(hhmmss[4:6]),
                microsecond=0,
            )
        except Exception:
            ts = now

        return MarketTick(
            stock_code=stock_code,
            price=price,
            volume=volume,
            timestamp=ts,
        )

    async def _listen_once(self, stock_codes: Sequence[str], on_tick: TickCallback) -> bool:
        try:
            import websockets
        except Exception as err:
            raise RuntimeError("websockets package is required for WS feed.") from err

        self._approval_key = self._get_approval_key()
        async with websockets.connect(
            self.ws_url,
            ping_interval=30,
            ping_timeout=60,
            close_timeout=10,
        ) as ws:
            self._ws = ws
            for stock_code in stock_codes:
                await self._send_subscribe(stock_code)

            while self._running:
                message = await asyncio.wait_for(ws.recv(), timeout=90)
                tick = self._parse_tick(message)
                if tick is None:
                    continue
                if asyncio.iscoroutinefunction(on_tick):
                    await on_tick(tick)
                else:
                    on_tick(tick)
            return True

    async def run(self, stock_codes: Sequence[str], on_tick: TickCallback) -> WSRunResult:
        self._running = True
        attempt = 0

        while self._running:
            try:
                ok = await self._listen_once(stock_codes, on_tick)
                if ok:
                    return WSRunResult(success=True, failure_policy=self.failure_policy, reason="stopped")
            except Exception as err:
                attempt += 1
                if attempt > self.max_reconnect_attempts:
                    reason = f"reconnect_exhausted: {err}"
                    logger.error(f"[WS] {reason}; policy={self.failure_policy}")
                    self._running = False
                    return WSRunResult(
                        success=False,
                        failure_policy=self.failure_policy,
                        reason=reason,
                    )
                delay = min(self.reconnect_base_delay * (2 ** (attempt - 1)), 30.0)
                logger.warning(
                    "[WS] disconnected; retry=%s/%s delay=%.1fs err=%s",
                    attempt,
                    self.max_reconnect_attempts,
                    delay,
                    err,
                )
                await asyncio.sleep(delay)

        return WSRunResult(success=True, failure_policy=self.failure_policy, reason="stopped")

