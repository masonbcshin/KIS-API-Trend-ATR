#!/usr/bin/env python3
"""CLI entrypoint for KIS daily report generation and Telegram delivery."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "kis_trend_atr_trading"
sys.path.insert(0, str(APP_ROOT))

from reporting.daily_report_service import DailyReportService
from utils.logger import get_logger

logger = get_logger("daily_report_cli")


def _load_env() -> None:
    root_env = PROJECT_ROOT / ".env"
    app_env = APP_ROOT / ".env"
    if root_env.exists():
        load_dotenv(root_env, override=False)
    if app_env.exists():
        load_dotenv(app_env, override=False)


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("날짜 형식은 YYYY-MM-DD 여야 합니다.") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KIS 일일 자동 리포트 생성/전송",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date", type=_parse_date, help="리포트 날짜 (YYYY-MM-DD)")
    group.add_argument("--yesterday", action="store_true", help="어제 날짜로 리포트 생성")

    parser.add_argument("--dry-run", action="store_true", help="텔레그램 전송 없이 콘솔 출력")
    parser.add_argument("--test-telegram", action="store_true", help="텔레그램 연결 테스트만 수행")
    parser.add_argument("--timezone", default="Asia/Seoul", help="기준 시간대 (기본: Asia/Seoul)")
    return parser


def _resolve_target_date(args: argparse.Namespace, tz: pytz.BaseTzInfo) -> date:
    if args.date:
        return args.date
    now = datetime.now(tz).date()
    if args.yesterday:
        return now - timedelta(days=1)
    return now


def main() -> int:
    _load_env()
    parser = _build_parser()
    args = parser.parse_args()

    try:
        timezone = pytz.timezone(args.timezone)
    except pytz.UnknownTimeZoneError:
        print(f"유효하지 않은 timezone: {args.timezone}")
        return 2

    service = DailyReportService()

    if args.test_telegram:
        success = service.test_telegram_connection()
        print("✅ 텔레그램 연결 성공" if success else "❌ 텔레그램 연결 실패")
        return 0 if success else 1

    target_date = _resolve_target_date(args, timezone)
    logger.info(f"[DAILY_REPORT] 대상일={target_date}, dry_run={args.dry_run}")

    try:
        report = service.build_report(target_date)
        message = service.render_message(report)
    except Exception as exc:
        logger.error(f"[DAILY_REPORT] 리포트 생성 실패: {exc}", exc_info=True)
        print(f"❌ 리포트 생성 실패: {exc}")
        return 1

    if args.dry_run:
        print(message)
        return 0

    sent = service.send_report_message(message)
    if not sent:
        logger.error("[DAILY_REPORT] 텔레그램 전송 실패 (프로세스는 정상 종료)")
        print("⚠️ 텔레그램 전송 실패 (로그 확인). 프로세스는 정상 종료합니다.")
        return 0

    print(f"✅ {target_date.isoformat()} 리포트 전송 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
