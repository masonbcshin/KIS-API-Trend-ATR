"""
KIS Trend-ATR Trading System - 기본 설정 (절대 안전 기본값)

═══════════════════════════════════════════════════════════════════════════════
⚠️ 이 파일은 절대 안전한 기본값만 정의합니다.
   실행 환경에 따라 settings_paper.py 또는 settings_real.py가 오버라이드합니다.
═══════════════════════════════════════════════════════════════════════════════

★ 설계 원칙:
  - 모든 값은 "가장 보수적인" 기본값
  - 위험한 설정은 기본 비활성화
  - API Key 등 민감 정보는 환경변수에서만 로드
  - 설정 실수로 계좌가 망가지지 않도록 설계

작성자: KIS Trend-ATR Trading System
버전: 2.0.0
"""

import os
from pathlib import Path
from typing import List

# 프로젝트 루트 경로
PROJECT_ROOT = Path(__file__).parent.parent


# ═══════════════════════════════════════════════════════════════════════════════
# API 인증 정보 (환경변수에서 로드)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 민감 정보는 환경변수에서만 로드
# ★ 기본값은 빈 문자열 (미설정 시 안전하게 실패)
APP_KEY: str = os.getenv("KIS_APP_KEY", "")
APP_SECRET: str = os.getenv("KIS_APP_SECRET", "")
ACCOUNT_NO: str = os.getenv("KIS_ACCOUNT_NO", "")
ACCOUNT_PRODUCT_CODE: str = os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "01")


# ═══════════════════════════════════════════════════════════════════════════════
# 실행 모드 설정 (핵심!)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 기본값: DRY_RUN (가장 안전)
# ★ 가능한 값: DRY_RUN, PAPER, REAL
EXECUTION_MODE: str = os.getenv("EXECUTION_MODE", "DRY_RUN")

# ★ REAL 모드 이중 승인 (환경변수)
# ★ 이 값만으로는 실계좌 주문 불가 (settings_real.py 확인도 필요)
ENABLE_REAL_TRADING: bool = os.getenv(
    "ENABLE_REAL_TRADING", "false"
).lower() in ("true", "1", "yes")


# ═══════════════════════════════════════════════════════════════════════════════
# API Base URL (자동 선택)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 기본값: 모의투자 URL (절대 안전)
# ★ REAL 모드에서도 이중 승인 없으면 모의투자 URL 사용
PAPER_API_URL: str = "https://openapivts.koreainvestment.com:29443"
REAL_API_URL: str = "https://openapi.koreainvestment.com:9443"

def get_api_base_url() -> str:
    """실행 모드에 맞는 API URL 반환"""
    if EXECUTION_MODE == "REAL" and ENABLE_REAL_TRADING:
        return REAL_API_URL
    return PAPER_API_URL

# 현재 API URL (다른 모듈에서 사용)
KIS_BASE_URL: str = get_api_base_url()


# ═══════════════════════════════════════════════════════════════════════════════
# Kill Switch & 긴급 정지 (안전 최우선)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 기본값: False (정상 운영)
# ★ True 시 모든 주문 즉시 차단
ENABLE_KILL_SWITCH: bool = os.getenv(
    "KILL_SWITCH", "false"
).lower() in ("true", "1", "yes")


# ═══════════════════════════════════════════════════════════════════════════════
# 거래 종목 및 수량 설정
# ═══════════════════════════════════════════════════════════════════════════════

# 기본 거래 종목 (삼성전자)
DEFAULT_STOCK_CODE: str = os.getenv("DEFAULT_STOCK_CODE", "005930")

# ★ 1회 주문 수량 (보수적 기본값: 1주)
ORDER_QUANTITY: int = int(os.getenv("ORDER_QUANTITY", "1"))

# ★ 최대 포지션 수 (기본: 1개)
MAX_POSITIONS: int = int(os.getenv("MAX_POSITIONS", "1"))


# ═══════════════════════════════════════════════════════════════════════════════
# Trend-ATR 전략 파라미터 (보수적 기본값)
# ═══════════════════════════════════════════════════════════════════════════════

# ATR(Average True Range) 계산 기간
ATR_PERIOD: int = 14

# 추세 판단용 이동평균 기간
TREND_MA_PERIOD: int = 50

# ★ 손절 배수 (ATR 기준) - 보수적으로 2.0
ATR_MULTIPLIER_SL: float = 2.0

# ★ 익절 배수 (ATR 기준) - 손익비 1.5:1 유지
ATR_MULTIPLIER_TP: float = 3.0


# ═══════════════════════════════════════════════════════════════════════════════
# 리스크 관리 파라미터 (절대 안전 기본값)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 최대 손실 비율 (진입가 대비, %) - 보수적 5%
MAX_LOSS_PCT: float = 5.0

# ★ ATR 급등 임계값 (평균 대비 배수) - 2.5배 초과 시 진입 거부
ATR_SPIKE_THRESHOLD: float = 2.5

# ★ ADX 임계값 (추세 강도) - 25 미만이면 횡보장 판단
ADX_THRESHOLD: float = 25.0

# ADX 계산 기간
ADX_PERIOD: int = 14


# ═══════════════════════════════════════════════════════════════════════════════
# 트레일링 스탑 설정 (기본 활성화)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 트레일링 스탑 활성화 (기본: True - 수익 보호)
ENABLE_TRAILING_STOP: bool = True

# 트레일링 스탑 ATR 배수
TRAILING_STOP_ATR_MULTIPLIER: float = 2.0

# 트레일링 스탑 활성화 기준 (진입가 대비 수익률 %)
TRAILING_STOP_ACTIVATION_PCT: float = 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# 갭 리스크 관리 (필수 활성화)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 갭 보호 활성화 - 멀티데이 전략 필수!
# ★ 기본값: True (절대 False로 변경 금지)
ENABLE_GAP_PROTECTION: bool = True

# ★ 최대 갭 손실 허용 비율 (%) - 보수적 2.0%
MAX_GAP_LOSS_PCT: float = 2.0

# 갭 보호 발동 임계값(%) - 누락/0 이하이면 비활성화 정책
GAP_THRESHOLD_PCT: float = 2.0

# 갭 보호 비교 epsilon(%) - 0% 근처 노이즈 오판 방지
GAP_EPSILON_PCT: float = 0.001

# 갭 보호 기준가: entry | stop | prev_close
GAP_REFERENCE: str = "entry"


# ═══════════════════════════════════════════════════════════════════════════════
# 일일 리스크 한도 (계좌 보호)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 일일 최대 손실 비율 (%) - 보수적 2.0%
DAILY_MAX_LOSS_PERCENT: float = 2.0

# ★ 일일 최대 손실 비율 (position_store 용) - 5.0%
DAILY_MAX_LOSS_PCT: float = 5.0

# ★ 일일 최대 거래 횟수 - 보수적 3회
DAILY_MAX_TRADES: int = 3

# ★ 연속 손실 허용 횟수 - 보수적 2회
MAX_CONSECUTIVE_LOSSES: int = 2


# ═══════════════════════════════════════════════════════════════════════════════
# 누적 드로다운 제어 (핵심 안전장치)
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 최대 누적 드로다운 허용 비율 (%) - 15% 도달 시 Kill Switch
MAX_CUMULATIVE_DRAWDOWN_PCT: float = 15.0

# ★ 누적 드로다운 경고 비율 (%) - 10% 도달 시 텔레그램 경고
CUMULATIVE_DRAWDOWN_WARNING_PCT: float = 10.0


# ═══════════════════════════════════════════════════════════════════════════════
# 주문 실행 설정
# ═══════════════════════════════════════════════════════════════════════════════

# 장종료/주문불가 시 pending_exit 재시도 백오프(분)
PENDING_EXIT_BACKOFF_MINUTES: int = 5

# 긴급 손절 최대 재시도 횟수
EMERGENCY_SELL_MAX_RETRIES: int = 10

# 긴급 손절 재시도 간격 (초)
EMERGENCY_SELL_RETRY_INTERVAL: int = 3

# 체결 확인 최대 대기 시간 (초)
ORDER_EXECUTION_TIMEOUT: int = 45

# 체결 확인 간격 (초)
ORDER_CHECK_INTERVAL: int = 2


# ═══════════════════════════════════════════════════════════════════════════════
# 체결 동기화 설정
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 동기화 주문 실행 활성화 (필수 True)
ENABLE_SYNCHRONIZED_ORDERS: bool = True

# 부분 체결 시 자동 재주문 활성화
ENABLE_PARTIAL_FILL_RETRY: bool = True

# 부분 체결 재주문 최대 횟수
PARTIAL_FILL_MAX_RETRIES: int = 3


# ═══════════════════════════════════════════════════════════════════════════════
# API 호출 설정
# ═══════════════════════════════════════════════════════════════════════════════

# API 호출 타임아웃 (초)
API_TIMEOUT: int = 15

# API 호출 실패 시 최대 재시도 횟수
MAX_RETRIES: int = 3

# 재시도 간 대기 시간 (초)
RETRY_DELAY: float = 1.0

# Rate Limit 대기 시간 (초) - KIS API 초당 20회 제한
RATE_LIMIT_DELAY: float = 0.1


# ═══════════════════════════════════════════════════════════════════════════════
# 실행 주기 설정
# ═══════════════════════════════════════════════════════════════════════════════

# 기본 실행 간격 (초) - 최소 60초 강제
DEFAULT_EXECUTION_INTERVAL: int = 60

# 손절선 근접 시 실행 간격 (초)
NEAR_STOPLOSS_EXECUTION_INTERVAL: int = 15

# 손절선 근접 임계값 (%)
NEAR_STOPLOSS_THRESHOLD_PCT: float = 70.0

# ★ 단일 인스턴스 강제 여부 (필수 True)
ENFORCE_SINGLE_INSTANCE: bool = True


# ═══════════════════════════════════════════════════════════════════════════════
# 시장 운영시간 설정
# ═══════════════════════════════════════════════════════════════════════════════

# KRX 정규장 시작 시간 (09:00)
MARKET_OPEN_HOUR: int = 9
MARKET_OPEN_MINUTE: int = 0

# KRX 정규장 종료 시간 (15:30)
MARKET_CLOSE_HOUR: int = 15
MARKET_CLOSE_MINUTE: int = 30

# ★ 장 운영시간 외 주문 차단 (필수 True)
ENFORCE_MARKET_HOURS: bool = True


# ═══════════════════════════════════════════════════════════════════════════════
# 이벤트 리스크 관리
# ═══════════════════════════════════════════════════════════════════════════════

# 고위험 이벤트일 신규 진입 제한 활성화
ENABLE_EVENT_RISK_CHECK: bool = False

# 고위험 이벤트 날짜 목록 (YYYY-MM-DD 형식)
HIGH_RISK_EVENT_DATES: List[str] = []


# ═══════════════════════════════════════════════════════════════════════════════
# 손익 알림 임계값
# ═══════════════════════════════════════════════════════════════════════════════

# 손절선 근접 알림 비율 (%)
ALERT_NEAR_STOPLOSS_PCT: float = 80.0

# 익절선 근접 알림 비율 (%)
ALERT_NEAR_TAKEPROFIT_PCT: float = 80.0


# ═══════════════════════════════════════════════════════════════════════════════
# 백테스트/CBT 설정
# ═══════════════════════════════════════════════════════════════════════════════

# 초기 자본금 (원)
INITIAL_CAPITAL: int = int(os.getenv("INITIAL_CAPITAL", "10000000"))
BACKTEST_INITIAL_CAPITAL: int = INITIAL_CAPITAL

# 수수료율 (0.015% = 0.00015)
COMMISSION_RATE: float = float(os.getenv("COMMISSION_RATE", "0.00015"))
BACKTEST_COMMISSION_RATE: float = COMMISSION_RATE

# CBT 설정
CBT_INITIAL_CAPITAL: int = INITIAL_CAPITAL
CBT_DATA_DIR: Path = PROJECT_ROOT / "cbt_data"
CBT_STORAGE_TYPE: str = os.getenv("CBT_STORAGE_TYPE", "json")
CBT_COMMISSION_RATE: float = COMMISSION_RATE
CBT_AUTO_REPORT_ENABLED: bool = os.getenv(
    "CBT_AUTO_REPORT_ENABLED", "true"
).lower() in ("true", "1", "yes")
CBT_EQUITY_SAVE_INTERVAL: int = int(os.getenv("CBT_EQUITY_SAVE_INTERVAL", "60"))


# ═══════════════════════════════════════════════════════════════════════════════
# 로깅 설정
# ═══════════════════════════════════════════════════════════════════════════════

# 로그 레벨: DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# 로그 파일 저장 경로
LOG_DIR: Path = PROJECT_ROOT / "logs"


# ═══════════════════════════════════════════════════════════════════════════════
# 텔레그램 알림 설정
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 텔레그램 봇 토큰 (환경변수에서만 로드)
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ★ 텔레그램 채팅 ID (환경변수에서만 로드)
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# 텔레그램 알림 활성화 여부
TELEGRAM_ENABLED: bool = os.getenv(
    "TELEGRAM_ENABLED", "true"
).lower() in ("true", "1", "yes")


# ═══════════════════════════════════════════════════════════════════════════════
# 데이터베이스 설정
# ═══════════════════════════════════════════════════════════════════════════════

# DB 연결 활성화
DB_ENABLED: bool = os.getenv("DB_ENABLED", "false").lower() in ("true", "1", "yes")

# DB 유형 (mysql 또는 postgres)
DB_TYPE: str = os.getenv("DB_TYPE", "mysql")

# DB 연결 정보 (환경변수에서만 로드)
DB_HOST: str = os.getenv("DB_HOST", "localhost")
DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
DB_NAME: str = os.getenv("DB_NAME", "kis_trading")
DB_USER: str = os.getenv("DB_USER", "")
DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")


# ═══════════════════════════════════════════════════════════════════════════════
# 데이터 저장 경로
# ═══════════════════════════════════════════════════════════════════════════════

DATA_DIR: Path = PROJECT_ROOT / "data"
POSITIONS_FILE: Path = DATA_DIR / "positions.json"
DAILY_TRADES_FILE: Path = DATA_DIR / "daily_trades.json"


# ═══════════════════════════════════════════════════════════════════════════════
# 설정 검증 함수
# ═══════════════════════════════════════════════════════════════════════════════

def validate_base_settings() -> tuple[bool, list[str]]:
    """
    기본 설정값 검증
    
    Returns:
        tuple: (성공여부, 오류 메시지 목록)
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
    
    if not ENFORCE_SINGLE_INSTANCE:
        errors.append("⚠️ ENFORCE_SINGLE_INSTANCE가 False입니다. 중복 실행 위험!")
    
    if not ENFORCE_MARKET_HOURS:
        errors.append("⚠️ ENFORCE_MARKET_HOURS가 False입니다. 장외 주문 위험!")
    
    # 리스크 설정 검증
    if DAILY_MAX_LOSS_PERCENT > 5.0:
        errors.append(f"⚠️ 일일 손실 한도가 {DAILY_MAX_LOSS_PERCENT}%로 너무 높습니다.")
    
    if MAX_CUMULATIVE_DRAWDOWN_PCT > 20.0:
        errors.append(f"⚠️ 누적 드로다운 한도가 {MAX_CUMULATIVE_DRAWDOWN_PCT}%로 너무 높습니다.")
    
    return (len(errors) == 0, errors)


def print_settings_summary() -> str:
    """설정 요약 문자열 반환"""
    mode_emoji = {
        "DRY_RUN": "🟢",
        "PAPER": "🟡",
        "REAL": "🔴",
    }
    
    return f"""
═══════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 설정 요약 (Base)
═══════════════════════════════════════════════════════════════
[실행 모드]
  모드: {mode_emoji.get(EXECUTION_MODE, '❓')} {EXECUTION_MODE}
  실계좌 승인: {'✅' if ENABLE_REAL_TRADING else '❌'}
  Kill Switch: {'⛔ 활성화' if ENABLE_KILL_SWITCH else '✅ 비활성화'}

[API 설정]
  Base URL: {KIS_BASE_URL}
  계좌번호: {ACCOUNT_NO[:4] if ACCOUNT_NO else 'N/A'}****

[리스크 관리]
  일일 손실 한도: {DAILY_MAX_LOSS_PERCENT}%
  누적 드로다운 한도: {MAX_CUMULATIVE_DRAWDOWN_PCT}%
  갭 보호: {'✅ ON' if ENABLE_GAP_PROTECTION else '❌ OFF'}

[전략 파라미터]
  손절 배수: {ATR_MULTIPLIER_SL}x ATR
  익절 배수: {ATR_MULTIPLIER_TP}x ATR
  트레일링 스탑: {'✅ ON' if ENABLE_TRAILING_STOP else '❌ OFF'}

[거래 설정]
  종목: {DEFAULT_STOCK_CODE}
  주문 수량: {ORDER_QUANTITY}주
  일일 최대 거래: {DAILY_MAX_TRADES}회
═══════════════════════════════════════════════════════════════
"""
