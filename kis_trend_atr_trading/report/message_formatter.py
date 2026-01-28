"""
KIS Trend-ATR Trading System - 메시지 포매터

DailyReport 데이터를 텔레그램 메시지 형식으로 포맷팅합니다.

지원 포맷:
    - 텍스트 (기본)
    - HTML (향후 확장 가능)
"""

from abc import ABC, abstractmethod
from typing import Optional

from report.report_calculator import DailyReport, ReportCalculator

from utils.logger import get_logger

logger = get_logger("message_formatter")


# ════════════════════════════════════════════════════════════════
# 메시지 템플릿
# ════════════════════════════════════════════════════════════════

TEXT_TEMPLATE = """[{report_date} 일일 트레이딩 리포트]

• 총 거래: {total_trades}회
• 승률: {win_rate:.1f}%
• 실현손익: {daily_pnl_formatted}원
• 누적손익(MTD): {mtd_pnl_formatted}원

[리스크]
• 최대 손실: {max_loss_pct_formatted}%
• 평균 보유시간: {avg_holding_minutes:.0f}분

[특이사항]
• {summary_text}"""

NO_DATA_TEMPLATE = """[{report_date} 일일 트레이딩 리포트]

• 거래 없음

금일 체결된 거래가 없습니다."""

HTML_TEMPLATE = """<b>[{report_date} 일일 트레이딩 리포트]</b>

• 총 거래: <b>{total_trades}회</b>
• 승률: <b>{win_rate:.1f}%</b>
• 실현손익: <b>{daily_pnl_formatted}원</b>
• 누적손익(MTD): <b>{mtd_pnl_formatted}원</b>

<b>[리스크]</b>
• 최대 손실: <b>{max_loss_pct_formatted}%</b>
• 평균 보유시간: <b>{avg_holding_minutes:.0f}분</b>

<b>[특이사항]</b>
• {summary_text}"""


# ════════════════════════════════════════════════════════════════
# 추상 포매터 클래스
# ════════════════════════════════════════════════════════════════

class BaseFormatter(ABC):
    """메시지 포매터 추상 클래스"""
    
    @abstractmethod
    def format(self, report: DailyReport) -> str:
        """리포트를 메시지로 포맷팅합니다."""
        pass
    
    @staticmethod
    def format_number(value: float, with_sign: bool = True) -> str:
        """
        숫자를 천 단위 구분 문자열로 변환합니다.
        
        Args:
            value: 숫자 값
            with_sign: 양수에 + 부호 포함 여부
        
        Returns:
            str: 포맷팅된 문자열
        """
        if with_sign and value > 0:
            return f"+{value:,.0f}"
        elif value < 0:
            return f"{value:,.0f}"
        else:
            return f"{value:,.0f}"
    
    @staticmethod
    def format_loss_pct(value: float) -> str:
        """
        손실률을 포맷팅합니다.
        
        Args:
            value: 손실률 (양수)
        
        Returns:
            str: 포맷팅된 문자열 (음수 형태)
        """
        if value > 0:
            return f"-{value:.1f}"
        return "0.0"


# ════════════════════════════════════════════════════════════════
# 텍스트 포매터 클래스
# ════════════════════════════════════════════════════════════════

class MessageFormatter(BaseFormatter):
    """
    일일 리포트를 텔레그램 텍스트 메시지로 포맷팅하는 클래스
    
    Usage:
        formatter = MessageFormatter()
        message = formatter.format(report)
    """
    
    def __init__(self, calculator: Optional[ReportCalculator] = None):
        """
        메시지 포매터 초기화
        
        Args:
            calculator: 특이사항 요약 생성용 계산기 (선택)
        """
        self._calculator = calculator or ReportCalculator()
    
    def format(self, report: DailyReport) -> str:
        """
        DailyReport를 텔레그램 텍스트 메시지로 변환합니다.
        
        Args:
            report: 일일 리포트 데이터
        
        Returns:
            str: 포맷팅된 텍스트 메시지
        """
        # 데이터 없음 처리
        if not report.has_data or report.total_trades == 0:
            return NO_DATA_TEMPLATE.format(report_date=report.report_date)
        
        # 특이사항 요약 생성
        summary_text = self._calculator.generate_summary_text(report)
        
        # 숫자 포맷팅
        daily_pnl_formatted = self.format_number(report.daily_pnl)
        mtd_pnl_formatted = self.format_number(report.mtd_pnl)
        max_loss_pct_formatted = self.format_loss_pct(report.max_loss_pct)
        
        # 템플릿 적용
        message = TEXT_TEMPLATE.format(
            report_date=report.report_date,
            total_trades=report.total_trades,
            win_rate=report.win_rate,
            daily_pnl_formatted=daily_pnl_formatted,
            mtd_pnl_formatted=mtd_pnl_formatted,
            max_loss_pct_formatted=max_loss_pct_formatted,
            avg_holding_minutes=report.avg_holding_minutes,
            summary_text=summary_text
        )
        
        return message
    
    def format_detailed(self, report: DailyReport) -> str:
        """
        상세 정보가 포함된 리포트 메시지를 생성합니다.
        
        Args:
            report: 일일 리포트 데이터
        
        Returns:
            str: 상세 정보가 포함된 메시지
        """
        base_message = self.format(report)
        
        if not report.has_data or report.total_trades == 0:
            return base_message
        
        # 추가 상세 정보
        details = []
        
        # 승/패 횟수
        details.append(
            f"\n[상세 통계]\n"
            f"• 승리: {report.win_count}회 (총 {self.format_number(report.total_win_amount)}원)\n"
            f"• 패배: {report.loss_count}회 (총 {self.format_number(report.total_loss_amount, with_sign=False)}원)"
        )
        
        # 최악의 거래 상세
        if report.worst_trade:
            worst = report.worst_trade
            details.append(
                f"\n[최대 손실 거래 상세]\n"
                f"• 종목: {worst.symbol}\n"
                f"• 진입가: {worst.entry_price:,.0f}원\n"
                f"• 청산가: {worst.exit_price:,.0f}원\n"
                f"• 손실액: {worst.pnl:,.0f}원 ({worst.loss_pct:.1f}%)\n"
                f"• 보유시간: {worst.holding_minutes:.0f}분"
            )
        
        return base_message + "".join(details)


# ════════════════════════════════════════════════════════════════
# HTML 포매터 클래스 (향후 확장용)
# ════════════════════════════════════════════════════════════════

class HTMLFormatter(BaseFormatter):
    """
    일일 리포트를 HTML 형식으로 포맷팅하는 클래스
    
    텔레그램의 HTML parse_mode를 사용할 때 활용합니다.
    향후 웹 리포트 등으로 확장 가능합니다.
    
    Usage:
        formatter = HTMLFormatter()
        html_message = formatter.format(report)
    """
    
    def __init__(self, calculator: Optional[ReportCalculator] = None):
        """
        HTML 포매터 초기화
        
        Args:
            calculator: 특이사항 요약 생성용 계산기 (선택)
        """
        self._calculator = calculator or ReportCalculator()
    
    def format(self, report: DailyReport) -> str:
        """
        DailyReport를 HTML 메시지로 변환합니다.
        
        Args:
            report: 일일 리포트 데이터
        
        Returns:
            str: HTML 포맷 메시지
        """
        # 데이터 없음 처리
        if not report.has_data or report.total_trades == 0:
            return f"<b>[{report.report_date} 일일 트레이딩 리포트]</b>\n\n• 거래 없음"
        
        # 특이사항 요약 생성
        summary_text = self._calculator.generate_summary_text(report)
        # HTML 이스케이프
        summary_text = self._escape_html(summary_text)
        
        # 숫자 포맷팅
        daily_pnl_formatted = self.format_number(report.daily_pnl)
        mtd_pnl_formatted = self.format_number(report.mtd_pnl)
        max_loss_pct_formatted = self.format_loss_pct(report.max_loss_pct)
        
        # 템플릿 적용
        message = HTML_TEMPLATE.format(
            report_date=report.report_date,
            total_trades=report.total_trades,
            win_rate=report.win_rate,
            daily_pnl_formatted=daily_pnl_formatted,
            mtd_pnl_formatted=mtd_pnl_formatted,
            max_loss_pct_formatted=max_loss_pct_formatted,
            avg_holding_minutes=report.avg_holding_minutes,
            summary_text=summary_text
        )
        
        return message
    
    @staticmethod
    def _escape_html(text: str) -> str:
        """HTML 특수문자를 이스케이프합니다."""
        replacements = {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text


# ════════════════════════════════════════════════════════════════
# 팩토리 함수
# ════════════════════════════════════════════════════════════════

def create_formatter(
    format_type: str = "text",
    **kwargs
) -> BaseFormatter:
    """
    포맷 유형에 따라 적절한 포매터를 생성합니다.
    
    Args:
        format_type: 포맷 유형 ("text" 또는 "html")
        **kwargs: 추가 설정
    
    Returns:
        BaseFormatter: 포매터 인스턴스
    
    Raises:
        ValueError: 지원하지 않는 포맷 유형인 경우
    """
    format_type = format_type.lower()
    
    if format_type == "text":
        return MessageFormatter(**kwargs)
    elif format_type == "html":
        return HTMLFormatter(**kwargs)
    else:
        raise ValueError(f"지원하지 않는 포맷 유형: {format_type}")
