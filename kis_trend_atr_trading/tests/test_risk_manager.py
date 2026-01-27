"""
KIS Trend-ATR Trading System - Risk Manager 테스트

Kill Switch 및 Daily Loss Limit 기능 테스트
"""

import sys
from pathlib import Path
from datetime import date
from unittest.mock import patch, MagicMock
import pytest

# 프로젝트 루트를 path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from engine.risk_manager import (
    RiskManager,
    RiskCheckResult,
    DailyPnL,
    create_risk_manager_from_settings
)


# ════════════════════════════════════════════════════════════════
# DailyPnL 클래스 테스트
# ════════════════════════════════════════════════════════════════

class TestDailyPnL:
    """DailyPnL 데이터 클래스 테스트"""
    
    def test_초기화(self):
        """DailyPnL 기본 초기화 확인"""
        pnl = DailyPnL(starting_capital=10_000_000)
        
        assert pnl.date == date.today()
        assert pnl.starting_capital == 10_000_000
        assert pnl.realized_pnl == 0
        assert pnl.trades_count == 0
    
    def test_거래_손익_추가(self):
        """거래 손익 추가 기능 테스트"""
        pnl = DailyPnL(starting_capital=10_000_000)
        
        # 첫 번째 거래: +100,000원
        pnl.add_trade_pnl(100_000)
        assert pnl.realized_pnl == 100_000
        assert pnl.trades_count == 1
        
        # 두 번째 거래: -50,000원
        pnl.add_trade_pnl(-50_000)
        assert pnl.realized_pnl == 50_000
        assert pnl.trades_count == 2
    
    def test_손실_비율_계산(self):
        """손실 비율 계산 테스트"""
        pnl = DailyPnL(starting_capital=10_000_000)
        
        # 300,000원 손실 = 3%
        pnl.add_trade_pnl(-300_000)
        assert pnl.get_loss_percent() == pytest.approx(-3.0)
        
        # 추가 손실로 총 500,000원 손실 = 5%
        pnl.add_trade_pnl(-200_000)
        assert pnl.get_loss_percent() == pytest.approx(-5.0)
    
    def test_손실_비율_자본금_0(self):
        """자본금이 0일 때 손실 비율 계산"""
        pnl = DailyPnL(starting_capital=0)
        pnl.add_trade_pnl(-100_000)
        
        assert pnl.get_loss_percent() == 0.0
    
    def test_초기화_리셋(self):
        """리셋 기능 테스트"""
        pnl = DailyPnL(starting_capital=10_000_000)
        pnl.add_trade_pnl(-300_000)
        
        pnl.reset(starting_capital=15_000_000)
        
        assert pnl.date == date.today()
        assert pnl.starting_capital == 15_000_000
        assert pnl.realized_pnl == 0
        assert pnl.trades_count == 0


# ════════════════════════════════════════════════════════════════
# RiskManager - Kill Switch 테스트
# ════════════════════════════════════════════════════════════════

class TestKillSwitch:
    """Kill Switch 기능 테스트"""
    
    def test_킬스위치_비활성화시_주문허용(self):
        """킬 스위치 비활성화 시 주문이 허용되어야 함"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        result = rm.check_order_allowed()
        
        assert result.passed is True
        assert result.should_exit is False
    
    def test_킬스위치_활성화시_주문차단(self):
        """킬 스위치 활성화 시 모든 주문이 차단되어야 함"""
        rm = RiskManager(
            enable_kill_switch=True,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        result = rm.check_order_allowed()
        
        assert result.passed is False
        assert result.should_exit is True
        assert "Kill Switch" in result.reason
    
    def test_킬스위치_동적_활성화(self):
        """킬 스위치 동적 활성화 테스트"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 초기: 주문 가능
        assert rm.check_order_allowed().passed is True
        
        # 킬 스위치 활성화
        rm.enable_kill_switch()
        
        # 활성화 후: 주문 차단
        assert rm.kill_switch_enabled is True
        result = rm.check_order_allowed()
        assert result.passed is False
        assert result.should_exit is True
    
    def test_킬스위치_동적_비활성화(self):
        """킬 스위치 동적 비활성화 테스트"""
        rm = RiskManager(
            enable_kill_switch=True,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 초기: 주문 차단
        assert rm.check_order_allowed().passed is False
        
        # 킬 스위치 비활성화
        rm.disable_kill_switch()
        
        # 비활성화 후: 주문 가능
        assert rm.kill_switch_enabled is False
        assert rm.check_order_allowed().passed is True
    
    def test_check_kill_switch_개별_체크(self):
        """킬 스위치만 개별 체크"""
        rm = RiskManager(
            enable_kill_switch=True,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        result = rm.check_kill_switch()
        
        assert result.passed is False
        assert result.should_exit is True


# ════════════════════════════════════════════════════════════════
# RiskManager - Daily Loss Limit 테스트
# ════════════════════════════════════════════════════════════════

class TestDailyLossLimit:
    """일일 손실 제한 기능 테스트"""
    
    def test_손실한도_미도달시_주문허용(self):
        """손실 한도 미도달 시 주문이 허용되어야 함"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 2% 손실 (한도: 3%)
        rm.record_trade_pnl(-200_000)
        
        result = rm.check_order_allowed()
        assert result.passed is True
    
    def test_손실한도_초과시_신규주문차단(self):
        """손실 한도 초과 시 신규 주문이 차단되어야 함"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 3.5% 손실 (한도: 3%)
        rm.record_trade_pnl(-350_000)
        
        result = rm.check_order_allowed(is_closing_position=False)
        
        assert result.passed is False
        assert result.should_exit is False
        assert "Daily loss limit reached" in result.reason
    
    def test_손실한도_초과시_청산주문허용(self):
        """손실 한도 초과해도 청산 주문은 허용되어야 함"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 3.5% 손실 (한도 초과)
        rm.record_trade_pnl(-350_000)
        
        # 청산 주문은 허용
        result = rm.check_order_allowed(is_closing_position=True)
        
        assert result.passed is True
    
    def test_손실한도_정확히_도달(self):
        """손실 한도 정확히 도달 시 차단되어야 함"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 정확히 3% 손실
        rm.record_trade_pnl(-300_000)
        
        result = rm.check_order_allowed(is_closing_position=False)
        
        assert result.passed is False
    
    def test_누적_손익_계산(self):
        """여러 거래의 누적 손익 계산"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 첫 번째 거래: +200,000원
        rm.record_trade_pnl(200_000)
        assert rm.check_order_allowed().passed is True
        
        # 두 번째 거래: -400,000원 (누적: -200,000원 = 2%)
        rm.record_trade_pnl(-400_000)
        assert rm.check_order_allowed().passed is True
        
        # 세 번째 거래: -150,000원 (누적: -350,000원 = 3.5%)
        rm.record_trade_pnl(-150_000)
        assert rm.check_order_allowed(is_closing_position=False).passed is False
    
    def test_손실한도_설정_변경(self):
        """손실 한도 동적 변경"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 2.5% 손실
        rm.record_trade_pnl(-250_000)
        
        # 한도 3%: 통과
        assert rm.check_order_allowed().passed is True
        
        # 한도를 2%로 변경
        rm.set_daily_max_loss_percent(2.0)
        
        # 이미 손실 한도 초과 상태이므로 record_trade_pnl을 다시 호출해야 플래그 업데이트
        rm.record_trade_pnl(0)  # 0원 손익으로 체크 트리거
        
        # 한도 2%: 차단 (2.5% > 2%)
        assert rm.check_order_allowed(is_closing_position=False).passed is False
    
    def test_check_daily_loss_limit_개별_체크(self):
        """일일 손실 한도만 개별 체크"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        rm.record_trade_pnl(-350_000)
        
        result = rm.check_daily_loss_limit(is_closing_position=False)
        
        assert result.passed is False


# ════════════════════════════════════════════════════════════════
# RiskManager - 복합 시나리오 테스트
# ════════════════════════════════════════════════════════════════

class TestRiskManagerIntegration:
    """리스크 매니저 통합 테스트"""
    
    def test_킬스위치와_손실한도_둘다체크(self):
        """킬 스위치가 손실 한도보다 우선"""
        rm = RiskManager(
            enable_kill_switch=True,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 손실 한도 미도달이어도 킬 스위치로 차단
        result = rm.check_order_allowed()
        
        assert result.passed is False
        assert result.should_exit is True
        assert "Kill Switch" in result.reason
    
    def test_get_status(self):
        """상태 조회 기능 테스트"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        rm.record_trade_pnl(-200_000)
        
        status = rm.get_status()
        
        assert status["kill_switch_enabled"] is False
        assert status["daily_max_loss_percent"] == 3.0
        assert status["daily_limit_reached"] is False
        assert status["trading_allowed"] is True
        assert status["daily_pnl"]["realized_pnl"] == -200_000
        assert status["daily_pnl"]["loss_percent"] == pytest.approx(-2.0)
    
    def test_get_daily_pnl_summary(self):
        """일일 손익 요약 조회"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        rm.record_trade_pnl(100_000)
        rm.record_trade_pnl(-50_000)
        
        summary = rm.get_daily_pnl_summary()
        
        assert summary["starting_capital"] == 10_000_000
        assert summary["realized_pnl"] == 50_000
        assert summary["trades_count"] == 2
        assert summary["daily_limit_reached"] is False
    
    def test_자본금_설정(self):
        """시작 자본금 설정"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        rm.set_starting_capital(20_000_000)
        
        # 600,000원 손실 = 3% of 20M (한도 도달)
        rm.record_trade_pnl(-600_000)
        
        assert rm.check_order_allowed(is_closing_position=False).passed is False
    
    def test_일일_손실_수동_리셋(self):
        """일일 손실 한도 수동 리셋"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 손실 한도 초과
        rm.record_trade_pnl(-350_000)
        assert rm.check_order_allowed(is_closing_position=False).passed is False
        
        # 수동 리셋
        rm.reset_daily_loss_limit()
        
        # 리셋 후 주문 가능
        assert rm.check_order_allowed().passed is True


# ════════════════════════════════════════════════════════════════
# Factory 함수 테스트
# ════════════════════════════════════════════════════════════════

class TestFactoryFunction:
    """팩토리 함수 테스트"""
    
    def test_설정파일_기반_생성(self):
        """settings.py 기반 RiskManager 생성"""
        # 실제 settings에서 설정값 사용하여 생성 테스트
        rm = create_risk_manager_from_settings()
        
        # 기본값으로 생성되는지 확인
        assert isinstance(rm, RiskManager)
        # settings.py의 기본값들과 일치하는지 확인
        assert rm.kill_switch_enabled is False  # ENABLE_KILL_SWITCH = False
        assert rm.daily_max_loss_percent == 3.0  # DAILY_MAX_LOSS_PERCENT = 3.0


# ════════════════════════════════════════════════════════════════
# RiskCheckResult 테스트
# ════════════════════════════════════════════════════════════════

class TestRiskCheckResult:
    """RiskCheckResult 데이터 클래스 테스트"""
    
    def test_통과_결과(self):
        """통과 결과 생성"""
        result = RiskCheckResult(passed=True)
        
        assert result.passed is True
        assert result.reason == ""
        assert result.should_exit is False
    
    def test_차단_결과(self):
        """차단 결과 생성"""
        result = RiskCheckResult(
            passed=False,
            reason="테스트 차단",
            should_exit=True
        )
        
        assert result.passed is False
        assert result.reason == "테스트 차단"
        assert result.should_exit is True


# ════════════════════════════════════════════════════════════════
# 엣지 케이스 테스트
# ════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """엣지 케이스 테스트"""
    
    def test_자본금_0_손실비율(self):
        """자본금 0일 때 손실 비율 처리"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=0
        )
        
        rm.record_trade_pnl(-100_000)
        
        # 자본금 0이면 손실 비율은 0으로 처리
        summary = rm.get_daily_pnl_summary()
        assert summary["loss_percent"] == 0.0
    
    def test_손실한도_음수_설정_무시(self):
        """음수 손실 한도 설정 시 무시"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        rm.set_daily_max_loss_percent(-5.0)
        
        # 기존 값 유지
        assert rm.daily_max_loss_percent == 3.0
    
    def test_이익_상태에서_주문허용(self):
        """이익 상태에서는 항상 주문 허용"""
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 큰 이익
        rm.record_trade_pnl(1_000_000)
        
        result = rm.check_order_allowed()
        assert result.passed is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
