"""
KIS Trend-ATR Trading System - 실행 모드 관리 모듈

⚠️ 실계좌 안전 보호를 위한 핵심 모듈입니다.

═══════════════════════════════════════════════════════════════════════════════
실행 모드 3단계 구조
═══════════════════════════════════════════════════════════════════════════════

1. DRY_RUN (가장 안전)
   - 매매 판단만 수행, 주문 API 절대 호출 ❌
   - 가상 체결로 성과 측정
   - 텔레그램으로 판단 결과만 전송

2. PAPER (모의투자)
   - 한국투자증권 모의투자 API 사용
   - 실제 주문 발생하지만 가상 자금
   - 전략 실전 테스트용

3. REAL (실계좌) ⚠️
   - 실계좌 API 사용, 실제 돈 거래
   - 기본 비활성화
   - 환경변수 + 설정파일 이중 승인 필수

═══════════════════════════════════════════════════════════════════════════════
안전 장치
═══════════════════════════════════════════════════════════════════════════════

★ REAL 모드 활성화 조건:
  1. EXECUTION_MODE=REAL 환경변수 설정
  2. ENABLE_REAL_TRADING=true 환경변수 설정
  3. 설정 파일에서 REAL_TRADING_CONFIRMED=True

세 가지 모두 충족해야만 실계좌 주문 가능

★ 이중 차단:
  - API 레벨에서 모드 검증
  - 주문 함수에서 모드 검증
  - 텔레그램 알림으로 모드 표시

작성자: KIS Trend-ATR Trading System
버전: 2.0.0 (안전 강화 버전)
"""

import os
import sys
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Callable, Any
from functools import wraps

from utils.logger import get_logger

logger = get_logger("execution_mode")


# ═══════════════════════════════════════════════════════════════════════════════
# 실행 모드 열거형
# ═══════════════════════════════════════════════════════════════════════════════

class ExecutionMode(Enum):
    """
    실행 모드 열거형
    
    ★ 중학생도 이해할 수 있는 설명:
        - DRY_RUN: 연습만 (실제 주문 절대 안 함)
        - PAPER: 모의투자 (가짜 돈으로 실전 연습)
        - REAL: 실계좌 (진짜 돈, 위험!)
    """
    DRY_RUN = "DRY_RUN"   # 가상 체결만
    PAPER = "PAPER"       # 모의투자 API
    REAL = "REAL"         # 실계좌 API (위험!)
    
    @classmethod
    def from_string(cls, value: str) -> "ExecutionMode":
        """문자열에서 ExecutionMode로 변환"""
        value = value.upper().strip()
        
        # 하위 호환성: 기존 모드명 지원
        mode_map = {
            "DRY_RUN": cls.DRY_RUN,
            "DRYRUN": cls.DRY_RUN,
            "CBT": cls.DRY_RUN,          # CBT → DRY_RUN으로 매핑
            "SIGNAL_ONLY": cls.DRY_RUN,  # SIGNAL_ONLY → DRY_RUN으로 매핑
            "PAPER": cls.PAPER,
            "REAL": cls.REAL,
            "LIVE": cls.REAL,            # LIVE → REAL로 매핑
        }
        
        if value in mode_map:
            return mode_map[value]
        
        # 기본값: 가장 안전한 DRY_RUN
        logger.warning(
            f"[MODE] 알 수 없는 모드 '{value}' → DRY_RUN으로 안전하게 전환"
        )
        return cls.DRY_RUN
    
    def is_safe(self) -> bool:
        """안전한 모드인지 확인 (REAL이 아닌 모든 모드)"""
        return self != ExecutionMode.REAL
    
    def allows_api_orders(self) -> bool:
        """API 주문이 허용되는 모드인지 확인"""
        return self in (ExecutionMode.PAPER, ExecutionMode.REAL)
    
    def get_display_name(self) -> str:
        """표시용 이름"""
        display_names = {
            ExecutionMode.DRY_RUN: "🟢 DRY_RUN (가상 체결)",
            ExecutionMode.PAPER: "🟡 PAPER (모의투자)",
            ExecutionMode.REAL: "🔴 REAL (실계좌)",
        }
        return display_names.get(self, str(self.value))
    
    def get_emoji(self) -> str:
        """이모지"""
        emojis = {
            ExecutionMode.DRY_RUN: "🟢",
            ExecutionMode.PAPER: "🟡",
            ExecutionMode.REAL: "🔴",
        }
        return emojis.get(self, "❓")


# ═══════════════════════════════════════════════════════════════════════════════
# 실행 모드 관리자
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ExecutionModeConfig:
    """
    실행 모드 설정 데이터 클래스
    
    REAL 모드 활성화를 위한 모든 조건을 관리합니다.
    """
    # 현재 실행 모드
    mode: ExecutionMode
    
    # REAL 모드 활성화 조건
    env_real_enabled: bool = False       # ENABLE_REAL_TRADING 환경변수
    config_real_confirmed: bool = False  # 설정 파일 REAL_TRADING_CONFIRMED
    
    # 추가 안전장치
    kill_switch_active: bool = False     # Kill Switch 상태
    
    def can_execute_real_orders(self) -> bool:
        """
        실계좌 주문이 가능한지 확인
        
        ★ 모든 조건이 충족되어야만 True
        """
        if self.kill_switch_active:
            return False
        
        if self.mode != ExecutionMode.REAL:
            return False
        
        # 이중 승인 체크
        if not self.env_real_enabled:
            return False
        
        if not self.config_real_confirmed:
            return False
        
        return True
    
    def get_rejection_reason(self) -> Optional[str]:
        """실계좌 주문이 불가능한 이유 반환"""
        if self.kill_switch_active:
            return "Kill Switch가 활성화되어 있습니다."
        
        if self.mode != ExecutionMode.REAL:
            return f"현재 모드가 {self.mode.value}입니다. REAL 모드가 아닙니다."
        
        if not self.env_real_enabled:
            return "ENABLE_REAL_TRADING 환경변수가 'true'로 설정되지 않았습니다."
        
        if not self.config_real_confirmed:
            return "설정 파일에서 REAL_TRADING_CONFIRMED가 True가 아닙니다."
        
        return None


class ExecutionModeManager:
    """
    실행 모드 관리자
    
    시스템 전체의 실행 모드를 관리하고,
    주문 실행 전 모드 검증을 수행합니다.
    
    ★ 싱글톤 패턴으로 구현
    
    사용 예시:
        manager = get_execution_mode_manager()
        
        # 현재 모드 확인
        if manager.is_dry_run():
            print("DRY_RUN 모드입니다. 가상 체결만 수행합니다.")
        
        # 주문 실행 전 검증
        if manager.can_place_orders():
            api.place_buy_order(...)
        else:
            print("주문이 허용되지 않는 모드입니다.")
    """
    
    _instance: Optional["ExecutionModeManager"] = None
    
    def __new__(cls, *args, **kwargs):
        """싱글톤 패턴"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """
        실행 모드 관리자 초기화
        
        환경변수와 설정 파일에서 모드 정보를 로드합니다.
        """
        # 이미 초기화되었으면 건너뜀
        if hasattr(self, "_initialized") and self._initialized:
            return
        
        self._initialized = True
        
        # 환경변수에서 모드 로드
        env_mode = os.getenv("EXECUTION_MODE", "DRY_RUN")
        self._mode = ExecutionMode.from_string(env_mode)
        
        # REAL 모드 이중 승인 체크
        self._env_real_enabled = os.getenv(
            "ENABLE_REAL_TRADING", "false"
        ).lower() in ("true", "1", "yes")
        
        # 설정 파일에서 확인 플래그 로드
        self._config_real_confirmed = self._load_config_confirmation()
        
        # Kill Switch 상태
        self._kill_switch_active = os.getenv(
            "KILL_SWITCH", "false"
        ).lower() in ("true", "1", "yes")
        
        # 수동 Kill Switch 파일 체크
        self._check_manual_kill_switch_file()
        
        # 초기화 로그
        self._log_initialization()
        
        # REAL 모드 안전 검증
        if self._mode == ExecutionMode.REAL:
            self._validate_real_mode()
    
    def _load_config_confirmation(self) -> bool:
        """설정 파일에서 REAL_TRADING_CONFIRMED 로드"""
        try:
            # settings_real.py에서 확인 플래그 로드 시도
            from config.settings_real import REAL_TRADING_CONFIRMED
            return REAL_TRADING_CONFIRMED
        except ImportError:
            return False
        except Exception as e:
            logger.warning(f"[MODE] 설정 파일 로드 오류: {e}")
            return False
    
    def _check_manual_kill_switch_file(self) -> None:
        """수동 Kill Switch 파일 체크"""
        from pathlib import Path
        kill_switch_file = Path(__file__).parent.parent / "data" / "KILL_SWITCH"
        
        if kill_switch_file.exists():
            self._kill_switch_active = True
            logger.warning(
                f"[MODE] 수동 Kill Switch 파일 감지: {kill_switch_file}"
            )
    
    def _log_initialization(self) -> None:
        """초기화 로그 출력"""
        logger.info("=" * 60)
        logger.info("[MODE] 실행 모드 관리자 초기화")
        logger.info(f"[MODE] 현재 모드: {self._mode.get_display_name()}")
        logger.info(f"[MODE] ENABLE_REAL_TRADING: {self._env_real_enabled}")
        logger.info(f"[MODE] CONFIG_CONFIRMED: {self._config_real_confirmed}")
        logger.info(f"[MODE] Kill Switch: {'활성화' if self._kill_switch_active else '비활성화'}")
        logger.info("=" * 60)
    
    def _validate_real_mode(self) -> None:
        """REAL 모드 안전 검증"""
        config = self.get_config()
        
        if not config.can_execute_real_orders():
            reason = config.get_rejection_reason()
            
            logger.error("=" * 60)
            logger.error("[MODE] ⛔ REAL 모드 활성화 실패!")
            logger.error(f"[MODE] 사유: {reason}")
            logger.error("[MODE] 안전을 위해 DRY_RUN 모드로 전환합니다.")
            logger.error("=" * 60)
            
            # 안전을 위해 DRY_RUN으로 강제 전환
            self._mode = ExecutionMode.DRY_RUN
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 모드 조회 메서드
    # ═══════════════════════════════════════════════════════════════════════════
    
    @property
    def mode(self) -> ExecutionMode:
        """현재 실행 모드"""
        return self._mode
    
    def get_config(self) -> ExecutionModeConfig:
        """현재 설정 반환"""
        return ExecutionModeConfig(
            mode=self._mode,
            env_real_enabled=self._env_real_enabled,
            config_real_confirmed=self._config_real_confirmed,
            kill_switch_active=self._kill_switch_active
        )
    
    def is_dry_run(self) -> bool:
        """DRY_RUN 모드인지 확인"""
        return self._mode == ExecutionMode.DRY_RUN
    
    def is_paper(self) -> bool:
        """PAPER 모드인지 확인"""
        return self._mode == ExecutionMode.PAPER
    
    def is_real(self) -> bool:
        """REAL 모드인지 확인"""
        return self._mode == ExecutionMode.REAL
    
    def is_safe_mode(self) -> bool:
        """안전한 모드인지 확인 (REAL이 아닌 모든 모드)"""
        return self._mode.is_safe()
    
    def can_place_orders(self) -> bool:
        """
        주문 실행이 가능한지 확인
        
        ★ DRY_RUN: False (가상 체결만)
        ★ PAPER: True (모의투자 API)
        ★ REAL: 이중 승인 필요
        """
        if self._kill_switch_active:
            return False
        
        if self._mode == ExecutionMode.DRY_RUN:
            return False
        
        if self._mode == ExecutionMode.PAPER:
            return True
        
        if self._mode == ExecutionMode.REAL:
            config = self.get_config()
            return config.can_execute_real_orders()
        
        return False
    
    def get_api_base_url(self) -> str:
        """
        모드에 맞는 API Base URL 반환
        
        ★ REAL 모드가 아니면 무조건 모의투자 URL 반환
        """
        PAPER_URL = "https://openapivts.koreainvestment.com:29443"
        REAL_URL = "https://openapi.koreainvestment.com:9443"
        
        if self._mode == ExecutionMode.REAL and self.get_config().can_execute_real_orders():
            return REAL_URL
        
        # 안전을 위해 기본값은 모의투자 URL
        return PAPER_URL
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Kill Switch 관리
    # ═══════════════════════════════════════════════════════════════════════════
    
    def activate_kill_switch(self, reason: str = "수동 활성화") -> None:
        """
        Kill Switch 활성화
        
        즉시 모든 주문을 차단합니다.
        """
        self._kill_switch_active = True
        
        logger.critical("=" * 60)
        logger.critical("[MODE] ⛔ KILL SWITCH 활성화!")
        logger.critical(f"[MODE] 사유: {reason}")
        logger.critical("[MODE] 모든 주문이 즉시 차단됩니다.")
        logger.critical("=" * 60)
        
        # 수동 Kill Switch 파일 생성
        from pathlib import Path
        kill_switch_file = Path(__file__).parent.parent / "data" / "KILL_SWITCH"
        kill_switch_file.parent.mkdir(parents=True, exist_ok=True)
        
        from datetime import datetime
        kill_switch_file.write_text(
            f"{reason}\nActivated at: {datetime.now().isoformat()}"
        )
    
    def deactivate_kill_switch(self) -> None:
        """Kill Switch 비활성화"""
        self._kill_switch_active = False
        
        # 수동 Kill Switch 파일 제거
        from pathlib import Path
        kill_switch_file = Path(__file__).parent.parent / "data" / "KILL_SWITCH"
        
        if kill_switch_file.exists():
            kill_switch_file.unlink()
        
        logger.info("[MODE] Kill Switch 비활성화됨")
    
    @property
    def kill_switch_active(self) -> bool:
        """Kill Switch 활성화 상태"""
        return self._kill_switch_active
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 상태 출력
    # ═══════════════════════════════════════════════════════════════════════════
    
    def print_status(self) -> None:
        """현재 상태 출력"""
        config = self.get_config()
        
        print("\n" + "═" * 60)
        print("         [EXECUTION MODE STATUS]")
        print("═" * 60)
        print(f"  현재 모드: {self._mode.get_display_name()}")
        print(f"  Kill Switch: {'⛔ 활성화' if self._kill_switch_active else '✅ 비활성화'}")
        print("-" * 60)
        print(f"  주문 가능 여부: {'✅ 가능' if self.can_place_orders() else '⛔ 불가'}")
        print(f"  API URL: {self.get_api_base_url()}")
        print("-" * 60)
        
        if self._mode == ExecutionMode.REAL:
            print("  [REAL 모드 이중 승인 상태]")
            print(f"  - ENABLE_REAL_TRADING: {'✅' if self._env_real_enabled else '❌'}")
            print(f"  - CONFIG_CONFIRMED: {'✅' if self._config_real_confirmed else '❌'}")
            
            if not config.can_execute_real_orders():
                reason = config.get_rejection_reason()
                print(f"  - 차단 사유: {reason}")
        
        print("═" * 60 + "\n")
    
    def get_status_dict(self) -> dict:
        """상태를 딕셔너리로 반환"""
        config = self.get_config()
        
        return {
            "mode": self._mode.value,
            "mode_display": self._mode.get_display_name(),
            "kill_switch_active": self._kill_switch_active,
            "can_place_orders": self.can_place_orders(),
            "api_url": self.get_api_base_url(),
            "env_real_enabled": self._env_real_enabled,
            "config_real_confirmed": self._config_real_confirmed,
            "real_orders_allowed": config.can_execute_real_orders() if self._mode == ExecutionMode.REAL else False
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 데코레이터
# ═══════════════════════════════════════════════════════════════════════════════

def require_order_permission(func: Callable) -> Callable:
    """
    주문 권한 검증 데코레이터
    
    ★ 주문 함수에 이 데코레이터를 붙이면
       DRY_RUN 모드에서 자동으로 가상 체결로 전환됩니다.
    
    사용 예시:
        @require_order_permission
        def place_buy_order(stock_code, quantity, price):
            ...
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        manager = get_execution_mode_manager()
        
        if not manager.can_place_orders():
            mode = manager.mode
            logger.info(
                f"[MODE] {mode.value} 모드 - 실제 주문 건너뜀, "
                f"가상 체결 처리: {func.__name__}"
            )
            
            # 가상 체결 결과 반환
            return {
                "success": True,
                "mode": mode.value,
                "virtual": True,
                "message": f"{mode.value} 모드 - 가상 체결"
            }
        
        # 실제 주문 실행
        return func(*args, **kwargs)
    
    return wrapper


def block_in_dry_run(func: Callable) -> Callable:
    """
    DRY_RUN 모드에서 함수 실행 차단 데코레이터
    
    사용 예시:
        @block_in_dry_run
        def dangerous_operation():
            ...
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        manager = get_execution_mode_manager()
        
        if manager.is_dry_run():
            logger.warning(
                f"[MODE] DRY_RUN 모드에서 차단된 함수: {func.__name__}"
            )
            return None
        
        return func(*args, **kwargs)
    
    return wrapper


# ═══════════════════════════════════════════════════════════════════════════════
# 싱글톤 접근 함수
# ═══════════════════════════════════════════════════════════════════════════════

_manager_instance: Optional[ExecutionModeManager] = None


def get_execution_mode_manager() -> ExecutionModeManager:
    """
    싱글톤 ExecutionModeManager 인스턴스 반환
    
    Returns:
        ExecutionModeManager: 실행 모드 관리자
    """
    global _manager_instance
    
    if _manager_instance is None:
        _manager_instance = ExecutionModeManager()
    
    return _manager_instance


def get_current_mode() -> ExecutionMode:
    """현재 실행 모드 반환"""
    return get_execution_mode_manager().mode


def is_dry_run() -> bool:
    """DRY_RUN 모드인지 확인"""
    return get_execution_mode_manager().is_dry_run()


def is_paper() -> bool:
    """PAPER 모드인지 확인"""
    return get_execution_mode_manager().is_paper()


def is_real() -> bool:
    """REAL 모드인지 확인"""
    return get_execution_mode_manager().is_real()


def can_place_orders() -> bool:
    """주문 가능 여부 확인"""
    return get_execution_mode_manager().can_place_orders()


# ═══════════════════════════════════════════════════════════════════════════════
# 모드 설명 (문서용)
# ═══════════════════════════════════════════════════════════════════════════════

MODE_DESCRIPTION = """
═══════════════════════════════════════════════════════════════════════════════
                     실행 모드 3단계 가이드
═══════════════════════════════════════════════════════════════════════════════

🟢 DRY_RUN (권장 시작점)
────────────────────────────────────────────────────────────────────────────────
• 실제 주문: ❌ 없음
• API 호출: 시세 조회만
• 체결 방식: 가상 체결 (현재가 기준)
• 손익 계산: ✅ 가능
• 텔레그램: 판단 결과만 전송

→ 전략 논리 검증에 사용
→ 서버 없이도 성과 측정 가능

🟡 PAPER (실전 테스트)
────────────────────────────────────────────────────────────────────────────────
• 실제 주문: ✅ 모의투자 서버
• API 호출: 모의투자 API
• 체결 방식: 실제 체결 (가상 자금)
• 손익 계산: ✅ 가능
• 텔레그램: 전체 알림

→ 실전과 동일한 환경에서 테스트
→ 실제 체결 지연, 미체결 등 경험

🔴 REAL (실계좌) ⚠️
────────────────────────────────────────────────────────────────────────────────
• 실제 주문: ✅ 실계좌 서버
• API 호출: 실계좌 API
• 체결 방식: 실제 체결 (진짜 돈)
• 손익 계산: ✅ 가능
• 텔레그램: 전체 알림

★ 활성화 조건 (모두 충족 필요):
  1. EXECUTION_MODE=REAL 환경변수
  2. ENABLE_REAL_TRADING=true 환경변수
  3. settings_real.py에서 REAL_TRADING_CONFIRMED=True

⚠️ 하나라도 미충족 시 DRY_RUN으로 자동 전환

═══════════════════════════════════════════════════════════════════════════════
"""


def print_mode_guide() -> None:
    """실행 모드 가이드 출력"""
    print(MODE_DESCRIPTION)


# ═══════════════════════════════════════════════════════════════════════════════
# 직접 실행 시 테스트
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print_mode_guide()
    
    manager = get_execution_mode_manager()
    manager.print_status()
