"""
KIS Trend-ATR Trading System - 포지션 매니저 테스트

테스트 항목:
- 포지션 오픈/청산
- 포지션 복구
- Exit 조건 체크
- 트레일링 스탑
"""

import pytest
import sys
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch
import tempfile
import json

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.position_manager import (
    PositionManager,
    ManagedPosition,
    PositionState,
    ExitReason,
    get_position_manager
)


class TestPositionManager:
    """포지션 매니저 테스트"""
    
    @pytest.fixture
    def manager(self):
        """테스트용 포지션 매니저"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PositionManager(
                data_dir=Path(tmpdir),
                max_positions=5,
                enable_trailing=True,
                trailing_atr_multiplier=2.0,
                trailing_activation_pct=1.0
            )
            yield manager
    
    def test_open_position(self, manager):
        """포지션 오픈 테스트"""
        position = manager.open_position(
            stock_code="005930",
            entry_price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        assert position is not None
        assert position.stock_code == "005930"
        assert position.entry_price == 70000
        assert position.quantity == 10
        assert position.stop_loss == 68000
        assert position.take_profit == 75000
        assert position.atr_at_entry == 1500
        assert manager.has_position("005930")
    
    def test_duplicate_position_blocked(self, manager):
        """중복 포지션 차단 테스트"""
        manager.open_position(
            stock_code="005930",
            entry_price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        # 동일 종목 중복 오픈 시도
        duplicate = manager.open_position(
            stock_code="005930",
            entry_price=71000,
            quantity=5,
            stop_loss=69000,
            take_profit=76000,
            atr=1500
        )
        
        assert duplicate is None
        assert manager.count_positions() == 1
    
    def test_max_positions_limit(self, manager):
        """최대 포지션 수 제한 테스트"""
        # 5개 포지션 오픈
        for i in range(5):
            manager.open_position(
                stock_code=f"00593{i}",
                entry_price=70000,
                quantity=10,
                stop_loss=68000,
                take_profit=75000,
                atr=1500
            )
        
        assert manager.count_positions() == 5
        
        # 6번째 포지션 시도 (실패해야 함)
        sixth = manager.open_position(
            stock_code="999999",
            entry_price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        assert sixth is None
        assert manager.count_positions() == 5
    
    def test_close_position(self, manager):
        """포지션 청산 테스트"""
        manager.open_position(
            stock_code="005930",
            entry_price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        closed = manager.close_position(
            stock_code="005930",
            exit_price=72000,
            reason=ExitReason.TAKE_PROFIT
        )
        
        assert closed is not None
        assert closed.state == PositionState.EXITED
        assert closed.exit_price == 72000
        assert closed.exit_reason == ExitReason.TAKE_PROFIT
        assert closed.realized_pnl == (72000 - 70000) * 10
        assert not manager.has_position("005930")
    
    def test_update_position(self, manager):
        """포지션 업데이트 테스트"""
        manager.open_position(
            stock_code="005930",
            entry_price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        updated = manager.update_position("005930", current_price=72000)
        
        assert updated is not None
        assert updated.current_price == 72000
        assert updated.unrealized_pnl == (72000 - 70000) * 10
        assert updated.highest_price == 72000


class TestExitConditions:
    """Exit 조건 체크 테스트"""
    
    @pytest.fixture
    def manager_with_position(self):
        """포지션이 있는 매니저"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PositionManager(
                data_dir=Path(tmpdir),
                enable_trailing=True,
                trailing_atr_multiplier=2.0,
                trailing_activation_pct=1.0
            )
            
            manager.open_position(
                stock_code="005930",
                entry_price=70000,
                quantity=10,
                stop_loss=68000,
                take_profit=75000,
                atr=1500
            )
            
            yield manager
    
    def test_atr_stop_triggered(self, manager_with_position):
        """ATR 손절 조건 테스트"""
        exit_reason = manager_with_position.check_exit_conditions(
            stock_code="005930",
            current_price=67000  # 손절가(68000) 미만
        )
        
        assert exit_reason == ExitReason.ATR_STOP
    
    def test_take_profit_triggered(self, manager_with_position):
        """익절 조건 테스트"""
        exit_reason = manager_with_position.check_exit_conditions(
            stock_code="005930",
            current_price=76000  # 익절가(75000) 초과
        )
        
        assert exit_reason == ExitReason.TAKE_PROFIT
    
    def test_trend_broken_triggered(self, manager_with_position):
        """추세 이탈 조건 테스트"""
        exit_reason = manager_with_position.check_exit_conditions(
            stock_code="005930",
            current_price=71000,
            current_trend_bullish=False  # 하락 추세
        )
        
        assert exit_reason == ExitReason.TREND_BROKEN
    
    def test_no_exit_in_range(self, manager_with_position):
        """범위 내에서 Exit 없음 테스트"""
        exit_reason = manager_with_position.check_exit_conditions(
            stock_code="005930",
            current_price=72000,  # 손절가와 익절가 사이
            current_trend_bullish=True
        )
        
        assert exit_reason is None


class TestTrailingStop:
    """트레일링 스탑 테스트"""
    
    @pytest.fixture
    def manager(self):
        """트레일링이 활성화된 매니저"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PositionManager(
                data_dir=Path(tmpdir),
                enable_trailing=True,
                trailing_atr_multiplier=2.0,
                trailing_activation_pct=1.0
            )
            yield manager
    
    def test_trailing_stop_activation(self, manager):
        """트레일링 스탑 활성화 테스트"""
        manager.open_position(
            stock_code="005930",
            entry_price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        initial_trailing = manager.get_position("005930").trailing_stop
        
        # 가격 상승 → 트레일링 갱신 예상
        # 수익률 = (72000-70000)/70000 = 2.86% > 1% (활성화 기준)
        manager.update_position("005930", current_price=72000)
        
        updated_trailing = manager.get_position("005930").trailing_stop
        
        # 새 트레일링 = 72000 - (1500 * 2.0) = 69000
        assert updated_trailing > initial_trailing
    
    def test_trailing_stop_only_increases(self, manager):
        """트레일링 스탑은 상승만 가능 테스트"""
        manager.open_position(
            stock_code="005930",
            entry_price=70000,
            quantity=10,
            stop_loss=68000,
            take_profit=75000,
            atr=1500
        )
        
        # 상승
        manager.update_position("005930", current_price=73000)
        high_trailing = manager.get_position("005930").trailing_stop
        
        # 하락
        manager.update_position("005930", current_price=71000)
        current_trailing = manager.get_position("005930").trailing_stop
        
        # 트레일링은 하락하지 않아야 함
        assert current_trailing == high_trailing


class TestPositionPersistence:
    """포지션 영속화 테스트"""
    
    def test_save_and_load(self):
        """저장 및 로드 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 포지션 생성 및 저장
            manager1 = PositionManager(data_dir=Path(tmpdir))
            manager1.open_position(
                stock_code="005930",
                entry_price=70000,
                quantity=10,
                stop_loss=68000,
                take_profit=75000,
                atr=1500
            )
            
            # 새 인스턴스로 로드
            manager2 = PositionManager(data_dir=Path(tmpdir))
            
            assert manager2.has_position("005930")
            
            loaded = manager2.get_position("005930")
            assert loaded.entry_price == 70000
            assert loaded.quantity == 10
            assert loaded.stop_loss == 68000
    
    def test_position_recovery(self):
        """포지션 복구 테스트 (프로그램 재시작 시나리오)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. 첫 번째 실행 - 포지션 오픈
            manager1 = PositionManager(data_dir=Path(tmpdir))
            manager1.open_position(
                stock_code="005930",
                entry_price=70000,
                quantity=10,
                stop_loss=68000,
                take_profit=75000,
                atr=1500
            )
            del manager1  # 종료 시뮬레이션
            
            # 2. 두 번째 실행 - 포지션 복구
            manager2 = PositionManager(data_dir=Path(tmpdir))
            
            assert manager2.has_position("005930")
            position = manager2.get_position("005930")
            
            # ATR은 진입 시 값 유지 (재계산 금지)
            assert position.atr_at_entry == 1500


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
