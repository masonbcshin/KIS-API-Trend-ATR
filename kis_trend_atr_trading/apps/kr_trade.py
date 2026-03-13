"""Unified KR trading app entrypoint (rest/ws feed switch)."""

from __future__ import annotations

import argparse
import os
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

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
from engine.strategy_pipeline_persistence import (
    PipelinePersistenceThread,
    StrategyPipelinePersistenceManager,
    slice_recovery_result_for_symbol,
)
from strategy.multiday_trend_atr import MultidayTrendATRStrategy
from utils.logger import get_logger, setup_logger
from utils.market_hours import KST

logger = get_logger("apps.kr_trade")


def _resolve_feed(args_feed: Optional[str]) -> str:
    if args_feed:
        return args_feed
    feed = str(getattr(settings, "DATA_FEED_DEFAULT", "rest")).strip().lower()
    return feed if feed in ("rest", "ws") else "rest"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified KR trade app")
    parser.add_argument("--mode", choices=["trade", "paper", "cbt"], default="trade")
    parser.add_argument("--feed", choices=["rest", "ws"], default=None)
    parser.add_argument("--stock", default=settings.DEFAULT_STOCK_CODE)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--max-runs", type=int, default=None)
    parser.add_argument("--order-quantity", type=int, default=settings.ORDER_QUANTITY)
    return parser


def run_trade_app(args: argparse.Namespace) -> int:
    feed = _resolve_feed(args.feed)

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

    pipeline_persistence_manager = StrategyPipelinePersistenceManager(
        state_dir=str(getattr(settings, "PIPELINE_STATE_DIR", "data/pipeline_state") or "data/pipeline_state"),
        enabled=bool(getattr(settings, "ENABLE_PIPELINE_STATE_PERSISTENCE", False)),
        candidate_snapshot_interval_sec=float(
            getattr(settings, "PIPELINE_CANDIDATE_SNAPSHOT_INTERVAL_SEC", 15) or 15.0
        ),
        intent_journal_enabled=bool(getattr(settings, "PIPELINE_INTENT_JOURNAL_ENABLED", True)),
        intent_max_age_sec=float(getattr(settings, "PIPELINE_INTENT_MAX_AGE_SEC", 120) or 120.0),
        candidate_max_recover_age_sec=float(
            getattr(settings, "PIPELINE_CANDIDATE_MAX_RECOVER_AGE_SEC", 300) or 300.0
        ),
        recover_only_current_trade_date=bool(
            getattr(settings, "PIPELINE_RECOVER_ONLY_CURRENT_TRADE_DATE", True)
        ),
    )
    pipeline_persistence_manager.log_startup_configuration()
    pipeline_persistence_stop_event = None
    pipeline_persistence_worker = None
    if pipeline_persistence_manager.prepare_process_global_writer():
        pipeline_persistence_stop_event = threading.Event()
        pipeline_persistence_worker = PipelinePersistenceThread(
            persistence_manager=pipeline_persistence_manager,
            stop_event=pipeline_persistence_stop_event,
        )
        try:
            pipeline_persistence_worker.start()
        except Exception as exc:
            pipeline_persistence_manager.disable(
                error_state=f"writer_start_failed:{type(exc).__name__}:{exc}"
            )
            logger.exception(
                "[KR_TRADE] pipeline_persistence_start_failed state_dir=%s",
                pipeline_persistence_manager.state_dir,
            )
            pipeline_persistence_worker = None
            pipeline_persistence_stop_event = None

    executor = MultidayExecutor(
        api=api,
        strategy=strategy,
        stock_code=args.stock,
        order_quantity=max(int(args.order_quantity), 1),
        market_data_provider=provider,
        pipeline_persistence_manager=pipeline_persistence_manager,
    )
    executor.restore_position_on_start()
    if pipeline_persistence_manager.enabled:
        current_now = datetime.now(KST)
        recovery = pipeline_persistence_manager.load_recovery_state_once(
            current_trade_date=executor._trade_date_key(current_now),
            now=current_now,
            reconciled_symbols=executor._pipeline_reconciled_symbols(),
        )
        executor.set_bootstrap_pipeline_recovery(
            slice_recovery_result_for_symbol(recovery, symbol=executor.stock_code)
        )

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
        if pipeline_persistence_stop_event is not None:
            pipeline_persistence_stop_event.set()
        if pipeline_persistence_worker is not None:
            try:
                pipeline_persistence_worker.join(timeout=5.0)
            except Exception:
                pass
        if stop_ws:
            stop_ws()
        try:
            # WS/단발 실행 경로에서도 포지션 체크포인트 저장 보장
            executor._save_position_on_exit()
        except Exception:
            pass
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    setup_logger("apps.kr_trade", settings.LOG_LEVEL)
    parser = _build_parser()
    args = parser.parse_args(argv)
    return run_trade_app(args)


if __name__ == "__main__":
    raise SystemExit(main())
