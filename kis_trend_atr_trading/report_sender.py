#!/usr/bin/env python3
"""
KIS Trend-ATR Trading System - 일일 리포트 전송기

자동매매 결과 데이터를 집계하여 텔레그램으로 일일 리포트를 전송합니다.

사용법:
    # 오늘 날짜로 리포트 전송
    python report_sender.py
    
    # 특정 날짜로 리포트 전송
    python report_sender.py --date 2024-01-15
    
    # CSV 파일 경로 지정
    python report_sender.py --source-path data/trades.csv
    
    # DB 사용
    python report_sender.py --source-type db --source-path data/trades.db
    
    # 연결 테스트만 수행
    python report_sender.py --test

환경변수:
    TELEGRAM_BOT_TOKEN: 텔레그램 봇 토큰 (필수)
    TELEGRAM_CHAT_ID: 텔레그램 채팅 ID (필수)
    TRADE_DATA_PATH: 거래 데이터 파일 경로 (선택, 기본: data/trades.csv)
    TRADE_DATA_TYPE: 데이터 소스 유형 (선택, 기본: csv)

Cron 등록 예시:
    # 매일 18:00에 리포트 전송 (장 마감 후)
    0 18 * * 1-5 cd /path/to/kis_trend_atr_trading && /usr/bin/python3 report_sender.py >> logs/report.log 2>&1
    
    # 매일 09:00에 전일 리포트 전송
    0 9 * * 1-5 cd /path/to/kis_trend_atr_trading && /usr/bin/python3 report_sender.py --date yesterday >> logs/report.log 2>&1

텔레그램 봇 설정:
    1. 텔레그램에서 @BotFather 검색하여 대화 시작
    2. /newbot 명령어 입력 후 봇 이름/사용자명 설정
    3. 발급받은 토큰을 TELEGRAM_BOT_TOKEN에 설정
    4. 봇과 대화 시작 후 https://api.telegram.org/bot<토큰>/getUpdates 에서 chat_id 확인
    5. chat_id를 TELEGRAM_CHAT_ID에 설정

작성자: KIS Trend-ATR Trading System
버전: 1.0.0
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from report.data_loader import create_data_loader
from report.report_calculator import ReportCalculator, DailyReport
from report.message_formatter import MessageFormatter, HTMLFormatter
from report.telegram_sender import TelegramReportSender

from utils.logger import get_logger
from utils.market_hours import KST

# 환경변수 로드
load_dotenv()

logger = get_logger("report_sender")


def kst_today() -> date:
    """KST 기준 오늘 날짜를 반환합니다."""
    return datetime.now(KST).date()


# ════════════════════════════════════════════════════════════════
# 메인 리포트 전송기 클래스
# ════════════════════════════════════════════════════════════════

class DailyReportSender:
    """
    일일 리포트 전송을 총괄하는 클래스
    
    데이터 로드 → 통계 계산 → 메시지 포맷팅 → 텔레그램 전송
    """
    
    def __init__(
        self,
        source_type: str = "csv",
        source_path: str = None,
        format_type: str = "text"
    ):
        """
        리포트 전송기 초기화
        
        Args:
            source_type: 데이터 소스 유형 ("csv" 또는 "db")
            source_path: 데이터 소스 경로
            format_type: 메시지 포맷 ("text" 또는 "html")
        """
        # 기본 경로 설정
        if source_path is None:
            source_path = os.getenv(
                "TRADE_DATA_PATH",
                str(PROJECT_ROOT / "data" / "trades.csv")
            )
        
        # 컴포넌트 초기화
        self._data_loader = create_data_loader(
            source_type=source_type,
            source_path=source_path
        )
        self._calculator = ReportCalculator()
        
        if format_type == "html":
            self._formatter = HTMLFormatter(calculator=self._calculator)
            self._parse_mode = "HTML"
        else:
            self._formatter = MessageFormatter(calculator=self._calculator)
            self._parse_mode = None
        
        self._sender = TelegramReportSender()
        
        logger.info(
            f"[REPORT_SENDER] 초기화 완료 "
            f"(소스: {source_type}, 경로: {source_path})"
        )
    
    def send_daily_report(
        self,
        target_date: date = None,
        detailed: bool = False
    ) -> bool:
        """
        일일 리포트를 전송합니다.
        
        Args:
            target_date: 리포트 대상 날짜 (기본: 오늘)
            detailed: 상세 리포트 여부
        
        Returns:
            bool: 전송 성공 여부
        """
        if target_date is None:
            target_date = kst_today()
        
        logger.info(f"[REPORT_SENDER] {target_date} 리포트 생성 시작")
        
        try:
            # 1. 데이터 로드
            daily_df = self._data_loader.load_daily_trades(target_date)
            mtd_df = self._data_loader.load_trades(target_date, include_mtd=True)
            
            logger.info(
                f"[REPORT_SENDER] 데이터 로드 완료 "
                f"(당일: {len(daily_df)}건, MTD: {len(mtd_df)}건)"
            )
            
            # 2. 통계 계산
            report = self._calculator.calculate(
                daily_df=daily_df,
                mtd_df=mtd_df,
                target_date=target_date
            )
            
            # 3. 메시지 포맷팅
            if detailed and isinstance(self._formatter, MessageFormatter):
                message = self._formatter.format_detailed(report)
            else:
                message = self._formatter.format(report)
            
            logger.debug(f"[REPORT_SENDER] 메시지 생성 완료:\n{message}")
            
            # 4. 텔레그램 전송
            success = self._sender.send_report(
                message=message,
                parse_mode=self._parse_mode
            )
            
            if success:
                logger.info(f"[REPORT_SENDER] {target_date} 리포트 전송 성공")
            else:
                logger.error(f"[REPORT_SENDER] {target_date} 리포트 전송 실패")
            
            return success
            
        except Exception as e:
            logger.error(f"[REPORT_SENDER] 리포트 전송 중 오류: {e}")
            return False
    
    def test_connection(self) -> bool:
        """텔레그램 연결을 테스트합니다."""
        return self._sender.test_connection()


# ════════════════════════════════════════════════════════════════
# CLI 인터페이스
# ════════════════════════════════════════════════════════════════

def parse_date(date_str: str) -> date:
    """
    날짜 문자열을 date 객체로 변환합니다.
    
    지원 형식:
        - YYYY-MM-DD
        - today
        - yesterday
    """
    date_str = date_str.lower().strip()
    
    if date_str == "today":
        return kst_today()
    elif date_str == "yesterday":
        return kst_today() - timedelta(days=1)
    else:
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"잘못된 날짜 형식: {date_str} (YYYY-MM-DD, today, yesterday 사용 가능)"
            )


def create_parser() -> argparse.ArgumentParser:
    """명령행 인자 파서를 생성합니다."""
    parser = argparse.ArgumentParser(
        description="KIS 자동매매 일일 리포트 전송기",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
    %(prog)s                           # 오늘 리포트 전송
    %(prog)s --date 2024-01-15         # 특정 날짜 리포트 전송
    %(prog)s --date yesterday          # 어제 리포트 전송
    %(prog)s --source-type db          # DB에서 데이터 로드
    %(prog)s --test                    # 연결 테스트
    %(prog)s --detailed                # 상세 리포트 전송

Cron 등록:
    # 매일 18:00 장 마감 후 리포트 전송
    0 18 * * 1-5 cd /path/to/project && python3 report_sender.py >> logs/report.log 2>&1
"""
    )
    
    parser.add_argument(
        "--date", "-d",
        type=parse_date,
        default=kst_today(),
        help="리포트 대상 날짜 (YYYY-MM-DD, today, yesterday)"
    )
    
    parser.add_argument(
        "--source-type", "-t",
        choices=["csv", "db"],
        default=os.getenv("TRADE_DATA_TYPE", "csv"),
        help="데이터 소스 유형 (기본: csv)"
    )
    
    parser.add_argument(
        "--source-path", "-p",
        type=str,
        default=None,
        help="데이터 소스 경로 (기본: data/trades.csv)"
    )
    
    parser.add_argument(
        "--format", "-f",
        choices=["text", "html"],
        default="text",
        help="메시지 포맷 (기본: text)"
    )
    
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="상세 리포트 전송"
    )
    
    parser.add_argument(
        "--test",
        action="store_true",
        help="텔레그램 연결 테스트만 수행"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="상세 로그 출력"
    )
    
    return parser


def main():
    """메인 함수"""
    parser = create_parser()
    args = parser.parse_args()
    
    # 리포트 전송기 생성
    sender = DailyReportSender(
        source_type=args.source_type,
        source_path=args.source_path,
        format_type=args.format
    )
    
    # 연결 테스트 모드
    if args.test:
        print("텔레그램 연결 테스트 중...")
        if sender.test_connection():
            print("✅ 텔레그램 연결 테스트 성공")
            return 0
        else:
            print("❌ 텔레그램 연결 테스트 실패")
            return 1
    
    # 리포트 전송
    print(f"📊 {args.date} 일일 리포트 전송 중...")
    
    success = sender.send_daily_report(
        target_date=args.date,
        detailed=args.detailed
    )
    
    if success:
        print(f"✅ {args.date} 리포트 전송 완료")
        return 0
    else:
        print(f"❌ {args.date} 리포트 전송 실패")
        return 1


# ════════════════════════════════════════════════════════════════
# 실행
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    sys.exit(main())
