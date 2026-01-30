"""
KIS Trend-ATR Trading System - 통합 설정 파일

═══════════════════════════════════════════════════════════════════════════════
⚠️ 이 파일은 실행 모드에 따라 적절한 설정을 자동으로 로드합니다.
═══════════════════════════════════════════════════════════════════════════════

★ 실행 모드별 설정 파일:
  - DRY_RUN: settings_base.py (가장 안전)
  - PAPER: settings_paper.py (모의투자용)
  - REAL: settings_real.py (실계좌용, 매우 보수적)

★ 설정 로드 순서:
  1. .env 파일 로드
  2. EXECUTION_MODE 환경변수 확인
  3. 해당 모드의 설정 파일 로드

작성자: KIS Trend-ATR Trading System
버전: 2.0.0
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# .env 파일 로드
env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)


# ═══════════════════════════════════════════════════════════════════════════════
# 실행 모드별 설정 로드
# ═══════════════════════════════════════════════════════════════════════════════

def _get_execution_mode() -> str:
    """환경변수에서 실행 모드 가져오기"""
    mode = os.getenv("EXECUTION_MODE", "DRY_RUN").upper()
    
    # 하위 호환성
    mode_map = {
        "CBT": "DRY_RUN",
        "SIGNAL_ONLY": "DRY_RUN",
        "LIVE": "REAL",
    }
    
    return mode_map.get(mode, mode)


# 현재 실행 모드
_CURRENT_MODE = _get_execution_mode()

# 모드별 설정 로드
if _CURRENT_MODE == "REAL":
    from config.settings_real import *
    _LOADED_SETTINGS = "settings_real.py"
elif _CURRENT_MODE == "PAPER":
    from config.settings_paper import *
    _LOADED_SETTINGS = "settings_paper.py"
else:
    # 기본값: DRY_RUN (가장 안전)
    from config.settings_base import *
    _LOADED_SETTINGS = "settings_base.py"

# 로드된 설정 파일 로깅
import logging
logging.getLogger("settings").info(
    f"[SETTINGS] {_CURRENT_MODE} 모드 → {_LOADED_SETTINGS} 로드됨"
)


# ═══════════════════════════════════════════════════════════════════════════════
# 하위 호환성을 위한 변수 (기존 코드 지원)
# ═══════════════════════════════════════════════════════════════════════════════

# TRADING_MODE (하위 호환성)
TRADING_MODE = _CURRENT_MODE
if _CURRENT_MODE == "DRY_RUN":
    TRADING_MODE = "CBT"  # 기존 코드 호환

# IS_PAPER_TRADING (하위 호환성)
IS_PAPER_TRADING = _CURRENT_MODE != "REAL"


# ═══════════════════════════════════════════════════════════════════════════════
# 설정 검증 함수 (하위 호환성)
# ═══════════════════════════════════════════════════════════════════════════════

def validate_settings() -> bool:
    """
    필수 설정값들이 올바르게 설정되었는지 검증합니다.
    
    Returns:
        bool: 검증 성공 여부
    """
    errors = []
    
    # API 키 검증
    if not APP_KEY:
        errors.append("KIS_APP_KEY가 설정되지 않았습니다.")
    if not APP_SECRET:
        errors.append("KIS_APP_SECRET이 설정되지 않았습니다.")
    if not ACCOUNT_NO:
        errors.append("KIS_ACCOUNT_NO가 설정되지 않았습니다.")
    
    # 안전 설정 검증
    if not ENABLE_GAP_PROTECTION:
        errors.append("⚠️ ENABLE_GAP_PROTECTION이 False입니다. 매우 위험합니다!")
    
    # REAL 모드일 때 추가 검증
    if _CURRENT_MODE == "REAL":
        if "openapivts" in KIS_BASE_URL:
            errors.append("⚠️ REAL 모드이지만 모의투자 URL을 사용 중입니다.")
    
    if errors:
        for error in errors:
            print(f"[설정 오류] {error}")
        return False
    
    return True


def get_settings_summary() -> str:
    """
    현재 설정 요약을 반환합니다.
    
    Returns:
        str: 설정 요약 문자열
    """
    telegram_status = "✅ 활성화" if (TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) else "❌ 비활성화"
    
    mode_emoji = {
        "REAL": "🔴",
        "PAPER": "🟡",
        "DRY_RUN": "🟢",
        "CBT": "🟢",
    }
    
    return f"""
═══════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 설정 요약
═══════════════════════════════════════════════════════════════
[실행 모드]
  모드: {mode_emoji.get(_CURRENT_MODE, '❓')} {_CURRENT_MODE}
  설정 파일: {_LOADED_SETTINGS}
  API URL: {KIS_BASE_URL}

[리스크 관리]
  일일 손실 한도: {DAILY_MAX_LOSS_PERCENT}%
  누적 드로다운 한도: {MAX_CUMULATIVE_DRAWDOWN_PCT}%
  갭 보호: {'✅ ON' if ENABLE_GAP_PROTECTION else '❌ OFF'}
  Kill Switch: {'⛔ ON' if ENABLE_KILL_SWITCH else '✅ OFF'}

[전략 파라미터]
  종목: {DEFAULT_STOCK_CODE}
  손절 배수: {ATR_MULTIPLIER_SL}x ATR
  익절 배수: {ATR_MULTIPLIER_TP}x ATR
  트레일링 스탑: {'✅ ON' if ENABLE_TRAILING_STOP else '❌ OFF'}

[주문 설정]
  주문 수량: {ORDER_QUANTITY}주
  일일 최대 거래: {DAILY_MAX_TRADES}회

[텔레그램 알림]
  상태: {telegram_status}
═══════════════════════════════════════════════════════════════
"""


def is_cbt_mode() -> bool:
    """CBT(DRY_RUN) 모드인지 확인합니다."""
    return _CURRENT_MODE == "DRY_RUN" or TRADING_MODE == "CBT"


def is_dry_run_mode() -> bool:
    """DRY_RUN 모드인지 확인합니다."""
    return _CURRENT_MODE == "DRY_RUN"


def is_live_mode() -> bool:
    """REAL(실계좌) 모드인지 확인합니다."""
    return _CURRENT_MODE == "REAL"


def is_paper_mode() -> bool:
    """PAPER(모의투자) 모드인지 확인합니다."""
    return _CURRENT_MODE == "PAPER"


def can_place_orders() -> bool:
    """
    실제 주문이 가능한 모드인지 확인합니다.
    
    ★ DRY_RUN: False (가상 체결만)
    ★ PAPER: True (모의투자 API)
    ★ REAL: 이중 승인 필요
    """
    if _CURRENT_MODE == "DRY_RUN":
        return False
    
    if _CURRENT_MODE == "PAPER":
        return True
    
    if _CURRENT_MODE == "REAL":
        # 실계좌는 이중 승인 필요
        from config.execution_mode import get_execution_mode_manager
        return get_execution_mode_manager().can_place_orders()
    
    return False


def get_execution_mode() -> str:
    """현재 실행 모드 반환"""
    return _CURRENT_MODE


def get_cbt_settings_summary() -> str:
    """
    DRY_RUN(CBT) 모드 설정 요약을 반환합니다.
    
    Returns:
        str: 설정 요약 문자열
    """
    if _CURRENT_MODE != "DRY_RUN":
        return ""
    
    return f"""
═══════════════════════════════════════════════════════════════
🟢 DRY_RUN (가상 체결) 모드 설정
═══════════════════════════════════════════════════════════════
• 초기 자본금: {INITIAL_CAPITAL:,}원
• 수수료율: {COMMISSION_RATE * 100:.3f}%
• 자동 리포트: {'✅ 활성화' if CBT_AUTO_REPORT_ENABLED else '❌ 비활성화'}
• 데이터 저장 경로: {CBT_DATA_DIR}
═══════════════════════════════════════════════════════════════
✅ DRY_RUN 모드: 실계좌 주문이 발생하지 않습니다.
   모든 체결은 가상으로 처리됩니다.
═══════════════════════════════════════════════════════════════
"""
