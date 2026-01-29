"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - Trend + ATR 전략 (환경 독립)
═══════════════════════════════════════════════════════════════════════════════

이 모듈은 Trend-ATR 전략의 핵심 로직을 구현합니다.

★★★ 절대 규칙: 이 모듈은 환경(DEV/PROD)을 전혀 알지 못합니다 ★★★

    - env.py를 임포트하지 않습니다.
    - config_loader.py를 임포트하지 않습니다.
    - trader.py를 임포트하지 않습니다.
    - 어떤 주문도 실행하지 않습니다.
    
    이 전략은 오직 "시그널 생성"만 담당합니다.
    주문 실행은 main.py나 실행 엔진이 trader.py를 통해 수행합니다.

★ 전략 개요:
    1. 추세 판단: 종가 > N일 이동평균 → 상승 추세
    2. 진입 조건: 상승 추세 + 직전 캔들 고가 돌파
    3. 손절/익절: ATR 기반 동적 설정

★ 입력과 출력:
    - 입력: OHLCV 데이터프레임, 현재가, 전략 파라미터
    - 출력: 매매 시그널 (BUY, SELL, HOLD)

═══════════════════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, Dict, Any
import pandas as pd
import numpy as np


# ═══════════════════════════════════════════════════════════════════════════════
# 열거형 및 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class SignalType(Enum):
    """매매 시그널 타입"""
    BUY = "BUY"       # 매수 시그널
    SELL = "SELL"     # 매도 시그널 (익절/손절)
    HOLD = "HOLD"     # 관망


class TrendType(Enum):
    """추세 타입"""
    UPTREND = "UPTREND"       # 상승 추세
    DOWNTREND = "DOWNTREND"   # 하락 추세
    SIDEWAYS = "SIDEWAYS"     # 횡보


@dataclass
class Signal:
    """
    매매 시그널 데이터 클래스
    
    ★ 환경 독립: 이 클래스는 환경 정보를 포함하지 않습니다.
    """
    signal_type: SignalType
    price: float
    stop_loss: float = 0.0
    take_profit: float = 0.0
    reason: str = ""
    atr: float = 0.0
    trend: TrendType = TrendType.SIDEWAYS


@dataclass
class Position:
    """포지션 데이터 클래스"""
    stock_code: str
    entry_price: float
    quantity: int
    stop_loss: float
    take_profit: float
    entry_date: str
    atr_at_entry: float


@dataclass
class StrategyParams:
    """
    전략 파라미터 데이터 클래스
    
    ★ 환경 독립: 파라미터는 외부에서 주입됩니다.
    """
    atr_period: int = 14
    trend_ma_period: int = 50
    atr_multiplier_sl: float = 2.0
    atr_multiplier_tp: float = 3.0
    max_loss_pct: float = 5.0
    atr_spike_threshold: float = 2.5
    adx_threshold: float = 25.0
    adx_period: int = 14


# ═══════════════════════════════════════════════════════════════════════════════
# 전략 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class TrendATRStrategy:
    """
    Trend + ATR 기반 매매 전략 클래스
    
    ★★★ 환경 독립 ★★★
    이 클래스는 환경(DEV/PROD)에 대해 전혀 알지 못합니다.
    - 설정 파일을 읽지 않습니다.
    - API를 호출하지 않습니다.
    - 주문을 실행하지 않습니다.
    
    순수하게 "데이터 → 시그널" 변환만 담당합니다.
    
    ★ 전략 규칙:
        1. 추세 판단: 종가 > MA(N) → 상승 추세
        2. 진입 조건: 상승 추세 + 직전 캔들 고가 돌파
        3. 손절가: 진입가 - (ATR * 손절배수)
        4. 익절가: 진입가 + (ATR * 익절배수)
        5. 포지션 보유 중 추가 진입 금지
    """
    
    def __init__(self, params: StrategyParams = None):
        """
        전략 초기화
        
        ★ 환경 독립: 모든 파라미터는 외부에서 주입됩니다.
        
        Args:
            params: 전략 파라미터 (없으면 기본값 사용)
        """
        # ★ 파라미터는 외부에서 주입됨 (환경 설정 직접 참조 안 함)
        self.params = params or StrategyParams()
        
        # 포지션 상태
        self.position: Optional[Position] = None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 기술적 지표 계산
    # ═══════════════════════════════════════════════════════════════════════════
    
    def calculate_atr(self, df: pd.DataFrame) -> pd.Series:
        """
        ATR(Average True Range)을 계산합니다.
        
        Args:
            df: OHLCV 데이터프레임
        
        Returns:
            Series: ATR 값
        """
        high = df['high']
        low = df['low']
        close = df['close']
        
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        return true_range.rolling(window=self.params.atr_period).mean()
    
    def calculate_ma(self, df: pd.DataFrame) -> pd.Series:
        """
        이동평균을 계산합니다.
        
        Args:
            df: OHLCV 데이터프레임
        
        Returns:
            Series: 이동평균 값
        """
        return df['close'].rolling(window=self.params.trend_ma_period).mean()
    
    def calculate_adx(self, df: pd.DataFrame) -> pd.Series:
        """
        ADX(Average Directional Index)를 계산합니다.
        
        Args:
            df: OHLCV 데이터프레임
        
        Returns:
            Series: ADX 값
        """
        period = self.params.adx_period
        
        high = df['high']
        low = df['low']
        close = df['close']
        
        plus_dm = high.diff()
        minus_dm = -low.diff()
        
        plus_dm = np.where(
            (plus_dm > minus_dm) & (plus_dm > 0),
            plus_dm,
            0
        )
        
        minus_dm = np.where(
            (minus_dm > plus_dm) & (minus_dm > 0),
            minus_dm,
            0
        )
        
        plus_dm = pd.Series(plus_dm, index=df.index)
        minus_dm = pd.Series(minus_dm, index=df.index)
        
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        atr_smooth = true_range.ewm(alpha=1/period, adjust=False).mean()
        plus_dm_smooth = plus_dm.ewm(alpha=1/period, adjust=False).mean()
        minus_dm_smooth = minus_dm.ewm(alpha=1/period, adjust=False).mean()
        
        plus_di = 100 * plus_dm_smooth / atr_smooth
        minus_di = 100 * minus_dm_smooth / atr_smooth
        
        di_sum = plus_di + minus_di
        di_diff = abs(plus_di - minus_di)
        dx = 100 * di_diff / di_sum.replace(0, np.nan)
        
        return dx.ewm(alpha=1/period, adjust=False).mean()
    
    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        데이터프레임에 기술적 지표를 추가합니다.
        
        Args:
            df: OHLCV 데이터프레임
        
        Returns:
            DataFrame: 지표가 추가된 데이터프레임
        """
        df = df.copy()
        
        df['atr'] = self.calculate_atr(df)
        df['ma'] = self.calculate_ma(df)
        df['adx'] = self.calculate_adx(df)
        
        df['trend'] = np.where(
            df['close'] > df['ma'],
            TrendType.UPTREND.value,
            TrendType.DOWNTREND.value
        )
        
        df['prev_high'] = df['high'].shift(1)
        
        return df
    
    # ═══════════════════════════════════════════════════════════════════════════
    # ATR 검증
    # ═══════════════════════════════════════════════════════════════════════════
    
    def is_atr_valid(self, df: pd.DataFrame) -> Tuple[bool, str]:
        """
        ATR이 정상 범위인지 검증합니다.
        
        Args:
            df: 지표가 계산된 데이터프레임
        
        Returns:
            Tuple[bool, str]: (유효 여부, 사유)
        """
        min_periods = self.params.atr_period * 2
        if len(df) < min_periods:
            return True, ""
        
        current_atr = df.iloc[-1]['atr']
        
        if pd.isna(current_atr):
            return True, ""
        
        recent_atr = df['atr'].iloc[-min_periods:-1]
        avg_atr = recent_atr.mean()
        
        if pd.isna(avg_atr) or avg_atr <= 0:
            return True, ""
        
        atr_ratio = current_atr / avg_atr
        if atr_ratio > self.params.atr_spike_threshold:
            return False, (
                f"ATR 급등 감지 (현재: {current_atr:,.0f}, "
                f"평균: {avg_atr:,.0f}, 비율: {atr_ratio:.1f}x)"
            )
        
        return True, ""
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 추세 판단
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_trend(self, df: pd.DataFrame) -> TrendType:
        """
        현재 추세를 판단합니다.
        
        Args:
            df: 지표가 계산된 데이터프레임
        
        Returns:
            TrendType: 현재 추세
        """
        if df.empty or len(df) < self.params.trend_ma_period:
            return TrendType.SIDEWAYS
        
        latest = df.iloc[-1]
        
        if pd.isna(latest['ma']):
            return TrendType.SIDEWAYS
        
        if latest['close'] > latest['ma']:
            return TrendType.UPTREND
        else:
            return TrendType.DOWNTREND
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 손절/익절 가격 계산
    # ═══════════════════════════════════════════════════════════════════════════
    
    def calculate_stop_loss(self, entry_price: float, atr: float) -> float:
        """
        손절가를 계산합니다.
        
        Args:
            entry_price: 진입가
            atr: ATR 값
        
        Returns:
            float: 손절가
        """
        atr_stop_loss = entry_price - (atr * self.params.atr_multiplier_sl)
        max_loss_stop = entry_price * (1 - self.params.max_loss_pct / 100)
        
        return max(0, max(atr_stop_loss, max_loss_stop))
    
    def calculate_take_profit(self, entry_price: float, atr: float) -> float:
        """
        익절가를 계산합니다.
        
        Args:
            entry_price: 진입가
            atr: ATR 값
        
        Returns:
            float: 익절가
        """
        return entry_price + (atr * self.params.atr_multiplier_tp)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 진입/청산 조건 판단
    # ═══════════════════════════════════════════════════════════════════════════
    
    def check_entry_condition(
        self,
        df: pd.DataFrame,
        current_price: float
    ) -> Tuple[bool, str]:
        """
        진입 조건을 확인합니다.
        
        Args:
            df: 지표가 계산된 데이터프레임
            current_price: 현재가
        
        Returns:
            Tuple[bool, str]: (진입 여부, 사유)
        """
        if self.position is not None:
            return False, "포지션 보유 중"
        
        if df.empty or len(df) < self.params.trend_ma_period:
            return False, "데이터 부족"
        
        latest = df.iloc[-1]
        
        if pd.isna(latest['atr']) or latest['atr'] <= 0:
            return False, "ATR 미계산"
        
        atr_valid, atr_reason = self.is_atr_valid(df)
        if not atr_valid:
            return False, atr_reason
        
        adx = latest.get('adx', None)
        if adx is not None and not pd.isna(adx):
            if adx < self.params.adx_threshold:
                return False, f"추세 강도 부족 (ADX: {adx:.1f})"
        
        trend = self.get_trend(df)
        if trend != TrendType.UPTREND:
            return False, f"하락/횡보 추세 ({trend.value})"
        
        prev_high = latest['prev_high']
        if pd.isna(prev_high):
            return False, "직전 고가 없음"
        
        if current_price <= prev_high:
            return False, f"돌파 미발생 (현재가: {current_price:,.0f} <= 직전고가: {prev_high:,.0f})"
        
        adx_str = f"{adx:.1f}" if adx is not None and not pd.isna(adx) else "N/A"
        return True, f"상승 추세(ADX:{adx_str}) + 직전 고가({prev_high:,.0f}) 돌파"
    
    def check_exit_condition(
        self, 
        current_price: float,
        df: pd.DataFrame = None
    ) -> Tuple[bool, str]:
        """
        청산 조건을 확인합니다.
        
        ★ 청산 조건 (우선순위 순):
            1. 손절가 도달 (ATR Stop)
            2. 익절가 도달 (Take Profit)
            3. 추세 이탈 (MA 하향 돌파)
        
        Args:
            current_price: 현재가
            df: OHLCV 데이터프레임 (추세 이탈 체크용)
        
        Returns:
            Tuple[bool, str]: (청산 여부, 사유)
        """
        if self.position is None:
            return False, "포지션 없음"
        
        pnl_pct = ((current_price - self.position.entry_price) / 
                   self.position.entry_price * 100)
        
        # 1. 손절 체크 (ATR Stop)
        if current_price <= self.position.stop_loss:
            return True, f"손절 도달 (손절가: {self.position.stop_loss:,.0f}, 손익: {pnl_pct:.2f}%)"
        
        # 2. 익절 체크 (Take Profit)
        if current_price >= self.position.take_profit:
            return True, f"익절 도달 (익절가: {self.position.take_profit:,.0f}, 손익: {pnl_pct:.2f}%)"
        
        # 3. 추세 이탈 체크 (MA 하향 돌파)
        if df is not None and len(df) >= self.params.trend_ma_period:
            trend = self.get_trend(df)
            if trend == TrendType.DOWNTREND:
                # 하락 추세로 전환된 경우
                return True, f"추세 이탈 - MA 하향 돌파 (손익: {pnl_pct:.2f}%)"
        
        return False, "청산 조건 미충족"
    
    def check_trend_exit(self, df: pd.DataFrame) -> Tuple[bool, str]:
        """
        추세 이탈 조건만 별도로 확인합니다.
        
        포지션 보유 중 추세가 하락으로 전환되면 청산합니다.
        
        Args:
            df: OHLCV 데이터프레임
        
        Returns:
            Tuple[bool, str]: (청산 여부, 사유)
        """
        if self.position is None:
            return False, "포지션 없음"
        
        if df is None or len(df) < self.params.trend_ma_period:
            return False, "데이터 부족"
        
        df_with_indicators = self.add_indicators(df)
        trend = self.get_trend(df_with_indicators)
        
        if trend == TrendType.DOWNTREND:
            latest = df_with_indicators.iloc[-1]
            ma = latest.get('ma', 0)
            close = latest.get('close', 0)
            
            return True, (
                f"추세 이탈 - 종가({close:,.0f}) < MA({ma:,.0f})"
            )
        
        return False, "추세 유지 중"
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 시그널 생성
    # ═══════════════════════════════════════════════════════════════════════════
    
    def generate_signal(
        self,
        df: pd.DataFrame,
        current_price: float
    ) -> Signal:
        """
        매매 시그널을 생성합니다.
        
        ★ 환경 독립: 이 메서드는 순수하게 데이터만 분석합니다.
            주문 실행은 호출자의 책임입니다.
        
        ★ 청산 조건 (우선순위 순):
            1. 손절가 도달 (ATR Stop)
            2. 익절가 도달 (Take Profit)
            3. 추세 이탈 (MA 하향 돌파)
        
        Args:
            df: OHLCV 데이터프레임
            current_price: 현재가
        
        Returns:
            Signal: 매매 시그널
        """
        df_with_indicators = self.add_indicators(df)
        
        if df_with_indicators.empty:
            return Signal(
                signal_type=SignalType.HOLD,
                price=current_price,
                reason="데이터 없음"
            )
        
        latest = df_with_indicators.iloc[-1]
        atr = latest['atr'] if not pd.isna(latest['atr']) else 0
        trend = self.get_trend(df_with_indicators)
        
        # 포지션 보유 중 → 청산 조건 확인
        if self.position is not None:
            # ★ 추세 이탈 체크를 포함한 청산 조건 확인
            should_exit, exit_reason = self.check_exit_condition(
                current_price, 
                df_with_indicators  # 추세 이탈 체크용
            )
            
            if should_exit:
                return Signal(
                    signal_type=SignalType.SELL,
                    price=current_price,
                    stop_loss=self.position.stop_loss,
                    take_profit=self.position.take_profit,
                    reason=exit_reason,
                    atr=atr,
                    trend=trend
                )
            
            return Signal(
                signal_type=SignalType.HOLD,
                price=current_price,
                stop_loss=self.position.stop_loss,
                take_profit=self.position.take_profit,
                reason="포지션 유지 중",
                atr=atr,
                trend=trend
            )
        
        # 포지션 미보유 → 진입 조건 확인
        should_enter, entry_reason = self.check_entry_condition(
            df_with_indicators, current_price
        )
        
        if should_enter:
            stop_loss = self.calculate_stop_loss(current_price, atr)
            take_profit = self.calculate_take_profit(current_price, atr)
            
            return Signal(
                signal_type=SignalType.BUY,
                price=current_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reason=entry_reason,
                atr=atr,
                trend=trend
            )
        
        return Signal(
            signal_type=SignalType.HOLD,
            price=current_price,
            reason=entry_reason,
            atr=atr,
            trend=trend
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 포지션 관리
    # ═══════════════════════════════════════════════════════════════════════════
    
    def open_position(
        self,
        stock_code: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        take_profit: float,
        entry_date: str,
        atr: float
    ) -> Position:
        """
        포지션을 오픈합니다.
        
        ★ 환경 독립: 이 메서드는 상태만 업데이트합니다.
            실제 주문은 호출자가 trader.py를 통해 수행합니다.
        
        Args:
            stock_code: 종목 코드
            entry_price: 진입가
            quantity: 수량
            stop_loss: 손절가
            take_profit: 익절가
            entry_date: 진입일
            atr: 진입 시 ATR
        
        Returns:
            Position: 생성된 포지션
        """
        self.position = Position(
            stock_code=stock_code,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_date=entry_date,
            atr_at_entry=atr
        )
        
        return self.position
    
    def close_position(self, exit_price: float, reason: str = "") -> Optional[Dict[str, Any]]:
        """
        포지션을 청산합니다.
        
        ★ 환경 독립: 이 메서드는 상태만 업데이트합니다.
            실제 주문은 호출자가 trader.py를 통해 수행합니다.
        
        Args:
            exit_price: 청산가
            reason: 청산 사유
        
        Returns:
            Dict: 청산 결과 (포지션이 없으면 None)
        """
        if self.position is None:
            return None
        
        pnl = (exit_price - self.position.entry_price) * self.position.quantity
        pnl_pct = (exit_price - self.position.entry_price) / self.position.entry_price * 100
        
        result = {
            "stock_code": self.position.stock_code,
            "entry_price": self.position.entry_price,
            "exit_price": exit_price,
            "quantity": self.position.quantity,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "entry_date": self.position.entry_date,
            "reason": reason
        }
        
        self.position = None
        
        return result
    
    def has_position(self) -> bool:
        """포지션 보유 여부를 반환합니다."""
        return self.position is not None
    
    def get_position_pnl(self, current_price: float) -> Tuple[float, float]:
        """
        현재 포지션의 손익을 계산합니다.
        
        Args:
            current_price: 현재가
        
        Returns:
            Tuple[float, float]: (손익금액, 손익률)
        """
        if self.position is None:
            return 0.0, 0.0
        
        pnl = (current_price - self.position.entry_price) * self.position.quantity
        pnl_pct = (current_price - self.position.entry_price) / self.position.entry_price * 100
        
        return pnl, pnl_pct


# ═══════════════════════════════════════════════════════════════════════════════
# 팩토리 함수
# ═══════════════════════════════════════════════════════════════════════════════

def create_strategy_from_config(config_strategy: Dict[str, Any]) -> TrendATRStrategy:
    """
    설정 딕셔너리에서 전략을 생성합니다.
    
    ★ 환경 독립: 설정은 외부에서 주입됩니다.
        이 함수 내에서 설정 파일을 직접 읽지 않습니다.
    
    Args:
        config_strategy: 전략 설정 딕셔너리
    
    Returns:
        TrendATRStrategy: 전략 인스턴스
    """
    params = StrategyParams(
        atr_period=config_strategy.get("atr_period", 14),
        trend_ma_period=config_strategy.get("trend_ma_period", 50),
        atr_multiplier_sl=config_strategy.get("atr_multiplier_sl", 2.0),
        atr_multiplier_tp=config_strategy.get("atr_multiplier_tp", 3.0)
    )
    
    return TrendATRStrategy(params)
