"""
KIS Trend-ATR Trading System - 리포트 계산기

거래 데이터를 기반으로 일일 리포트에 필요한 통계를 계산합니다.

계산 항목:
    - 총 거래 횟수
    - 승률 (%)
    - 당일 실현손익
    - 누적 실현손익 (MTD)
    - 최대 손실률 (단일 거래 기준)
    - 평균 보유 시간 (분)
    - 손실이 가장 컸던 거래 정보
    - 거래 횟수 이상치 여부
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, List, Dict, Any

import pandas as pd
import numpy as np

from utils.logger import get_logger
from utils.symbol_resolver import get_symbol_resolver

logger = get_logger("report_calculator")


# ════════════════════════════════════════════════════════════════
# 데이터 클래스 정의
# ════════════════════════════════════════════════════════════════

@dataclass
class TradeInfo:
    """개별 거래 정보"""
    trade_date: str
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    holding_minutes: float
    loss_pct: float = 0.0  # 손실률 (%)


@dataclass
class DailyReport:
    """일일 리포트 데이터 구조"""
    # 기본 정보
    report_date: str
    
    # 일일 통계
    total_trades: int = 0
    win_rate: float = 0.0
    daily_pnl: float = 0.0
    mtd_pnl: float = 0.0
    
    # 리스크 지표
    max_loss_pct: float = 0.0
    avg_holding_minutes: float = 0.0
    
    # 특이사항
    worst_trade: Optional[TradeInfo] = None
    is_high_trade_volume: bool = False
    trade_volume_ratio: float = 1.0  # 평균 대비 배수
    
    # 추가 통계
    win_count: int = 0
    loss_count: int = 0
    total_win_amount: float = 0.0
    total_loss_amount: float = 0.0
    
    # 메타데이터
    has_data: bool = False
    error_message: Optional[str] = None


# ════════════════════════════════════════════════════════════════
# 리포트 계산기 클래스
# ════════════════════════════════════════════════════════════════

class ReportCalculator:
    """
    거래 데이터를 기반으로 일일 리포트를 계산하는 클래스
    
    Usage:
        calculator = ReportCalculator()
        report = calculator.calculate(daily_df, mtd_df, target_date)
    """
    
    def __init__(
        self,
        high_volume_threshold: float = 2.0,
        avg_trades_lookback_days: int = 20
    ):
        """
        리포트 계산기 초기화
        
        Args:
            high_volume_threshold: 거래량 경고 임계값 (평균 대비 배수)
            avg_trades_lookback_days: 평균 거래 횟수 계산 기간 (일)
        """
        self.high_volume_threshold = high_volume_threshold
        self.avg_trades_lookback_days = avg_trades_lookback_days
    
    def calculate(
        self,
        daily_df: pd.DataFrame,
        mtd_df: pd.DataFrame,
        target_date: date,
        historical_df: Optional[pd.DataFrame] = None
    ) -> DailyReport:
        """
        일일 리포트를 계산합니다.
        
        Args:
            daily_df: 당일 거래 데이터
            mtd_df: MTD(월초~당일) 거래 데이터
            target_date: 리포트 대상 날짜
            historical_df: 거래량 평균 계산용 과거 데이터 (선택)
        
        Returns:
            DailyReport: 계산된 리포트
        """
        report = DailyReport(report_date=target_date.strftime("%Y-%m-%d"))
        
        try:
            # 데이터 존재 여부 확인
            if daily_df is None or daily_df.empty:
                report.has_data = False
                logger.info(f"[REPORT] {target_date} 거래 데이터 없음")
                return report
            
            report.has_data = True
            
            # 일일 통계 계산
            self._calculate_daily_stats(report, daily_df)
            
            # MTD 통계 계산
            self._calculate_mtd_stats(report, mtd_df)
            
            # 리스크 지표 계산
            self._calculate_risk_metrics(report, daily_df)
            
            # 최악의 거래 식별
            self._identify_worst_trade(report, daily_df)
            
            # 거래량 이상치 판정
            self._check_volume_anomaly(report, daily_df, historical_df)
            
            logger.info(
                f"[REPORT] {target_date} 리포트 계산 완료: "
                f"거래 {report.total_trades}회, 승률 {report.win_rate:.1f}%"
            )
            
        except Exception as e:
            logger.error(f"[REPORT] 리포트 계산 중 오류: {e}")
            report.error_message = str(e)
        
        return report
    
    def _calculate_daily_stats(
        self,
        report: DailyReport,
        df: pd.DataFrame
    ) -> None:
        """일일 기본 통계를 계산합니다."""
        # 총 거래 횟수
        report.total_trades = len(df)
        
        if report.total_trades == 0:
            return
        
        # 승/패 구분
        wins = df[df["pnl"] > 0]
        losses = df[df["pnl"] < 0]
        
        report.win_count = len(wins)
        report.loss_count = len(losses)
        
        # 승률 계산
        report.win_rate = (report.win_count / report.total_trades) * 100
        
        # 당일 실현손익
        report.daily_pnl = df["pnl"].sum()
        
        # 수익/손실 금액 합계
        report.total_win_amount = wins["pnl"].sum() if not wins.empty else 0.0
        report.total_loss_amount = losses["pnl"].sum() if not losses.empty else 0.0
    
    def _calculate_mtd_stats(
        self,
        report: DailyReport,
        mtd_df: pd.DataFrame
    ) -> None:
        """MTD 통계를 계산합니다."""
        if mtd_df is None or mtd_df.empty:
            report.mtd_pnl = report.daily_pnl
            return
        
        report.mtd_pnl = mtd_df["pnl"].sum()
    
    def _calculate_risk_metrics(
        self,
        report: DailyReport,
        df: pd.DataFrame
    ) -> None:
        """리스크 지표를 계산합니다."""
        if df.empty:
            return
        
        # 평균 보유 시간
        report.avg_holding_minutes = df["holding_minutes"].mean()
        
        # 최대 손실률 계산 (단일 거래 기준)
        # 손실률 = (exit_price - entry_price) / entry_price * 100
        # SELL의 경우 부호 반전 필요
        df = df.copy()
        df["loss_pct"] = self._calculate_loss_pct(df)
        
        # 최대 손실률 (음수 중 가장 낮은 값의 절대값)
        min_loss_pct = df["loss_pct"].min()
        report.max_loss_pct = abs(min_loss_pct) if min_loss_pct < 0 else 0.0
    
    def _calculate_loss_pct(self, df: pd.DataFrame) -> pd.Series:
        """
        각 거래의 손실률을 계산합니다.
        
        BUY 거래: (exit_price - entry_price) / entry_price * 100
        """
        loss_pct = pd.Series(index=df.index, dtype=float)
        
        for idx, row in df.iterrows():
            entry = row["entry_price"]
            exit_price = row["exit_price"]
            
            if entry > 0:
                pct_change = ((exit_price - entry) / entry) * 100
                loss_pct[idx] = pct_change
            else:
                loss_pct[idx] = 0.0
        
        return loss_pct
    
    def _identify_worst_trade(
        self,
        report: DailyReport,
        df: pd.DataFrame
    ) -> None:
        """손실이 가장 컸던 거래를 식별합니다."""
        if df.empty:
            return
        
        # 가장 큰 손실 거래 찾기
        losses = df[df["pnl"] < 0]
        
        if losses.empty:
            return
        
        worst_idx = losses["pnl"].idxmin()
        worst_row = df.loc[worst_idx]
        
        # 손실률 계산
        entry = worst_row["entry_price"]
        exit_price = worst_row["exit_price"]
        loss_pct = ((exit_price - entry) / entry * 100) if entry > 0 else 0.0
        
        report.worst_trade = TradeInfo(
            trade_date=str(worst_row["trade_date"])[:10],
            symbol=worst_row["symbol"],
            side=worst_row["side"],
            entry_price=worst_row["entry_price"],
            exit_price=worst_row["exit_price"],
            quantity=int(worst_row["quantity"]),
            pnl=worst_row["pnl"],
            holding_minutes=worst_row["holding_minutes"],
            loss_pct=loss_pct
        )
    
    def _check_volume_anomaly(
        self,
        report: DailyReport,
        daily_df: pd.DataFrame,
        historical_df: Optional[pd.DataFrame] = None
    ) -> None:
        """
        거래량 이상치를 판정합니다.
        
        과거 데이터가 없으면 기본 평균값(5회)을 사용합니다.
        """
        if daily_df.empty:
            return
        
        daily_count = len(daily_df)
        
        if historical_df is not None and not historical_df.empty:
            # 일별 거래 횟수 계산
            historical_df = historical_df.copy()
            if "trade_date" in historical_df.columns:
                daily_counts = historical_df.groupby(
                    historical_df["trade_date"].dt.date
                ).size()
                avg_trades = daily_counts.mean() if len(daily_counts) > 0 else 5.0
            else:
                avg_trades = 5.0
        else:
            # 기본 평균값
            avg_trades = 5.0
        
        # 비율 계산
        report.trade_volume_ratio = daily_count / avg_trades if avg_trades > 0 else 1.0
        
        # 이상치 판정 (평균 대비 2배 이상)
        report.is_high_trade_volume = (
            report.trade_volume_ratio >= self.high_volume_threshold
        )
    
    def generate_summary_text(self, report: DailyReport) -> str:
        """
        특이사항 요약 문장을 생성합니다.
        
        Args:
            report: 계산된 리포트
        
        Returns:
            str: 요약 문장
        """
        summaries = []
        
        # 거래 없음
        if not report.has_data or report.total_trades == 0:
            return "거래 없음"
        
        # 거래량 경고
        if report.is_high_trade_volume:
            summaries.append(
                f"⚠️ 거래 횟수가 평균 대비 {report.trade_volume_ratio:.1f}배 증가"
            )
        
        # 최악의 거래 정보
        if report.worst_trade:
            worst = report.worst_trade
            try:
                display_symbol = get_symbol_resolver().format_symbol(worst.symbol, refresh=False)
            except Exception:
                display_symbol = f"UNKNOWN({worst.symbol})"
            summaries.append(
                f"최대 손실 거래: {display_symbol} "
                f"({worst.pnl:,.0f}원, {worst.loss_pct:.1f}%)"
            )
        
        # 승률 기반 코멘트
        if report.win_rate >= 70:
            summaries.append("높은 승률 유지 중")
        elif report.win_rate < 40:
            summaries.append("⚠️ 낮은 승률 주의 필요")
        
        # 연속 손실 여부 (추후 확장 가능)
        
        if not summaries:
            summaries.append("정상 거래")
        
        return "\n• ".join(summaries)
