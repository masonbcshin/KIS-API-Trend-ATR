"""
KIS Trend-ATR Trading System - 멀티데이 Trend-ATR 전략

★ 전략의 본질:
    - 당일 매수·당일 매도(Day Trading)가 아닌
    - 익절 또는 손절 신호가 발생할 때까지 보유(Hold until Exit) 하는 구조

★ 절대 금지 사항:
    - ❌ 장 마감(EOD) 시간 기준 강제 청산 로직
    - ❌ "장이 끝났으니 판다"라는 시간 기반 종료 조건
    - ❌ 익일 ATR 재계산으로 손절선 변경

★ Exit 조건 (유일하게 허용된 청산 사유):
    - ATR 기반 손절 (진입 시 고정된 ATR 사용)
    - ATR 기반 트레일링 스탑
    - 추세 붕괴 신호 (선택적)
    - 갭 보호 (선택적, 시가가 손절가보다 크게 불리할 때)
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, Dict, Any
from datetime import datetime
import pandas as pd
import numpy as np

from config import settings
from utils.logger import get_logger, TradeLogger
from utils.market_hours import KST
from engine.trading_state import (
    TradingState, 
    TradingStateMachine, 
    MultidayPosition, 
    ExitReason
)

logger = get_logger("multiday_strategy")
trade_logger = TradeLogger("multiday_strategy")


class SignalType(Enum):
    """매매 시그널 타입"""
    BUY = "BUY"       # 진입 시그널
    SELL = "SELL"     # 청산 시그널
    HOLD = "HOLD"     # 관망 (변화 없음)


class TrendType(Enum):
    """추세 타입"""
    UPTREND = "UPTREND"       # 상승 추세
    DOWNTREND = "DOWNTREND"   # 하락 추세
    SIDEWAYS = "SIDEWAYS"     # 횡보


@dataclass
class TradingSignal:
    """
    매매 시그널 데이터 클래스
    
    Attributes:
        signal_type: 시그널 타입
        price: 시그널 발생 시 가격
        stop_loss: 손절가 (진입 시 ATR 기반, 고정)
        take_profit: 익절가 (선택적)
        trailing_stop: 현재 트레일링 스탑 가격
        exit_reason: 청산 사유 (SELL 시그널인 경우)
        reason: 상세 설명
        atr: 진입 시 ATR (고정)
        trend: 현재 추세
        near_stop_loss_pct: 손절선 근접 비율 (%)
        near_take_profit_pct: 익절선 근접 비율 (%)
    """
    signal_type: SignalType
    price: float
    stop_loss: float = 0.0
    take_profit: Optional[float] = None
    trailing_stop: float = 0.0
    exit_reason: Optional[ExitReason] = None
    reason: str = ""
    atr: float = 0.0
    trend: TrendType = TrendType.SIDEWAYS
    near_stop_loss_pct: float = 0.0
    near_take_profit_pct: float = 0.0


class MultidayTrendATRStrategy:
    """
    멀티데이 Trend-ATR 전략 클래스
    
    ★ 핵심 원칙:
        1. EOD 청산 로직 절대 없음
        2. Exit는 오직 가격 조건으로만 발생
        3. ATR은 진입 시 고정, 익일 재계산 금지
    
    전략 규칙:
        - 진입: 상승 추세 + 직전 캔들 고가 돌파
        - 손절: 진입가 - (진입ATR * 손절배수)  [고정]
        - 익절: 진입가 + (진입ATR * 익절배수) 또는 트레일링 스탑
        - 트레일링: 최고가 - (진입ATR * 트레일링배수)
    """
    
    def __init__(
        self,
        atr_period: int = None,
        ma_period: int = None,
        atr_multiplier_sl: float = None,
        atr_multiplier_tp: float = None,
        trailing_stop_multiplier: float = None,
        enable_trailing_stop: bool = None,
        trailing_activation_pct: float = None,
        enable_gap_protection: bool = None,
        max_gap_loss_pct: float = None,
        alert_near_sl_pct: float = None,
        alert_near_tp_pct: float = None
    ):
        """
        전략 초기화
        
        Args:
            atr_period: ATR 계산 기간
            ma_period: 추세 판단용 이동평균 기간
            atr_multiplier_sl: 손절 ATR 배수
            atr_multiplier_tp: 익절 ATR 배수
            trailing_stop_multiplier: 트레일링 스탑 ATR 배수
            enable_trailing_stop: 트레일링 스탑 활성화
            trailing_activation_pct: 트레일링 스탑 활성화 수익률
            enable_gap_protection: 갭 보호 활성화
            max_gap_loss_pct: 최대 갭 손실 허용률
            alert_near_sl_pct: 손절선 근접 알림 비율
            alert_near_tp_pct: 익절선 근접 알림 비율
        """
        # ATR 관련 설정
        self.atr_period = atr_period or settings.ATR_PERIOD
        self.ma_period = ma_period or settings.TREND_MA_PERIOD
        self.atr_multiplier_sl = atr_multiplier_sl or settings.ATR_MULTIPLIER_SL
        self.atr_multiplier_tp = atr_multiplier_tp or settings.ATR_MULTIPLIER_TP
        
        # 트레일링 스탑 설정
        self.trailing_stop_multiplier = trailing_stop_multiplier or settings.TRAILING_STOP_ATR_MULTIPLIER
        self.enable_trailing_stop = enable_trailing_stop if enable_trailing_stop is not None else settings.ENABLE_TRAILING_STOP
        self.trailing_activation_pct = trailing_activation_pct or settings.TRAILING_STOP_ACTIVATION_PCT
        
        # 갭 보호 설정
        self.enable_gap_protection = enable_gap_protection if enable_gap_protection is not None else settings.ENABLE_GAP_PROTECTION
        self.max_gap_loss_pct = max_gap_loss_pct or settings.MAX_GAP_LOSS_PCT
        
        # 알림 설정
        self.alert_near_sl_pct = alert_near_sl_pct or settings.ALERT_NEAR_STOPLOSS_PCT
        self.alert_near_tp_pct = alert_near_tp_pct or settings.ALERT_NEAR_TAKEPROFIT_PCT
        
        # 상태 머신
        self._state_machine = TradingStateMachine()
        
        logger.info(
            f"멀티데이 전략 초기화: "
            f"ATR({self.atr_period}), MA({self.ma_period}), "
            f"SL({self.atr_multiplier_sl}x), TP({self.atr_multiplier_tp}x), "
            f"Trailing({'ON' if self.enable_trailing_stop else 'OFF'})"
        )
    
    # ════════════════════════════════════════════════════════════════
    # 속성
    # ════════════════════════════════════════════════════════════════
    
    @property
    def state(self) -> TradingState:
        """현재 트레이딩 상태"""
        return self._state_machine.state
    
    @property
    def position(self) -> Optional[MultidayPosition]:
        """현재 포지션"""
        return self._state_machine.position
    
    @property
    def has_position(self) -> bool:
        """포지션 보유 여부"""
        return self._state_machine.has_position
    
    # ════════════════════════════════════════════════════════════════
    # 기술적 지표 계산
    # ════════════════════════════════════════════════════════════════
    
    def calculate_atr(self, df: pd.DataFrame) -> pd.Series:
        """
        ATR(Average True Range) 계산
        
        ★ 주의: 이 함수는 신규 진입 시에만 사용
        ★ 포지션 보유 중에는 진입 시 ATR 값을 그대로 사용
        """
        high = df['high']
        low = df['low']
        close = df['close']
        
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = true_range.rolling(window=self.atr_period).mean()
        
        return atr
    
    def calculate_ma(self, df: pd.DataFrame) -> pd.Series:
        """이동평균 계산"""
        return df['close'].rolling(window=self.ma_period).mean()
    
    def calculate_adx(self, df: pd.DataFrame, period: int = None) -> pd.Series:
        """ADX(추세 강도) 계산"""
        period = period or settings.ADX_PERIOD
        
        high = df['high']
        low = df['low']
        close = df['close']
        
        plus_dm = high.diff()
        minus_dm = -low.diff()
        
        plus_dm = np.where(
            (plus_dm > minus_dm) & (plus_dm > 0),
            plus_dm, 0
        )
        minus_dm = np.where(
            (minus_dm > plus_dm) & (minus_dm > 0),
            minus_dm, 0
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
        
        adx = dx.ewm(alpha=1/period, adjust=False).mean()
        
        return adx
    
    def add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """데이터프레임에 지표 추가"""
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
    
    # ════════════════════════════════════════════════════════════════
    # 추세 판단
    # ════════════════════════════════════════════════════════════════
    
    def get_trend(self, df: pd.DataFrame) -> TrendType:
        """현재 추세 판단"""
        if df.empty or len(df) < self.ma_period:
            return TrendType.SIDEWAYS
        
        latest = df.iloc[-1]
        
        if pd.isna(latest.get('ma', None)):
            return TrendType.SIDEWAYS
        
        if latest['close'] > latest['ma']:
            return TrendType.UPTREND
        else:
            return TrendType.DOWNTREND
    
    def detect_trend_reversal(self, df: pd.DataFrame) -> Tuple[bool, str]:
        """
        추세 붕괴 감지
        
        ★ 선택적 Exit 조건
        
        Returns:
            Tuple[bool, str]: (추세 붕괴 여부, 설명)
        """
        if len(df) < 3:
            return False, ""
        
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        # 상승 추세에서 MA 하향 돌파
        if prev['close'] > prev.get('ma', 0) and latest['close'] < latest.get('ma', 0):
            return True, "MA 하향 돌파 (추세 붕괴)"
        
        # ADX 급락 (추세 약화)
        if latest.get('adx', 30) < 20 and prev.get('adx', 30) >= 25:
            return True, "ADX 급락 (추세 약화)"
        
        return False, ""
    
    # ════════════════════════════════════════════════════════════════
    # 손절/익절 가격 계산
    # ════════════════════════════════════════════════════════════════
    
    def calculate_stop_loss(self, entry_price: float, atr: float) -> float:
        """
        손절가 계산
        
        ★ 진입 시점의 ATR 사용 (고정)
        ★ 이후 재계산 금지
        """
        atr_stop = entry_price - (atr * self.atr_multiplier_sl)
        max_loss_stop = entry_price * (1 - settings.MAX_LOSS_PCT / 100)
        
        return max(atr_stop, max_loss_stop, 0)
    
    def calculate_take_profit(self, entry_price: float, atr: float) -> float:
        """
        익절가 계산
        
        ★ 진입 시점의 ATR 사용 (고정)
        """
        return entry_price + (atr * self.atr_multiplier_tp)
    
    def calculate_trailing_stop(
        self, 
        highest_price: float, 
        atr_at_entry: float
    ) -> float:
        """
        트레일링 스탑 계산
        
        ★ 진입 시점의 ATR 사용 (고정)
        ★ highest_price만 갱신됨
        
        Args:
            highest_price: 포지션 보유 중 최고가
            atr_at_entry: 진입 시 ATR (고정)
        
        Returns:
            float: 트레일링 스탑 가격
        """
        return highest_price - (atr_at_entry * self.trailing_stop_multiplier)
    
    # ════════════════════════════════════════════════════════════════
    # Exit 조건 체크 (핵심)
    # ════════════════════════════════════════════════════════════════
    
    def check_exit_conditions(
        self,
        current_price: float,
        open_price: Optional[float] = None,
        df: pd.DataFrame = None
    ) -> Tuple[bool, Optional[ExitReason], str]:
        """
        Exit 조건 체크
        
        ★ 이 함수가 유일한 청산 판단 기준
        ★ EOD 청산 로직 절대 없음
        
        Exit 우선순위:
            1. 갭 보호 (시가가 손절가보다 크게 불리)
            2. ATR 손절 (현재가 <= 손절가)
            3. ATR 익절 (현재가 >= 익절가)
            4. 트레일링 스탑 (현재가 <= 트레일링스탑)
            5. 추세 붕괴 (선택적)
        
        Args:
            current_price: 현재가
            open_price: 당일 시가 (갭 체크용)
            df: 시장 데이터 (추세 체크용)
        
        Returns:
            Tuple[bool, ExitReason, str]: (청산 여부, 사유, 설명)
        """
        if not self.has_position:
            return False, None, "포지션 없음"
        
        pos = self.position
        
        # 1. 갭 보호 체크 (옵션)
        if self.enable_gap_protection and open_price:
            gap_loss_pct = (pos.entry_price - open_price) / pos.entry_price * 100
            if gap_loss_pct >= self.max_gap_loss_pct:
                return True, ExitReason.GAP_PROTECTION, (
                    f"갭 하락 보호 발동: 시가 {open_price:,.0f}원 "
                    f"(손실 {gap_loss_pct:.1f}% >= {self.max_gap_loss_pct}%)"
                )
        
        # 2. ATR 손절 체크
        if current_price <= pos.stop_loss:
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100
            return True, ExitReason.ATR_STOP_LOSS, (
                f"ATR 손절 도달: {current_price:,.0f}원 <= {pos.stop_loss:,.0f}원 "
                f"(손익 {pnl_pct:.2f}%)"
            )
        
        # 3. ATR 익절 체크
        if pos.take_profit and current_price >= pos.take_profit:
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100
            return True, ExitReason.ATR_TAKE_PROFIT, (
                f"ATR 익절 도달: {current_price:,.0f}원 >= {pos.take_profit:,.0f}원 "
                f"(손익 {pnl_pct:+.2f}%)"
            )
        
        # 4. 트레일링 스탑 체크
        if self.enable_trailing_stop:
            # 수익률이 활성화 기준 이상일 때만 체크
            current_pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100
            
            if current_pnl_pct >= self.trailing_activation_pct:
                # 최고가 갱신
                if current_price > pos.highest_price:
                    pos.update_highest_price(current_price)
                    new_trailing = self.calculate_trailing_stop(
                        pos.highest_price, 
                        pos.atr_at_entry
                    )
                    pos.update_trailing_stop(new_trailing)
                
                # 트레일링 스탑 도달 체크
                if pos.trailing_stop > 0 and current_price <= pos.trailing_stop:
                    pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100
                    return True, ExitReason.TRAILING_STOP, (
                        f"트레일링 스탑 도달: {current_price:,.0f}원 <= {pos.trailing_stop:,.0f}원 "
                        f"(최고가 {pos.highest_price:,.0f}원, 손익 {pnl_pct:+.2f}%)"
                    )
        
        # 5. 추세 붕괴 체크 (선택적)
        if df is not None and len(df) > 0:
            trend_broken, trend_reason = self.detect_trend_reversal(df)
            if trend_broken:
                pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100
                return True, ExitReason.TREND_BROKEN, (
                    f"추세 붕괴: {trend_reason} (손익 {pnl_pct:+.2f}%)"
                )
        
        return False, None, "Exit 조건 미충족"
    
    # ════════════════════════════════════════════════════════════════
    # 진입 조건 체크
    # ════════════════════════════════════════════════════════════════
    
    def check_entry_conditions(
        self,
        df: pd.DataFrame,
        current_price: float
    ) -> Tuple[bool, str, float]:
        """
        진입 조건 체크
        
        Args:
            df: 지표가 계산된 데이터프레임
            current_price: 현재가
        
        Returns:
            Tuple[bool, str, float]: (진입 여부, 사유, ATR)
        """
        # 이미 포지션 보유 중
        if self.has_position:
            return False, "포지션 보유 중 - 신규 진입 불가", 0.0
        
        if df.empty or len(df) < self.ma_period:
            return False, "데이터 부족", 0.0
        
        latest = df.iloc[-1]
        
        # ATR 확인
        atr = latest.get('atr', 0)
        if pd.isna(atr) or atr <= 0:
            return False, "ATR 미계산", 0.0
        
        # ATR 급등 검사
        atr_valid, atr_reason = self._check_atr_validity(df)
        if not atr_valid:
            return False, atr_reason, atr
        
        # ADX 검사
        adx = latest.get('adx', 0)
        if adx < settings.ADX_THRESHOLD:
            return False, f"추세 강도 부족 (ADX: {adx:.1f})", atr
        
        # 추세 확인
        trend = self.get_trend(df)
        if trend != TrendType.UPTREND:
            return False, f"하락/횡보 추세 ({trend.value})", atr
        
        # 직전 고가 돌파 확인
        prev_high = latest.get('prev_high', 0)
        if pd.isna(prev_high) or prev_high <= 0:
            return False, "직전 고가 없음", atr
        
        if current_price <= prev_high:
            return False, f"돌파 미발생 ({current_price:,.0f} <= {prev_high:,.0f})", atr
        
        # 이벤트 리스크 체크
        if settings.ENABLE_EVENT_RISK_CHECK:
            today = datetime.now().strftime("%Y-%m-%d")
            if today in settings.HIGH_RISK_EVENT_DATES:
                return False, f"고위험 이벤트일 ({today})", atr
        
        return True, f"상승 추세(ADX:{adx:.1f}) + 직전 고가({prev_high:,.0f}) 돌파", atr
    
    def _check_atr_validity(self, df: pd.DataFrame) -> Tuple[bool, str]:
        """ATR 유효성 검사"""
        min_periods = self.atr_period * 2
        if len(df) < min_periods:
            return True, ""
        
        current_atr = df.iloc[-1].get('atr', 0)
        if pd.isna(current_atr):
            return True, ""
        
        recent_atr = df['atr'].iloc[-min_periods:-1]
        avg_atr = recent_atr.mean()
        
        if pd.isna(avg_atr) or avg_atr <= 0:
            return True, ""
        
        atr_ratio = current_atr / avg_atr
        if atr_ratio > settings.ATR_SPIKE_THRESHOLD:
            return False, f"ATR 급등 ({atr_ratio:.1f}x > {settings.ATR_SPIKE_THRESHOLD}x)"
        
        return True, ""
    
    # ════════════════════════════════════════════════════════════════
    # 시그널 생성 (메인 로직)
    # ════════════════════════════════════════════════════════════════
    
    def generate_signal(
        self,
        df: pd.DataFrame,
        current_price: float,
        open_price: Optional[float] = None,
        stock_code: str = ""
    ) -> TradingSignal:
        """
        매매 시그널 생성
        
        ★ 핵심 로직:
            1. 포지션 보유 중 → Exit 조건만 체크 (신규 진입 불가)
            2. 포지션 미보유 → Entry 조건 체크
            3. EOD 청산 로직 절대 없음
        
        Args:
            df: OHLCV 데이터프레임
            current_price: 현재가
            open_price: 당일 시가 (갭 체크용)
            stock_code: 종목 코드
        
        Returns:
            TradingSignal: 매매 시그널
        """
        # 지표 계산
        df_with_indicators = self.add_indicators(df)
        
        if df_with_indicators.empty:
            return TradingSignal(
                signal_type=SignalType.HOLD,
                price=current_price,
                reason="데이터 없음"
            )
        
        latest = df_with_indicators.iloc[-1]
        current_atr = latest.get('atr', 0) if not pd.isna(latest.get('atr', 0)) else 0
        trend = self.get_trend(df_with_indicators)
        
        # ════════════════════════════════════════════════════════════
        # 포지션 보유 중 → Exit 조건만 체크
        # ════════════════════════════════════════════════════════════
        if self.has_position:
            pos = self.position
            
            # 최고가 갱신 (트레일링 스탑용)
            if self.enable_trailing_stop and current_price > pos.highest_price:
                pos.update_highest_price(current_price)
                new_trailing = self.calculate_trailing_stop(
                    pos.highest_price, pos.atr_at_entry
                )
                pos.update_trailing_stop(new_trailing)
            
            # Exit 조건 체크
            should_exit, exit_reason, exit_desc = self.check_exit_conditions(
                current_price=current_price,
                open_price=open_price,
                df=df_with_indicators
            )
            
            if should_exit:
                trade_logger.log_signal(
                    signal_type="SELL",
                    stock_code=stock_code,
                    price=current_price,
                    reason=exit_desc
                )
                
                return TradingSignal(
                    signal_type=SignalType.SELL,
                    price=current_price,
                    stop_loss=pos.stop_loss,
                    take_profit=pos.take_profit,
                    trailing_stop=pos.trailing_stop,
                    exit_reason=exit_reason,
                    reason=exit_desc,
                    atr=pos.atr_at_entry,
                    trend=trend,
                    near_stop_loss_pct=pos.get_distance_to_stop_loss(current_price),
                    near_take_profit_pct=pos.get_distance_to_take_profit(current_price)
                )
            
            # Exit 조건 미충족 → HOLD
            near_sl = pos.get_distance_to_stop_loss(current_price)
            near_tp = pos.get_distance_to_take_profit(current_price)
            
            return TradingSignal(
                signal_type=SignalType.HOLD,
                price=current_price,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                trailing_stop=pos.trailing_stop,
                reason="포지션 유지 중 (Exit 조건 미충족)",
                atr=pos.atr_at_entry,
                trend=trend,
                near_stop_loss_pct=near_sl,
                near_take_profit_pct=near_tp
            )
        
        # ════════════════════════════════════════════════════════════
        # 포지션 미보유 → Entry 조건 체크
        # ════════════════════════════════════════════════════════════
        can_enter, entry_reason, entry_atr = self.check_entry_conditions(
            df_with_indicators, current_price
        )
        
        if can_enter:
            stop_loss = self.calculate_stop_loss(current_price, entry_atr)
            take_profit = self.calculate_take_profit(current_price, entry_atr)
            
            trade_logger.log_signal(
                signal_type="BUY",
                stock_code=stock_code,
                price=current_price,
                reason=entry_reason
            )
            
            return TradingSignal(
                signal_type=SignalType.BUY,
                price=current_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop=stop_loss,  # 초기 트레일링 = 손절가
                reason=entry_reason,
                atr=entry_atr,
                trend=trend
            )
        
        # Entry 조건 미충족 → HOLD
        return TradingSignal(
            signal_type=SignalType.HOLD,
            price=current_price,
            reason=entry_reason,
            atr=current_atr,
            trend=trend
        )
    
    # ════════════════════════════════════════════════════════════════
    # 포지션 관리
    # ════════════════════════════════════════════════════════════════
    
    def open_position(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        atr: float,
        stop_loss: float,
        take_profit: Optional[float] = None
    ) -> MultidayPosition:
        """
        포지션 오픈
        
        ★ ATR은 이 시점에 고정됨
        ★ 이후 재계산 절대 금지
        """
        position = self._state_machine.enter_position(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            atr_at_entry=atr,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop=stop_loss
        )
        
        trade_logger.log_position(
            action="OPEN",
            stock_code=symbol,
            entry_price=entry_price,
            current_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit or 0,
            pnl_pct=0.0
        )

        def _fmt_num(value: Any) -> str:
            """로그 포맷 안전화: 숫자 변환 실패 시 원본 문자열 반환."""
            try:
                return f"{float(value):,.0f}"
            except (TypeError, ValueError):
                return str(value)

        tp_display = "Trailing Only"
        if take_profit is not None:
            tp_display = _fmt_num(take_profit)

        logger.info(
            f"[MULTIDAY] 포지션 오픈: {symbol} @ {_fmt_num(entry_price)}원, "
            f"ATR={_fmt_num(atr)} (고정), SL={_fmt_num(stop_loss)}원, "
            f"TP={tp_display}"
        )
        
        return position
    
    def close_position(
        self, 
        exit_price: float, 
        reason: ExitReason
    ) -> Optional[Dict[str, Any]]:
        """
        포지션 청산
        
        ★ 허용된 ExitReason만 사용 가능
        ★ EOD 청산 사유 없음
        """
        if not self.has_position:
            logger.warning("[MULTIDAY] 청산할 포지션 없음")
            return None
        
        pos = self.position
        result = self._state_machine.exit_position(reason, exit_price)
        
        trade_logger.log_position(
            action="CLOSE",
            stock_code=result["symbol"],
            entry_price=result["entry_price"],
            current_price=exit_price,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit or 0,
            pnl_pct=result["pnl_pct"]
        )
        
        logger.info(
            f"[MULTIDAY] 포지션 청산: {result['symbol']} @ {exit_price:,.0f}원, "
            f"손익={result['pnl']:+,.0f}원 ({result['pnl_pct']:+.2f}%), "
            f"사유={reason.value}, 보유일수={result['holding_days']}일"
        )
        
        return result
    
    def restore_position(self, position: MultidayPosition) -> None:
        """
        포지션 복원 (프로그램 재시작 시)
        
        ★ 저장된 값 그대로 사용
        ★ ATR 재계산 절대 금지
        """
        self._state_machine.restore_position(position)
        
        logger.info(
            f"[MULTIDAY] 포지션 복원: {position.symbol} @ {position.entry_price:,.0f}원, "
            f"ATR={position.atr_at_entry:,.0f} (고정값), "
            f"SL={position.stop_loss:,.0f}, 진입일={position.entry_date}"
        )
    
    def reset_to_wait(self) -> None:
        """WAIT 상태로 리셋"""
        self._state_machine.reset_to_wait()
    
    def get_position_pnl(self, current_price: float) -> Tuple[float, float]:
        """현재 포지션 손익 계산"""
        if not self.has_position:
            return 0.0, 0.0
        return self.position.get_pnl(current_price)
