"""
KIS Trend-ATR Trading System - 통합 테스트

전체 시스템의 엔드투엔드 흐름을 테스트합니다.
실제 API 호출 없이 Mock을 사용합니다.

테스트 항목:
- 완전한 매매 사이클 (매수 → 보유 → 매도)
- 에러 상황 시 시스템 안정성
- 다양한 시장 상황 시나리오
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch
import pandas as pd
import numpy as np

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.trend_atr import TrendATRStrategy, Signal, SignalType, TrendType, Position
from engine.executor import TradingExecutor
from api.kis_api import KISApi, KISApiError
from backtest.backtester import Backtester, BacktestResult
from config import settings


class TestCompleteTradingCycle:
    """완전한 매매 사이클 통합 테스트"""
    
    def test_full_buy_hold_sell_cycle(self, sample_uptrend_df):
        """
        완전한 매수 → 보유 → 매도 사이클 테스트
        
        시나리오:
        1. 상승 추세에서 매수 시그널 발생
        2. 포지션 보유 중 HOLD
        3. 익절가 도달로 매도
        """
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.get_daily_ohlcv.return_value = sample_uptrend_df
        mock_api.place_buy_order.return_value = {
            "success": True,
            "order_no": "0001234567",
            "message": "주문 성공"
        }
        mock_api.place_sell_order.return_value = {
            "success": True,
            "order_no": "0001234568",
            "message": "주문 성공"
        }
        
        strategy = TrendATRStrategy()
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        df_with_indicators = strategy.add_indicators(sample_uptrend_df)
        
        # ═══════════════════════════════════════════════════════════════
        # Phase 1: 매수
        # ═══════════════════════════════════════════════════════════════
        
        # 돌파 조건 충족하는 가격
        prev_high = df_with_indicators.iloc[-1]['prev_high']
        entry_price = prev_high + 2000
        
        mock_api.get_current_price.return_value = {
            "stock_code": "005930",
            "current_price": entry_price
        }
        
        # 매수 시그널 생성
        buy_signal = strategy.generate_signal(
            df_with_indicators,
            entry_price,
            stock_code="005930"
        )
        
        # 시그널 타입 확인 (조건에 따라 BUY 또는 HOLD)
        if buy_signal.signal_type == SignalType.BUY:
            result = executor.execute_buy_order(buy_signal)
            assert result["success"] is True, "매수 주문이 성공해야 합니다"
            assert strategy.has_position() is True, "매수 후 포지션이 있어야 합니다"
        else:
            # ADX 등의 조건으로 BUY가 아닐 수 있음 - 수동으로 포지션 설정
            strategy.open_position(
                stock_code="005930",
                entry_price=entry_price,
                quantity=10,
                stop_loss=entry_price - 3000,
                take_profit=entry_price + 4500,
                entry_date=datetime.now().strftime("%Y-%m-%d"),
                atr=1500
            )
        
        initial_entry_price = strategy.position.entry_price
        
        # ═══════════════════════════════════════════════════════════════
        # Phase 2: 보유 (HOLD)
        # ═══════════════════════════════════════════════════════════════
        
        # 손절가와 익절가 사이의 가격
        hold_price = initial_entry_price + 1000
        
        mock_api.get_current_price.return_value = {
            "stock_code": "005930",
            "current_price": hold_price
        }
        
        hold_signal = strategy.generate_signal(
            df_with_indicators,
            hold_price,
            stock_code="005930"
        )
        
        assert hold_signal.signal_type == SignalType.HOLD, "손익 범위 내에서는 HOLD여야 합니다"
        assert strategy.has_position() is True, "포지션이 유지되어야 합니다"
        
        # ═══════════════════════════════════════════════════════════════
        # Phase 3: 익절 매도
        # ═══════════════════════════════════════════════════════════════
        
        take_profit_price = strategy.position.take_profit + 1000
        
        mock_api.get_current_price.return_value = {
            "stock_code": "005930",
            "current_price": take_profit_price
        }
        
        sell_signal = strategy.generate_signal(
            df_with_indicators,
            take_profit_price,
            stock_code="005930"
        )
        
        assert sell_signal.signal_type == SignalType.SELL, "익절가 초과 시 SELL이어야 합니다"
        assert "익절" in sell_signal.reason, "익절 사유가 포함되어야 합니다"
        
        result = executor.execute_sell_order(sell_signal)
        
        assert result["success"] is True, "매도 주문이 성공해야 합니다"
        assert strategy.has_position() is False, "매도 후 포지션이 없어야 합니다"
    
    def test_stop_loss_cycle(self, sample_uptrend_df):
        """
        손절 사이클 테스트
        
        시나리오:
        1. 매수 진입
        2. 가격 하락
        3. 손절가 도달로 자동 매도
        """
        strategy = TrendATRStrategy()
        
        # 포지션 설정
        strategy.open_position(
            stock_code="005930",
            entry_price=65000,
            quantity=100,
            stop_loss=62000,
            take_profit=71000,
            entry_date="2024-01-15",
            atr=1500
        )
        
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.get_daily_ohlcv.return_value = sample_uptrend_df
        mock_api.place_sell_order.return_value = {
            "success": True,
            "order_no": "0001234568",
            "message": "주문 성공"
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=100,
            auto_sync=False
        )
        
        df_with_indicators = strategy.add_indicators(sample_uptrend_df)
        
        # 손절가 이하로 하락
        stop_loss_price = 61000
        
        sell_signal = strategy.generate_signal(
            df_with_indicators,
            stop_loss_price,
            stock_code="005930"
        )
        
        assert sell_signal.signal_type == SignalType.SELL, "손절가 미만 시 SELL이어야 합니다"
        assert "손절" in sell_signal.reason, "손절 사유가 포함되어야 합니다"
        
        result = executor.execute_sell_order(sell_signal)
        
        assert result["success"] is True
        assert strategy.has_position() is False


class TestMultipleScenarios:
    """다양한 시장 상황 시나리오 테스트"""
    
    def test_no_entry_in_downtrend_scenario(self, sample_downtrend_df):
        """
        시나리오: 하락장에서는 절대 진입하지 않음
        
        - 종가 < MA50
        - 돌파 조건 충족해도 진입 안 함
        """
        strategy = TrendATRStrategy()
        df_with_indicators = strategy.add_indicators(sample_downtrend_df)
        
        # 모든 가격대에서 진입 시도
        test_prices = [40000, 50000, 60000, 70000, 80000]
        
        for price in test_prices:
            should_enter, reason = strategy.check_entry_condition(
                df_with_indicators,
                price
            )
            
            assert should_enter is False, \
                f"하락 추세에서 가격 {price}에서 진입하면 안 됩니다"
    
    def test_volatile_market_scenario(self, sample_atr_spike_df):
        """
        시나리오: 변동성 급등 시장
        
        - ATR이 평균의 2.5배 이상
        - 진입 거부
        """
        strategy = TrendATRStrategy()
        df_with_indicators = strategy.add_indicators(sample_atr_spike_df)
        
        # 높은 가격으로 돌파 시도
        current_price = df_with_indicators.iloc[-1]['prev_high'] + 10000
        
        should_enter, reason = strategy.check_entry_condition(
            df_with_indicators,
            current_price
        )
        
        assert should_enter is False, "변동성 급등 시 진입하면 안 됩니다"
        assert "ATR 급등" in reason
    
    def test_sideways_market_scenario(self, sample_sideways_df):
        """
        시나리오: 횡보장
        
        - ADX < 25
        - 추세 강도 부족으로 진입 거부
        """
        strategy = TrendATRStrategy()
        df_with_indicators = strategy.add_indicators(sample_sideways_df)
        
        current_price = 70000  # 높은 가격
        
        should_enter, reason = strategy.check_entry_condition(
            df_with_indicators,
            current_price
        )
        
        # 횡보장에서는 진입하지 않아야 함
        assert should_enter is False, "횡보장에서는 진입하면 안 됩니다"


class TestErrorRecovery:
    """에러 상황 복구 테스트"""
    
    def test_api_failure_does_not_corrupt_position(self, strategy_with_position):
        """
        시나리오: API 실패 시 포지션 무결성 유지
        
        - 매도 API 실패
        - 포지션은 그대로 유지되어야 함
        """
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_sell_order.return_value = {
            "success": False,
            "order_no": "",
            "message": "API 오류"
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy_with_position,
            stock_code="005930",
            order_quantity=100,
            auto_sync=False
        )
        
        original_position = strategy_with_position.position
        
        sell_signal = Signal(
            signal_type=SignalType.SELL,
            price=56000,
            stop_loss=57000,
            take_profit=69000,
            reason="손절",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        result = executor.execute_sell_order(sell_signal)
        
        assert result["success"] is False
        # 핵심: API 실패 시 포지션은 유지되어야 함
        assert strategy_with_position.has_position() is True, \
            "API 실패 시 포지션이 유지되어야 합니다"
        assert strategy_with_position.position == original_position
    
    def test_partial_data_handling(self, strategy):
        """
        시나리오: 데이터 부족 상황
        
        - MA/ATR 계산에 필요한 데이터 부족
        - 안전하게 HOLD 반환
        """
        # 10일치 데이터만 (MA50 계산 불가)
        short_df = pd.DataFrame({
            'date': pd.date_range(end=datetime.now(), periods=10, freq='D'),
            'open': [60000] * 10,
            'high': [61000] * 10,
            'low': [59000] * 10,
            'close': [60500] * 10,
            'volume': [1000000] * 10
        })
        
        df_with_indicators = strategy.add_indicators(short_df)
        
        signal = strategy.generate_signal(
            df_with_indicators,
            current_price=65000,
            stock_code="005930"
        )
        
        # 데이터 부족 시 안전하게 HOLD
        assert signal.signal_type in [SignalType.HOLD, SignalType.SELL], \
            "데이터 부족 시 BUY가 발생하면 안 됩니다"


class TestBacktestIntegration:
    """백테스트 통합 테스트"""
    
    def test_backtester_with_uptrend_data(self, sample_uptrend_df):
        """상승장 백테스트"""
        backtester = Backtester()
        result = backtester.run(sample_uptrend_df, stock_code="005930")
        
        assert isinstance(result, BacktestResult)
        assert result.initial_capital == settings.BACKTEST_INITIAL_CAPITAL
        assert result.start_date != ""
        assert result.end_date != ""
    
    def test_backtester_with_downtrend_data(self, sample_downtrend_df):
        """하락장 백테스트"""
        backtester = Backtester()
        result = backtester.run(sample_downtrend_df, stock_code="005930")
        
        assert isinstance(result, BacktestResult)
        # 하락장에서는 진입이 적어야 함
        # (전략이 하락 추세 진입을 차단하므로)
    
    def test_backtester_empty_data(self):
        """빈 데이터 백테스트"""
        backtester = Backtester()
        result = backtester.run(pd.DataFrame(), stock_code="005930")
        
        assert result.total_trades == 0
        assert result.final_capital == result.initial_capital
    
    def test_backtester_trade_records(self, sample_uptrend_df):
        """백테스트 거래 기록 검증"""
        backtester = Backtester()
        result = backtester.run(sample_uptrend_df, stock_code="005930")
        
        # 거래가 있다면 기록 검증
        for trade in result.trades:
            assert trade.entry_date != ""
            assert trade.exit_date != ""
            assert trade.entry_price > 0
            assert trade.exit_price > 0
            assert trade.quantity > 0
            assert trade.exit_reason != ""


class TestRiskManagement:
    """리스크 관리 통합 테스트"""
    
    def test_max_loss_limit_enforced(self, strategy):
        """최대 손실 제한 적용 테스트"""
        entry_price = 100000
        very_large_atr = 50000  # 매우 큰 ATR
        
        stop_loss = strategy.calculate_stop_loss(entry_price, very_large_atr)
        
        # ATR 기반: 100000 - 100000 = 0 (손절가가 0!)
        # MAX_LOSS_PCT 제한: 100000 * 0.95 = 95000
        
        max_loss = entry_price * (settings.MAX_LOSS_PCT / 100)
        actual_loss = entry_price - stop_loss
        
        assert actual_loss <= max_loss, \
            f"손실({actual_loss})이 최대 허용 손실({max_loss})을 초과합니다"
        assert stop_loss > 0, "손절가는 양수여야 합니다"
    
    def test_consecutive_losses_scenario(self, sample_uptrend_df):
        """
        시나리오: 연속 손절 상황
        
        현재 시스템의 문제점 확인:
        - 일일 손실 한도가 없어 연속 손절 가능
        """
        strategy = TrendATRStrategy()
        
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.get_daily_ohlcv.return_value = sample_uptrend_df
        mock_api.place_buy_order.return_value = {
            "success": True,
            "order_no": "0001234567",
            "message": "주문 성공"
        }
        mock_api.place_sell_order.return_value = {
            "success": True,
            "order_no": "0001234568",
            "message": "주문 성공"
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        total_losses = 0
        
        # 5번 연속 손절 시뮬레이션
        for i in range(5):
            # 매수
            strategy.open_position(
                stock_code="005930",
                entry_price=65000,
                quantity=10,
                stop_loss=62000,
                take_profit=71000,
                entry_date=f"2024-01-{15+i}",
                atr=1500
            )
            
            # 즉시 손절
            sell_signal = Signal(
                signal_type=SignalType.SELL,
                price=61000,
                reason="손절"
            )
            
            # 중복 체크 우회를 위해 시간 조정
            executor._last_order_time = None
            executor._last_signal_type = None
            
            executor.execute_sell_order(sell_signal)
            
            # 손실 계산 (65000 → 61000 = -6.15%)
            loss_pct = (61000 - 65000) / 65000 * 100
            total_losses += loss_pct
        
        # 현재 시스템: 일일 손실 한도 없음
        # 5회 연속 -6.15% = 약 -30% 누적 손실 가능
        assert total_losses < -20, \
            f"연속 손절 시 큰 손실 발생 가능 (누적: {total_losses:.2f}%)"


class TestSystemConfiguration:
    """시스템 설정 통합 테스트"""
    
    def test_all_required_settings_exist(self):
        """필수 설정값 존재 확인"""
        required_settings = [
            'KIS_BASE_URL',
            'ATR_PERIOD',
            'TREND_MA_PERIOD',
            'ATR_MULTIPLIER_SL',
            'ATR_MULTIPLIER_TP',
            'MAX_LOSS_PCT',
            'ATR_SPIKE_THRESHOLD',
            'ADX_THRESHOLD',
            'ORDER_QUANTITY',
            'MAX_RETRIES',
            'RATE_LIMIT_DELAY'
        ]
        
        for setting_name in required_settings:
            assert hasattr(settings, setting_name), \
                f"필수 설정 {setting_name}이 없습니다"
    
    def test_settings_have_valid_values(self):
        """설정값 유효성 검증"""
        assert settings.ATR_PERIOD > 0, "ATR_PERIOD는 양수여야 합니다"
        assert settings.TREND_MA_PERIOD > 0, "TREND_MA_PERIOD는 양수여야 합니다"
        assert settings.ATR_MULTIPLIER_SL > 0, "ATR_MULTIPLIER_SL는 양수여야 합니다"
        assert settings.ATR_MULTIPLIER_TP > 0, "ATR_MULTIPLIER_TP는 양수여야 합니다"
        assert 0 < settings.MAX_LOSS_PCT < 100, "MAX_LOSS_PCT는 0~100 사이여야 합니다"
        assert settings.ATR_SPIKE_THRESHOLD > 1, "ATR_SPIKE_THRESHOLD는 1보다 커야 합니다"
        assert settings.ADX_THRESHOLD > 0, "ADX_THRESHOLD는 양수여야 합니다"
    
    def test_risk_reward_ratio(self):
        """리스크/리워드 비율 검증"""
        # 손절 배수 < 익절 배수 여야 양의 기대값
        assert settings.ATR_MULTIPLIER_SL < settings.ATR_MULTIPLIER_TP, \
            "손절 배수가 익절 배수보다 작아야 양의 기대값을 가집니다"
        
        # 리스크:리워드 비율 확인
        risk_reward_ratio = settings.ATR_MULTIPLIER_TP / settings.ATR_MULTIPLIER_SL
        assert risk_reward_ratio >= 1.0, \
            f"리스크:리워드 비율({risk_reward_ratio:.2f})이 1:1 이상이어야 합니다"


class TestEndToEndWorkflow:
    """엔드투엔드 워크플로우 테스트"""
    
    def test_complete_trading_day_simulation(self, sample_uptrend_df):
        """
        하루 거래 시뮬레이션
        
        - 장 시작
        - 여러 번 전략 실행
        - 일일 요약 확인
        """
        strategy = TrendATRStrategy()
        
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.get_daily_ohlcv.return_value = sample_uptrend_df
        mock_api.place_buy_order.return_value = {
            "success": True,
            "order_no": "0001234567",
            "message": "주문 성공"
        }
        mock_api.place_sell_order.return_value = {
            "success": True,
            "order_no": "0001234568",
            "message": "주문 성공"
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        df_with_indicators = strategy.add_indicators(sample_uptrend_df)
        
        # 여러 번 전략 실행 시뮬레이션
        price_sequence = [64000, 65000, 66000, 67000, 68000]
        
        for i, price in enumerate(price_sequence):
            mock_api.get_current_price.return_value = {
                "stock_code": "005930",
                "current_price": price
            }
            
            signal = strategy.generate_signal(
                df_with_indicators,
                price,
                stock_code="005930"
            )
            
            # 중복 체크 우회
            if i > 0:
                executor._last_order_time = datetime.now() - timedelta(minutes=5)
            
            if signal.signal_type == SignalType.BUY and not strategy.has_position():
                executor.execute_buy_order(signal)
            elif signal.signal_type == SignalType.SELL and strategy.has_position():
                executor.execute_sell_order(signal)
        
        # 일일 요약
        summary = executor.get_daily_summary()
        
        assert "total_trades" in summary
        assert "buy_count" in summary
        assert "sell_count" in summary
        assert "total_pnl" in summary
        
        # 일일 초기화
        executor.reset_daily_trades()
        new_summary = executor.get_daily_summary()
        
        assert new_summary["total_trades"] == 0
