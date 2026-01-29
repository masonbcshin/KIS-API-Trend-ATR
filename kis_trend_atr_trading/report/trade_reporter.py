"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 트레이딩 성과 측정 시스템
═══════════════════════════════════════════════════════════════════════════════

전체 트레이딩 성과를 실시간 및 누적으로 측정합니다.

★ 종목별 지표:
    - 평균 매수가
    - 현재 수익률
    - 실현 손익
    - 미실현 손익

★ 계좌 전체 지표:
    - 총 투자금
    - 총 평가금액
    - 총 수익률
    - 누적 거래 횟수
    - 승률
    - 최대 낙폭(MDD)
    - Profit Factor
    - Sharpe Ratio (근사)
    - 평균 보유 기간
"""

import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
import threading

from utils.logger import get_logger

logger = get_logger("trade_reporter")


# ═══════════════════════════════════════════════════════════════════════════════
# 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """개별 거래 기록"""
    trade_id: str
    stock_code: str
    stock_name: str = ""
    side: str = "BUY"                  # BUY / SELL
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: int = 0
    entry_date: str = ""
    exit_date: str = ""
    holding_days: int = 0
    pnl: float = 0.0                   # 손익 금액
    pnl_pct: float = 0.0               # 손익률 (%)
    exit_reason: str = ""              # ATR_STOP, TAKE_PROFIT, TREND_BROKEN 등
    commission: float = 0.0            # 수수료
    is_closed: bool = False


@dataclass
class StockPerformance:
    """종목별 성과"""
    stock_code: str
    stock_name: str = ""
    
    # 현재 포지션
    current_quantity: int = 0
    avg_entry_price: float = 0.0
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    
    # 실현 손익
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    realized_pnl: float = 0.0
    realized_pnl_pct: float = 0.0
    
    # 통계
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    max_pnl: float = 0.0
    min_pnl: float = 0.0
    avg_holding_days: float = 0.0


@dataclass
class AccountPerformance:
    """계좌 전체 성과"""
    # 기본 정보
    report_date: str = ""
    initial_capital: float = 0.0
    current_equity: float = 0.0
    cash_balance: float = 0.0
    
    # 손익
    total_pnl: float = 0.0             # 총 손익 (실현 + 미실현)
    realized_pnl: float = 0.0          # 실현 손익
    unrealized_pnl: float = 0.0        # 미실현 손익
    total_return_pct: float = 0.0      # 총 수익률 (%)
    
    # 거래 통계
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    
    # 리스크 지표
    max_drawdown: float = 0.0          # MDD 금액
    max_drawdown_pct: float = 0.0      # MDD 비율 (%)
    profit_factor: float = 0.0         # 총익 / 총손
    avg_win: float = 0.0               # 평균 수익
    avg_loss: float = 0.0              # 평균 손실
    expectancy: float = 0.0            # 기대값
    
    # 보유 현황
    total_positions: int = 0
    avg_holding_days: float = 0.0
    
    # 최고/최저 기록
    peak_equity: float = 0.0
    valley_equity: float = 0.0


@dataclass
class EquityPoint:
    """자산 추이 데이터 포인트"""
    timestamp: str
    equity: float
    cash: float
    position_value: float
    realized_pnl: float
    unrealized_pnl: float
    drawdown_pct: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 트레이드 리포터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class TradeReporter:
    """
    트레이딩 성과 측정 시스템
    
    실시간 및 누적 성과를 측정하고 리포트를 생성합니다.
    
    Usage:
        reporter = TradeReporter(initial_capital=10_000_000)
        
        # 거래 기록 추가
        reporter.record_trade(trade_record)
        
        # 실시간 업데이트
        reporter.update_unrealized_pnl(stock_code, current_price, quantity)
        
        # 성과 조회
        perf = reporter.get_account_performance()
        print(f"총 수익률: {perf.total_return_pct:.2f}%")
        
        # 텔레그램 리포트 전송
        reporter.send_telegram_report()
    """
    
    def __init__(
        self,
        initial_capital: float = 10_000_000,
        data_dir: Path = None,
        load_existing: bool = True
    ):
        """
        트레이드 리포터 초기화
        
        Args:
            initial_capital: 초기 자본금
            data_dir: 데이터 저장 경로
            load_existing: 기존 데이터 로드 여부
        """
        self.initial_capital = initial_capital
        self.data_dir = data_dir or Path(__file__).parent.parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self._lock = threading.Lock()
        
        # 파일 경로
        self._trades_file = self.data_dir / "trade_history.json"
        self._equity_file = self.data_dir / "equity_curve.json"
        
        # 거래 기록
        self._trades: List[TradeRecord] = []
        
        # 종목별 성과
        self._stock_performances: Dict[str, StockPerformance] = {}
        
        # 자산 추이
        self._equity_curve: List[EquityPoint] = []
        
        # 현재 상태
        self._cash_balance = initial_capital
        self._peak_equity = initial_capital
        self._valley_equity = initial_capital
        
        # 기존 데이터 로드
        if load_existing:
            self._load_data()
        
        logger.info(
            f"[REPORTER] 초기화 완료: "
            f"초기자본={initial_capital:,}원, "
            f"거래기록={len(self._trades)}건"
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 거래 기록
    # ═══════════════════════════════════════════════════════════════════════════
    
    def record_trade(self, trade: TradeRecord) -> None:
        """
        거래를 기록합니다.
        
        Args:
            trade: 거래 기록
        """
        with self._lock:
            self._trades.append(trade)
            
            # 종목별 성과 업데이트
            self._update_stock_performance(trade)
            
            # 현금 잔고 업데이트
            if trade.is_closed:
                self._cash_balance += trade.pnl - trade.commission
            
            # 자산 추이 기록
            self._record_equity_point()
            
            # 저장
            self._save_data()
            
            logger.info(
                f"[REPORTER] 거래 기록: {trade.stock_code} "
                f"{'청산' if trade.is_closed else '진입'}, "
                f"손익={trade.pnl:+,.0f}원"
            )
    
    def record_entry(
        self,
        trade_id: str,
        stock_code: str,
        entry_price: float,
        quantity: int,
        stock_name: str = "",
        commission: float = 0.0
    ) -> TradeRecord:
        """
        진입 기록을 생성합니다.
        
        Args:
            trade_id: 거래 ID
            stock_code: 종목 코드
            entry_price: 진입가
            quantity: 수량
            stock_name: 종목명
            commission: 수수료
            
        Returns:
            TradeRecord: 생성된 거래 기록
        """
        trade = TradeRecord(
            trade_id=trade_id,
            stock_code=stock_code,
            stock_name=stock_name,
            side="BUY",
            entry_price=entry_price,
            quantity=quantity,
            entry_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            commission=commission,
            is_closed=False
        )
        
        self.record_trade(trade)
        return trade
    
    def record_exit(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str = "",
        commission: float = 0.0
    ) -> Optional[TradeRecord]:
        """
        청산 기록을 업데이트합니다.
        
        Args:
            trade_id: 거래 ID
            exit_price: 청산가
            exit_reason: 청산 사유
            commission: 수수료
            
        Returns:
            Optional[TradeRecord]: 업데이트된 거래 기록
        """
        with self._lock:
            # 해당 거래 찾기
            for trade in self._trades:
                if trade.trade_id == trade_id and not trade.is_closed:
                    trade.exit_price = exit_price
                    trade.exit_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    trade.exit_reason = exit_reason
                    trade.commission += commission
                    trade.is_closed = True
                    
                    # 보유일수 계산
                    entry_dt = datetime.strptime(
                        trade.entry_date.split()[0], "%Y-%m-%d"
                    )
                    exit_dt = datetime.now()
                    trade.holding_days = (exit_dt - entry_dt).days + 1
                    
                    # 손익 계산
                    gross_pnl = (exit_price - trade.entry_price) * trade.quantity
                    trade.pnl = gross_pnl - trade.commission
                    trade.pnl_pct = (trade.pnl / (trade.entry_price * trade.quantity)) * 100
                    
                    # 종목별 성과 업데이트
                    self._update_stock_performance(trade)
                    
                    # 현금 잔고 업데이트
                    self._cash_balance += trade.pnl
                    
                    # 자산 추이 기록
                    self._record_equity_point()
                    
                    # 저장
                    self._save_data()
                    
                    logger.info(
                        f"[REPORTER] 청산 기록: {trade.stock_code}, "
                        f"손익={trade.pnl:+,.0f}원 ({trade.pnl_pct:+.2f}%)"
                    )
                    
                    return trade
            
            logger.warning(f"[REPORTER] 거래 ID 없음: {trade_id}")
            return None
    
    def _update_stock_performance(self, trade: TradeRecord) -> None:
        """종목별 성과를 업데이트합니다."""
        code = trade.stock_code
        
        if code not in self._stock_performances:
            self._stock_performances[code] = StockPerformance(
                stock_code=code,
                stock_name=trade.stock_name
            )
        
        perf = self._stock_performances[code]
        
        if trade.is_closed:
            # 청산 완료
            perf.total_trades += 1
            perf.realized_pnl += trade.pnl
            
            if trade.pnl > 0:
                perf.winning_trades += 1
            else:
                perf.losing_trades += 1
            
            # 승률 업데이트
            if perf.total_trades > 0:
                perf.win_rate = (perf.winning_trades / perf.total_trades) * 100
            
            # 최대/최소 손익
            perf.max_pnl = max(perf.max_pnl, trade.pnl)
            perf.min_pnl = min(perf.min_pnl, trade.pnl)
            
            # 평균 손익
            closed_trades = [t for t in self._trades if t.stock_code == code and t.is_closed]
            if closed_trades:
                perf.avg_pnl = sum(t.pnl for t in closed_trades) / len(closed_trades)
                perf.avg_holding_days = sum(t.holding_days for t in closed_trades) / len(closed_trades)
            
            # 포지션 청산
            perf.current_quantity = 0
            perf.avg_entry_price = 0.0
        else:
            # 진입
            perf.current_quantity = trade.quantity
            perf.avg_entry_price = trade.entry_price
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 실시간 업데이트
    # ═══════════════════════════════════════════════════════════════════════════
    
    def update_unrealized_pnl(
        self,
        stock_code: str,
        current_price: float,
        quantity: int = None
    ) -> None:
        """
        미실현 손익을 업데이트합니다.
        
        Args:
            stock_code: 종목 코드
            current_price: 현재가
            quantity: 보유 수량 (없으면 기존 값 사용)
        """
        with self._lock:
            if stock_code in self._stock_performances:
                perf = self._stock_performances[stock_code]
                
                if quantity is not None:
                    perf.current_quantity = quantity
                
                perf.current_price = current_price
                
                if perf.current_quantity > 0 and perf.avg_entry_price > 0:
                    perf.unrealized_pnl = (
                        (current_price - perf.avg_entry_price) * perf.current_quantity
                    )
                    perf.unrealized_pnl_pct = (
                        (current_price - perf.avg_entry_price) / perf.avg_entry_price * 100
                    )
    
    def update_cash_balance(self, cash: float) -> None:
        """현금 잔고를 업데이트합니다."""
        with self._lock:
            self._cash_balance = cash
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 성과 조회
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_account_performance(self) -> AccountPerformance:
        """
        계좌 전체 성과를 반환합니다.
        
        Returns:
            AccountPerformance: 계좌 성과
        """
        with self._lock:
            perf = AccountPerformance(
                report_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                initial_capital=self.initial_capital
            )
            
            # 실현 손익
            closed_trades = [t for t in self._trades if t.is_closed]
            perf.realized_pnl = sum(t.pnl for t in closed_trades)
            
            # 미실현 손익
            perf.unrealized_pnl = sum(
                sp.unrealized_pnl 
                for sp in self._stock_performances.values()
            )
            
            # 총 손익 및 현재 자산
            perf.total_pnl = perf.realized_pnl + perf.unrealized_pnl
            perf.cash_balance = self._cash_balance
            
            # 포지션 가치
            position_value = sum(
                sp.current_price * sp.current_quantity
                for sp in self._stock_performances.values()
                if sp.current_quantity > 0
            )
            
            perf.current_equity = self._cash_balance + position_value
            perf.total_return_pct = (
                (perf.current_equity - self.initial_capital) / self.initial_capital * 100
            )
            
            # 거래 통계
            perf.total_trades = len(closed_trades)
            perf.winning_trades = sum(1 for t in closed_trades if t.pnl > 0)
            perf.losing_trades = sum(1 for t in closed_trades if t.pnl <= 0)
            
            if perf.total_trades > 0:
                perf.win_rate = (perf.winning_trades / perf.total_trades) * 100
            
            # MDD 계산
            perf.peak_equity = self._peak_equity
            perf.valley_equity = self._valley_equity
            
            # Peak 업데이트
            if perf.current_equity > self._peak_equity:
                self._peak_equity = perf.current_equity
                perf.peak_equity = perf.current_equity
            
            # Drawdown 계산
            if self._peak_equity > 0:
                perf.max_drawdown = self._peak_equity - self._valley_equity
                perf.max_drawdown_pct = (perf.max_drawdown / self._peak_equity) * 100
            
            # Valley 업데이트
            if perf.current_equity < self._valley_equity:
                self._valley_equity = perf.current_equity
                perf.valley_equity = perf.current_equity
            
            # Profit Factor
            total_profit = sum(t.pnl for t in closed_trades if t.pnl > 0)
            total_loss = abs(sum(t.pnl for t in closed_trades if t.pnl < 0))
            
            if total_loss > 0:
                perf.profit_factor = total_profit / total_loss
            elif total_profit > 0:
                perf.profit_factor = float('inf')
            
            # 평균 수익/손실
            wins = [t.pnl for t in closed_trades if t.pnl > 0]
            losses = [t.pnl for t in closed_trades if t.pnl < 0]
            
            perf.avg_win = sum(wins) / len(wins) if wins else 0.0
            perf.avg_loss = sum(losses) / len(losses) if losses else 0.0
            
            # Expectancy (기대값)
            if perf.total_trades > 0:
                win_prob = perf.winning_trades / perf.total_trades
                loss_prob = perf.losing_trades / perf.total_trades
                perf.expectancy = (win_prob * perf.avg_win) + (loss_prob * perf.avg_loss)
            
            # 보유 현황
            perf.total_positions = sum(
                1 for sp in self._stock_performances.values()
                if sp.current_quantity > 0
            )
            
            # 평균 보유일수
            if closed_trades:
                perf.avg_holding_days = sum(
                    t.holding_days for t in closed_trades
                ) / len(closed_trades)
            
            return perf
    
    def get_stock_performance(self, stock_code: str) -> Optional[StockPerformance]:
        """
        종목별 성과를 반환합니다.
        
        Args:
            stock_code: 종목 코드
            
        Returns:
            Optional[StockPerformance]: 종목 성과
        """
        return self._stock_performances.get(stock_code)
    
    def get_all_stock_performances(self) -> Dict[str, StockPerformance]:
        """모든 종목의 성과를 반환합니다."""
        return self._stock_performances.copy()
    
    def get_trade_history(
        self,
        stock_code: str = None,
        start_date: str = None,
        end_date: str = None,
        closed_only: bool = True
    ) -> List[TradeRecord]:
        """
        거래 내역을 반환합니다.
        
        Args:
            stock_code: 종목 코드 (None이면 전체)
            start_date: 시작일 (YYYY-MM-DD)
            end_date: 종료일 (YYYY-MM-DD)
            closed_only: 청산 완료 건만
            
        Returns:
            List[TradeRecord]: 거래 내역
        """
        trades = self._trades.copy()
        
        # 필터링
        if stock_code:
            trades = [t for t in trades if t.stock_code == stock_code]
        
        if closed_only:
            trades = [t for t in trades if t.is_closed]
        
        if start_date:
            trades = [
                t for t in trades 
                if t.entry_date >= start_date
            ]
        
        if end_date:
            trades = [
                t for t in trades 
                if t.entry_date <= end_date
            ]
        
        return trades
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 자산 추이
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _record_equity_point(self) -> None:
        """자산 추이 포인트를 기록합니다."""
        # 포지션 가치
        position_value = sum(
            sp.current_price * sp.current_quantity
            for sp in self._stock_performances.values()
            if sp.current_quantity > 0
        )
        
        equity = self._cash_balance + position_value
        
        # Drawdown 계산
        drawdown_pct = 0.0
        if self._peak_equity > 0:
            drawdown_pct = ((self._peak_equity - equity) / self._peak_equity) * 100
        
        point = EquityPoint(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            equity=equity,
            cash=self._cash_balance,
            position_value=position_value,
            realized_pnl=sum(t.pnl for t in self._trades if t.is_closed),
            unrealized_pnl=sum(
                sp.unrealized_pnl 
                for sp in self._stock_performances.values()
            ),
            drawdown_pct=drawdown_pct
        )
        
        self._equity_curve.append(point)
        
        # Peak/Valley 업데이트
        if equity > self._peak_equity:
            self._peak_equity = equity
        if equity < self._valley_equity:
            self._valley_equity = equity
    
    def get_equity_curve(self) -> List[EquityPoint]:
        """자산 추이를 반환합니다."""
        return self._equity_curve.copy()
    
    def calculate_mdd(self) -> Tuple[float, float]:
        """
        최대 낙폭(MDD)을 계산합니다.
        
        Returns:
            Tuple[float, float]: (MDD 금액, MDD 비율)
        """
        if not self._equity_curve:
            return 0.0, 0.0
        
        peak = self._equity_curve[0].equity
        max_dd = 0.0
        max_dd_pct = 0.0
        
        for point in self._equity_curve:
            if point.equity > peak:
                peak = point.equity
            
            dd = peak - point.equity
            dd_pct = (dd / peak) * 100 if peak > 0 else 0
            
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct
        
        return max_dd, max_dd_pct
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 리포트 생성
    # ═══════════════════════════════════════════════════════════════════════════
    
    def generate_summary_text(self) -> str:
        """
        텍스트 형태의 요약 리포트를 생성합니다.
        
        Returns:
            str: 요약 리포트
        """
        perf = self.get_account_performance()
        mdd_amount, mdd_pct = self.calculate_mdd()
        
        text = f"""
════════════════════════════════════════════════════
             트레이딩 성과 리포트
════════════════════════════════════════════════════
📅 기준일시: {perf.report_date}

💰 자본금 현황
────────────────────────────────────────────────────
• 초기 자본금: {perf.initial_capital:,}원
• 현재 평가금: {perf.current_equity:,.0f}원
• 현금 잔고:   {perf.cash_balance:,.0f}원
• 총 수익률:   {perf.total_return_pct:+.2f}%

📈 손익 현황
────────────────────────────────────────────────────
• 실현 손익:   {perf.realized_pnl:+,.0f}원
• 미실현 손익: {perf.unrealized_pnl:+,.0f}원
• 총 손익:     {perf.total_pnl:+,.0f}원

📊 거래 통계
────────────────────────────────────────────────────
• 총 거래:     {perf.total_trades}회
• 승/패:       {perf.winning_trades}승 / {perf.losing_trades}패
• 승률:        {perf.win_rate:.1f}%
• 평균 수익:   {perf.avg_win:+,.0f}원
• 평균 손실:   {perf.avg_loss:,.0f}원
• Expectancy:  {perf.expectancy:+,.0f}원

📉 리스크 지표
────────────────────────────────────────────────────
• Max Drawdown: {mdd_amount:,.0f}원 ({mdd_pct:.2f}%)
• Profit Factor: {perf.profit_factor:.2f}
• 평균 보유일수: {perf.avg_holding_days:.1f}일

════════════════════════════════════════════════════
"""
        return text
    
    def get_telegram_report(self) -> str:
        """
        텔레그램용 리포트를 생성합니다.
        
        Returns:
            str: 텔레그램 형식 리포트
        """
        perf = self.get_account_performance()
        mdd_amount, mdd_pct = self.calculate_mdd()
        
        # 이모지 선택
        pnl_emoji = "📈" if perf.total_pnl >= 0 else "📉"
        wr_emoji = "🎯" if perf.win_rate >= 50 else "⚠️"
        
        return f"""
{pnl_emoji} *트레이딩 성과 리포트*
━━━━━━━━━━━━━━━━━━

💰 자본금 현황
• 초기: {perf.initial_capital:,}원
• 현재: {perf.current_equity:,.0f}원
• 수익률: {perf.total_return_pct:+.2f}%

📊 손익 현황
• 실현: {perf.realized_pnl:+,.0f}원
• 미실현: {perf.unrealized_pnl:+,.0f}원
• 총손익: {perf.total_pnl:+,.0f}원

{wr_emoji} 거래 통계
• 총 {perf.total_trades}회 ({perf.winning_trades}승/{perf.losing_trades}패)
• 승률: {perf.win_rate:.1f}%
• Expectancy: {perf.expectancy:+,.0f}원

📉 리스크
• MDD: {mdd_pct:.2f}%
• P.Factor: {perf.profit_factor:.2f}

━━━━━━━━━━━━━━━━━━
⏰ {perf.report_date}
"""
    
    def print_report(self) -> None:
        """리포트를 콘솔에 출력합니다."""
        print(self.generate_summary_text())
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 데이터 저장/로드
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _save_data(self) -> None:
        """데이터를 파일에 저장합니다."""
        try:
            # 거래 기록 저장
            trades_data = {
                "trades": [asdict(t) for t in self._trades],
                "stock_performances": {
                    k: asdict(v) for k, v in self._stock_performances.items()
                },
                "cash_balance": self._cash_balance,
                "peak_equity": self._peak_equity,
                "valley_equity": self._valley_equity,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            with open(self._trades_file, 'w', encoding='utf-8') as f:
                json.dump(trades_data, f, ensure_ascii=False, indent=2)
            
            # 자산 추이 저장 (최근 1000개)
            equity_data = {
                "curve": [asdict(p) for p in self._equity_curve[-1000:]],
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            with open(self._equity_file, 'w', encoding='utf-8') as f:
                json.dump(equity_data, f, ensure_ascii=False, indent=2)
            
            logger.debug("[REPORTER] 데이터 저장 완료")
            
        except Exception as e:
            logger.error(f"[REPORTER] 데이터 저장 실패: {e}")
    
    def _load_data(self) -> None:
        """저장된 데이터를 로드합니다."""
        try:
            # 거래 기록 로드
            if self._trades_file.exists():
                with open(self._trades_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                self._trades = [
                    TradeRecord(**t) for t in data.get("trades", [])
                ]
                
                self._stock_performances = {
                    k: StockPerformance(**v)
                    for k, v in data.get("stock_performances", {}).items()
                }
                
                self._cash_balance = data.get("cash_balance", self.initial_capital)
                self._peak_equity = data.get("peak_equity", self.initial_capital)
                self._valley_equity = data.get("valley_equity", self.initial_capital)
            
            # 자산 추이 로드
            if self._equity_file.exists():
                with open(self._equity_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                self._equity_curve = [
                    EquityPoint(**p) for p in data.get("curve", [])
                ]
            
            logger.info(
                f"[REPORTER] 데이터 로드 완료: "
                f"거래={len(self._trades)}건, "
                f"추이={len(self._equity_curve)}포인트"
            )
            
        except Exception as e:
            logger.warning(f"[REPORTER] 데이터 로드 실패: {e}")
    
    def reset(self) -> None:
        """모든 데이터를 초기화합니다."""
        with self._lock:
            self._trades = []
            self._stock_performances = {}
            self._equity_curve = []
            self._cash_balance = self.initial_capital
            self._peak_equity = self.initial_capital
            self._valley_equity = self.initial_capital
            
            # 파일 삭제
            if self._trades_file.exists():
                self._trades_file.unlink()
            if self._equity_file.exists():
                self._equity_file.unlink()
            
            logger.info("[REPORTER] 데이터 초기화 완료")


# ═══════════════════════════════════════════════════════════════════════════════
# 편의 함수
# ═══════════════════════════════════════════════════════════════════════════════

_reporter_instance: Optional[TradeReporter] = None


def get_trade_reporter(initial_capital: float = None) -> TradeReporter:
    """
    싱글톤 TradeReporter를 반환합니다.
    
    Args:
        initial_capital: 초기 자본금 (최초 생성 시)
        
    Returns:
        TradeReporter: 트레이드 리포터
    """
    global _reporter_instance
    
    if _reporter_instance is None:
        _reporter_instance = TradeReporter(
            initial_capital=initial_capital or 10_000_000
        )
    
    return _reporter_instance
