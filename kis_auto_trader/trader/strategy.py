"""
trader/strategy.py - 매매 전략 모듈

일반형(Neutral) 투자 성향 기준의 매매 전략을 구현합니다.
변동성 필터, 추세 확인 등 진입 조건을 정의합니다.
"""

from datetime import datetime
from typing import Optional, List, Tuple
from dataclasses import dataclass
from enum import Enum

import pandas as pd
import numpy as np

from config.settings import get_settings
from trader.broker_kis import KISBroker
from trader.risk_manager import RiskManager
from trader.notifier import get_notifier


class SignalType(Enum):
    """시그널 유형"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class TradingSignal:
    """매매 시그널 데이터 클래스"""
    signal_type: SignalType
    stock_code: str
    stock_name: str = ""
    price: float = 0
    reason: str = ""
    atr: float = 0
    volatility_ratio: float = 0


class TradingStrategy:
    """
    일반형(Neutral) 매매 전략 클래스
    
    진입 조건:
        - 변동성 필터 통과 (ATR 기준)
        - 추세 확인 (이동평균 기준)
        - 중복 진입 금지
        - 장중 거래 시간 확인
    
    청산 조건:
        - 손절: 진입가 대비 -2.5%
        - 익절: 진입가 대비 +5%
    """
    
    def __init__(self, broker: KISBroker, risk_manager: RiskManager):
        self.broker = broker
        self.risk_manager = risk_manager
        self.settings = get_settings()
        self.notifier = get_notifier()
        
        # 전략 파라미터
        self.atr_period = self.settings.strategy.atr_period
        self.volatility_threshold = self.settings.strategy.volatility_threshold
    
    # ═══════════════════════════════════════════════════════════════
    # 기술적 지표 계산
    # ═══════════════════════════════════════════════════════════════
    
    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        ATR(Average True Range) 계산
        
        Args:
            df: OHLCV 데이터프레임
            period: ATR 기간
        
        Returns:
            pd.Series: ATR 값
        """
        if len(df) < period:
            return pd.Series([np.nan] * len(df))
        
        high = df["high"]
        low = df["low"]
        close = df["close"]
        
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()
        
        return atr
    
    def calculate_sma(self, df: pd.DataFrame, period: int) -> pd.Series:
        """
        단순 이동평균 계산
        
        Args:
            df: OHLCV 데이터프레임
            period: 이동평균 기간
        
        Returns:
            pd.Series: SMA 값
        """
        return df["close"].rolling(window=period).mean()
    
    def calculate_volatility_ratio(
        self,
        df: pd.DataFrame,
        current_atr: float,
        period: int = 20
    ) -> float:
        """
        변동성 비율 계산 (현재 ATR / 평균 ATR)
        
        Args:
            df: OHLCV 데이터프레임
            current_atr: 현재 ATR
            period: 비교 기간
        
        Returns:
            float: 변동성 비율
        """
        atr = self.calculate_atr(df, self.atr_period)
        avg_atr = atr.tail(period).mean()
        
        if avg_atr <= 0 or pd.isna(avg_atr):
            return 0
        
        return current_atr / avg_atr
    
    # ═══════════════════════════════════════════════════════════════
    # 진입 조건 체크
    # ═══════════════════════════════════════════════════════════════
    
    def check_entry_conditions(
        self,
        stock_code: str,
        df: pd.DataFrame,
        current_price: float
    ) -> Tuple[bool, str]:
        """
        진입 조건 종합 체크
        
        Args:
            stock_code: 종목코드
            df: OHLCV 데이터프레임
            current_price: 현재가
        
        Returns:
            Tuple: (진입 가능 여부, 사유)
        """
        # 1. 데이터 충분성 체크
        if len(df) < max(self.atr_period, 50):
            return False, "데이터 부족"
        
        # 2. 중복 진입 체크
        can_enter, reason = self.risk_manager.can_enter_stock(stock_code)
        if not can_enter:
            return False, reason
        
        # 3. ATR 계산
        atr = self.calculate_atr(df, self.atr_period)
        current_atr = atr.iloc[-1]
        
        if pd.isna(current_atr) or current_atr <= 0:
            return False, "ATR 계산 불가"
        
        # 4. 변동성 필터 (과도한 변동성 제외)
        volatility_ratio = self.calculate_volatility_ratio(df, current_atr)
        if volatility_ratio > self.volatility_threshold:
            return False, f"변동성 과다 ({volatility_ratio:.2f}x)"
        
        # 5. 추세 확인 (20일 이동평균 위)
        sma_20 = self.calculate_sma(df, 20).iloc[-1]
        if current_price < sma_20:
            return False, "하락 추세"
        
        # 6. 직전 봉 대비 돌파 확인
        prev_high = df["high"].iloc[-2]
        if current_price <= prev_high:
            return False, "돌파 미발생"
        
        return True, "진입 조건 충족"
    
    # ═══════════════════════════════════════════════════════════════
    # 시그널 생성
    # ═══════════════════════════════════════════════════════════════
    
    def generate_signal(
        self,
        stock_code: str,
        stock_name: str = ""
    ) -> TradingSignal:
        """
        매매 시그널 생성
        
        Args:
            stock_code: 종목코드
            stock_name: 종목명
        
        Returns:
            TradingSignal: 매매 시그널
        """
        # 기본 시그널 (홀드)
        signal = TradingSignal(
            signal_type=SignalType.HOLD,
            stock_code=stock_code,
            stock_name=stock_name
        )
        
        try:
            # 현재가 조회
            price_data = self.broker.get_current_price(stock_code)
            current_price = price_data["current_price"]
            
            if current_price <= 0:
                signal.reason = "현재가 조회 실패"
                return signal
            
            signal.price = current_price
            
            # 기존 포지션 있으면 청산 조건 체크
            if self.risk_manager.has_position(stock_code):
                should_exit, exit_reason, position = \
                    self.risk_manager.check_exit_conditions(stock_code, current_price)
                
                if should_exit:
                    signal.signal_type = SignalType.SELL
                    signal.reason = exit_reason
                    return signal
                
                signal.reason = "포지션 유지"
                return signal
            
            # 일봉 데이터 조회
            df = self.broker.get_daily_ohlcv(stock_code, count=100)
            
            if df.empty:
                signal.reason = "시세 데이터 조회 실패"
                return signal
            
            # ATR 계산
            atr = self.calculate_atr(df, self.atr_period)
            current_atr = atr.iloc[-1]
            signal.atr = current_atr
            
            # 변동성 비율
            signal.volatility_ratio = self.calculate_volatility_ratio(df, current_atr)
            
            # 진입 조건 체크
            can_enter, reason = self.check_entry_conditions(
                stock_code, df, current_price
            )
            
            if can_enter:
                signal.signal_type = SignalType.BUY
                signal.reason = reason
            else:
                signal.reason = reason
            
        except Exception as e:
            signal.reason = f"오류: {str(e)}"
        
        return signal
    
    # ═══════════════════════════════════════════════════════════════
    # 매매 실행
    # ═══════════════════════════════════════════════════════════════
    
    def execute_signal(self, signal: TradingSignal) -> bool:
        """
        시그널에 따른 매매 실행
        
        Args:
            signal: 매매 시그널
        
        Returns:
            bool: 실행 성공 여부
        """
        if signal.signal_type == SignalType.HOLD:
            return True
        
        if signal.signal_type == SignalType.BUY:
            return self._execute_buy(signal)
        
        if signal.signal_type == SignalType.SELL:
            return self._execute_sell(signal)
        
        return False
    
    def _execute_buy(self, signal: TradingSignal) -> bool:
        """매수 실행"""
        # 잔고 조회
        balance = self.broker.get_balance()
        total_capital = balance["total_eval"]
        
        if total_capital <= 0:
            return False
        
        # 포지션 크기 계산
        quantity = self.risk_manager.calculate_position_size(
            entry_price=signal.price,
            total_capital=total_capital
        )
        
        if quantity <= 0:
            return False
        
        # 매수 주문
        result = self.broker.place_buy_order(
            stock_code=signal.stock_code,
            quantity=quantity
        )
        
        if result.success:
            # 포지션 등록
            position = self.risk_manager.add_position(
                stock_code=signal.stock_code,
                stock_name=signal.stock_name,
                entry_price=signal.price,
                quantity=quantity
            )
            
            # 텔레그램 알림
            self.notifier.notify_buy(
                stock_code=signal.stock_code,
                stock_name=signal.stock_name,
                price=signal.price,
                quantity=quantity,
                stop_loss=position.stop_loss_price,
                take_profit=position.take_profit_price
            )
            
            return True
        
        return False
    
    def _execute_sell(self, signal: TradingSignal) -> bool:
        """매도 실행 (손절/익절)"""
        position = self.risk_manager.get_position(signal.stock_code)
        if not position:
            return False
        
        if signal.reason == "stop_loss":
            return self.risk_manager.execute_stop_loss(
                signal.stock_code,
                signal.price
            )
        elif signal.reason == "take_profit":
            return self.risk_manager.execute_take_profit(
                signal.stock_code,
                signal.price
            )
        else:
            # 일반 청산
            result = self.broker.place_sell_order(
                stock_code=signal.stock_code,
                quantity=position.quantity
            )
            
            if result.success:
                pnl = (signal.price - position.entry_price) * position.quantity
                pnl_pct = ((signal.price / position.entry_price) - 1) * 100
                
                self.notifier.notify_sell(
                    stock_code=signal.stock_code,
                    stock_name=position.stock_name,
                    price=signal.price,
                    quantity=position.quantity,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    reason=signal.reason
                )
                
                self.risk_manager.remove_position(signal.stock_code)
                return True
        
        return False
    
    # ═══════════════════════════════════════════════════════════════
    # 포지션 모니터링
    # ═══════════════════════════════════════════════════════════════
    
    def monitor_positions(self) -> None:
        """
        보유 포지션 모니터링 및 손절/익절 체크
        """
        positions = self.risk_manager.get_all_positions()
        
        for position in positions:
            try:
                # 현재가 조회
                price_data = self.broker.get_current_price(position.stock_code)
                current_price = price_data["current_price"]
                
                if current_price <= 0:
                    continue
                
                # 청산 조건 체크
                should_exit, exit_reason, _ = \
                    self.risk_manager.check_exit_conditions(
                        position.stock_code,
                        current_price
                    )
                
                if should_exit:
                    if exit_reason == "stop_loss":
                        self.risk_manager.execute_stop_loss(
                            position.stock_code,
                            current_price
                        )
                    elif exit_reason == "take_profit":
                        self.risk_manager.execute_take_profit(
                            position.stock_code,
                            current_price
                        )
                        
            except Exception as e:
                self.notifier.notify_error(e, f"포지션 모니터링: {position.stock_code}")
    
    def run_strategy(self, watchlist: List[str]) -> None:
        """
        전략 1회 실행
        
        Args:
            watchlist: 감시 종목 리스트
        """
        # 1. 기존 포지션 모니터링
        self.monitor_positions()
        
        # 2. 신규 진입 검토
        for stock_code in watchlist:
            # 이미 보유 중이면 스킵
            if self.risk_manager.has_position(stock_code):
                continue
            
            # 신규 진입 가능 여부 체크
            can_open, _ = self.risk_manager.can_open_new_position()
            if not can_open:
                break  # 더 이상 진입 불가
            
            # 시그널 생성
            signal = self.generate_signal(stock_code)
            
            # 매수 시그널이면 실행
            if signal.signal_type == SignalType.BUY:
                self.execute_signal(signal)
