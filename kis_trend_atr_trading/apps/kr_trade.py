"""Unified KR trading app entrypoint (rest/ws feed switch)."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

# Legacy modules inside this repo use absolute imports like `from config import ...`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "kis_trend_atr_trading"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from adapters.kis_rest.market_data import KISRestMarketDataProvider
from adapters.kis_ws.market_data import KISWSMarketDataProvider
from api.kis_api import KISApi
from config import settings
from engine.multiday_executor import MultidayExecutor
from strategy.multiday_trend_atr import MultidayTrendATRStrategy
from utils.logger import get_logger, setup_logger

logger = get_logger("apps.kr_trade")


def _load_config_file(config_path: str) -> dict:
    path = Path(config_path)
    if not path.is_absolute():
        path = APP_ROOT / config_path
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_feed(args_feed: Optional[str], config_path: str) -> str:
    if args_feed:
        return args_feed
    raw = _load_config_file(config_path)
    market_data = raw.get("market_data", {}) if isinstance(raw, dict) else {}
    feed = str(market_data.get("data_feed", "rest")).strip().lower()
    return feed if feed in ("rest", "ws") else "rest"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified KR trade app")
    parser.add_argument("--mode", choices=["trade", "paper", "cbt"], default="trade")
    parser.add_argument("--feed", choices=["rest", "ws"], default=None)
    parser.add_argument("--config", default="config/dev.yaml")
    parser.add_argument("--stock", default=settings.DEFAULT_STOCK_CODE)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--max-runs", type=int, default=None)
    parser.add_argument("--order-quantity", type=int, default=settings.ORDER_QUANTITY)
    return parser


def run_trade_app(args: argparse.Namespace) -> int:
    feed = _resolve_feed(args.feed, args.config)

    if args.mode == "cbt":
        from apps.kr_cbt import run_cbt_mode

        return run_cbt_mode(stock=args.stock, interval=args.interval, max_runs=args.max_runs)

    if args.mode == "paper":
        os.environ["TRADING_MODE"] = "PAPER"

    is_real_mode = str(os.getenv("TRADING_MODE", "PAPER")).upper() == "REAL"
    api = KISApi(is_paper_trading=not is_real_mode)
    strategy = MultidayTrendATRStrategy()

    rest_provider = KISRestMarketDataProvider(api=api)
    stop_ws = None
    provider = rest_provider

    ws_provider = None
    if feed == "ws":
        ws_provider = KISWSMarketDataProvider(rest_fallback_provider=rest_provider)
        provider = ws_provider

    logger.info(
        "[KR_TRADE] start mode=%s feed=%s stock=%s interval=%s max_runs=%s",
        args.mode,
        feed,
        args.stock,
        args.interval,
        args.max_runs,
    )

    executor = MultidayExecutor(
        api=api,
        strategy=strategy,
        stock_code=args.stock,
        order_quantity=max(int(args.order_quantity), 1),
        market_data_provider=provider,
    )
    executor.restore_position_on_start()

    try:
        if feed == "ws" and ws_provider is not None:
            # WS mode: evaluate strategy only on completed 1m bar callback.
            run_count = {"value": 0}
            stop_requested = {"value": False}
            heartbeat = {"last": time.time()}

            def _on_completed_bar(_bar) -> None:
                if stop_requested["value"]:
                    return
                logger.info(
                    "[KR_TRADE][WS] completed 1m bar stock=%s start=%s end=%s close=%s volume=%s",
                    getattr(_bar, "stock_code", "unknown"),
                    getattr(_bar, "start_at", "unknown"),
                    getattr(_bar, "end_at", "unknown"),
                    getattr(_bar, "close", "unknown"),
                    getattr(_bar, "volume", "unknown"),
                )
                executor.run_once()
                run_count["value"] += 1
                if args.max_runs and run_count["value"] >= int(args.max_runs):
                    stop_requested["value"] = True

            stop_ws = ws_provider.subscribe_bars([args.stock], "1m", _on_completed_bar)
            while not stop_requested["value"]:
                now = time.time()
                if now - heartbeat["last"] >= 30:
                    logger.info(
                        "[KR_TRADE][WS] waiting completed bar run_count=%s ws_running=%s ws_failed=%s",
                        run_count["value"],
                        ws_provider.ws_running,
                        ws_provider.ws_failed,
                    )
                    heartbeat["last"] = now
                if ws_provider.ws_failed and ws_provider.ws_running is False:
                    logger.warning("[KR_TRADE] WS unavailable -> rest fallback polling loop")
                    executor.run(
                        interval_seconds=max(int(args.interval), 60),
                        max_iterations=args.max_runs,
                    )
                    stop_requested["value"] = True
                    break
                time.sleep(0.5)
        else:
            executor.run(
                interval_seconds=max(int(args.interval), 60),
                max_iterations=args.max_runs,
            )
    except KeyboardInterrupt:
        logger.info("[KR_TRADE] interrupted by user")
    finally:
        if stop_ws:
            stop_ws()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    setup_logger("apps.kr_trade", settings.LOG_LEVEL)
    parser = _build_parser()
    args = parser.parse_args(argv)
    return run_trade_app(args)


if __name__ == "__main__":
    raise SystemExit(main())
