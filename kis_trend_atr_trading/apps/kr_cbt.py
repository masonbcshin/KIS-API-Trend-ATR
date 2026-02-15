"""Unified CBT app entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

# Legacy modules inside this repo use absolute imports like `from config import ...`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "kis_trend_atr_trading"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from config import settings
from main_cbt import export_trades_csv, reset_cbt_account, run_cbt_trading, show_performance_report
from utils.logger import setup_logger


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified KR CBT app")
    parser.add_argument("--mode", choices=["cbt", "report", "reset", "export"], default="cbt")
    parser.add_argument("--stock", default=settings.DEFAULT_STOCK_CODE)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--max-runs", type=int, default=None)
    return parser


def run_cbt_mode(stock: str, interval: int, max_runs: Optional[int]) -> int:
    run_cbt_trading(stock_code=stock, interval=max(60, int(interval)), max_runs=max_runs)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    setup_logger("apps.kr_cbt", settings.LOG_LEVEL)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.mode == "cbt":
        return run_cbt_mode(args.stock, args.interval, args.max_runs)
    if args.mode == "report":
        show_performance_report()
        return 0
    if args.mode == "reset":
        reset_cbt_account()
        return 0
    if args.mode == "export":
        export_trades_csv()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

