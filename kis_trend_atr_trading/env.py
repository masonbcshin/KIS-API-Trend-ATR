"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 환경 판별 모듈
═══════════════════════════════════════════════════════════════════════════════

이 모듈은 시스템 전체에서 사용되는 환경 판별 로직을 담당합니다.
다른 모듈에서는 이 모듈을 통해서만 현재 환경을 확인해야 합니다.

★ 구조적 안전장치:
    1. 기본 환경은 항상 DEV (모의투자)입니다.
    2. PROD는 명시적으로 TRADING_MODE=PROD를 설정해야만 활성화됩니다.
    3. PROD 환경에서는 시작 시 경고 메시지가 출력됩니다.

★ 사용 방법:
    from env import get_environment, is_dev, is_prod, Environment

    # 현재 환경 확인
    env = get_environment()
    
    # DEV/PROD 여부 확인
    if is_dev():
        # 모의투자 로직
    elif is_prod():
        # 실계좌 로직 (주의!)

⚠️ 주의사항:
    - 다른 파일에서 직접 os.getenv("TRADING_MODE")를 호출하지 마십시오.
    - 반드시 이 모듈의 함수를 통해 환경을 확인하십시오.
    - 환경 판별 로직의 단일화로 실수를 방지합니다.

═══════════════════════════════════════════════════════════════════════════════
"""

import os
import sys
from pathlib import Path
from enum import Enum
from typing import Optional


class Environment(Enum):
    """
    실행 환경 열거형
    
    ★ DEV가 기본값입니다. PROD는 명시적 설정이 필요합니다.
    """
    DEV = "DEV"    # 모의투자 환경 (기본값)
    PROD = "PROD"  # 실계좌 환경 (주의!)


# ═══════════════════════════════════════════════════════════════════════════════
# 환경 변수 상수
# ═══════════════════════════════════════════════════════════════════════════════

# 환경 변수 이름
TRADING_MODE_ENV_VAR = "TRADING_MODE"

# 허용 모드 (정식)
ALLOWED_TRADING_MODES = {"PAPER", "REAL"}

# 하위 호환 모드 매핑
LEGACY_MODE_MAP = {
    "DEV": "PAPER",
    "PROD": "REAL",
    "LIVE": "REAL",
    "CBT": "PAPER",
    "SIGNAL_ONLY": "PAPER",
}

# 기본값: PAPER(모의투자)
DEFAULT_TRADING_MODE = "PAPER"
DEFAULT_ENVIRONMENT = Environment.DEV


# ═══════════════════════════════════════════════════════════════════════════════
# 캐시된 환경 상태 (시작 시 1회만 결정)
# ═══════════════════════════════════════════════════════════════════════════════

_cached_environment: Optional[Environment] = None
_environment_logged: bool = False


# ═══════════════════════════════════════════════════════════════════════════════
# 환경 판별 함수
# ═══════════════════════════════════════════════════════════════════════════════

def get_environment() -> Environment:
    """
    현재 실행 환경을 반환합니다.
    
    ★ 구조적 안전장치:
        - TRADING_MODE 환경 변수가 "PROD"가 아니면 항상 DEV를 반환합니다.
        - 환경 변수 오타, 잘못된 값 등 모든 예외 상황에서 DEV로 폴백합니다.
    
    Returns:
        Environment: 현재 실행 환경 (DEV 또는 PROD)
    
    Example:
        >>> env = get_environment()
        >>> if env == Environment.PROD:
        ...     print("실계좌 모드입니다. 주의하세요!")
    """
    global _cached_environment, _environment_logged
    
    # 이미 결정된 환경이 있으면 캐시된 값 반환
    if _cached_environment is not None:
        return _cached_environment
    
    mode = get_trading_mode()

    if mode == "REAL":
        _cached_environment = Environment.PROD
    else:
        _cached_environment = Environment.DEV
    
    # 환경 로깅 (최초 1회만)
    if not _environment_logged:
        _log_environment_status(_cached_environment)
        _environment_logged = True
    
    return _cached_environment


def is_dev() -> bool:
    """
    현재 환경이 모의투자(DEV)인지 확인합니다.
    
    Returns:
        bool: DEV 환경이면 True
    
    Example:
        >>> if is_dev():
        ...     print("모의투자 모드입니다.")
    """
    return get_environment() == Environment.DEV


def is_prod() -> bool:
    """
    현재 환경이 실계좌(PROD)인지 확인합니다.
    
    ⚠️ 주의: 이 함수가 True를 반환하면 실제 돈이 거래됩니다.
    
    Returns:
        bool: PROD 환경이면 True
    
    Example:
        >>> if is_prod():
        ...     print("⚠️ 실계좌 모드입니다! 주의하세요!")
    """
    return get_environment() == Environment.PROD


def get_environment_name() -> str:
    """
    현재 환경의 이름을 문자열로 반환합니다.
    
    Returns:
        str: "DEV" 또는 "PROD"
    """
    return get_environment().value


def get_trading_mode() -> str:
    """
    현재 트레이딩 모드를 PAPER/REAL로 반환합니다.

    Raises:
        ValueError: 허용값 외 TRADING_MODE가 설정된 경우
    """
    raw_mode = os.getenv(TRADING_MODE_ENV_VAR, DEFAULT_TRADING_MODE).strip().upper()
    normalized = normalize_trading_mode(raw_mode)

    if normalized not in ALLOWED_TRADING_MODES:
        raise ValueError(
            f"유효하지 않은 TRADING_MODE='{raw_mode}'. 허용값: {sorted(ALLOWED_TRADING_MODES)}"
        )
    return normalized


def normalize_trading_mode(raw_mode: str) -> str:
    """레거시 모드명을 PAPER/REAL 표준 모드명으로 정규화합니다."""
    normalized = LEGACY_MODE_MAP.get(raw_mode, raw_mode)
    return normalized


def assert_not_real_mode(trading_mode: str) -> None:
    """
    PAPER 실행 경로에서 REAL 모드가 감지되면 즉시 예외를 발생시킵니다.
    """
    if trading_mode == "REAL":
        raise AssertionError("PAPER 실행 경로에서 REAL 모드가 감지되었습니다.")


def get_environment_description() -> str:
    """
    현재 환경에 대한 설명을 반환합니다.
    
    Returns:
        str: 환경 설명 문자열
    """
    env = get_environment()
    if env == Environment.DEV:
        return "모의투자 환경 (Paper Trading)"
    else:
        return "⚠️ 실계좌 환경 (Real Trading) - 실제 돈이 거래됩니다!"


# ═══════════════════════════════════════════════════════════════════════════════
# 환경 검증 함수
# ═══════════════════════════════════════════════════════════════════════════════

def validate_environment() -> bool:
    """
    환경 설정이 올바른지 검증합니다.
    
    Returns:
        bool: 검증 성공 여부
    """
    env = get_environment()
    mode = get_trading_mode()

    # .env와 런타임 환경 변수 불일치 확인
    dotenv_mode = _read_dotenv_trading_mode()
    runtime_mode_raw = os.getenv(TRADING_MODE_ENV_VAR, DEFAULT_TRADING_MODE).strip().upper()
    runtime_mode = normalize_trading_mode(runtime_mode_raw)
    if dotenv_mode and runtime_mode and dotenv_mode != runtime_mode:
        print(
            f"⚠️ TRADING_MODE 불일치: .env={dotenv_mode}, runtime={runtime_mode_raw}({runtime_mode}). "
            "프로그램을 종료합니다."
        )
        return False

    # PAPER 모드에서 실계좌 전용 키 존재 차단 (2중 방어)
    if mode == "PAPER":
        real_key_vars = [
            "REAL_KIS_APP_KEY",
            "REAL_KIS_APP_SECRET",
            "REAL_KIS_ACCOUNT_NO",
        ]
        configured = [k for k in real_key_vars if os.getenv(k)]
        if configured:
            print(
                f"⚠️ PAPER 모드에서 실계좌 키가 감지되었습니다: {configured}. "
                "프로그램을 종료합니다."
            )
            return False
    
    # DEV 환경은 항상 유효
    if env == Environment.DEV:
        return True
    
    # PROD 환경 추가 검증
    if env == Environment.PROD:
        # 필수 환경변수 확인
        required_vars = [
            "KIS_APP_KEY",
            "KIS_APP_SECRET", 
            "KIS_ACCOUNT_NO"
        ]
        
        missing_vars = [var for var in required_vars if not os.getenv(var)]
        
        if missing_vars:
            print(f"⚠️ PROD 환경에서 필수 환경변수가 누락되었습니다: {missing_vars}")
            return False
    
    return True


def require_dev_environment() -> None:
    """
    DEV 환경이 아니면 프로그램을 종료합니다.
    
    특정 작업이 반드시 DEV 환경에서만 수행되어야 할 때 사용합니다.
    
    Raises:
        SystemExit: PROD 환경일 경우
    """
    if is_prod():
        print("═" * 60)
        print("❌ 오류: 이 작업은 DEV(모의투자) 환경에서만 수행할 수 있습니다.")
        print("   현재 환경: PROD (실계좌)")
        print("═" * 60)
        sys.exit(1)


def require_prod_confirmation() -> bool:
    """
    PROD 환경에서 사용자 확인을 요청합니다.
    
    ★ 이 함수는 실계좌 주문 전 호출되어야 합니다.
    
    Returns:
        bool: 사용자가 확인한 경우 True
    """
    if is_dev():
        return True
    
    print("\n" + "═" * 60)
    print("⚠️⚠️⚠️  실계좌 환경 확인  ⚠️⚠️⚠️")
    print("═" * 60)
    print("현재 PROD(실계좌) 환경에서 실행 중입니다.")
    print("실제 돈이 거래됩니다.")
    print("═" * 60)
    
    try:
        response = input("계속하려면 'CONFIRM'을 입력하세요: ").strip()
        return response == "CONFIRM"
    except (EOFError, KeyboardInterrupt):
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# 내부 유틸리티 함수
# ═══════════════════════════════════════════════════════════════════════════════

def _log_environment_status(env: Environment) -> None:
    """
    환경 상태를 로깅합니다.
    
    PROD 환경에서는 눈에 띄는 경고 메시지를 출력합니다.
    
    Args:
        env: 현재 환경
    """
    if env == Environment.DEV:
        print("╔═══════════════════════════════════════════════════════════╗")
        print("║  📊 모의투자 환경 (DEV)                                    ║")
        print("║  가상 자금으로 거래합니다. 실제 손익이 발생하지 않습니다.  ║")
        print("╚═══════════════════════════════════════════════════════════╝")
    else:
        # PROD 환경: 눈에 띄는 경고 출력
        print("")
        print("╔═══════════════════════════════════════════════════════════╗")
        print("║  ⚠️⚠️⚠️  REAL ACCOUNT MODE  ⚠️⚠️⚠️                          ║")
        print("╠═══════════════════════════════════════════════════════════╣")
        print("║  🔴 실계좌 환경 (PROD)에서 실행 중입니다.                  ║")
        print("║  🔴 실제 돈이 거래됩니다!                                  ║")
        print("║  🔴 모든 주문은 실제 주문으로 처리됩니다!                  ║")
        print("╚═══════════════════════════════════════════════════════════╝")
        print("")


def _read_dotenv_trading_mode() -> Optional[str]:
    """
    .env 파일의 TRADING_MODE 값을 읽어 정규화합니다.
    파일/값이 없으면 None을 반환합니다.
    """
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return None

    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() != TRADING_MODE_ENV_VAR:
                continue
            raw_mode = value.strip().strip('"').strip("'").upper()
            normalized = LEGACY_MODE_MAP.get(raw_mode, raw_mode)
            if normalized in ALLOWED_TRADING_MODES:
                return normalized
            return raw_mode
    except Exception:
        return None

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 모듈 레벨 초기화
# ═══════════════════════════════════════════════════════════════════════════════

def _initialize_on_import() -> None:
    """
    모듈 임포트 시 환경을 초기화합니다.
    
    ★ 프로그램 시작 시 환경이 결정되고 이후 변경되지 않습니다.
    """
    # 환경 결정 (캐시됨)
    get_environment()


# 모듈 임포트 시 자동 초기화
_initialize_on_import()


# ═══════════════════════════════════════════════════════════════════════════════
# 테스트용 함수 (실제 운영에서는 사용 금지)
# ═══════════════════════════════════════════════════════════════════════════════

def _reset_environment_cache_for_testing() -> None:
    """
    ⚠️ 테스트 전용: 환경 캐시를 리셋합니다.
    
    실제 운영 코드에서는 절대 호출하지 마십시오.
    """
    global _cached_environment, _environment_logged
    _cached_environment = None
    _environment_logged = False
