"""
KIS Trend-ATR Trading System - CBT 성과 지표 계산

이 모듈은 CBT 모드의 거래 성과를 자동 계산합니다.

계산 지표:
    - 누적 수익률
    - 승률 (Win Rate)
    - 평균 수익 / 평균 손실
    - Expectancy (기대값)
    - Maximum Drawdown (최대 낙폭)
    - Profit Factor
    - Sharpe Ratio (일간 기준)

작성자: KIS Trend-ATR Trading System
버전: 1.0.0
"""

import math
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

from .trade_store import Trade, TradeStore
from .virtual_account import VirtualAccount, EquitySnapshot
from utils.logger import get_logger
from utils.market_hours import KST

logger = get_logger("cbt_metrics")


@dataclass
class PerformanceReport:
    """
    성과 리포트 데이터 클래스
    
    모든 주요 성과 지표를 담습니다.
    """
    # 기본 정보
    report_date: str
    initial_capital: float
    final_equity: float
    
    # 수익률
    total_return: float  # 총 수익금
    total_return_pct: float  # 총 수익률 (%)
    realized_pnl: float  # 실현 손익
    unrealized_pnl: float  # 미실현 손익
    
    # 거래 통계
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float  # 승률 (%)
    
    # 손익 분석
    avg_profit: float  # 평균 수익
    avg_loss: float  # 평균 손실
    max_profit: float  # 최대 수익
    max_loss: float  # 최대 손실
    profit_factor: float  # 총 수익 / 총 손실
    
    # 리스크 지표
    expectancy: float  # 기대값 (한 거래당 예상 손익)
    expectancy_pct: float  # 기대값 (%)
    max_drawdown: float  # 최대 낙폭 (금액)
    max_drawdown_pct: float  # 최대 낙폭 (%)
    
    # 효율성 지표
    avg_holding_days: float  # 평균 보유일수
    trades_per_month: float  # 월평균 거래 횟수
    
    # 기타
    total_commission: float  # 총 수수료
    net_profit_after_commission: float  # 수수료 제외 순이익
    
    def to_dict(self) -> Dict:
        """딕셔너리로 변환"""
        return asdict(self)
    
    def get_summary_text(self) -> str:
        """텍스트 요약 반환"""
        return f"""
═══════════════════════════════════════════════════
📊 CBT 성과 리포트
═══════════════════════════════════════════════════
📅 기준일: {self.report_date}

💰 자본금 현황
━━━━━━━━━━━━━━━━━━━━━━━━━━━
• 초기 자본금: {self.initial_capital:,.0f}원
• 현재 평가금: {self.final_equity:,.0f}원
• 총 수익금: {self.total_return:+,.0f}원 ({self.total_return_pct:+.2f}%)
• 실현 손익: {self.realized_pnl:+,.0f}원
• 미실현 손익: {self.unrealized_pnl:+,.0f}원

📈 거래 성과
━━━━━━━━━━━━━━━━━━━━━━━━━━━
• 총 거래 횟수: {self.total_trades}회
• 승리/패배: {self.winning_trades}승 / {self.losing_trades}패
• 승률: {self.win_rate:.1f}%

💵 손익 분석
━━━━━━━━━━━━━━━━━━━━━━━━━━━
• 평균 수익: {self.avg_profit:+,.0f}원
• 평균 손실: {self.avg_loss:,.0f}원
• 최대 수익: {self.max_profit:+,.0f}원
• 최대 손실: {self.max_loss:,.0f}원
• Profit Factor: {self.profit_factor:.2f}

📉 리스크 지표
━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Expectancy: {self.expectancy:+,.0f}원 ({self.expectancy_pct:+.2f}%)
• Maximum Drawdown: {self.max_drawdown:,.0f}원 ({self.max_drawdown_pct:.2f}%)

⏱️ 효율성
━━━━━━━━━━━━━━━━━━━━━━━━━━━
• 평균 보유일수: {self.avg_holding_days:.1f}일
• 월평균 거래: {self.trades_per_month:.1f}회

💸 수수료
━━━━━━━━━━━━━━━━━━━━━━━━━━━
• 총 수수료: {self.total_commission:,.0f}원
• 순이익(수수료후): {self.net_profit_after_commission:+,.0f}원
═══════════════════════════════════════════════════
"""


class CBTMetrics:
    """
    CBT 성과 지표 계산 클래스
    
    VirtualAccount와 TradeStore의 데이터를 기반으로
    다양한 성과 지표를 계산합니다.
    
    Usage:
        metrics = CBTMetrics(account, trade_store)
        
        # 전체 성과 리포트 생성
        report = metrics.generate_report()
        
        # 개별 지표 계산
        mdd = metrics.calculate_max_drawdown()
        expectancy = metrics.calculate_expectancy()
    """
    
    def __init__(
        self,
        account: VirtualAccount,
        trade_store: TradeStore
    ):
        """
        성과 지표 계산기 초기화
        
        Args:
            account: VirtualAccount 인스턴스
            trade_store: TradeStore 인스턴스
        """
        self.account = account
        self.trade_store = trade_store
    
    # ════════════════════════════════════════════════════════════════
    # 수익률 계산
    # ════════════════════════════════════════════════════════════════
    
    def calculate_total_return(self, current_price: float = None) -> tuple:
        """
        총 수익률 계산
        
        Args:
            current_price: 현재가 (포지션 평가용)
        
        Returns:
            tuple: (수익금액, 수익률%)
        """
        initial = self.account.initial_capital
        final = self.account.get_total_equity(current_price)
        
        return_amount = final - initial
        return_pct = (return_amount / initial) * 100
        
        return return_amount, return_pct
    
    # ════════════════════════════════════════════════════════════════
    # 승률 및 손익 분석
    # ════════════════════════════════════════════════════════════════
    
    def calculate_win_rate(self) -> float:
        """
        승률 계산
        
        Returns:
            float: 승률 (%)
        """
        trades = self.trade_store.get_all_trades()
        if not trades:
            return 0.0
        
        winners = sum(1 for t in trades if t.is_winner())
        return (winners / len(trades)) * 100
    
    def calculate_avg_profit_loss(self) -> tuple:
        """
        평균 수익 / 평균 손실 계산
        
        Returns:
            tuple: (평균 수익, 평균 손실)
        """
        trades = self.trade_store.get_all_trades()
        
        winners = [t.pnl for t in trades if t.is_winner()]
        losers = [t.pnl for t in trades if t.is_loser()]
        
        avg_profit = sum(winners) / len(winners) if winners else 0
        avg_loss = sum(losers) / len(losers) if losers else 0
        
        return avg_profit, avg_loss
    
    def calculate_profit_factor(self) -> float:
        """
        Profit Factor 계산 (총 수익 / 총 손실)
        
        Returns:
            float: Profit Factor (1 이상이면 수익)
        """
        trades = self.trade_store.get_all_trades()
        
        total_profit = sum(t.pnl for t in trades if t.is_winner())
        total_loss = abs(sum(t.pnl for t in trades if t.is_loser()))
        
        if total_loss == 0:
            return float('inf') if total_profit > 0 else 0.0
        
        return total_profit / total_loss
    
    # ════════════════════════════════════════════════════════════════
    # Expectancy (기대값)
    # ════════════════════════════════════════════════════════════════
    
    def calculate_expectancy(self) -> tuple:
        """
        Expectancy (기대값) 계산
        
        Expectancy = (승률 × 평균수익) - (패배율 × 평균손실)
        
        양수면 장기적으로 수익이 기대되는 전략입니다.
        
        Returns:
            tuple: (기대값 금액, 기대값 %)
        """
        trades = self.trade_store.get_all_trades()
        
        if not trades:
            return 0.0, 0.0
        
        winners = [t for t in trades if t.is_winner()]
        losers = [t for t in trades if t.is_loser()]
        
        win_rate = len(winners) / len(trades)
        loss_rate = 1 - win_rate
        
        avg_profit = sum(t.pnl for t in winners) / len(winners) if winners else 0
        avg_loss = abs(sum(t.pnl for t in losers) / len(losers)) if losers else 0
        
        expectancy = (win_rate * avg_profit) - (loss_rate * avg_loss)
        
        # 평균 진입금액 대비 기대값 %
        avg_entry_value = sum(t.entry_price * t.quantity for t in trades) / len(trades)
        expectancy_pct = (expectancy / avg_entry_value) * 100 if avg_entry_value > 0 else 0
        
        return expectancy, expectancy_pct
    
    # ════════════════════════════════════════════════════════════════
    # Maximum Drawdown (최대 낙폭)
    # ════════════════════════════════════════════════════════════════
    
    def calculate_max_drawdown(self) -> tuple:
        """
        Maximum Drawdown 계산
        
        Equity Curve에서 고점 대비 최대 하락폭을 계산합니다.
        
        Returns:
            tuple: (최대 낙폭 금액, 최대 낙폭 %)
        """
        equity_curve = self.account.get_equity_curve()
        
        if len(equity_curve) < 2:
            return 0.0, 0.0
        
        peak = equity_curve[0]["total_equity"]
        max_dd = 0.0
        max_dd_pct = 0.0
        
        for snapshot in equity_curve:
            equity = snapshot["total_equity"]
            
            if equity > peak:
                peak = equity
            
            dd = peak - equity
            dd_pct = (dd / peak) * 100 if peak > 0 else 0
            
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct
        
        return max_dd, max_dd_pct
    
    def calculate_max_drawdown_from_trades(self) -> tuple:
        """
        거래 기록 기반 Maximum Drawdown 계산
        
        Equity Curve가 없을 때 사용합니다.
        
        Returns:
            tuple: (최대 낙폭 금액, 최대 낙폭 %)
        """
        trades = self.trade_store.get_all_trades()
        
        if not trades:
            return 0.0, 0.0
        
        # 거래를 시간순으로 정렬
        sorted_trades = sorted(trades, key=lambda t: t.exit_date)
        
        initial = self.account.initial_capital
        equity = initial
        peak = initial
        max_dd = 0.0
        max_dd_pct = 0.0
        
        for trade in sorted_trades:
            equity += trade.pnl
            
            if equity > peak:
                peak = equity
            
            dd = peak - equity
            dd_pct = (dd / peak) * 100 if peak > 0 else 0
            
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct
        
        return max_dd, max_dd_pct
    
    # ════════════════════════════════════════════════════════════════
    # 기타 지표
    # ════════════════════════════════════════════════════════════════
    
    def calculate_avg_holding_days(self) -> float:
        """평균 보유일수 계산"""
        trades = self.trade_store.get_all_trades()
        
        if not trades:
            return 0.0
        
        total_days = sum(t.holding_days for t in trades)
        return total_days / len(trades)
    
    def calculate_trades_per_month(self) -> float:
        """월평균 거래 횟수 계산"""
        trades = self.trade_store.get_all_trades()
        
        if not trades:
            return 0.0
        
        # 첫 거래와 마지막 거래 사이의 월수 계산
        sorted_trades = sorted(trades, key=lambda t: t.exit_date)
        
        first_date = datetime.strptime(sorted_trades[0].exit_date.split()[0], "%Y-%m-%d")
        last_date = datetime.strptime(sorted_trades[-1].exit_date.split()[0], "%Y-%m-%d")
        
        months = ((last_date.year - first_date.year) * 12 + 
                  (last_date.month - first_date.month) + 1)
        
        return len(trades) / max(months, 1)
    
    def calculate_total_commission(self) -> float:
        """총 수수료 계산"""
        trades = self.trade_store.get_all_trades()
        return sum(t.commission for t in trades)
    
    # ════════════════════════════════════════════════════════════════
    # 리포트 생성
    # ════════════════════════════════════════════════════════════════
    
    def generate_report(self, current_price: float = None) -> PerformanceReport:
        """
        전체 성과 리포트 생성
        
        Args:
            current_price: 현재가 (포지션 평가용)
        
        Returns:
            PerformanceReport: 성과 리포트
        """
        trades = self.trade_store.get_all_trades()
        
        # 기본 정보
        initial = self.account.initial_capital
        final = self.account.get_total_equity(current_price)
        
        # 수익률
        total_return, total_return_pct = self.calculate_total_return(current_price)
        
        # 손익 분석
        avg_profit, avg_loss = self.calculate_avg_profit_loss()
        
        winners = [t for t in trades if t.is_winner()]
        losers = [t for t in trades if t.is_loser()]
        
        max_profit = max(t.pnl for t in winners) if winners else 0
        max_loss = min(t.pnl for t in losers) if losers else 0
        
        # 리스크 지표
        expectancy, expectancy_pct = self.calculate_expectancy()
        
        # MDD 계산 (Equity Curve 우선, 없으면 거래 기록 사용)
        if self.account.equity_curve:
            max_dd, max_dd_pct = self.calculate_max_drawdown()
        else:
            max_dd, max_dd_pct = self.calculate_max_drawdown_from_trades()
        
        # 수수료
        total_commission = self.calculate_total_commission()
        
        report = PerformanceReport(
            report_date=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
            initial_capital=initial,
            final_equity=final,
            
            total_return=total_return,
            total_return_pct=total_return_pct,
            realized_pnl=self.account.realized_pnl,
            unrealized_pnl=self.account.unrealized_pnl,
            
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=self.calculate_win_rate(),
            
            avg_profit=avg_profit,
            avg_loss=avg_loss,
            max_profit=max_profit,
            max_loss=max_loss,
            profit_factor=self.calculate_profit_factor(),
            
            expectancy=expectancy,
            expectancy_pct=expectancy_pct,
            max_drawdown=max_dd,
            max_drawdown_pct=max_dd_pct,
            
            avg_holding_days=self.calculate_avg_holding_days(),
            trades_per_month=self.calculate_trades_per_month(),
            
            total_commission=total_commission,
            net_profit_after_commission=total_return - total_commission
        )
        
        logger.info(
            f"[CBT] 성과 리포트 생성: "
            f"수익률={report.total_return_pct:+.2f}%, "
            f"승률={report.win_rate:.1f}%, "
            f"MDD={report.max_drawdown_pct:.2f}%"
        )
        
        return report
    
    def generate_trade_summary(self, trade: Trade) -> Dict:
        """
        개별 거래 요약 생성
        
        Args:
            trade: Trade 객체
        
        Returns:
            Dict: 거래 요약
        """
        return {
            "trade_id": trade.trade_id,
            "stock_code": trade.stock_code,
            "entry_date": trade.entry_date,
            "exit_date": trade.exit_date,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "quantity": trade.quantity,
            "pnl": trade.pnl,
            "return_pct": trade.return_pct,
            "holding_days": trade.holding_days,
            "exit_reason": trade.exit_reason,
            "is_winner": trade.is_winner()
        }
    
    def generate_daily_report(self, date: str = None) -> Dict:
        """
        일일 성과 리포트 생성
        
        Args:
            date: 날짜 (YYYY-MM-DD, 미입력 시 오늘)
        
        Returns:
            Dict: 일일 리포트
        """
        if date is None:
            date = datetime.now(KST).strftime("%Y-%m-%d")
        
        trades = self.trade_store.get_trades_by_date(date, date)
        
        if not trades:
            return {
                "date": date,
                "trades": 0,
                "pnl": 0,
                "win_rate": 0,
                "trade_list": []
            }
        
        winners = [t for t in trades if t.is_winner()]
        total_pnl = sum(t.pnl for t in trades)
        
        return {
            "date": date,
            "trades": len(trades),
            "winning_trades": len(winners),
            "pnl": total_pnl,
            "win_rate": len(winners) / len(trades) * 100,
            "avg_return_pct": sum(t.return_pct for t in trades) / len(trades),
            "trade_list": [self.generate_trade_summary(t) for t in trades]
        }


# ════════════════════════════════════════════════════════════════
# 헬퍼 함수
# ════════════════════════════════════════════════════════════════

def format_currency(amount: float) -> str:
    """금액 포맷팅 (한국원)"""
    if amount >= 0:
        return f"{amount:,.0f}원"
    else:
        return f"-{abs(amount):,.0f}원"


def format_percentage(pct: float, decimals: int = 2) -> str:
    """퍼센트 포맷팅"""
    return f"{pct:+.{decimals}f}%"


def get_performance_emoji(return_pct: float) -> str:
    """수익률에 따른 이모지 반환"""
    if return_pct >= 10:
        return "🚀"
    elif return_pct >= 5:
        return "📈"
    elif return_pct >= 0:
        return "✅"
    elif return_pct >= -5:
        return "⚠️"
    else:
        return "🔻"
