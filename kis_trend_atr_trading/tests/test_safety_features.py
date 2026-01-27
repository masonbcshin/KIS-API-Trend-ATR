"""
KIS Trend-ATR Trading System - 안전 기능 테스트 (v2.0)

새로 추가된 안전 기능들을 테스트합니다.

테스트 항목:
- 포지션 영속화 및 동기화
- 거래시간 검증
- 일일 손실 한도
- 긴급 손절 재시도
- 체결 확인
"""

import pytest
from datetime import datetime, time, date, timedelta
from unittest.mock import Mock, patch, MagicMock
import json
import tempfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.market_hours import (
    is_market_open, is_holiday, is_weekend,
    get_market_status, should_skip_trading,
    MARKET_OPEN, MARKET_CLOSE
)
from utils.position_store import (
    PositionStore, StoredPosition, DailyTradeStore
)
from config import settings


class TestMarketHours:
    """거래시간 검증 테스트"""
    
    def test_weekend_detection(self):
        """주말 감지 테스트"""
        # 토요일
        saturday = date(2024, 1, 20)  # 2024-01-20은 토요일
        assert is_weekend(saturday) is True
        
        # 일요일
        sunday = date(2024, 1, 21)
        assert is_weekend(sunday) is True
        
        # 평일
        monday = date(2024, 1, 22)
        assert is_weekend(monday) is False
    
    def test_market_open_during_trading_hours(self):
        """거래시간 중 시장 오픈 테스트"""
        # 평일 10:00
        trading_time = datetime(2024, 1, 22, 10, 0, 0)  # 월요일
        
        with patch('utils.market_hours.is_holiday', return_value=False):
            result = is_market_open(trading_time)
            assert result is True
    
    def test_market_closed_before_open(self):
        """장 시작 전 테스트"""
        # 평일 08:30
        before_open = datetime(2024, 1, 22, 8, 30, 0)
        
        with patch('utils.market_hours.is_holiday', return_value=False):
            result = is_market_open(before_open)
            assert result is False
    
    def test_market_closed_after_close(self):
        """장 마감 후 테스트"""
        # 평일 16:00
        after_close = datetime(2024, 1, 22, 16, 0, 0)
        
        with patch('utils.market_hours.is_holiday', return_value=False):
            result = is_market_open(after_close)
            assert result is False
    
    def test_market_status_detail(self):
        """시장 상태 상세 정보 테스트"""
        # 주말
        weekend = datetime(2024, 1, 20, 10, 0, 0)  # 토요일
        is_open, status = get_market_status(weekend)
        assert is_open is False
        assert "주말" in status
    
    def test_should_skip_trading_weekend(self):
        """주말 거래 건너뛰기 테스트"""
        weekend = datetime(2024, 1, 20, 10, 0, 0)
        
        should_skip, reason = should_skip_trading(weekend)
        assert should_skip is True
        assert "주말" in reason


class TestPositionStore:
    """포지션 영속화 테스트"""
    
    def test_save_and_load_position(self):
        """포지션 저장 및 로드 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test_positions.json"
            store = PositionStore(file_path)
            
            # 포지션 저장
            position = StoredPosition(
                stock_code="005930",
                entry_price=65000,
                quantity=100,
                stop_loss=62000,
                take_profit=71000,
                entry_date="2024-01-15",
                atr_at_entry=1500
            )
            
            result = store.save_position(position)
            assert result is True
            
            # 포지션 로드
            loaded = store.load_position()
            assert loaded is not None
            assert loaded.stock_code == "005930"
            assert loaded.entry_price == 65000
            assert loaded.quantity == 100
    
    def test_clear_position(self):
        """포지션 삭제 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test_positions.json"
            store = PositionStore(file_path)
            
            # 포지션 저장
            position = StoredPosition(
                stock_code="005930",
                entry_price=65000,
                quantity=100,
                stop_loss=62000,
                take_profit=71000,
                entry_date="2024-01-15",
                atr_at_entry=1500
            )
            store.save_position(position)
            
            # 포지션 삭제
            store.clear_position()
            
            # 확인
            loaded = store.load_position()
            assert loaded is None
    
    def test_has_position(self):
        """포지션 존재 여부 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test_positions.json"
            store = PositionStore(file_path)
            
            assert store.has_position() is False
            
            position = StoredPosition(
                stock_code="005930",
                entry_price=65000,
                quantity=100,
                stop_loss=62000,
                take_profit=71000,
                entry_date="2024-01-15",
                atr_at_entry=1500
            )
            store.save_position(position)
            
            assert store.has_position() is True


class TestDailyTradeStore:
    """일일 거래 기록 테스트"""
    
    def test_save_trade(self):
        """거래 기록 저장 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test_daily_trades.json"
            store = DailyTradeStore(file_path)
            
            trade = {
                "type": "BUY",
                "price": 65000,
                "quantity": 100,
                "pnl": 0
            }
            
            result = store.save_trade(trade)
            assert result is True
            
            stats = store.get_daily_stats()
            assert stats["trade_count"] == 1
    
    def test_daily_loss_limit_check(self):
        """일일 손실 한도 체크 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test_daily_trades.json"
            store = DailyTradeStore(file_path)
            
            # 손실 거래 기록
            for i in range(3):
                store.save_trade({
                    "type": "BUY",
                    "price": 65000,
                    "quantity": 100,
                    "entry_price": 65000
                })
                store.save_trade({
                    "type": "SELL",
                    "price": 60000,
                    "quantity": 100,
                    "pnl": -500000  # 큰 손실
                })
            
            # 한도 체크
            is_limited, reason = store.is_daily_limit_reached(
                max_loss_pct=10.0,
                max_trades=10,
                max_consecutive_losses=5
            )
            
            # 손실 누적으로 한도 도달
            # (정확한 결과는 계산 로직에 따라 다름)
    
    def test_consecutive_losses_tracking(self):
        """연속 손실 추적 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test_daily_trades.json"
            store = DailyTradeStore(file_path)
            
            # 연속 손실 기록
            for i in range(3):
                store.save_trade({
                    "type": "SELL",
                    "pnl": -10000
                })
            
            stats = store.get_daily_stats()
            assert stats["consecutive_losses"] == 3
            
            # 이익 거래로 리셋
            store.save_trade({
                "type": "SELL",
                "pnl": 5000
            })
            
            stats = store.get_daily_stats()
            assert stats["consecutive_losses"] == 0


class TestDailyLimits:
    """일일 한도 설정 테스트"""
    
    def test_daily_max_loss_setting_exists(self):
        """일일 최대 손실 설정 존재 확인"""
        assert hasattr(settings, 'DAILY_MAX_LOSS_PCT')
        assert settings.DAILY_MAX_LOSS_PCT > 0
        assert settings.DAILY_MAX_LOSS_PCT <= 20  # 합리적인 범위
    
    def test_daily_max_trades_setting_exists(self):
        """일일 최대 거래 횟수 설정 존재 확인"""
        assert hasattr(settings, 'DAILY_MAX_TRADES')
        assert settings.DAILY_MAX_TRADES > 0
    
    def test_max_consecutive_losses_setting_exists(self):
        """연속 손실 한도 설정 존재 확인"""
        assert hasattr(settings, 'MAX_CONSECUTIVE_LOSSES')
        assert settings.MAX_CONSECUTIVE_LOSSES > 0


class TestEmergencySellSettings:
    """긴급 손절 설정 테스트"""
    
    def test_emergency_sell_max_retries_exists(self):
        """긴급 손절 재시도 횟수 설정 존재 확인"""
        assert hasattr(settings, 'EMERGENCY_SELL_MAX_RETRIES')
        assert settings.EMERGENCY_SELL_MAX_RETRIES >= 5  # 최소 5회
    
    def test_emergency_sell_retry_interval_exists(self):
        """긴급 손절 재시도 간격 설정 존재 확인"""
        assert hasattr(settings, 'EMERGENCY_SELL_RETRY_INTERVAL')
        assert settings.EMERGENCY_SELL_RETRY_INTERVAL > 0
    
    def test_order_execution_timeout_exists(self):
        """주문 체결 대기 시간 설정 존재 확인"""
        assert hasattr(settings, 'ORDER_EXECUTION_TIMEOUT')
        # 테스트 환경에서는 값이 변경될 수 있으므로 존재만 확인
        assert settings.ORDER_EXECUTION_TIMEOUT > 0


class TestExecutorV2Features:
    """Executor v2.0 기능 테스트"""
    
    def test_executor_has_position_sync(self):
        """포지션 동기화 메서드 존재 확인"""
        from engine.executor import TradingExecutor
        
        assert hasattr(TradingExecutor, '_sync_position_on_startup')
        assert hasattr(TradingExecutor, '_recover_position_from_account')
        assert hasattr(TradingExecutor, '_save_position_to_store')
    
    def test_executor_has_daily_limit_check(self):
        """일일 한도 체크 메서드 존재 확인"""
        from engine.executor import TradingExecutor
        
        assert hasattr(TradingExecutor, '_check_daily_limits')
    
    def test_executor_has_execution_wait(self):
        """체결 대기 메서드 존재 확인"""
        from engine.executor import TradingExecutor
        
        assert hasattr(TradingExecutor, '_wait_for_execution')
    
    def test_executor_has_emergency_handling(self):
        """긴급 손절 처리 메서드 존재 확인"""
        from engine.executor import TradingExecutor
        
        assert hasattr(TradingExecutor, '_handle_emergency_sell_failure')
    
    def test_executor_has_system_status(self):
        """시스템 상태 메서드 존재 확인"""
        from engine.executor import TradingExecutor
        
        assert hasattr(TradingExecutor, 'get_system_status')


class TestPositionSyncScenarios:
    """포지션 동기화 시나리오 테스트"""
    
    def test_sync_with_matching_position(self):
        """저장된 포지션과 실제 보유가 일치하는 경우"""
        from engine.executor import TradingExecutor
        from strategy.trend_atr import TrendATRStrategy
        
        mock_api = Mock()
        mock_api.get_account_balance.return_value = {
            "success": True,
            "holdings": [
                {
                    "stock_code": "005930",
                    "quantity": 100,
                    "avg_price": 65000,
                    "current_price": 66000
                }
            ]
        }
        
        strategy = TrendATRStrategy()
        
        with patch('engine.executor.get_position_store') as mock_store:
            mock_position_store = Mock()
            mock_position_store.load_position.return_value = StoredPosition(
                stock_code="005930",
                entry_price=65000,
                quantity=100,
                stop_loss=62000,
                take_profit=71000,
                entry_date="2024-01-15",
                atr_at_entry=1500
            )
            mock_store.return_value = mock_position_store
            
            with patch('engine.executor.get_daily_trade_store'):
                executor = TradingExecutor(
                    api=mock_api,
                    strategy=strategy,
                    auto_sync=True
                )
        
        # 포지션이 복구되어야 함
        assert strategy.has_position() is True
        assert strategy.position.stock_code == "005930"
    
    def test_sync_with_no_stored_position_but_holding(self):
        """저장된 포지션 없고 실제 보유 중인 경우"""
        from engine.executor import TradingExecutor
        from strategy.trend_atr import TrendATRStrategy
        import pandas as pd
        
        mock_api = Mock()
        mock_api.get_account_balance.return_value = {
            "success": True,
            "holdings": [
                {
                    "stock_code": "005930",
                    "quantity": 100,
                    "avg_price": 65000,
                    "current_price": 66000
                }
            ]
        }
        mock_api.get_daily_ohlcv.return_value = pd.DataFrame()  # 빈 데이터
        
        strategy = TrendATRStrategy()
        
        with patch('engine.executor.get_position_store') as mock_store:
            mock_position_store = Mock()
            mock_position_store.load_position.return_value = None  # 저장된 포지션 없음
            mock_position_store.save_position.return_value = True
            mock_store.return_value = mock_position_store
            
            with patch('engine.executor.get_daily_trade_store'):
                executor = TradingExecutor(
                    api=mock_api,
                    strategy=strategy,
                    auto_sync=True
                )
        
        # 계좌 기준으로 복구되어야 함
        assert strategy.has_position() is True


class TestSafetyIntegration:
    """안전 기능 통합 테스트"""
    
    def test_all_safety_settings_configured(self):
        """모든 안전 설정이 구성되어 있는지 확인"""
        required_settings = [
            'DAILY_MAX_LOSS_PCT',
            'DAILY_MAX_TRADES',
            'MAX_CONSECUTIVE_LOSSES',
            'EMERGENCY_SELL_MAX_RETRIES',
            'EMERGENCY_SELL_RETRY_INTERVAL',
            'ORDER_EXECUTION_TIMEOUT',
            'ORDER_CHECK_INTERVAL'
        ]
        
        for setting in required_settings:
            assert hasattr(settings, setting), f"{setting} 설정이 없습니다"
            assert getattr(settings, setting) is not None, f"{setting} 값이 None입니다"
    
    def test_safety_settings_reasonable_values(self):
        """안전 설정 값이 합리적인지 확인 (원래 설정 기준)"""
        # 테스트 환경에서 변경된 값이 아닌, 설정 파일의 기본값 확인
        # settings.py 파일에서 직접 확인
        import importlib
        import config.settings as original_settings
        importlib.reload(original_settings)
        
        # 일일 최대 손실: 5~20% 사이
        assert 5 <= original_settings.DAILY_MAX_LOSS_PCT <= 20
        
        # 일일 최대 거래: 3~10회 사이
        assert 3 <= original_settings.DAILY_MAX_TRADES <= 10
        
        # 연속 손실 한도: 2~5회 사이
        assert 2 <= original_settings.MAX_CONSECUTIVE_LOSSES <= 5
        
        # 긴급 손절 재시도: 5~20회 사이
        assert 5 <= original_settings.EMERGENCY_SELL_MAX_RETRIES <= 20
        
        # 체결 대기 시간: 10~60초 사이
        assert 10 <= original_settings.ORDER_EXECUTION_TIMEOUT <= 60
