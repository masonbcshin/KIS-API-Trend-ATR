"""
일일 리포트 모듈 테스트

테스트 대상:
    - DataLoader (CSV/DB)
    - ReportCalculator
    - MessageFormatter
    - TelegramReportSender
"""

import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

# 프로젝트 루트를 sys.path에 추가
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from report.data_loader import (
    CSVDataLoader,
    DBDataLoader,
    create_data_loader,
    REQUIRED_COLUMNS
)
from report.report_calculator import (
    ReportCalculator,
    DailyReport,
    TradeInfo
)
from report.message_formatter import (
    MessageFormatter,
    HTMLFormatter,
    create_formatter
)
from report.telegram_sender import (
    TelegramReportSender,
    create_telegram_sender
)


# ════════════════════════════════════════════════════════════════
# 테스트 픽스처
# ════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_trades_df():
    """샘플 거래 데이터 DataFrame"""
    return pd.DataFrame({
        "trade_date": pd.to_datetime([
            "2024-01-15", "2024-01-15", "2024-01-15",
            "2024-01-15", "2024-01-15"
        ]),
        "symbol": ["005930", "005930", "035720", "035720", "005380"],
        "side": ["BUY", "SELL", "BUY", "SELL", "SELL"],
        "entry_price": [70000, 71000, 55000, 54000, 180000],
        "exit_price": [71000, 72000, 54000, 53000, 175000],
        "quantity": [10, 10, 20, 20, 5],
        "pnl": [10000, 10000, -20000, -20000, -25000],
        "holding_minutes": [30, 45, 60, 90, 120]
    })


@pytest.fixture
def sample_mtd_df(sample_trades_df):
    """샘플 MTD 데이터 (당일 + 이전 거래)"""
    prev_trades = pd.DataFrame({
        "trade_date": pd.to_datetime(["2024-01-10", "2024-01-12"]),
        "symbol": ["005930", "035720"],
        "side": ["BUY", "SELL"],
        "entry_price": [68000, 53000],
        "exit_price": [70000, 55000],
        "quantity": [10, 15],
        "pnl": [20000, 30000],
        "holding_minutes": [60, 45]
    })
    return pd.concat([prev_trades, sample_trades_df], ignore_index=True)


@pytest.fixture
def temp_csv_file(sample_trades_df):
    """임시 CSV 파일"""
    fd, path = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    sample_trades_df.to_csv(path, index=False)
    yield path
    os.unlink(path)


# ════════════════════════════════════════════════════════════════
# DataLoader 테스트
# ════════════════════════════════════════════════════════════════

class TestCSVDataLoader:
    """CSVDataLoader 테스트"""
    
    def test_load_daily_trades(self, temp_csv_file):
        """당일 거래 데이터 로드 테스트"""
        loader = CSVDataLoader(temp_csv_file)
        df = loader.load_daily_trades(date(2024, 1, 15))
        
        assert len(df) == 5
        assert all(col in df.columns for col in REQUIRED_COLUMNS)
    
    def test_load_trades_with_mtd(self, temp_csv_file):
        """MTD 데이터 로드 테스트"""
        loader = CSVDataLoader(temp_csv_file)
        df = loader.load_trades(date(2024, 1, 15), include_mtd=True)
        
        assert len(df) == 5
    
    def test_missing_file(self):
        """존재하지 않는 파일 처리 테스트"""
        loader = CSVDataLoader("/nonexistent/path/trades.csv")
        df = loader.load_daily_trades(date.today())
        
        assert df.empty
        assert all(col in df.columns for col in REQUIRED_COLUMNS)


class TestDataLoaderFactory:
    """DataLoader 팩토리 테스트"""
    
    def test_create_csv_loader(self, temp_csv_file):
        """CSV 로더 생성 테스트"""
        loader = create_data_loader(
            source_type="csv",
            source_path=temp_csv_file
        )
        assert isinstance(loader, CSVDataLoader)
    
    def test_create_db_loader(self):
        """DB 로더 생성 테스트"""
        loader = create_data_loader(
            source_type="db",
            source_path="/tmp/test.db"
        )
        assert isinstance(loader, DBDataLoader)
    
    def test_invalid_source_type(self):
        """잘못된 소스 타입 처리 테스트"""
        with pytest.raises(ValueError):
            create_data_loader(source_type="invalid")


# ════════════════════════════════════════════════════════════════
# ReportCalculator 테스트
# ════════════════════════════════════════════════════════════════

class TestReportCalculator:
    """ReportCalculator 테스트"""
    
    def test_calculate_basic_stats(self, sample_trades_df, sample_mtd_df):
        """기본 통계 계산 테스트"""
        calculator = ReportCalculator()
        report = calculator.calculate(
            daily_df=sample_trades_df,
            mtd_df=sample_mtd_df,
            target_date=date(2024, 1, 15)
        )
        
        assert report.has_data is True
        assert report.total_trades == 5
        assert report.win_count == 2
        assert report.loss_count == 3
        assert report.daily_pnl == -45000  # 10000 + 10000 - 20000 - 20000 - 25000
    
    def test_win_rate_calculation(self, sample_trades_df, sample_mtd_df):
        """승률 계산 테스트"""
        calculator = ReportCalculator()
        report = calculator.calculate(
            daily_df=sample_trades_df,
            mtd_df=sample_mtd_df,
            target_date=date(2024, 1, 15)
        )
        
        expected_win_rate = (2 / 5) * 100  # 40%
        assert report.win_rate == pytest.approx(expected_win_rate)
    
    def test_mtd_pnl_calculation(self, sample_trades_df, sample_mtd_df):
        """MTD 손익 계산 테스트"""
        calculator = ReportCalculator()
        report = calculator.calculate(
            daily_df=sample_trades_df,
            mtd_df=sample_mtd_df,
            target_date=date(2024, 1, 15)
        )
        
        expected_mtd = 20000 + 30000 + (-45000)  # 이전 거래 + 당일
        assert report.mtd_pnl == expected_mtd
    
    def test_avg_holding_time(self, sample_trades_df, sample_mtd_df):
        """평균 보유 시간 계산 테스트"""
        calculator = ReportCalculator()
        report = calculator.calculate(
            daily_df=sample_trades_df,
            mtd_df=sample_mtd_df,
            target_date=date(2024, 1, 15)
        )
        
        expected_avg = (30 + 45 + 60 + 90 + 120) / 5
        assert report.avg_holding_minutes == pytest.approx(expected_avg)
    
    def test_worst_trade_identification(self, sample_trades_df, sample_mtd_df):
        """최악의 거래 식별 테스트"""
        calculator = ReportCalculator()
        report = calculator.calculate(
            daily_df=sample_trades_df,
            mtd_df=sample_mtd_df,
            target_date=date(2024, 1, 15)
        )
        
        assert report.worst_trade is not None
        assert report.worst_trade.pnl == -25000  # 가장 큰 손실
        assert report.worst_trade.symbol == "005380"
    
    def test_empty_data(self):
        """빈 데이터 처리 테스트"""
        calculator = ReportCalculator()
        empty_df = pd.DataFrame(columns=REQUIRED_COLUMNS)
        
        report = calculator.calculate(
            daily_df=empty_df,
            mtd_df=empty_df,
            target_date=date(2024, 1, 15)
        )
        
        assert report.has_data is False
        assert report.total_trades == 0
    
    def test_high_volume_detection(self, sample_trades_df, sample_mtd_df):
        """고거래량 감지 테스트"""
        calculator = ReportCalculator(high_volume_threshold=1.5)
        
        # 과거 평균 2건 대비 5건이면 2.5배
        historical_df = pd.DataFrame({
            "trade_date": pd.to_datetime(["2024-01-10", "2024-01-11"]),
            "symbol": ["005930", "035720"],
            "side": ["BUY", "SELL"],
            "entry_price": [68000, 53000],
            "exit_price": [70000, 55000],
            "quantity": [10, 15],
            "pnl": [20000, 30000],
            "holding_minutes": [60, 45]
        })
        
        report = calculator.calculate(
            daily_df=sample_trades_df,
            mtd_df=sample_mtd_df,
            target_date=date(2024, 1, 15),
            historical_df=historical_df
        )
        
        # 5건 / 1건(일평균) = 5배 > 1.5배 임계값
        assert bool(report.is_high_trade_volume) is True


# ════════════════════════════════════════════════════════════════
# MessageFormatter 테스트
# ════════════════════════════════════════════════════════════════

class TestMessageFormatter:
    """MessageFormatter 테스트"""
    
    def test_format_basic_report(self, sample_trades_df, sample_mtd_df):
        """기본 리포트 포맷팅 테스트"""
        calculator = ReportCalculator()
        report = calculator.calculate(
            daily_df=sample_trades_df,
            mtd_df=sample_mtd_df,
            target_date=date(2024, 1, 15)
        )
        
        formatter = MessageFormatter()
        message = formatter.format(report)
        
        assert "[2024-01-15 일일 트레이딩 리포트]" in message
        assert "총 거래: 5회" in message
        assert "승률: 40.0%" in message
        assert "[리스크]" in message
        assert "[특이사항]" in message
    
    def test_format_no_data_report(self):
        """데이터 없음 리포트 포맷팅 테스트"""
        report = DailyReport(report_date="2024-01-15", has_data=False)
        
        formatter = MessageFormatter()
        message = formatter.format(report)
        
        assert "거래 없음" in message
    
    def test_number_formatting(self):
        """숫자 포맷팅 테스트"""
        formatter = MessageFormatter()
        
        assert formatter.format_number(1234567) == "+1,234,567"
        assert formatter.format_number(-1234567) == "-1,234,567"
        assert formatter.format_number(0) == "0"
        assert formatter.format_number(1234567, with_sign=False) == "1,234,567"


class TestHTMLFormatter:
    """HTMLFormatter 테스트"""
    
    def test_format_html_report(self, sample_trades_df, sample_mtd_df):
        """HTML 리포트 포맷팅 테스트"""
        calculator = ReportCalculator()
        report = calculator.calculate(
            daily_df=sample_trades_df,
            mtd_df=sample_mtd_df,
            target_date=date(2024, 1, 15)
        )
        
        formatter = HTMLFormatter()
        message = formatter.format(report)
        
        assert "<b>" in message
        assert "</b>" in message


class TestFormatterFactory:
    """Formatter 팩토리 테스트"""
    
    def test_create_text_formatter(self):
        """텍스트 포매터 생성 테스트"""
        formatter = create_formatter("text")
        assert isinstance(formatter, MessageFormatter)
    
    def test_create_html_formatter(self):
        """HTML 포매터 생성 테스트"""
        formatter = create_formatter("html")
        assert isinstance(formatter, HTMLFormatter)
    
    def test_invalid_format_type(self):
        """잘못된 포맷 타입 처리 테스트"""
        with pytest.raises(ValueError):
            create_formatter("invalid")


# ════════════════════════════════════════════════════════════════
# TelegramReportSender 테스트
# ════════════════════════════════════════════════════════════════

class TestTelegramReportSender:
    """TelegramReportSender 테스트"""
    
    def test_init_without_config(self):
        """설정 없이 초기화 테스트"""
        # 환경변수 초기화
        with patch.dict(os.environ, {}, clear=True):
            sender = TelegramReportSender()
            assert sender.is_configured is False
    
    def test_init_with_config(self):
        """설정과 함께 초기화 테스트"""
        sender = TelegramReportSender(
            bot_token="test_token",
            chat_id="test_chat_id"
        )
        assert sender.is_configured is True
    
    @patch('report.telegram_sender.requests.post')
    def test_send_report_success(self, mock_post):
        """리포트 전송 성공 테스트"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True}
        mock_post.return_value = mock_response
        
        sender = TelegramReportSender(
            bot_token="test_token",
            chat_id="test_chat_id"
        )
        
        result = sender.send_report("테스트 메시지")
        
        assert result is True
        mock_post.assert_called_once()
    
    @patch('report.telegram_sender.requests.post')
    def test_send_report_retry_on_failure(self, mock_post):
        """전송 실패 시 재시도 테스트"""
        # 첫 2번 실패, 3번째 성공
        mock_response_fail = MagicMock()
        mock_response_fail.status_code = 500
        
        mock_response_success = MagicMock()
        mock_response_success.status_code = 200
        mock_response_success.json.return_value = {"ok": True}
        
        mock_post.side_effect = [
            mock_response_fail,
            mock_response_fail,
            mock_response_success
        ]
        
        sender = TelegramReportSender(
            bot_token="test_token",
            chat_id="test_chat_id",
            retry_delay=0.01  # 테스트 속도를 위해 짧게
        )
        
        result = sender.send_report("테스트 메시지")
        
        assert result is True
        assert mock_post.call_count == 3
    
    @patch('report.telegram_sender.requests.post')
    def test_send_report_max_retries_exceeded(self, mock_post):
        """최대 재시도 초과 테스트"""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_post.return_value = mock_response
        
        sender = TelegramReportSender(
            bot_token="test_token",
            chat_id="test_chat_id",
            max_retries=3,
            retry_delay=0.01
        )
        
        result = sender.send_report("테스트 메시지")
        
        assert result is False
        assert mock_post.call_count == 3
    
    def test_empty_message_rejection(self):
        """빈 메시지 거부 테스트"""
        sender = TelegramReportSender(
            bot_token="test_token",
            chat_id="test_chat_id"
        )
        
        result = sender.send_report("")
        assert result is False
    
    def test_message_truncation(self):
        """긴 메시지 자르기 테스트"""
        sender = TelegramReportSender(
            bot_token="test_token",
            chat_id="test_chat_id"
        )
        
        # 4096자 초과 메시지
        long_message = "A" * 5000
        
        with patch('report.telegram_sender.requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"ok": True}
            mock_post.return_value = mock_response
            
            sender.send_report(long_message)
            
            # 전송된 메시지 확인
            call_args = mock_post.call_args
            sent_text = call_args[1]["json"]["text"]
            assert len(sent_text) <= 4096


# ════════════════════════════════════════════════════════════════
# 통합 테스트
# ════════════════════════════════════════════════════════════════

class TestIntegration:
    """통합 테스트"""
    
    @patch('report.telegram_sender.requests.post')
    def test_full_pipeline(self, mock_post, temp_csv_file):
        """전체 파이프라인 테스트"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True}
        mock_post.return_value = mock_response
        
        # 1. 데이터 로드
        loader = CSVDataLoader(temp_csv_file)
        daily_df = loader.load_daily_trades(date(2024, 1, 15))
        mtd_df = loader.load_trades(date(2024, 1, 15), include_mtd=True)
        
        # 2. 통계 계산
        calculator = ReportCalculator()
        report = calculator.calculate(
            daily_df=daily_df,
            mtd_df=mtd_df,
            target_date=date(2024, 1, 15)
        )
        
        # 3. 메시지 포맷팅
        formatter = MessageFormatter()
        message = formatter.format(report)
        
        # 4. 텔레그램 전송
        sender = TelegramReportSender(
            bot_token="test_token",
            chat_id="test_chat_id"
        )
        result = sender.send_report(message)
        
        assert result is True
        assert report.total_trades == 5
        assert "2024-01-15" in message
