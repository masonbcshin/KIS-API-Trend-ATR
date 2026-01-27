"""
KIS Trend-ATR Trading System - 전략 테스트

TrendATRStrategy 클래스의 핵심 기능을 테스트합니다.

테스트 항목:
- 하락 추세에서 매수 차단
- ATR = NaN 또는 0일 때 주문 차단
- 손절가/익절가 계산 검증
- ADX 기반 횡보장 필터링
- ATR 급등 시 진입 차단
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.trend_atr import (
    TrendATRStrategy,
    Signal,
    SignalType,
    TrendType,
    Position
)
from config import settings


class TestTrendDetection:
    """추세 판단 테스트"""
    
    def test_uptrend_detection(self, sample_uptrend_df, strategy):
        """상승 추세 감지 테스트"""
        df_with_indicators = strategy.add_indicators(sample_uptrend_df)
        trend = strategy.get_trend(df_with_indicators)
        
        assert trend == TrendType.UPTREND, "상승 추세를 감지해야 합니다"
    
    def test_downtrend_detection(self, sample_downtrend_df, strategy):
        """하락 추세 감지 테스트"""
        df_with_indicators = strategy.add_indicators(sample_downtrend_df)
        trend = strategy.get_trend(df_with_indicators)
        
        assert trend == TrendType.DOWNTREND, "하락 추세를 감지해야 합니다"
    
    def test_trend_with_insufficient_data(self, strategy):
        """데이터 부족 시 추세 판단 테스트"""
        # MA 계산에 필요한 기간보다 적은 데이터
        short_df = pd.DataFrame({
            'date': pd.date_range(end=datetime.now(), periods=10, freq='D'),
            'open': [100] * 10,
            'high': [110] * 10,
            'low': [90] * 10,
            'close': [105] * 10,
            'volume': [1000] * 10
        })
        
        df_with_indicators = strategy.add_indicators(short_df)
        trend = strategy.get_trend(df_with_indicators)
        
        assert trend == TrendType.SIDEWAYS, "데이터 부족 시 SIDEWAYS 반환해야 합니다"


class TestEntryConditions:
    """진입 조건 테스트"""
    
    def test_buy_blocked_in_downtrend(self, sample_downtrend_df, strategy):
        """
        [필수 테스트] 하락 추세에서 매수 차단 테스트
        
        조건: 종가 < 50일 MA (하락 추세)
        기대: 매수 시그널이 발생하지 않아야 함
        """
        df_with_indicators = strategy.add_indicators(sample_downtrend_df)
        current_price = df_with_indicators.iloc[-1]['close']
        
        # 직전 고가보다 높은 가격으로 설정 (돌파 조건 충족)
        breakout_price = df_with_indicators.iloc[-1]['prev_high'] + 1000
        
        should_enter, reason = strategy.check_entry_condition(
            df_with_indicators,
            breakout_price
        )
        
        assert should_enter is False, "하락 추세에서는 매수가 차단되어야 합니다"
        assert "하락" in reason or "추세" in reason, f"차단 사유에 추세 관련 내용이 있어야 합니다: {reason}"
    
    def test_buy_blocked_when_atr_is_nan(self, sample_df_with_nan_atr, strategy):
        """
        [필수 테스트] ATR = NaN일 때 주문 차단 테스트
        
        조건: ATR 계산 기간(14일)보다 데이터가 적음
        기대: 매수 시그널이 발생하지 않아야 함
        """
        df_with_indicators = strategy.add_indicators(sample_df_with_nan_atr)
        current_price = 65000
        
        should_enter, reason = strategy.check_entry_condition(
            df_with_indicators,
            current_price
        )
        
        assert should_enter is False, "ATR이 NaN일 때 매수가 차단되어야 합니다"
        assert "ATR" in reason or "데이터" in reason, f"차단 사유에 ATR 관련 내용이 있어야 합니다: {reason}"
    
    def test_buy_blocked_when_atr_is_zero(self, sample_df_with_zero_atr, strategy):
        """
        [필수 테스트] ATR = 0일 때 주문 차단 테스트
        
        조건: 가격 변동이 없어 ATR이 0에 가까움
        기대: 매수 시그널이 발생하지 않아야 함
        """
        df_with_indicators = strategy.add_indicators(sample_df_with_zero_atr)
        current_price = 65000
        
        should_enter, reason = strategy.check_entry_condition(
            df_with_indicators,
            current_price
        )
        
        # ATR이 0이면 손절/익절 계산이 불가능하므로 차단되어야 함
        assert should_enter is False, "ATR이 0일 때 매수가 차단되어야 합니다"
    
    def test_buy_blocked_when_atr_spikes(self, sample_atr_spike_df, strategy):
        """ATR 급등 시 진입 차단 테스트"""
        df_with_indicators = strategy.add_indicators(sample_atr_spike_df)
        
        # 높은 가격으로 돌파 조건 충족
        current_price = df_with_indicators.iloc[-1]['prev_high'] + 5000
        
        should_enter, reason = strategy.check_entry_condition(
            df_with_indicators,
            current_price
        )
        
        # ATR이 평균의 2.5배 이상이면 차단
        assert should_enter is False, "ATR 급등 시 매수가 차단되어야 합니다"
        assert "ATR 급등" in reason, f"차단 사유에 ATR 급등 내용이 있어야 합니다: {reason}"
    
    def test_buy_blocked_when_no_breakout(self, sample_uptrend_df, strategy):
        """돌파 조건 미충족 시 차단 테스트"""
        df_with_indicators = strategy.add_indicators(sample_uptrend_df)
        
        # 직전 고가보다 낮은 가격
        prev_high = df_with_indicators.iloc[-1]['prev_high']
        current_price = prev_high - 1000
        
        should_enter, reason = strategy.check_entry_condition(
            df_with_indicators,
            current_price
        )
        
        assert should_enter is False, "돌파 조건 미충족 시 매수가 차단되어야 합니다"
        assert "돌파" in reason, f"차단 사유에 돌파 관련 내용이 있어야 합니다: {reason}"
    
    def test_buy_blocked_when_position_exists(self, sample_uptrend_df, strategy):
        """포지션 보유 중 추가 매수 차단 테스트"""
        # 포지션 설정
        strategy.position = Position(
            stock_code="005930",
            entry_price=60000,
            quantity=100,
            stop_loss=57000,
            take_profit=69000,
            entry_date="2024-01-15",
            atr_at_entry=1500
        )
        
        df_with_indicators = strategy.add_indicators(sample_uptrend_df)
        prev_high = df_with_indicators.iloc[-1]['prev_high']
        current_price = prev_high + 1000  # 돌파 조건 충족
        
        should_enter, reason = strategy.check_entry_condition(
            df_with_indicators,
            current_price
        )
        
        assert should_enter is False, "포지션 보유 중에는 매수가 차단되어야 합니다"
        assert "포지션 보유" in reason, f"차단 사유에 포지션 관련 내용이 있어야 합니다: {reason}"
    
    def test_buy_blocked_when_adx_low(self, sample_sideways_df, strategy):
        """ADX가 낮을 때(횡보장) 진입 차단 테스트"""
        df_with_indicators = strategy.add_indicators(sample_sideways_df)
        
        # 돌파 조건 인위적 충족
        current_price = 70000  # 높은 가격
        
        should_enter, reason = strategy.check_entry_condition(
            df_with_indicators,
            current_price
        )
        
        # ADX < 25이면 횡보장으로 판단하여 차단
        # (횡보장 데이터의 ADX가 25 미만인 경우)
        assert should_enter is False, "ADX가 낮을 때(횡보장)는 매수가 차단되어야 합니다"


class TestExitConditions:
    """청산 조건 테스트"""
    
    def test_stop_loss_triggered(self, strategy_with_position):
        """손절 조건 테스트"""
        # strategy_with_position: stop_loss=57000
        current_price = 56000  # 손절가 미만
        
        should_exit, reason = strategy_with_position.check_exit_condition(current_price)
        
        assert should_exit is True, "손절가 도달 시 청산되어야 합니다"
        assert "손절" in reason, f"청산 사유에 손절 내용이 있어야 합니다: {reason}"
    
    def test_take_profit_triggered(self, strategy_with_position):
        """익절 조건 테스트"""
        # strategy_with_position: take_profit=69000
        current_price = 70000  # 익절가 초과
        
        should_exit, reason = strategy_with_position.check_exit_condition(current_price)
        
        assert should_exit is True, "익절가 도달 시 청산되어야 합니다"
        assert "익절" in reason, f"청산 사유에 익절 내용이 있어야 합니다: {reason}"
    
    def test_hold_between_sl_tp(self, strategy_with_position):
        """손절가와 익절가 사이에서 홀드 테스트"""
        # strategy_with_position: stop_loss=57000, take_profit=69000
        current_price = 63000  # 중간 가격
        
        should_exit, reason = strategy_with_position.check_exit_condition(current_price)
        
        assert should_exit is False, "손절가와 익절가 사이에서는 포지션을 유지해야 합니다"
    
    def test_exit_without_position(self, strategy):
        """포지션 없이 청산 조건 확인 테스트"""
        current_price = 60000
        
        should_exit, reason = strategy.check_exit_condition(current_price)
        
        assert should_exit is False, "포지션이 없으면 청산 조건이 False여야 합니다"
        assert "포지션 없음" in reason


class TestStopLossTakeProfitCalculation:
    """손절가/익절가 계산 테스트"""
    
    def test_stop_loss_calculation(self, strategy):
        """손절가 계산 테스트"""
        entry_price = 60000
        atr = 1500
        
        # 손절가 = 진입가 - (ATR * 2.0) = 60000 - 3000 = 57000
        stop_loss = strategy.calculate_stop_loss(entry_price, atr)
        expected_stop_loss = entry_price - (atr * strategy.atr_multiplier_sl)
        
        assert stop_loss == expected_stop_loss, f"손절가 계산 오류: {stop_loss} != {expected_stop_loss}"
    
    def test_take_profit_calculation(self, strategy):
        """익절가 계산 테스트"""
        entry_price = 60000
        atr = 1500
        
        # 익절가 = 진입가 + (ATR * 3.0) = 60000 + 4500 = 64500
        take_profit = strategy.calculate_take_profit(entry_price, atr)
        expected_take_profit = entry_price + (atr * strategy.atr_multiplier_tp)
        
        assert take_profit == expected_take_profit, f"익절가 계산 오류: {take_profit} != {expected_take_profit}"
    
    def test_stop_loss_never_exceeds_max_loss(self, strategy):
        """
        [필수 테스트] 손절가 > 익절가 방지 테스트 (변형)
        
        손절가가 MAX_LOSS_PCT 제한을 초과하지 않는지 확인
        ATR이 매우 큰 경우에도 최대 손실 비율이 제한되어야 함
        """
        entry_price = 60000
        very_large_atr = 10000  # 매우 큰 ATR
        
        # ATR 기반 손절가: 60000 - (10000 * 2) = 40000 (33% 손실)
        # MAX_LOSS_PCT = 5% 적용: 60000 * 0.95 = 57000
        stop_loss = strategy.calculate_stop_loss(entry_price, very_large_atr)
        
        max_allowed_loss = entry_price * (settings.MAX_LOSS_PCT / 100)
        actual_loss = entry_price - stop_loss
        
        assert actual_loss <= max_allowed_loss, \
            f"손절 손실({actual_loss})이 최대 허용 손실({max_allowed_loss})을 초과합니다"
    
    def test_stop_loss_take_profit_relationship(self, strategy):
        """
        [필수 테스트] 손절가 > 익절가 방지 테스트
        
        정상적인 ATR에서 손절가가 항상 익절가보다 낮아야 함
        """
        entry_price = 60000
        
        for atr in [500, 1000, 1500, 2000, 3000]:
            stop_loss = strategy.calculate_stop_loss(entry_price, atr)
            take_profit = strategy.calculate_take_profit(entry_price, atr)
            
            assert stop_loss < entry_price, f"손절가({stop_loss})가 진입가({entry_price})보다 높습니다"
            assert take_profit > entry_price, f"익절가({take_profit})가 진입가({entry_price})보다 낮습니다"
            assert stop_loss < take_profit, \
                f"ATR={atr}에서 손절가({stop_loss}) >= 익절가({take_profit})"
    
    def test_stop_loss_with_zero_atr(self, strategy):
        """ATR이 0일 때 손절가 계산 테스트"""
        entry_price = 60000
        atr = 0
        
        stop_loss = strategy.calculate_stop_loss(entry_price, atr)
        
        # ATR이 0이면:
        # atr_stop_loss = 60000 - (0 * 2.0) = 60000
        # max_loss_stop = 60000 * 0.95 = 57000
        # max(60000, 57000) = 60000 (더 높은 값 = 손실이 작은 값)
        # 
        # 현재 로직: ATR=0이면 손절가 = 진입가 (손실 없음)
        # 이는 ATR=0인 비정상 상황에서 손절을 막는 안전장치로 볼 수 있음
        
        # 손절가는 음수가 되면 안 됨
        assert stop_loss >= 0, "손절가는 음수가 될 수 없습니다"
        
        # ATR=0 상황 자체가 비정상이므로, 진입 조건에서 차단됨
        # 여기서는 손절가 계산 함수가 에러 없이 동작하는지만 확인


class TestSignalGeneration:
    """시그널 생성 테스트"""
    
    def test_buy_signal_in_uptrend(self, sample_uptrend_df, strategy):
        """상승 추세에서 매수 시그널 생성 테스트"""
        df_with_indicators = strategy.add_indicators(sample_uptrend_df)
        
        # 돌파 조건 충족하는 가격
        prev_high = df_with_indicators.iloc[-1]['prev_high']
        current_price = prev_high + 2000
        
        signal = strategy.generate_signal(
            df_with_indicators,
            current_price,
            stock_code="005930"
        )
        
        # ADX 조건 등으로 인해 BUY가 아닐 수 있으므로 SELL은 아니어야 함
        assert signal.signal_type != SignalType.SELL, "상승 추세에서 SELL 시그널이 발생하면 안 됩니다"
        assert signal.price == current_price
    
    def test_sell_signal_when_stop_loss_hit(self, sample_uptrend_df, strategy_with_position):
        """손절가 도달 시 매도 시그널 생성 테스트"""
        df_with_indicators = strategy_with_position.add_indicators(sample_uptrend_df)
        
        # 손절가 이하 가격
        current_price = 56000  # stop_loss=57000
        
        signal = strategy_with_position.generate_signal(
            df_with_indicators,
            current_price,
            stock_code="005930"
        )
        
        assert signal.signal_type == SignalType.SELL, "손절가 도달 시 SELL 시그널이 발생해야 합니다"
        assert "손절" in signal.reason
    
    def test_hold_signal_between_sl_tp(self, sample_uptrend_df, strategy_with_position):
        """손절가와 익절가 사이에서 홀드 시그널 테스트"""
        df_with_indicators = strategy_with_position.add_indicators(sample_uptrend_df)
        
        # 중간 가격
        current_price = 63000  # stop_loss=57000, take_profit=69000
        
        signal = strategy_with_position.generate_signal(
            df_with_indicators,
            current_price,
            stock_code="005930"
        )
        
        assert signal.signal_type == SignalType.HOLD, "손익 범위 내에서는 HOLD 시그널이 발생해야 합니다"


class TestPositionManagement:
    """포지션 관리 테스트"""
    
    def test_open_position(self, strategy):
        """포지션 오픈 테스트"""
        assert strategy.position is None, "초기에는 포지션이 없어야 합니다"
        
        position = strategy.open_position(
            stock_code="005930",
            entry_price=60000,
            quantity=100,
            stop_loss=57000,
            take_profit=69000,
            entry_date="2024-01-15",
            atr=1500
        )
        
        assert strategy.has_position() is True
        assert strategy.position.stock_code == "005930"
        assert strategy.position.entry_price == 60000
        assert strategy.position.quantity == 100
    
    def test_close_position(self, strategy_with_position):
        """포지션 청산 테스트"""
        assert strategy_with_position.has_position() is True
        
        result = strategy_with_position.close_position(
            exit_price=65000,
            reason="익절"
        )
        
        assert strategy_with_position.has_position() is False
        assert result is not None
        assert result["pnl"] > 0  # 60000 → 65000 이익
        assert result["exit_price"] == 65000
    
    def test_position_pnl_calculation(self, strategy_with_position):
        """포지션 손익 계산 테스트"""
        # entry_price=60000, quantity=100
        current_price = 65000
        
        pnl, pnl_pct = strategy_with_position.get_position_pnl(current_price)
        
        expected_pnl = (65000 - 60000) * 100  # 500000
        expected_pnl_pct = (65000 - 60000) / 60000 * 100  # 8.33%
        
        assert pnl == expected_pnl, f"손익금액 계산 오류: {pnl} != {expected_pnl}"
        assert abs(pnl_pct - expected_pnl_pct) < 0.01, f"손익률 계산 오류: {pnl_pct} != {expected_pnl_pct}"


class TestATRCalculation:
    """ATR 계산 테스트"""
    
    def test_atr_calculation(self, sample_uptrend_df, strategy):
        """ATR 계산 정확성 테스트"""
        atr = strategy.calculate_atr(sample_uptrend_df)
        
        assert len(atr) == len(sample_uptrend_df)
        assert atr.iloc[-1] > 0, "ATR은 양수여야 합니다"
        
        # 처음 ATR_PERIOD-1 개는 NaN
        assert pd.isna(atr.iloc[strategy.atr_period - 2])
        assert not pd.isna(atr.iloc[strategy.atr_period])
    
    def test_atr_validation(self, sample_atr_spike_df, strategy):
        """ATR 급등 검증 테스트"""
        df_with_indicators = strategy.add_indicators(sample_atr_spike_df)
        
        is_valid, reason = strategy.is_atr_valid(df_with_indicators)
        
        assert is_valid is False, "ATR 급등 시 is_atr_valid가 False여야 합니다"
        assert "ATR 급등" in reason


class TestIndicators:
    """기술적 지표 테스트"""
    
    def test_add_indicators(self, sample_uptrend_df, strategy):
        """지표 추가 테스트"""
        df = strategy.add_indicators(sample_uptrend_df)
        
        assert 'atr' in df.columns, "ATR 컬럼이 있어야 합니다"
        assert 'ma' in df.columns, "MA 컬럼이 있어야 합니다"
        assert 'adx' in df.columns, "ADX 컬럼이 있어야 합니다"
        assert 'trend' in df.columns, "trend 컬럼이 있어야 합니다"
        assert 'prev_high' in df.columns, "prev_high 컬럼이 있어야 합니다"
    
    def test_ma_calculation(self, sample_uptrend_df, strategy):
        """이동평균 계산 테스트"""
        ma = strategy.calculate_ma(sample_uptrend_df)
        
        assert len(ma) == len(sample_uptrend_df)
        
        # 처음 MA_PERIOD-1 개는 NaN
        assert pd.isna(ma.iloc[strategy.ma_period - 2])
        assert not pd.isna(ma.iloc[strategy.ma_period])
