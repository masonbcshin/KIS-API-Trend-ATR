"""
KIS Trend-ATR Trading System - 리스크 관리 모듈

실계좌 적용 전 필수 안전장치:
1. Kill Switch (긴급 정지)
2. Daily Loss Limit (일일 손실 제한)

이 모듈은 전략 로직을 수정하지 않고 독립적으로 동작합니다.
주문 실행 전 반드시 이 모듈의 체크를 통과해야 합니다.
"""

import sys
from datetime import datetime, date
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, field

from utils.logger import get_logger

logger = get_logger("risk_manager")


# ════════════════════════════════════════════════════════════════
# 데이터 클래스
# ════════════════════════════════════════════════════════════════

@dataclass
class DailyPnL:
    """
    당일 손익 추적 데이터 클래스
    
    Attributes:
        date: 날짜
        starting_capital: 시작 자본금
        realized_pnl: 실현 손익 (청산된 거래)
        trades_count: 거래 횟수
    """
    date: date = field(default_factory=date.today)
    starting_capital: float = 0.0
    realized_pnl: float = 0.0
    trades_count: int = 0
    
    def reset(self, starting_capital: float = 0.0) -> None:
        """당일 기록 초기화"""
        self.date = date.today()
        self.starting_capital = starting_capital
        self.realized_pnl = 0.0
        self.trades_count = 0
    
    def add_trade_pnl(self, pnl: float) -> None:
        """거래 손익 추가"""
        self.realized_pnl += pnl
        self.trades_count += 1
    
    def get_loss_percent(self) -> float:
        """
        시작 자본금 대비 손실 비율 계산
        
        Returns:
            float: 손실 비율 (%). 음수면 손실, 양수면 이익
        """
        if self.starting_capital <= 0:
            return 0.0
        return (self.realized_pnl / self.starting_capital) * 100


# ════════════════════════════════════════════════════════════════
# 리스크 체크 결과 클래스
# ════════════════════════════════════════════════════════════════

@dataclass
class RiskCheckResult:
    """
    리스크 체크 결과
    
    Attributes:
        passed: 체크 통과 여부 (True면 주문 가능)
        reason: 차단 사유 (passed=False인 경우)
        should_exit: 프로그램 종료 필요 여부
    """
    passed: bool
    reason: str = ""
    should_exit: bool = False


# ════════════════════════════════════════════════════════════════
# 리스크 매니저 클래스
# ════════════════════════════════════════════════════════════════

class RiskManager:
    """
    리스크 관리자 클래스
    
    주문 실행 전 반드시 check_order_allowed()를 호출하여
    리스크 조건을 확인해야 합니다.
    
    기능:
        1. Kill Switch: 긴급 정지 (모든 주문 즉시 차단)
        2. Daily Loss Limit: 일일 손실 한도 초과 시 신규 주문 차단
    
    Usage:
        risk_manager = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000
        )
        
        # 주문 전 체크
        result = risk_manager.check_order_allowed()
        if not result.passed:
            logger.warning(result.reason)
            if result.should_exit:
                sys.exit(0)
    """
    
    def __init__(
        self,
        enable_kill_switch: bool = False,
        daily_max_loss_percent: float = 3.0,
        starting_capital: float = 0.0
    ):
        """
        리스크 매니저 초기화
        
        Args:
            enable_kill_switch: 킬 스위치 활성화 여부
            daily_max_loss_percent: 일일 최대 손실 허용 비율 (%)
            starting_capital: 시작 자본금 (원)
        """
        self._enable_kill_switch = enable_kill_switch
        self._daily_max_loss_percent = daily_max_loss_percent
        
        # 일일 손익 추적
        self._daily_pnl = DailyPnL(
            starting_capital=starting_capital
        )
        
        # 일일 손실 한도 도달 플래그
        self._daily_limit_reached = False
        
        logger.info(
            f"[RISK] 리스크 매니저 초기화 완료 | "
            f"Kill Switch: {'ON' if enable_kill_switch else 'OFF'} | "
            f"일일 손실 한도: {daily_max_loss_percent}%"
        )
        
        # 킬 스위치 활성화 상태면 즉시 경고
        if self._enable_kill_switch:
            logger.warning(
                "[RISK] ⚠️ KILL SWITCH ACTIVATED - "
                "모든 신규 주문이 차단됩니다."
            )
    
    # ════════════════════════════════════════════════════════════════
    # 설정 조회/변경
    # ════════════════════════════════════════════════════════════════
    
    @property
    def kill_switch_enabled(self) -> bool:
        """킬 스위치 활성화 상태"""
        return self._enable_kill_switch
    
    @property
    def daily_max_loss_percent(self) -> float:
        """일일 최대 손실 허용 비율"""
        return self._daily_max_loss_percent
    
    @property
    def daily_loss_limit_reached(self) -> bool:
        """일일 손실 한도 도달 여부"""
        return self._daily_limit_reached
    
    def enable_kill_switch(self) -> None:
        """킬 스위치 활성화"""
        self._enable_kill_switch = True
        logger.warning(
            "[RISK] ⚠️ KILL SWITCH ACTIVATED - "
            "모든 신규 주문이 차단됩니다."
        )
    
    def disable_kill_switch(self) -> None:
        """킬 스위치 비활성화"""
        self._enable_kill_switch = False
        logger.info("[RISK] Kill Switch 비활성화됨")
    
    def set_daily_max_loss_percent(self, percent: float) -> None:
        """
        일일 최대 손실 비율 설정
        
        Args:
            percent: 손실 비율 (%, 양수로 입력)
        """
        if percent <= 0:
            logger.warning(
                "[RISK] 일일 손실 한도는 양수여야 합니다. "
                f"입력값: {percent}"
            )
            return
        
        self._daily_max_loss_percent = percent
        logger.info(f"[RISK] 일일 손실 한도 변경: {percent}%")
    
    # ════════════════════════════════════════════════════════════════
    # 일일 손익 관리
    # ════════════════════════════════════════════════════════════════
    
    def set_starting_capital(self, capital: float) -> None:
        """
        시작 자본금 설정
        
        Args:
            capital: 자본금 (원)
        """
        self._daily_pnl.starting_capital = capital
        logger.info(f"[RISK] 시작 자본금 설정: {capital:,.0f}원")
    
    def record_trade_pnl(self, pnl: float) -> None:
        """
        거래 손익 기록
        
        청산된 거래의 손익을 기록합니다.
        일일 손실 한도 체크도 함께 수행합니다.
        
        Args:
            pnl: 손익 금액 (원, 손실은 음수)
        """
        # 날짜가 변경되었으면 초기화
        if self._daily_pnl.date != date.today():
            self._reset_daily_tracking()
        
        self._daily_pnl.add_trade_pnl(pnl)
        
        current_loss_pct = self._daily_pnl.get_loss_percent()
        
        logger.info(
            f"[RISK] 거래 손익 기록: {pnl:+,.0f}원 | "
            f"당일 누적: {self._daily_pnl.realized_pnl:+,.0f}원 "
            f"({current_loss_pct:+.2f}%)"
        )
        
        # 손실 한도 체크
        if current_loss_pct <= -self._daily_max_loss_percent:
            self._daily_limit_reached = True
            logger.warning(
                f"[RISK] ⚠️ Daily loss limit reached! "
                f"손실: {current_loss_pct:.2f}% | "
                f"한도: -{self._daily_max_loss_percent}%"
            )
    
    def _reset_daily_tracking(self) -> None:
        """
        일일 손익 추적 초기화 (날짜 변경 시)
        """
        old_date = self._daily_pnl.date
        old_pnl = self._daily_pnl.realized_pnl
        starting_capital = self._daily_pnl.starting_capital
        
        self._daily_pnl.reset(starting_capital)
        self._daily_limit_reached = False
        
        logger.info(
            f"[RISK] 새로운 거래일 시작 | "
            f"이전일({old_date}): {old_pnl:+,.0f}원 | "
            f"금일 초기화 완료"
        )
    
    def reset_daily_loss_limit(self) -> None:
        """
        일일 손실 한도 플래그 수동 리셋
        
        주의: 이 함수는 신중하게 사용해야 합니다.
        """
        self._daily_limit_reached = False
        self._daily_pnl.reset(self._daily_pnl.starting_capital)
        logger.warning(
            "[RISK] ⚠️ 일일 손실 한도 수동 리셋됨 - "
            "이 작업은 기록됩니다."
        )
    
    # ════════════════════════════════════════════════════════════════
    # 리스크 체크 (핵심 기능)
    # ════════════════════════════════════════════════════════════════
    
    def check_order_allowed(self, is_closing_position: bool = False) -> RiskCheckResult:
        """
        주문 허용 여부를 체크합니다.
        
        이 함수는 모든 주문 실행 전에 반드시 호출해야 합니다.
        
        체크 순서:
            1. Kill Switch 확인
            2. Daily Loss Limit 확인
        
        Args:
            is_closing_position: 청산 주문 여부
                - True: 포지션 청산 주문 (손실 한도 체크 건너뜀)
                - False: 신규 진입 주문
        
        Returns:
            RiskCheckResult: 체크 결과
        """
        # 날짜가 변경되었으면 일일 추적 초기화
        if self._daily_pnl.date != date.today():
            self._reset_daily_tracking()
        
        # 1. Kill Switch 체크
        if self._enable_kill_switch:
            logger.error(
                "[RISK] Kill Switch 활성화 - "
                "모든 주문이 차단됩니다. 프로그램을 종료합니다."
            )
            return RiskCheckResult(
                passed=False,
                reason="[RISK] Kill Switch activated. All trading halted.",
                should_exit=True
            )
        
        # 2. Daily Loss Limit 체크
        # 청산 주문은 허용 (추가 손실 방지 목적으로 포지션 강제 청산 금지)
        if self._daily_limit_reached and not is_closing_position:
            current_loss_pct = self._daily_pnl.get_loss_percent()
            logger.warning(
                f"[RISK] Daily loss limit reached. Trading halted. "
                f"현재 손실: {current_loss_pct:.2f}%"
            )
            return RiskCheckResult(
                passed=False,
                reason=(
                    f"[RISK] Daily loss limit reached. Trading halted. "
                    f"(Loss: {current_loss_pct:.2f}%, "
                    f"Limit: -{self._daily_max_loss_percent}%)"
                ),
                should_exit=False
            )
        
        return RiskCheckResult(passed=True)
    
    def check_kill_switch(self) -> RiskCheckResult:
        """
        킬 스위치만 체크합니다.
        
        Returns:
            RiskCheckResult: 체크 결과
        """
        if self._enable_kill_switch:
            return RiskCheckResult(
                passed=False,
                reason="[RISK] Kill Switch activated. All trading halted.",
                should_exit=True
            )
        return RiskCheckResult(passed=True)
    
    def check_daily_loss_limit(self, is_closing_position: bool = False) -> RiskCheckResult:
        """
        일일 손실 한도만 체크합니다.
        
        Args:
            is_closing_position: 청산 주문 여부
        
        Returns:
            RiskCheckResult: 체크 결과
        """
        # 날짜 변경 체크
        if self._daily_pnl.date != date.today():
            self._reset_daily_tracking()
        
        if self._daily_limit_reached and not is_closing_position:
            current_loss_pct = self._daily_pnl.get_loss_percent()
            return RiskCheckResult(
                passed=False,
                reason=(
                    f"[RISK] Daily loss limit reached. Trading halted. "
                    f"(Loss: {current_loss_pct:.2f}%)"
                ),
                should_exit=False
            )
        return RiskCheckResult(passed=True)
    
    # ════════════════════════════════════════════════════════════════
    # 상태 조회
    # ════════════════════════════════════════════════════════════════
    
    def get_daily_pnl_summary(self) -> Dict:
        """
        당일 손익 요약을 반환합니다.
        
        Returns:
            Dict: 손익 요약 정보
        """
        # 날짜 변경 체크
        if self._daily_pnl.date != date.today():
            self._reset_daily_tracking()
        
        return {
            "date": str(self._daily_pnl.date),
            "starting_capital": self._daily_pnl.starting_capital,
            "realized_pnl": self._daily_pnl.realized_pnl,
            "loss_percent": self._daily_pnl.get_loss_percent(),
            "trades_count": self._daily_pnl.trades_count,
            "daily_limit_reached": self._daily_limit_reached,
            "max_loss_percent": self._daily_max_loss_percent
        }
    
    def get_status(self) -> Dict:
        """
        리스크 매니저 전체 상태를 반환합니다.
        
        Returns:
            Dict: 상태 정보
        """
        pnl_summary = self.get_daily_pnl_summary()
        
        return {
            "kill_switch_enabled": self._enable_kill_switch,
            "daily_max_loss_percent": self._daily_max_loss_percent,
            "daily_limit_reached": self._daily_limit_reached,
            "daily_pnl": pnl_summary,
            "trading_allowed": not self._enable_kill_switch and not self._daily_limit_reached
        }
    
    def print_status(self) -> None:
        """
        현재 리스크 상태를 콘솔에 출력합니다.
        """
        status = self.get_status()
        pnl = status["daily_pnl"]
        
        print("\n" + "═" * 60)
        print("             [RISK MANAGER STATUS]")
        print("═" * 60)
        print(f"  Kill Switch        : {'⛔ ON (주문 차단)' if status['kill_switch_enabled'] else '✅ OFF'}")
        print(f"  일일 손실 한도     : {status['daily_max_loss_percent']}%")
        print(f"  한도 도달 여부     : {'⛔ YES (신규 주문 차단)' if status['daily_limit_reached'] else '✅ NO'}")
        print(f"  거래 가능 상태     : {'✅ YES' if status['trading_allowed'] else '⛔ NO'}")
        print("-" * 60)
        print(f"  날짜               : {pnl['date']}")
        print(f"  시작 자본금        : {pnl['starting_capital']:,.0f}원")
        print(f"  당일 실현 손익     : {pnl['realized_pnl']:+,.0f}원")
        print(f"  손익률             : {pnl['loss_percent']:+.2f}%")
        print(f"  거래 횟수          : {pnl['trades_count']}회")
        print("═" * 60 + "\n")


# ════════════════════════════════════════════════════════════════
# 헬퍼 함수 (편의용)
# ════════════════════════════════════════════════════════════════

def create_risk_manager_from_settings() -> RiskManager:
    """
    settings.py의 설정값으로 RiskManager를 생성합니다.
    
    Returns:
        RiskManager: 설정된 리스크 매니저
    """
    from config import settings
    
    # 설정값 가져오기 (없으면 기본값 사용)
    enable_kill_switch = getattr(settings, "ENABLE_KILL_SWITCH", False)
    daily_max_loss_percent = getattr(settings, "DAILY_MAX_LOSS_PERCENT", 3.0)
    starting_capital = getattr(settings, "BACKTEST_INITIAL_CAPITAL", 10_000_000)
    
    return RiskManager(
        enable_kill_switch=enable_kill_switch,
        daily_max_loss_percent=daily_max_loss_percent,
        starting_capital=starting_capital
    )


def safe_exit_with_message(message: str) -> None:
    """
    안전하게 프로그램을 종료합니다.
    
    Args:
        message: 종료 메시지
    """
    logger.critical(f"[RISK] 프로그램 종료: {message}")
    print("\n" + "=" * 60)
    print(f"[RISK] 안전 종료: {message}")
    print("=" * 60 + "\n")
    sys.exit(0)
