"""
KIS Trend-ATR Trading System - Trend + ATR 전략

이 모듈은 추세 추종과 ATR(Average True Range) 기반의
손절/익절 전략을 구현합니다.

전략 개요:
    1. 추세 판단: 종가 > 50일 이동평균 → 상승 추세
    2. 진입 조건: 상승 추세 + 직전 캔들 고가 돌파
    3. 손절/익절: ATR 기반 동적 설정
    
⚠️ 주의: 하락 추세에서는 신규 진입을 금지합니다.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple
import pandas as pd
import numpy as np

from config import settings
from utils.logger import get_logger, TradeLogger

logger = get_logger("strategy")
trade_logger = TradeLogger("strategy")


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
    
    Attributes:
        signal_type: 시그널 타입 (BUY, SELL, HOLD)
        price: 시그널 발생 시 가격
        stop_loss: 손절가
        take_profit: 익절가
        reason: 시그널 발생 사유
        atr: 현재 ATR 값
        trend: 현재 추세
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
    """
    포지션 데이터 클래스
    
    Attributes:
        stock_code: 종목 코드
        entry_price: 진입가
        quantity: 보유 수량
        stop_loss: 손절가
        take_profit: 익절가
        entry_date: 진입일
        atr_at_entry: 진입 시 ATR
    """
    stock_code: str
    entry_price: float
    quantity: int
    stop_loss: float
    take_profit: float
    entry_date: str
    atr_at_entry: float


class TrendATRStrategy:
    """
    Trend + ATR 기반 매매 전략 클래스
    
    추세 추종 전략과 ATR 기반 변동성 손절/익절을 결합합니다.
    
    전략 규칙:
        1. 추세 판단: 종가 > MA(50) → 상승 추세
        2. 진입 조건: 상승 추세 + 직전 캔들 고가 돌파
        3. 손절가: 진입가 - (ATR * 2.0)
        4. 익절가: 진입가 + (ATR * 3.0)
        5. 포지션 보유 중 추가 진입 금지
    
    Attributes:
        atr_period: ATR 계산 기간
        ma_period: 추세 판단용 이동평균 기간
        atr_multiplier_sl: 손절 ATR 배수
        atr_multiplier_tp: 익절 ATR 배수
        position: 현재 포지션
    """
    
    def __init__(
        self,
        atr_period: int = None,
        ma_period: int = None,
        atr_multiplier_sl: float = None,
        atr_multiplier_tp: float = None
    ):
        """
        전략 초기화
        
        Args:
            atr_period: ATR 계산 기간 (기본: 14)
            ma_period: 추세 판단용 이동평균 기간 (기본: 50)
            atr_multiplier_sl: 손절 ATR 배수 (기본: 2.0)
            atr_multiplier_tp: 익절 ATR 배수 (기본: 3.0)
        """
        self.atr_period = atr_period or settings.ATR_PERIOD
        self.ma_period = ma_period or settings.TREND_MA_PERIOD
        self.atr_multiplier_sl = atr_multiplier_sl or settings.ATR_MULTIPLIER_SL
        self.atr_multiplier_tp = atr_multiplier_tp or settings.ATR_MULTIPLIER_TP
        
        # 현재 포지션 (None = 포지션 없음)
        self.position: Optional[Position] = None
        
        logger.info(
            f"전략 초기화: ATR({self.atr_period}), MA({self.ma_period}), "
            f"SL({self.atr_multiplier_sl}x), TP({self.atr_multiplier_tp}x)"
        )
    
    # ════════════════════════════════════════════════════════════════
    # 기술적 지표 계산
    # ════════════════════════════════════════════════════════════════
    
    def calculate_atr(self, df: pd.DataFrame) -> pd.Series:
        """
        ATR(Average True Range)을 계산합니다.
        
        ATR은 변동성을 측정하는 지표로, 다음 세 값 중 최대값의 평균입니다:
            1. 당일 고가 - 당일 저가
            2. |당일 고가 - 전일 종가|
            3. |당일 저가 - 전일 종가|
        
        Args:
            df: OHLCV 데이터프레임 (high, low, close 컬럼 필요)
        
        Returns:
            pd.Series: ATR 값 시리즈
        """
        high = df['high']
        low = df['low']
        close = df['close']
        
        # True Range 계산
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # ATR = True Range의 이동평균
        atr = true_range.rolling(window=self.atr_period).mean()
        
        return atr
    
    def calculate_ma(self, df: pd.DataFrame) -> pd.Series:
        """
        단순 이동평균을 계산합니다.
        
        Args:
            df: OHLCV 데이터프레임 (close 컬럼 필요)
        
        Returns:
            pd.Series: 이동평균 시리즈
        """
        return df['close'].rolling(window=self.ma_period).mean()
    
    def calculate_adx(self, df: pd.DataFrame, period: int = None) -> pd.Series:
        """
        ADX(Average Directional Index)를 계산합니다.
        
        ADX는 추세의 강도를 측정하는 지표입니다:
            - ADX > 25: 추세 존재 (강한 추세)
            - ADX < 25: 횡보장 (추세 약함)
        
        Args:
            df: OHLCV 데이터프레임 (high, low, close 컬럼 필요)
            period: ADX 계산 기간 (기본: settings.ADX_PERIOD)
        
        Returns:
            pd.Series: ADX 값 시리즈
        """
        period = period or settings.ADX_PERIOD
        
        high = df['high']
        low = df['low']
        close = df['close']
        
        # +DM (Positive Directional Movement)
        plus_dm = high.diff()
        minus_dm = -low.diff()
        
        # +DM은 고가 상승분이 저가 하락분보다 클 때만 유효
        plus_dm = np.where(
            (plus_dm > minus_dm) & (plus_dm > 0),
            plus_dm,
            0
        )
        
        # -DM은 저가 하락분이 고가 상승분보다 클 때만 유효
        minus_dm = np.where(
            (minus_dm > plus_dm) & (minus_dm > 0),
            minus_dm,
            0
        )
        
        plus_dm = pd.Series(plus_dm, index=df.index)
        minus_dm = pd.Series(minus_dm, index=df.index)
        
        # True Range
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # Smoothed TR, +DM, -DM (Wilder's smoothing)
        atr_smooth = true_range.ewm(alpha=1/period, adjust=False).mean()
        plus_dm_smooth = plus_dm.ewm(alpha=1/period, adjust=False).mean()
        minus_dm_smooth = minus_dm.ewm(alpha=1/period, adjust=False).mean()
        
        # +DI, -DI
        plus_di = 100 * plus_dm_smooth / atr_smooth
        minus_di = 100 * minus_dm_smooth / atr_smooth
        
        # DX (Directional Index)
        di_sum = plus_di + minus_di
        di_diff = abs(plus_di - minus_di)
        dx = 100 * di_diff / di_sum.replace(0, np.nan)
        
        # ADX (smoothed DX)
        adx = dx.ewm(alpha=1/period, adjust=False).mean()
        
        return adx
    
    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        데이터프레임에 기술적 지표를 추가합니다.
        
        Args:
            df: OHLCV 데이터프레임
        
        Returns:
            pd.DataFrame: 지표가 추가된 데이터프레임
        """
        df = df.copy()
        
        # ATR 계산
        df['atr'] = self.calculate_atr(df)
        
        # 추세 판단용 이동평균
        df['ma'] = self.calculate_ma(df)
        
        # ADX (추세 강도) 계산
        df['adx'] = self.calculate_adx(df)
        
        # 추세 판단
        df['trend'] = np.where(
            df['close'] > df['ma'],
            TrendType.UPTREND.value,
            TrendType.DOWNTREND.value
        )
        
        # 직전 캔들 고가 (진입 조건용)
        df['prev_high'] = df['high'].shift(1)
        
        return df
    
    # ════════════════════════════════════════════════════════════════
    # ATR 검증
    # ════════════════════════════════════════════════════════════════
    
    def is_atr_valid(self, df: pd.DataFrame) -> Tuple[bool, str]:
        """
        ATR이 정상 범위인지 검증합니다.
        
        ATR 급등 시 손절/익절 폭이 비정상적으로 넓어지므로,
        평균 대비 일정 배수를 초과하면 진입을 거부합니다.
        
        Args:
            df: 지표가 계산된 데이터프레임
        
        Returns:
            Tuple[bool, str]: (유효 여부, 사유)
        """
        # 비교용 데이터가 부족하면 통과
        min_periods = self.atr_period * 2
        if len(df) < min_periods:
            return True, ""
        
        current_atr = df.iloc[-1]['atr']
        
        # 현재 ATR이 NaN이면 이미 다른 곳에서 필터링됨
        if pd.isna(current_atr):
            return True, ""
        
        # 최근 ATR 평균 계산 (현재 값 제외)
        recent_atr = df['atr'].iloc[-min_periods:-1]
        avg_atr = recent_atr.mean()
        
        if pd.isna(avg_atr) or avg_atr <= 0:
            return True, ""
        
        # ATR 급등 검사
        atr_ratio = current_atr / avg_atr
        if atr_ratio > settings.ATR_SPIKE_THRESHOLD:
            return False, (
                f"ATR 급등 감지 (현재: {current_atr:,.0f}, "
                f"평균: {avg_atr:,.0f}, 비율: {atr_ratio:.1f}x > "
                f"임계값: {settings.ATR_SPIKE_THRESHOLD}x)"
            )
        
        return True, ""
    
    # ════════════════════════════════════════════════════════════════
    # 추세 판단
    # ════════════════════════════════════════════════════════════════
    
    def get_trend(self, df: pd.DataFrame) -> TrendType:
        """
        현재 추세를 판단합니다.
        
        판단 기준:
            - 종가 > 50일 MA → 상승 추세
            - 종가 < 50일 MA → 하락 추세
        
        Args:
            df: 지표가 계산된 데이터프레임
        
        Returns:
            TrendType: 현재 추세
        """
        if df.empty or len(df) < self.ma_period:
            return TrendType.SIDEWAYS
        
        latest = df.iloc[-1]
        
        if pd.isna(latest['ma']):
            return TrendType.SIDEWAYS
        
        if latest['close'] > latest['ma']:
            return TrendType.UPTREND
        else:
            return TrendType.DOWNTREND
    
    # ════════════════════════════════════════════════════════════════
    # 손절/익절 가격 계산
    # ════════════════════════════════════════════════════════════════
    
    def calculate_stop_loss(self, entry_price: float, atr: float) -> float:
        """
        손절가를 계산합니다.
        
        손절가 = 진입가 - (ATR * 손절 배수)
        단, 최대 손실 비율을 초과하지 않도록 제한합니다.
        
        Args:
            entry_price: 진입가
            atr: 현재 ATR 값
        
        Returns:
            float: 손절가
        """
        # ATR 기반 손절가
        atr_stop_loss = entry_price - (atr * self.atr_multiplier_sl)
        
        # 최대 손실 비율 기반 손절가
        max_loss_stop = entry_price * (1 - settings.MAX_LOSS_PCT / 100)
        
        # 둘 중 더 높은 값(손실이 작은 값) 선택
        stop_loss = max(atr_stop_loss, max_loss_stop)
        
        # 손절가가 제한된 경우 로깅
        if stop_loss > atr_stop_loss:
            logger.debug(
                f"손절가 제한 적용: ATR 기반 {atr_stop_loss:,.0f}원 → "
                f"최대손실 제한 {stop_loss:,.0f}원 (MAX_LOSS: {settings.MAX_LOSS_PCT}%)"
            )
        
        return max(0, stop_loss)  # 음수 방지
    
    def calculate_take_profit(self, entry_price: float, atr: float) -> float:
        """
        익절가를 계산합니다.
        
        익절가 = 진입가 + (ATR * 익절 배수)
        
        Args:
            entry_price: 진입가
            atr: 현재 ATR 값
        
        Returns:
            float: 익절가
        """
        return entry_price + (atr * self.atr_multiplier_tp)
    
    # ════════════════════════════════════════════════════════════════
    # 진입/청산 조건 판단
    # ════════════════════════════════════════════════════════════════
    
    def check_entry_condition(
        self,
        df: pd.DataFrame,
        current_price: float
    ) -> Tuple[bool, str]:
        """
        진입 조건을 확인합니다.
        
        진입 조건:
            1. 포지션 미보유
            2. 상승 추세 (종가 > MA50)
            3. 현재가 > 직전 캔들 고가 (돌파)
            4. ATR 정상 범위 (급등 아님)
            5. ADX > 임계값 (추세 강도 충분)
        
        Args:
            df: 지표가 계산된 데이터프레임
            current_price: 현재가
        
        Returns:
            Tuple[bool, str]: (진입 여부, 사유)
        """
        # 포지션 보유 중이면 진입 금지
        if self.position is not None:
            return False, "포지션 보유 중"
        
        if df.empty or len(df) < self.ma_period:
            return False, "데이터 부족"
        
        latest = df.iloc[-1]
        
        # ATR이 계산되지 않은 경우
        if pd.isna(latest['atr']) or latest['atr'] <= 0:
            return False, "ATR 미계산"
        
        # ATR 급등 검사
        atr_valid, atr_reason = self.is_atr_valid(df)
        if not atr_valid:
            return False, atr_reason
        
        # ADX(추세 강도) 검사 - 횡보장 필터
        adx = latest.get('adx', None)
        if adx is not None and not pd.isna(adx):
            if adx < settings.ADX_THRESHOLD:
                return False, (
                    f"추세 강도 부족 (ADX: {adx:.1f} < "
                    f"임계값: {settings.ADX_THRESHOLD})"
                )
        
        # 추세 확인
        trend = self.get_trend(df)
        if trend != TrendType.UPTREND:
            return False, f"하락/횡보 추세 ({trend.value})"
        
        # 직전 캔들 고가 돌파 확인
        prev_high = latest['prev_high']
        if pd.isna(prev_high):
            return False, "직전 고가 없음"
        
        if current_price <= prev_high:
            return False, f"돌파 미발생 (현재가: {current_price:,.0f} <= 직전고가: {prev_high:,.0f})"
        
        return True, f"상승 추세(ADX:{adx:.1f}) + 직전 고가({prev_high:,.0f}) 돌파"
    
    def check_exit_condition(
        self,
        current_price: float
    ) -> Tuple[bool, str]:
        """
        청산 조건을 확인합니다.
        
        청산 조건:
            1. 손절: 현재가 <= 손절가
            2. 익절: 현재가 >= 익절가
        
        Args:
            current_price: 현재가
        
        Returns:
            Tuple[bool, str]: (청산 여부, 사유)
        """
        if self.position is None:
            return False, "포지션 없음"
        
        # 손절 확인
        if current_price <= self.position.stop_loss:
            pnl_pct = ((current_price - self.position.entry_price) / 
                       self.position.entry_price * 100)
            return True, f"손절 도달 (손절가: {self.position.stop_loss:,.0f}, 손익: {pnl_pct:.2f}%)"
        
        # 익절 확인
        if current_price >= self.position.take_profit:
            pnl_pct = ((current_price - self.position.entry_price) / 
                       self.position.entry_price * 100)
            return True, f"익절 도달 (익절가: {self.position.take_profit:,.0f}, 손익: {pnl_pct:.2f}%)"
        
        return False, "청산 조건 미충족"
    
    # ════════════════════════════════════════════════════════════════
    # 시그널 생성
    # ════════════════════════════════════════════════════════════════
    
    def generate_signal(
        self,
        df: pd.DataFrame,
        current_price: float,
        stock_code: str = ""
    ) -> Signal:
        """
        매매 시그널을 생성합니다.
        
        Args:
            df: OHLCV 데이터프레임
            current_price: 현재가
            stock_code: 종목 코드
        
        Returns:
            Signal: 매매 시그널
        """
        # 지표 계산
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
        
        # 포지션 보유 중인 경우 청산 조건 확인
        if self.position is not None:
            should_exit, exit_reason = self.check_exit_condition(current_price)
            
            if should_exit:
                trade_logger.log_signal(
                    signal_type="SELL",
                    stock_code=stock_code,
                    price=current_price,
                    reason=exit_reason
                )
                return Signal(
                    signal_type=SignalType.SELL,
                    price=current_price,
                    stop_loss=self.position.stop_loss,
                    take_profit=self.position.take_profit,
                    reason=exit_reason,
                    atr=atr,
                    trend=trend
                )
            
            # 청산 조건 미충족 → HOLD
            return Signal(
                signal_type=SignalType.HOLD,
                price=current_price,
                stop_loss=self.position.stop_loss,
                take_profit=self.position.take_profit,
                reason="포지션 유지 중",
                atr=atr,
                trend=trend
            )
        
        # 포지션 미보유 시 진입 조건 확인
        should_enter, entry_reason = self.check_entry_condition(
            df_with_indicators, current_price
        )
        
        if should_enter:
            stop_loss = self.calculate_stop_loss(current_price, atr)
            take_profit = self.calculate_take_profit(current_price, atr)
            
            trade_logger.log_signal(
                signal_type="BUY",
                stock_code=stock_code,
                price=current_price,
                reason=entry_reason
            )
            
            return Signal(
                signal_type=SignalType.BUY,
                price=current_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reason=entry_reason,
                atr=atr,
                trend=trend
            )
        
        # 진입 조건 미충족 → HOLD
        return Signal(
            signal_type=SignalType.HOLD,
            price=current_price,
            reason=entry_reason,
            atr=atr,
            trend=trend
        )
    
    # ════════════════════════════════════════════════════════════════
    # 포지션 관리
    # ════════════════════════════════════════════════════════════════
    
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
        
        trade_logger.log_position(
            action="OPEN",
            stock_code=stock_code,
            entry_price=entry_price,
            current_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            pnl_pct=0.0
        )
        
        logger.info(
            f"포지션 오픈: {stock_code} @ {entry_price:,.0f}원, "
            f"수량: {quantity}주, 손절: {stop_loss:,.0f}원, 익절: {take_profit:,.0f}원"
        )
        
        return self.position
    
    def close_position(self, exit_price: float, reason: str = "") -> Optional[dict]:
        """
        포지션을 청산합니다.
        
        Args:
            exit_price: 청산가
            reason: 청산 사유
        
        Returns:
            dict: 청산 결과 (포지션이 없으면 None)
        """
        if self.position is None:
            logger.warning("청산할 포지션이 없습니다.")
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
        
        trade_logger.log_position(
            action="CLOSE",
            stock_code=self.position.stock_code,
            entry_price=self.position.entry_price,
            current_price=exit_price,
            stop_loss=self.position.stop_loss,
            take_profit=self.position.take_profit,
            pnl_pct=pnl_pct
        )
        
        logger.info(
            f"포지션 청산: {self.position.stock_code} @ {exit_price:,.0f}원, "
            f"손익: {pnl:,.0f}원 ({pnl_pct:+.2f}%), 사유: {reason}"
        )
        
        # 포지션 초기화
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
