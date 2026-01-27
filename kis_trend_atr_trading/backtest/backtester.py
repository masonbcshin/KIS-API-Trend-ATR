"""
KIS Trend-ATR Trading System - 백테스트 모듈

과거 데이터를 기반으로 Trend-ATR 전략의 성과를 검증합니다.
실제 주문 없이 전략 효과를 사전에 평가할 수 있습니다.

출력 지표:
    - 총 수익률
    - 승률
    - 최대 낙폭(MDD)
    - 평균 보유 기간
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
import pandas as pd
import numpy as np

from config import settings
from strategy.trend_atr import TrendATRStrategy, SignalType, TrendType
from utils.logger import get_logger

import warnings
warnings.filterwarnings('ignore', category=RuntimeWarning)

logger = get_logger("backtester")


@dataclass
class Trade:
    """
    백테스트 개별 거래 기록
    
    Attributes:
        entry_date: 진입일
        exit_date: 청산일
        entry_price: 진입가
        exit_price: 청산가
        quantity: 거래 수량
        pnl: 손익금액
        pnl_pct: 손익률 (%)
        holding_days: 보유 기간 (일)
        exit_reason: 청산 사유
    """
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_pct: float
    holding_days: int
    exit_reason: str


@dataclass
class BacktestResult:
    """
    백테스트 결과 데이터 클래스
    
    Attributes:
        start_date: 백테스트 시작일
        end_date: 백테스트 종료일
        initial_capital: 초기 자본금
        final_capital: 최종 자본금
        total_return: 총 수익률 (%)
        total_trades: 총 거래 횟수
        winning_trades: 승리 거래 횟수
        losing_trades: 패배 거래 횟수
        win_rate: 승률 (%)
        max_drawdown: 최대 낙폭 (%)
        avg_holding_days: 평균 보유 기간 (일)
        profit_factor: 수익 팩터
        trades: 개별 거래 기록 목록
        equity_curve: 자산 곡선
    """
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_return: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    max_drawdown: float
    avg_holding_days: float
    profit_factor: float
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)


class Backtester:
    """
    백테스트 실행 클래스
    
    과거 OHLCV 데이터를 기반으로 전략 성과를 검증합니다.
    
    Attributes:
        strategy: Trend-ATR 전략
        initial_capital: 초기 자본금
        commission_rate: 수수료율
    """
    
    def __init__(
        self,
        strategy: TrendATRStrategy = None,
        initial_capital: float = None,
        commission_rate: float = None
    ):
        """
        백테스터 초기화
        
        Args:
            strategy: 전략 인스턴스 (미입력 시 자동 생성)
            initial_capital: 초기 자본금 (기본: 설정 파일 값)
            commission_rate: 수수료율 (기본: 설정 파일 값)
        """
        self.strategy = strategy or TrendATRStrategy()
        self.initial_capital = initial_capital or settings.BACKTEST_INITIAL_CAPITAL
        self.commission_rate = commission_rate or settings.BACKTEST_COMMISSION_RATE
        
        logger.info(
            f"백테스터 초기화: 자본금={self.initial_capital:,.0f}원, "
            f"수수료율={self.commission_rate*100:.3f}%"
        )
    
    def _calculate_position_size(self, price: float, capital: float) -> int:
        """
        포지션 크기를 계산합니다.
        
        자본금의 100%를 사용하는 단순한 포지션 사이징입니다.
        
        Args:
            price: 현재가
            capital: 가용 자본금
        
        Returns:
            int: 매수 가능 수량
        """
        if price <= 0:
            return 0
        
        # 수수료 고려
        available = capital / (1 + self.commission_rate)
        quantity = int(available // price)
        
        return max(0, quantity)
    
    def _calculate_commission(self, price: float, quantity: int) -> float:
        """
        수수료를 계산합니다.
        
        Args:
            price: 거래 가격
            quantity: 거래 수량
        
        Returns:
            float: 수수료 금액
        """
        return price * quantity * self.commission_rate
    
    def run(
        self,
        df: pd.DataFrame,
        stock_code: str = ""
    ) -> BacktestResult:
        """
        백테스트를 실행합니다.
        
        Args:
            df: OHLCV 데이터프레임 (date, open, high, low, close, volume 컬럼 필요)
            stock_code: 종목 코드 (로깅용)
        
        Returns:
            BacktestResult: 백테스트 결과
        """
        if df.empty:
            logger.error("데이터가 없어 백테스트를 실행할 수 없습니다.")
            return self._create_empty_result()
        
        # 데이터 정렬 및 인덱스 리셋
        df = df.sort_values("date").reset_index(drop=True)
        
        logger.info(f"백테스트 시작: {stock_code}, {len(df)}개 캔들")
        logger.info(f"기간: {df.iloc[0]['date']} ~ {df.iloc[-1]['date']}")
        
        # 초기화
        capital = self.initial_capital
        position = None  # {"entry_price", "quantity", "stop_loss", "take_profit", "entry_date", "entry_idx"}
        trades: List[Trade] = []
        equity_curve = [capital]
        
        # 지표 계산
        df_with_indicators = self.strategy.add_indicators(df)
        
        # MA 계산에 필요한 최소 기간 이후부터 시작
        start_idx = self.strategy.ma_period
        
        for i in range(start_idx, len(df_with_indicators)):
            row = df_with_indicators.iloc[i]
            current_date = str(row["date"])[:10]
            current_close = row["close"]
            current_high = row["high"]
            current_low = row["low"]
            atr = row["atr"]
            ma = row["ma"]
            prev_high = row["prev_high"]
            
            # ATR이 없으면 스킵
            if pd.isna(atr) or atr <= 0:
                equity_curve.append(capital)
                continue
            
            # ════════════════════════════════════════════════════════
            # 포지션 보유 중인 경우: 청산 조건 확인
            # ════════════════════════════════════════════════════════
            if position is not None:
                exit_price = None
                exit_reason = ""
                
                # 손절 확인 (저가가 손절가 이하)
                if current_low <= position["stop_loss"]:
                    exit_price = position["stop_loss"]
                    exit_reason = "손절"
                
                # 익절 확인 (고가가 익절가 이상)
                elif current_high >= position["take_profit"]:
                    exit_price = position["take_profit"]
                    exit_reason = "익절"
                
                # 청산 실행
                if exit_price is not None:
                    # 수수료 계산
                    sell_commission = self._calculate_commission(exit_price, position["quantity"])
                    
                    # 손익 계산
                    gross_pnl = (exit_price - position["entry_price"]) * position["quantity"]
                    net_pnl = gross_pnl - position["entry_commission"] - sell_commission
                    pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
                    
                    # 자본금 업데이트
                    capital = capital + gross_pnl - sell_commission
                    
                    # 보유 기간 계산
                    holding_days = i - position["entry_idx"]
                    
                    # 거래 기록
                    trade = Trade(
                        entry_date=position["entry_date"],
                        exit_date=current_date,
                        entry_price=position["entry_price"],
                        exit_price=exit_price,
                        quantity=position["quantity"],
                        pnl=net_pnl,
                        pnl_pct=pnl_pct,
                        holding_days=holding_days,
                        exit_reason=exit_reason
                    )
                    trades.append(trade)
                    
                    logger.debug(
                        f"[청산] {current_date} | {exit_reason} | "
                        f"가격: {exit_price:,.0f}원 | 손익: {net_pnl:,.0f}원 ({pnl_pct:+.2f}%)"
                    )
                    
                    # 포지션 초기화
                    position = None
            
            # ════════════════════════════════════════════════════════
            # 포지션 미보유 시: 진입 조건 확인
            # ════════════════════════════════════════════════════════
            else:
                # ADX (추세 강도) 가져오기
                adx = row.get('adx', None)
                if adx is not None and pd.isna(adx):
                    adx = None
                
                # 진입 조건:
                # 1. 상승 추세 (종가 > MA)
                # 2. 직전 캔들 고가 돌파
                # 3. ADX > 임계값 (추세 강도 충분)
                # 4. ATR 급등 아님
                is_uptrend = current_close > ma
                is_breakout = not pd.isna(prev_high) and current_high > prev_high
                
                # ADX 필터: 추세 강도 확인 (횡보장 필터)
                has_trend_strength = True
                if adx is not None:
                    has_trend_strength = adx >= settings.ADX_THRESHOLD
                
                # ATR 급등 필터
                is_atr_normal = True
                min_periods = self.strategy.atr_period * 2
                if i >= min_periods:
                    recent_atr = df_with_indicators['atr'].iloc[i-min_periods:i]
                    avg_atr = recent_atr.mean()
                    if not pd.isna(avg_atr) and avg_atr > 0:
                        atr_ratio = atr / avg_atr
                        if atr_ratio > settings.ATR_SPIKE_THRESHOLD:
                            is_atr_normal = False
                            logger.debug(
                                f"[진입 거부] {current_date} | ATR 급등 "
                                f"(비율: {atr_ratio:.1f}x > {settings.ATR_SPIKE_THRESHOLD}x)"
                            )
                
                if is_uptrend and is_breakout and has_trend_strength and is_atr_normal:
                    # 진입가: 직전 캔들 고가 (돌파 시점)
                    entry_price = prev_high
                    
                    # 포지션 크기 계산
                    quantity = self._calculate_position_size(entry_price, capital)
                    
                    if quantity > 0:
                        # 손절/익절가 계산 (최대 손실 제한 포함)
                        atr_stop_loss = entry_price - (atr * self.strategy.atr_multiplier_sl)
                        max_loss_stop = entry_price * (1 - settings.MAX_LOSS_PCT / 100)
                        stop_loss = max(atr_stop_loss, max_loss_stop)
                        
                        take_profit = entry_price + (atr * self.strategy.atr_multiplier_tp)
                        
                        # 수수료
                        entry_commission = self._calculate_commission(entry_price, quantity)
                        
                        # 자본금에서 매수금액 차감
                        buy_amount = entry_price * quantity + entry_commission
                        capital = capital - entry_commission  # 수수료만 차감 (주식은 자산으로 보유)
                        
                        # 포지션 생성
                        position = {
                            "entry_price": entry_price,
                            "quantity": quantity,
                            "stop_loss": stop_loss,
                            "take_profit": take_profit,
                            "entry_date": current_date,
                            "entry_idx": i,
                            "entry_commission": entry_commission
                        }
                        
                        adx_str = f", ADX: {adx:.1f}" if adx else ""
                        logger.debug(
                            f"[진입] {current_date} | "
                            f"가격: {entry_price:,.0f}원 | 수량: {quantity}주 | "
                            f"손절: {stop_loss:,.0f}원 | 익절: {take_profit:,.0f}원{adx_str}"
                        )
            
            # 자산 곡선 업데이트
            if position is not None:
                # 포지션 평가 금액 포함
                position_value = position["quantity"] * current_close
                total_equity = capital + position_value - (position["entry_price"] * position["quantity"])
                equity_curve.append(total_equity)
            else:
                equity_curve.append(capital)
        
        # ════════════════════════════════════════════════════════════
        # 백테스트 종료: 미청산 포지션 처리
        # ════════════════════════════════════════════════════════════
        if position is not None:
            # 마지막 종가로 청산
            exit_price = df_with_indicators.iloc[-1]["close"]
            exit_date = str(df_with_indicators.iloc[-1]["date"])[:10]
            
            sell_commission = self._calculate_commission(exit_price, position["quantity"])
            gross_pnl = (exit_price - position["entry_price"]) * position["quantity"]
            net_pnl = gross_pnl - position["entry_commission"] - sell_commission
            pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
            
            capital = capital + gross_pnl - sell_commission
            holding_days = len(df_with_indicators) - 1 - position["entry_idx"]
            
            trade = Trade(
                entry_date=position["entry_date"],
                exit_date=exit_date,
                entry_price=position["entry_price"],
                exit_price=exit_price,
                quantity=position["quantity"],
                pnl=net_pnl,
                pnl_pct=pnl_pct,
                holding_days=holding_days,
                exit_reason="백테스트 종료"
            )
            trades.append(trade)
            
            logger.debug(f"[종료 청산] {exit_date} | 가격: {exit_price:,.0f}원")
        
        # ════════════════════════════════════════════════════════════
        # 결과 계산
        # ════════════════════════════════════════════════════════════
        result = self._calculate_results(
            trades=trades,
            equity_curve=equity_curve,
            df=df_with_indicators
        )
        
        self._print_summary(result)
        
        return result
    
    def _calculate_results(
        self,
        trades: List[Trade],
        equity_curve: List[float],
        df: pd.DataFrame
    ) -> BacktestResult:
        """
        백테스트 결과를 계산합니다.
        
        Args:
            trades: 거래 기록 목록
            equity_curve: 자산 곡선
            df: 데이터프레임
        
        Returns:
            BacktestResult: 계산된 결과
        """
        # 기본값
        start_date = str(df.iloc[0]["date"])[:10] if not df.empty else ""
        end_date = str(df.iloc[-1]["date"])[:10] if not df.empty else ""
        final_capital = equity_curve[-1] if equity_curve else self.initial_capital
        
        total_return = ((final_capital - self.initial_capital) / self.initial_capital) * 100
        
        # 거래 통계
        total_trades = len(trades)
        winning_trades = sum(1 for t in trades if t.pnl > 0)
        losing_trades = sum(1 for t in trades if t.pnl <= 0)
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        
        # 평균 보유 기간
        avg_holding_days = (
            sum(t.holding_days for t in trades) / total_trades
            if total_trades > 0 else 0
        )
        
        # 수익 팩터
        gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # 최대 낙폭 (MDD) 계산
        max_drawdown = self._calculate_mdd(equity_curve)
        
        return BacktestResult(
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
            final_capital=final_capital,
            total_return=total_return,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            max_drawdown=max_drawdown,
            avg_holding_days=avg_holding_days,
            profit_factor=profit_factor,
            trades=trades,
            equity_curve=equity_curve
        )
    
    def _calculate_mdd(self, equity_curve: List[float]) -> float:
        """
        최대 낙폭(Maximum Drawdown)을 계산합니다.
        
        MDD = (고점 - 저점) / 고점 * 100
        
        Args:
            equity_curve: 자산 곡선
        
        Returns:
            float: MDD (%)
        """
        if not equity_curve:
            return 0.0
        
        equity_array = np.array(equity_curve)
        
        # 누적 최고점
        running_max = np.maximum.accumulate(equity_array)
        
        # 낙폭
        drawdowns = (running_max - equity_array) / running_max * 100
        
        # 최대 낙폭
        max_drawdown = np.max(drawdowns)
        
        return float(max_drawdown)
    
    def _create_empty_result(self) -> BacktestResult:
        """빈 결과를 생성합니다."""
        return BacktestResult(
            start_date="",
            end_date="",
            initial_capital=self.initial_capital,
            final_capital=self.initial_capital,
            total_return=0.0,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=0.0,
            max_drawdown=0.0,
            avg_holding_days=0.0,
            profit_factor=0.0,
            trades=[],
            equity_curve=[]
        )
    
    def _print_summary(self, result: BacktestResult) -> None:
        """
        백테스트 결과 요약을 출력합니다.
        
        Args:
            result: 백테스트 결과
        """
        summary = f"""
═══════════════════════════════════════════════════════════════════
                    백테스트 결과 요약
═══════════════════════════════════════════════════════════════════
📅 기간: {result.start_date} ~ {result.end_date}

💰 자본금 변화
   - 초기 자본금: {result.initial_capital:>15,.0f} 원
   - 최종 자본금: {result.final_capital:>15,.0f} 원
   - 총 수익률:   {result.total_return:>15.2f} %

📊 거래 통계
   - 총 거래 횟수: {result.total_trades:>10} 회
   - 승리:         {result.winning_trades:>10} 회
   - 패배:         {result.losing_trades:>10} 회
   - 승률:         {result.win_rate:>10.2f} %

📉 리스크 지표
   - 최대 낙폭(MDD): {result.max_drawdown:>10.2f} %
   - 수익 팩터:     {result.profit_factor:>10.2f}

⏱️ 보유 기간
   - 평균 보유 기간: {result.avg_holding_days:>10.1f} 일
═══════════════════════════════════════════════════════════════════
"""
        print(summary)
        logger.info("백테스트 완료")
    
    def get_trade_details(self, result: BacktestResult) -> pd.DataFrame:
        """
        개별 거래 상세 내역을 DataFrame으로 반환합니다.
        
        Args:
            result: 백테스트 결과
        
        Returns:
            pd.DataFrame: 거래 상세 내역
        """
        if not result.trades:
            return pd.DataFrame()
        
        data = [
            {
                "진입일": t.entry_date,
                "청산일": t.exit_date,
                "진입가": t.entry_price,
                "청산가": t.exit_price,
                "수량": t.quantity,
                "손익금액": t.pnl,
                "손익률(%)": t.pnl_pct,
                "보유기간(일)": t.holding_days,
                "청산사유": t.exit_reason
            }
            for t in result.trades
        ]
        
        return pd.DataFrame(data)
