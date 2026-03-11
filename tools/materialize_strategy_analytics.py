#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "kis_trend_atr_trading"
for _path in (APP_ROOT, PROJECT_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from analytics.materializer import StrategyAnalyticsMaterializer


def _load_env() -> None:
    for env_path in (PROJECT_ROOT / ".env", APP_ROOT / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _parse_date(raw: str) -> str:
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Append-only strategy analytics events를 요약 테이블로 materialize합니다.")
    parser.add_argument("--date", required=True, type=_parse_date, help="대상 거래일 (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="DB 적재 없이 결과만 출력")
    return parser


def main() -> int:
    _load_env()
    args = _build_parser().parse_args()
    materializer = StrategyAnalyticsMaterializer()
    result = materializer.materialize_trade_date(trade_date=args.date, persist=not args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

