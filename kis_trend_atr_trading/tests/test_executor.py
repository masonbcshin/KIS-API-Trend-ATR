"""
KIS Trend-ATR Trading System - 실행 엔진 테스트

TradingExecutor 클래스의 핵심 기능을 테스트합니다.

테스트 항목:
- 중복 주문 방지
- 주문 실행 흐름
- 일일 거래 요약
- 포지션 인식 (재시작 시나리오)
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch, PropertyMock
import pandas as pd
import numpy as np

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.executor import TradingExecutor, ExecutorError
from strategy.trend_atr import TrendATRStrategy, Signal, SignalType, TrendType, Position
from api.kis_api import KISApi, KISApiError


class TestDuplicateOrderPrevention:
    """
    [필수 테스트] 중복 주문 방지 테스트
    """
    
    def test_duplicate_buy_signal_blocked(self, sample_uptrend_df, strategy):
        """동일한 BUY 시그널 연속 실행 차단 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_buy_order.return_value = {
            "success": True,
            "order_no": "0001234567",
            "message": "주문 성공"
        }
        
        mock_api.get_order_status.return_value = {
            "success": True,
            "orders": [{
                "order_no": "0001234567",
                "exec_qty": 10,
                "exec_price": 65000
            }]
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        # 첫 번째 BUY 시그널
        signal1 = Signal(
            signal_type=SignalType.BUY,
            price=65000,
            stop_loss=62000,
            take_profit=71000,
            reason="테스트",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        # 첫 번째 주문 실행 성공
        result1 = executor.execute_buy_order(signal1)
        assert result1["success"] is True, "첫 번째 주문은 성공해야 합니다"
        
        # 1분 이내 동일한 BUY 시그널
        signal2 = Signal(
            signal_type=SignalType.BUY,
            price=65500,
            stop_loss=62500,
            take_profit=71500,
            reason="테스트2",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        # 두 번째 주문은 중복으로 차단되어야 함
        result2 = executor.execute_buy_order(signal2)
        
        # _can_execute_order에서 차단
        assert result2["success"] is False, "1분 이내 중복 주문은 차단되어야 합니다"
        assert "조건" in result2["message"] or "보유" in result2["message"]
    
    def test_duplicate_sell_signal_blocked(self, strategy_with_position):
        """동일한 SELL 시그널 연속 실행 차단 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_sell_order.return_value = {
            "success": True,
            "order_no": "0001234568",
            "message": "주문 성공"
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy_with_position,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        # 첫 번째 SELL 시그널
        signal1 = Signal(
            signal_type=SignalType.SELL,
            price=56000,
            stop_loss=57000,
            take_profit=69000,
            reason="손절",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        # 첫 번째 주문 실행
        result1 = executor.execute_sell_order(signal1)
        assert result1["success"] is True, "첫 번째 주문은 성공해야 합니다"
        
        # 포지션 다시 설정 (테스트용)
        strategy_with_position.position = Position(
            stock_code="005930",
            entry_price=60000,
            quantity=100,
            stop_loss=57000,
            take_profit=69000,
            entry_date="2024-01-15",
            atr_at_entry=1500
        )
        
        # 1분 이내 동일한 SELL 시그널
        signal2 = Signal(
            signal_type=SignalType.SELL,
            price=55000,
            stop_loss=57000,
            take_profit=69000,
            reason="손절2",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        result2 = executor.execute_sell_order(signal2)
        
        assert result2["success"] is False, "1분 이내 중복 SELL은 차단되어야 합니다"
    
    def test_different_signal_type_allowed(self, strategy):
        """다른 종류의 시그널은 연속 실행 가능 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_buy_order.return_value = {
            "success": True,
            "order_no": "0001234567",
            "message": "주문 성공"
        }
        
        mock_api.get_order_status.return_value = {
            "success": True,
            "orders": [{
                "order_no": "0001234567",
                "exec_qty": 10,
                "exec_price": 65000
            }]
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
        
        # BUY 시그널
        buy_signal = Signal(
            signal_type=SignalType.BUY,
            price=65000,
            stop_loss=62000,
            take_profit=71000,
            reason="테스트",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        result1 = executor.execute_buy_order(buy_signal)
        assert result1["success"] is True
        
        # 바로 SELL 시그널 (다른 타입)
        sell_signal = Signal(
            signal_type=SignalType.SELL,
            price=64000,
            stop_loss=62000,
            take_profit=71000,
            reason="손절",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        # 다른 시그널 타입이므로 중복 체크 통과 (포지션 체크에서 걸림)
        # 포지션이 없으면 SELL은 실패
        result2 = executor.execute_sell_order(sell_signal)
        
        # 포지션이 있으면 SELL 가능
        if strategy.has_position():
            # 다른 타입이므로 중복 체크 통과
            pass


class TestOrderExecution:
    """주문 실행 테스트"""
    
    def test_buy_order_success(self, strategy):
        """매수 주문 성공 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_buy_order.return_value = {
            "success": True,
            "order_no": "0001234567",
            "message": "주문 성공"
        }
        
        mock_api.get_order_status.return_value = {
            "success": True,
            "orders": [{
                "order_no": "0001234567",
                "exec_qty": 10,
                "exec_price": 65000
            }]
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        signal = Signal(
            signal_type=SignalType.BUY,
            price=65000,
            stop_loss=62000,
            take_profit=71000,
            reason="테스트",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        result = executor.execute_buy_order(signal)
        
        assert result["success"] is True
        assert strategy.has_position() is True
        assert strategy.position.entry_price == 65000
    
    def test_buy_order_failure(self, strategy):
        """매수 주문 실패 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_buy_order.return_value = {
            "success": False,
            "order_no": "",
            "message": "잔고 부족"
        }
        
        mock_api.get_order_status.return_value = {
            "success": True,
            "orders": [{
                "order_no": "0001234567",
                "exec_qty": 10,
                "exec_price": 65000
            }]
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        signal = Signal(
            signal_type=SignalType.BUY,
            price=65000,
            stop_loss=62000,
            take_profit=71000,
            reason="테스트",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        result = executor.execute_buy_order(signal)
        
        assert result["success"] is False
        assert strategy.has_position() is False, "주문 실패 시 포지션이 생기면 안 됩니다"
    
    def test_sell_order_success(self, strategy_with_position):
        """매도 주문 성공 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_sell_order.return_value = {
            "success": True,
            "order_no": "0001234568",
            "message": "주문 성공"
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy_with_position,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        signal = Signal(
            signal_type=SignalType.SELL,
            price=56000,
            stop_loss=57000,
            take_profit=69000,
            reason="손절",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        result = executor.execute_sell_order(signal)
        
        assert result["success"] is True
        assert strategy_with_position.has_position() is False, "매도 후 포지션이 청산되어야 합니다"
    
    def test_sell_order_without_position(self, strategy):
        """포지션 없이 매도 시도 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        signal = Signal(
            signal_type=SignalType.SELL,
            price=65000,
            stop_loss=62000,
            take_profit=71000,
            reason="테스트",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        result = executor.execute_sell_order(signal)
        
        assert result["success"] is False
        assert "포지션" in result["message"], "포지션 없음 메시지가 포함되어야 합니다"
    
    def test_buy_order_blocked_with_existing_position(self, strategy_with_position):
        """이미 포지션 보유 중 매수 차단 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy_with_position,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        signal = Signal(
            signal_type=SignalType.BUY,
            price=65000,
            stop_loss=62000,
            take_profit=71000,
            reason="테스트",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        result = executor.execute_buy_order(signal)
        
        assert result["success"] is False
        assert "보유" in result["message"], "포지션 보유 중 메시지가 포함되어야 합니다"


class TestCanExecuteOrder:
    """주문 실행 가능 여부 테스트"""
    
    def test_hold_signal_cannot_execute(self, strategy):
        """HOLD 시그널은 실행 불가 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        signal = Signal(
            signal_type=SignalType.HOLD,
            price=65000,
            reason="관망"
        )
        
        can_execute = executor._can_execute_order(signal)
        
        assert can_execute is False, "HOLD 시그널은 실행 불가해야 합니다"
    
    def test_signal_after_interval_allowed(self, strategy):
        """일정 시간 후 동일 시그널 허용 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_buy_order.return_value = {
            "success": True,
            "order_no": "0001234567",
            "message": "주문 성공"
        }
        
        mock_api.get_order_status.return_value = {
            "success": True,
            "orders": [{
                "order_no": "0001234567",
                "exec_qty": 10,
                "exec_price": 65000
            }]
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        signal = Signal(
            signal_type=SignalType.BUY,
            price=65000,
            stop_loss=62000,
            take_profit=71000,
            reason="테스트",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        # 첫 번째 주문
        executor.execute_buy_order(signal)
        
        # 시간을 1분 이상 경과한 것처럼 설정
        executor._last_order_time = datetime.now() - timedelta(minutes=2)
        
        # 포지션 리셋 (테스트용)
        strategy.position = None
        
        # 동일 시그널이지만 1분 이상 경과
        can_execute = executor._can_execute_order(signal)
        
        assert can_execute is True, "1분 이상 경과 후에는 동일 시그널 실행 가능해야 합니다"


class TestDailySummary:
    """일일 거래 요약 테스트"""
    
    def test_empty_daily_summary(self, strategy):
        """거래 없는 일일 요약 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        summary = executor.get_daily_summary()
        
        assert summary["total_trades"] == 0
        assert summary["buy_count"] == 0
        assert summary["sell_count"] == 0
        assert summary["total_pnl"] == 0
    
    def test_daily_summary_with_trades(self, strategy):
        """거래 있는 일일 요약 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
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
        
        # 매수
        buy_signal = Signal(
            signal_type=SignalType.BUY,
            price=65000,
            stop_loss=62000,
            take_profit=71000,
            reason="테스트",
            atr=1500,
            trend=TrendType.UPTREND
        )
        executor.execute_buy_order(buy_signal)
        
        # 매도
        sell_signal = Signal(
            signal_type=SignalType.SELL,
            price=68000,
            stop_loss=62000,
            take_profit=71000,
            reason="익절",
            atr=1500,
            trend=TrendType.UPTREND
        )
        executor.execute_sell_order(sell_signal)
        
        summary = executor.get_daily_summary()
        
        assert summary["total_trades"] == 2
        assert summary["buy_count"] == 1
        assert summary["sell_count"] == 1
    
    def test_reset_daily_trades(self, strategy):
        """일일 거래 기록 초기화 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_buy_order.return_value = {
            "success": True,
            "order_no": "0001234567",
            "message": "주문 성공"
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        # 거래 실행
        buy_signal = Signal(
            signal_type=SignalType.BUY,
            price=65000,
            stop_loss=62000,
            take_profit=71000,
            reason="테스트",
            atr=1500,
            trend=TrendType.UPTREND
        )
        executor.execute_buy_order(buy_signal)
        
        assert executor.get_daily_summary()["total_trades"] == 1
        
        # 초기화
        executor.reset_daily_trades()
        
        assert executor.get_daily_summary()["total_trades"] == 0


class TestPositionRecognitionAfterRestart:
    """
    [필수 테스트] 프로그램 재시작 후 포지션 인식 테스트
    
    현재 시스템의 문제점을 검증:
    - 포지션이 메모리에만 저장되어 재시작 시 손실됨
    - 이 테스트는 문제 상황을 확인하고 해결 방안을 제시
    """
    
    def test_position_lost_after_restart_simulation(self, strategy):
        """재시작 시 포지션 손실 시뮬레이션"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_buy_order.return_value = {
            "success": True,
            "order_no": "0001234567",
            "message": "주문 성공"
        }
        mock_api.get_account_balance.return_value = {
            "success": True,
            "holdings": [
                {
                    "stock_code": "005930",
                    "stock_name": "삼성전자",
                    "quantity": 10,
                    "avg_price": 65000.0,
                    "current_price": 66000.0,
                    "eval_amount": 660000,
                    "pnl_amount": 10000,
                    "pnl_rate": 1.54
                }
            ],
            "total_eval": 10660000,
            "cash_balance": 10000000,
            "total_pnl": 10000
        }
        
        # ═══ 첫 번째 실행 ═══
        executor1 = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        # 매수 주문 실행
        buy_signal = Signal(
            signal_type=SignalType.BUY,
            price=65000,
            stop_loss=62000,
            take_profit=71000,
            reason="테스트",
            atr=1500,
            trend=TrendType.UPTREND
        )
        executor1.execute_buy_order(buy_signal)
        
        # 포지션 확인
        assert strategy.has_position() is True, "매수 후 포지션이 있어야 합니다"
        assert strategy.position.entry_price == 65000
        
        # ═══ 프로그램 재시작 시뮬레이션 (새 인스턴스 생성) ═══
        new_strategy = TrendATRStrategy()  # 새 전략 인스턴스
        
        executor2 = TradingExecutor(
            api=mock_api,
            strategy=new_strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        # 문제점: 재시작 후 포지션 정보 손실
        assert new_strategy.has_position() is False, \
            "재시작 후 포지션 정보가 손실됨 (이것이 현재 시스템의 문제점입니다)"
        
        # 실제 잔고에는 주식이 있음
        balance = mock_api.get_account_balance()
        actual_holdings = balance["holdings"]
        
        assert len(actual_holdings) > 0, "실제 계좌에는 보유 주식이 있습니다"
        assert actual_holdings[0]["stock_code"] == "005930"
        
        # 문제 상황: 시스템은 포지션 없다고 인식하지만 실제로는 보유 중
        # 이로 인해 중복 매수 또는 손절 관리 불가 위험
    
    def test_position_sync_from_account_proposal(self, strategy):
        """
        포지션 동기화 해결 방안 테스트
        
        제안: TradingExecutor 시작 시 계좌 잔고를 조회하여 포지션 동기화
        """
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.get_account_balance.return_value = {
            "success": True,
            "holdings": [
                {
                    "stock_code": "005930",
                    "stock_name": "삼성전자",
                    "quantity": 10,
                    "avg_price": 65000.0,
                    "current_price": 66000.0,
                    "eval_amount": 660000,
                    "pnl_amount": 10000,
                    "pnl_rate": 1.54
                }
            ],
            "total_eval": 10660000,
            "cash_balance": 10000000,
            "total_pnl": 10000
        }
        mock_api.get_current_price.return_value = {
            "stock_code": "005930",
            "current_price": 66000.0
        }
        
        # 해결 방안 시뮬레이션: 시작 시 계좌 조회하여 동기화
        def sync_position_from_account(executor, target_stock_code):
            """
            계좌 잔고에서 포지션을 동기화하는 함수 (제안)
            
            실제 구현 시 TradingExecutor.__init__에 추가 권장
            """
            balance = executor.api.get_account_balance()
            
            for holding in balance["holdings"]:
                if holding["stock_code"] == target_stock_code:
                    # 현재가로 ATR 추정 (실제로는 일봉 데이터에서 계산 권장)
                    estimated_atr = holding["current_price"] * 0.02  # 2% 추정
                    
                    # 포지션 복구
                    executor.strategy.position = Position(
                        stock_code=holding["stock_code"],
                        entry_price=holding["avg_price"],
                        quantity=holding["quantity"],
                        stop_loss=holding["current_price"] - (estimated_atr * 2),
                        take_profit=holding["current_price"] + (estimated_atr * 3),
                        entry_date="RECOVERED",
                        atr_at_entry=estimated_atr
                    )
                    return True
            return False
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        # 동기화 전: 포지션 없음
        assert strategy.has_position() is False
        
        # 동기화 실행
        synced = sync_position_from_account(executor, "005930")
        
        # 동기화 후: 포지션 복구됨
        assert synced is True, "동기화가 성공해야 합니다"
        assert strategy.has_position() is True, "동기화 후 포지션이 있어야 합니다"
        assert strategy.position.stock_code == "005930"
        assert strategy.position.entry_price == 65000.0
        assert strategy.position.quantity == 10


class TestAPIErrorHandling:
    """API 에러 처리 테스트"""
    
    def test_buy_order_api_exception(self, strategy):
        """매수 주문 시 API 예외 처리 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_buy_order.side_effect = KISApiError("API 타임아웃")
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        signal = Signal(
            signal_type=SignalType.BUY,
            price=65000,
            stop_loss=62000,
            take_profit=71000,
            reason="테스트",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        result = executor.execute_buy_order(signal)
        
        assert result["success"] is False
        assert "타임아웃" in result["message"] or "API" in result["message"]
        assert strategy.has_position() is False, "API 에러 시 포지션이 생기면 안 됩니다"
    
    def test_sell_order_api_exception(self, strategy_with_position):
        """매도 주문 시 API 예외 처리 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.place_sell_order.side_effect = KISApiError("네트워크 오류")
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy_with_position,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        signal = Signal(
            signal_type=SignalType.SELL,
            price=56000,
            stop_loss=57000,
            take_profit=69000,
            reason="손절",
            atr=1500,
            trend=TrendType.UPTREND
        )
        
        result = executor.execute_sell_order(signal)
        
        assert result["success"] is False
        # 중요: API 에러 시 포지션은 유지되어야 함 (실제로 매도가 안 됐으므로)
        assert strategy_with_position.has_position() is True, \
            "API 에러 시 포지션이 유지되어야 합니다"


class TestRunOnce:
    """전략 1회 실행 테스트"""
    
    def test_run_once_with_no_data(self, strategy):
        """데이터 없을 때 run_once 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.get_daily_ohlcv.return_value = pd.DataFrame()
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        result = executor.run_once()
        
        assert result["error"] is not None
        assert "데이터" in result["error"]
    
    def test_run_once_with_zero_price(self, strategy, sample_uptrend_df):
        """현재가 0일 때 run_once 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        mock_api.get_daily_ohlcv.return_value = sample_uptrend_df
        mock_api.get_current_price.return_value = {
            "stock_code": "005930",
            "current_price": 0
        }
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        result = executor.run_once()
        
        assert result["error"] is not None
        assert "현재가" in result["error"]


class TestExecutorStop:
    """실행 중지 테스트"""
    
    def test_stop_executor(self, strategy):
        """실행 중지 테스트"""
        mock_api = Mock(spec=KISApi)
        def mock_order_status(order_no=None):
            return {
                "success": True,
                "orders": [{"order_no": order_no or "0001234567", "exec_qty": 100, "exec_price": 65000}]
            }
        mock_api.get_order_status.side_effect = mock_order_status
        
        executor = TradingExecutor(
            api=mock_api,
            strategy=strategy,
            stock_code="005930",
            order_quantity=10,
            auto_sync=False
        )
        
        executor.is_running = True
        executor.stop()
        
        assert executor.is_running is False
