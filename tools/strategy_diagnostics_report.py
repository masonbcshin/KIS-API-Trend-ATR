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

from analytics.diagnostics import render_diagnostics_text
from analytics.materializer import StrategyAnalyticsMaterializer


def _load_env() -> None:
    for env_path in (PROJECT_ROOT / ".env", APP_ROOT / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _parse_date(raw: str) -> str:
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Strategy diagnostics report")
    parser.add_argument("--date", required=True, type=_parse_date, help="대상 거래일 (YYYY-MM-DD)")
    parser.add_argument("--event-dir", default=None, help="live analytics event dir override")
    parser.add_argument("--replay-event-dir", default=None, help="replay analytics event dir")
    parser.add_argument("--json", action="store_true", help="JSON 출력")
    parser.add_argument("--top-n", type=int, default=5, help="strategy별 top 항목 개수")
    return parser


def main() -> int:
    _load_env()
    args = _build_parser().parse_args()
    materializer = StrategyAnalyticsMaterializer(event_dir=args.event_dir)
    analytics_payload = materializer.materialize_trade_date(trade_date=args.date, persist=False)
    parity_rows = []
    if args.replay_event_dir:
        replay_payload = StrategyAnalyticsMaterializer(event_dir=args.replay_event_dir).materialize_trade_date(
            trade_date=args.date,
            persist=False,
        )
        parity_rows = materializer.build_parity_rows(
            trade_date=args.date,
            live_payload=analytics_payload,
            replay_payload=replay_payload,
        )
    alert_rows = materializer.build_alert_rows(
        trade_date=args.date,
        analytics_payload=analytics_payload,
        parity_rows=parity_rows,
    )
    report = materializer.build_diagnostics_report(
        trade_date=args.date,
        analytics_payload=analytics_payload,
        alert_rows=alert_rows,
        parity_rows=parity_rows,
        top_n=max(int(args.top_n or 0), 1),
    )
    if args.replay_event_dir:
        report["replay_input_diagnostics"] = dict(replay_payload.get("event_input_diagnostics") or {})
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_diagnostics_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
