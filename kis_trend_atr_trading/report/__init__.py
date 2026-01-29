"""
KIS Trend-ATR Trading System - 일일 리포트 모듈

이 패키지는 자동매매 결과를 집계하여 텔레그램으로 일일 리포트를 전송합니다.

모듈 구성:
    - data_loader: 거래 데이터 로딩 (CSV/DB 지원)
    - report_calculator: 일일 통계 계산
    - message_formatter: 텔레그램 메시지 포맷팅
    - telegram_sender: 텔레그램 전송 (재시도 로직 포함)
    - trade_reporter: 전체 성과 측정 시스템 (MDD, Profit Factor 등)
"""

from report.data_loader import DataLoader, CSVDataLoader, DBDataLoader
from report.report_calculator import ReportCalculator, DailyReport
from report.message_formatter import MessageFormatter
from report.telegram_sender import TelegramReportSender
from report.trade_reporter import (
    TradeReporter,
    TradeRecord,
    StockPerformance,
    AccountPerformance,
    get_trade_reporter
)

__all__ = [
    "DataLoader",
    "CSVDataLoader",
    "DBDataLoader",
    "ReportCalculator",
    "DailyReport",
    "MessageFormatter",
    "TelegramReportSender",
    "TradeReporter",
    "TradeRecord",
    "StockPerformance",
    "AccountPerformance",
    "get_trade_reporter",
]
