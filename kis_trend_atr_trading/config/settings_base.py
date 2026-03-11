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
ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "14"))

# 추세 판단용 이동평균 기간
TREND_MA_PERIOD: int = int(os.getenv("TREND_MA_PERIOD", "50"))

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
ATR_SPIKE_THRESHOLD: float = float(os.getenv("ATR_SPIKE_THRESHOLD", "2.5"))

# ★ ADX 임계값 (추세 강도) - 25 미만이면 횡보장 판단
ADX_THRESHOLD: float = float(os.getenv("ADX_THRESHOLD", "25.0"))

# ADX 계산 기간
ADX_PERIOD: int = int(os.getenv("ADX_PERIOD", "14"))


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

# pending_exit 상태 자동 정리 최대 보관 시간(시간)
PENDING_EXIT_MAX_AGE_HOURS: int = int(os.getenv("PENDING_EXIT_MAX_AGE_HOURS", "72"))

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

# 토큰 발급 재시도 간격 (초) - KIS 토큰발급 API 1분당 1회 제한 대응
TOKEN_RETRY_DELAY_SECONDS: float = float(os.getenv("TOKEN_RETRY_DELAY_SECONDS", "61.0"))

# 토큰 만료 임박 판단 기준 (분)
TOKEN_REFRESH_MARGIN_MINUTES: int = int(os.getenv("TOKEN_REFRESH_MARGIN_MINUTES", "30"))

# 장 시작 전 토큰 프리워밍 시각 (KST, 기본 08:00)
TOKEN_PREWARM_HOUR: int = int(os.getenv("TOKEN_PREWARM_HOUR", "8"))
TOKEN_PREWARM_MINUTE: int = int(os.getenv("TOKEN_PREWARM_MINUTE", "0"))

# 계좌/보유 조회 단기 캐시 (burst 완화용)
ACCOUNT_BALANCE_CACHE_TTL_SEC: float = float(os.getenv("ACCOUNT_BALANCE_CACHE_TTL_SEC", "2.0"))
ACCOUNT_HOLDINGS_CACHE_TTL_SEC: float = float(os.getenv("ACCOUNT_HOLDINGS_CACHE_TTL_SEC", "2.0"))


# ═══════════════════════════════════════════════════════════════════════════════
# 실행 주기 설정
# ═══════════════════════════════════════════════════════════════════════════════

# 기본 실행 간격 (초)
DEFAULT_EXECUTION_INTERVAL: int = 60

# 손절선 근접 시 실행 간격 (초)
NEAR_STOPLOSS_EXECUTION_INTERVAL: int = 15

# 손절선 근접 임계값 (%)
NEAR_STOPLOSS_THRESHOLD_PCT: float = 70.0

# 신규 BUY 품질 보호 설정
# - 기본값은 모두 기존 동작 유지(OFF)입니다.
# - 권장 운영 예시:
#   ENABLE_BREAKOUT_EXTENSION_CAP=true
#   MAX_BREAKOUT_EXTENSION_PCT_ETF=0.004
#   MAX_BREAKOUT_EXTENSION_PCT_STOCK=0.007
#   BREAKOUT_EXTENSION_OPENING_CAP_MINUTES=90
#   MAX_BREAKOUT_EXTENSION_PCT_ETF_OPENING=0.012
#   MAX_BREAKOUT_EXTENSION_PCT_STOCK_OPENING=0.018
#   ENABLE_BREAKOUT_EXTENSION_ATR_CAP=true
#   BREAKOUT_EXTENSION_ATR_MULTIPLIER=0.35
#   MAX_BREAKOUT_EXTENSION_PCT_ETF_HARD=0.02
#   MAX_BREAKOUT_EXTENSION_PCT_STOCK_HARD=0.035
#   ENABLE_ENTRY_GAP_FILTER=true
#   MAX_ENTRY_GAP_PCT_ETF=0.01
#   MAX_ENTRY_GAP_PCT_STOCK=0.015
#   MAX_OPEN_VS_PREV_HIGH_PCT=0.005
#   ENABLE_OPENING_NO_ENTRY_GUARD=true
#   OPENING_NO_ENTRY_MINUTES=10
#   ENTRY_ORDER_STYLE=protected_limit
#   ENTRY_PROTECT_TICKS_ETF=1
#   ENTRY_PROTECT_TICKS_STOCK=2
#   ENTRY_MAX_SLIPPAGE_PCT=0.004
#   ENABLE_STALE_QUOTE_GUARD=true
#   QUOTE_MAX_AGE_SEC=3
ENABLE_BREAKOUT_EXTENSION_CAP: bool = os.getenv("ENABLE_BREAKOUT_EXTENSION_CAP", "false").lower() in (
    "true",
    "1",
    "yes",
)
MAX_BREAKOUT_EXTENSION_PCT_ETF: float = float(os.getenv("MAX_BREAKOUT_EXTENSION_PCT_ETF", "0.0"))
MAX_BREAKOUT_EXTENSION_PCT_STOCK: float = float(os.getenv("MAX_BREAKOUT_EXTENSION_PCT_STOCK", "0.0"))
BREAKOUT_EXTENSION_OPENING_CAP_MINUTES: int = int(os.getenv("BREAKOUT_EXTENSION_OPENING_CAP_MINUTES", "0"))
MAX_BREAKOUT_EXTENSION_PCT_ETF_OPENING: float = float(
    os.getenv("MAX_BREAKOUT_EXTENSION_PCT_ETF_OPENING", "0.0")
)
MAX_BREAKOUT_EXTENSION_PCT_STOCK_OPENING: float = float(
    os.getenv("MAX_BREAKOUT_EXTENSION_PCT_STOCK_OPENING", "0.0")
)
ENABLE_BREAKOUT_EXTENSION_ATR_CAP: bool = os.getenv("ENABLE_BREAKOUT_EXTENSION_ATR_CAP", "false").lower() in (
    "true",
    "1",
    "yes",
)
BREAKOUT_EXTENSION_ATR_MULTIPLIER: float = float(os.getenv("BREAKOUT_EXTENSION_ATR_MULTIPLIER", "0.0"))
MAX_BREAKOUT_EXTENSION_PCT_ETF_HARD: float = float(os.getenv("MAX_BREAKOUT_EXTENSION_PCT_ETF_HARD", "0.0"))
MAX_BREAKOUT_EXTENSION_PCT_STOCK_HARD: float = float(
    os.getenv("MAX_BREAKOUT_EXTENSION_PCT_STOCK_HARD", "0.0")
)

ENABLE_ENTRY_GAP_FILTER: bool = os.getenv("ENABLE_ENTRY_GAP_FILTER", "false").lower() in (
    "true",
    "1",
    "yes",
)
MAX_ENTRY_GAP_PCT_ETF: float = float(os.getenv("MAX_ENTRY_GAP_PCT_ETF", "0.0"))
MAX_ENTRY_GAP_PCT_STOCK: float = float(os.getenv("MAX_ENTRY_GAP_PCT_STOCK", "0.0"))
MAX_OPEN_VS_PREV_HIGH_PCT: float = float(os.getenv("MAX_OPEN_VS_PREV_HIGH_PCT", "0.0"))

ENABLE_OPENING_NO_ENTRY_GUARD: bool = os.getenv("ENABLE_OPENING_NO_ENTRY_GUARD", "false").lower() in (
    "true",
    "1",
    "yes",
)
OPENING_NO_ENTRY_MINUTES: int = int(os.getenv("OPENING_NO_ENTRY_MINUTES", "0"))

ENTRY_ORDER_STYLE: str = os.getenv("ENTRY_ORDER_STYLE", "market").strip().lower() or "market"
ENTRY_PROTECT_TICKS_ETF: int = int(os.getenv("ENTRY_PROTECT_TICKS_ETF", "0"))
ENTRY_PROTECT_TICKS_STOCK: int = int(os.getenv("ENTRY_PROTECT_TICKS_STOCK", "0"))
ENTRY_MAX_SLIPPAGE_PCT: float = float(os.getenv("ENTRY_MAX_SLIPPAGE_PCT", "0.0"))

ENABLE_STALE_QUOTE_GUARD: bool = os.getenv("ENABLE_STALE_QUOTE_GUARD", "false").lower() in (
    "true",
    "1",
    "yes",
)
QUOTE_MAX_AGE_SEC: float = float(os.getenv("QUOTE_MAX_AGE_SEC", "0"))

# Opening Range Breakout(ORB) 보조 진입 슬리브 설정
# - 장초 갭으로 기존 prev_high 기반 cap/gap 필터를 통과하기 어려운 강한 종목을
#   opening range 재돌파로만 제한적으로 허용합니다.
# - 기본값은 OFF로 두어 기존 동작을 보호합니다.
# - 권장 운영 예시:
#   ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY=true
#   ORB_OPENING_RANGE_MINUTES=5
#   ORB_ENTRY_CUTOFF_MINUTES=90
#   ORB_MIN_OPEN_ABOVE_PREV_HIGH_PCT=0.003
#   ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_ETF=0.05
#   ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_STOCK=0.10
#   ORB_MAX_EXTENSION_PCT_ETF=0.006
#   ORB_MAX_EXTENSION_PCT_STOCK=0.01
#   ORB_REQUIRE_ABOVE_VWAP=true
#   ORB_RECENT_BREAKOUT_LOOKBACK_BARS=3
#   ORB_REARM_BAND_PCT=0.002
ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY: bool = os.getenv(
    "ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY",
    "false",
).lower() in (
    "true",
    "1",
    "yes",
)
ORB_OPENING_RANGE_MINUTES: int = int(os.getenv("ORB_OPENING_RANGE_MINUTES", "5"))
ORB_ENTRY_START_MINUTES: int = int(os.getenv("ORB_ENTRY_START_MINUTES", "0"))
ORB_ENTRY_CUTOFF_MINUTES: int = int(os.getenv("ORB_ENTRY_CUTOFF_MINUTES", "90"))
ORB_MIN_OPEN_ABOVE_PREV_HIGH_PCT: float = float(os.getenv("ORB_MIN_OPEN_ABOVE_PREV_HIGH_PCT", "0.0"))
ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_ETF: float = float(
    os.getenv("ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_ETF", "0.0")
)
ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_STOCK: float = float(
    os.getenv("ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_STOCK", "0.0")
)
ORB_MAX_EXTENSION_PCT_ETF: float = float(os.getenv("ORB_MAX_EXTENSION_PCT_ETF", "0.0"))
ORB_MAX_EXTENSION_PCT_STOCK: float = float(os.getenv("ORB_MAX_EXTENSION_PCT_STOCK", "0.0"))
ORB_REQUIRE_ABOVE_VWAP: bool = os.getenv("ORB_REQUIRE_ABOVE_VWAP", "true").lower() in (
    "true",
    "1",
    "yes",
)
ORB_USE_ADX_FILTER: bool = os.getenv("ORB_USE_ADX_FILTER", "true").lower() in (
    "true",
    "1",
    "yes",
)
ORB_MIN_ADX: float = float(os.getenv("ORB_MIN_ADX", "20.0"))
ORB_RECENT_BREAKOUT_LOOKBACK_BARS: int = int(os.getenv("ORB_RECENT_BREAKOUT_LOOKBACK_BARS", "3"))
ORB_REARM_BAND_PCT: float = float(os.getenv("ORB_REARM_BAND_PCT", "0.002"))
ORB_BLOCK_IF_PENDING_ORDER: bool = os.getenv("ORB_BLOCK_IF_PENDING_ORDER", "true").lower() in (
    "true",
    "1",
    "yes",
)
ORB_ONLY_MAIN_MARKET: bool = os.getenv("ORB_ONLY_MAIN_MARKET", "true").lower() in (
    "true",
    "1",
    "yes",
)
ORB_ALLOWED_ENTRY_VENUES: str = os.getenv("ORB_ALLOWED_ENTRY_VENUES", "KRX").strip() or "KRX"

# Pullback / Re-breakout 보조 진입 슬리브 설정
# - Trend-ATR의 기존 돌파 진입을 대체하지 않고 보완하는 보조 전략입니다.
# - 기본값은 OFF로 두어 기존 동작을 보호합니다.
# - 권장 운영 예시:
#   ENABLE_PULLBACK_REBREAKOUT_STRATEGY=true
#   PULLBACK_LOOKBACK_BARS=12
#   PULLBACK_SWING_LOOKBACK_BARS=15
#   PULLBACK_MIN_PULLBACK_PCT=0.015
#   PULLBACK_MAX_PULLBACK_PCT=0.06
#   PULLBACK_REQUIRE_ABOVE_MA20=true
#   PULLBACK_REBREAKOUT_LOOKBACK_BARS=3
#   PULLBACK_USE_ADX_FILTER=true
#   PULLBACK_MIN_ADX=20
#   PULLBACK_ONLY_MAIN_MARKET=true
#   PULLBACK_ALLOWED_ENTRY_VENUES=KRX
#   PULLBACK_BLOCK_IF_EXISTING_POSITION=true
#   PULLBACK_BLOCK_IF_PENDING_ORDER=true
#   ENABLE_THREADED_PULLBACK_PIPELINE=true
#   ENABLE_MULTI_STRATEGY_THREADED_PIPELINE=false
#   THREADED_PIPELINE_ENABLED_STRATEGIES=pullback_rebreakout
#   THREADED_PIPELINE_ENABLED_STRATEGIES=pullback_rebreakout,trend_atr
#   STRATEGY_CANDIDATE_MAX_AGE_SEC=300
#   PULLBACK_SETUP_REFRESH_SEC=60
#   PULLBACK_TIMING_DIRTY_POLL_SEC=0.5
#   PULLBACK_ENTRY_INTENT_QUEUE_MAXSIZE=256
#   ENABLE_PULLBACK_DAILY_REFRESH_THREAD=true
#   DAILY_CONTEXT_REFRESH_SEC=60
#   DAILY_CONTEXT_FORCE_REFRESH_ON_TRADE_DATE_CHANGE=true
#   DAILY_CONTEXT_STORE_MAX_SYMBOLS=256
#   DAILY_CONTEXT_STALE_SEC=180
ENABLE_PULLBACK_REBREAKOUT_STRATEGY: bool = os.getenv(
    "ENABLE_PULLBACK_REBREAKOUT_STRATEGY",
    "false",
).lower() in (
    "true",
    "1",
    "yes",
)
PULLBACK_LOOKBACK_BARS: int = int(os.getenv("PULLBACK_LOOKBACK_BARS", "12"))
PULLBACK_SWING_LOOKBACK_BARS: int = int(os.getenv("PULLBACK_SWING_LOOKBACK_BARS", "15"))
PULLBACK_MIN_PULLBACK_PCT: float = float(os.getenv("PULLBACK_MIN_PULLBACK_PCT", "0.015"))
PULLBACK_MAX_PULLBACK_PCT: float = float(os.getenv("PULLBACK_MAX_PULLBACK_PCT", "0.06"))
PULLBACK_REQUIRE_ABOVE_MA20: bool = os.getenv("PULLBACK_REQUIRE_ABOVE_MA20", "true").lower() in (
    "true",
    "1",
    "yes",
)
PULLBACK_REBREAKOUT_LOOKBACK_BARS: int = int(os.getenv("PULLBACK_REBREAKOUT_LOOKBACK_BARS", "3"))
PULLBACK_USE_ADX_FILTER: bool = os.getenv("PULLBACK_USE_ADX_FILTER", "true").lower() in (
    "true",
    "1",
    "yes",
)
PULLBACK_MIN_ADX: float = float(os.getenv("PULLBACK_MIN_ADX", "20"))
PULLBACK_ONLY_MAIN_MARKET: bool = os.getenv("PULLBACK_ONLY_MAIN_MARKET", "true").lower() in (
    "true",
    "1",
    "yes",
)
PULLBACK_ALLOWED_ENTRY_VENUES: str = (
    str(os.getenv("PULLBACK_ALLOWED_ENTRY_VENUES", "KRX") or "KRX").strip().upper() or "KRX"
)
PULLBACK_BLOCK_IF_EXISTING_POSITION: bool = os.getenv(
    "PULLBACK_BLOCK_IF_EXISTING_POSITION",
    "true",
).lower() in (
    "true",
    "1",
    "yes",
)
PULLBACK_BLOCK_IF_PENDING_ORDER: bool = os.getenv(
    "PULLBACK_BLOCK_IF_PENDING_ORDER",
    "true",
).lower() in (
    "true",
    "1",
    "yes",
)
ENABLE_THREADED_PULLBACK_PIPELINE: bool = os.getenv(
    "ENABLE_THREADED_PULLBACK_PIPELINE",
    "false",
).lower() in (
    "true",
    "1",
    "yes",
)
ENABLE_MULTI_STRATEGY_THREADED_PIPELINE: bool = os.getenv(
    "ENABLE_MULTI_STRATEGY_THREADED_PIPELINE",
    "false",
).lower() in (
    "true",
    "1",
    "yes",
)
THREADED_PIPELINE_ENABLED_STRATEGIES: str = (
    str(os.getenv("THREADED_PIPELINE_ENABLED_STRATEGIES", "pullback_rebreakout") or "pullback_rebreakout").strip()
    or "pullback_rebreakout"
)
STRATEGY_CANDIDATE_MAX_AGE_SEC: int = int(os.getenv("STRATEGY_CANDIDATE_MAX_AGE_SEC", "300"))
PULLBACK_SETUP_REFRESH_SEC: int = int(os.getenv("PULLBACK_SETUP_REFRESH_SEC", "60"))
PULLBACK_TIMING_DIRTY_POLL_SEC: float = float(os.getenv("PULLBACK_TIMING_DIRTY_POLL_SEC", "0.5"))
PULLBACK_ENTRY_INTENT_QUEUE_MAXSIZE: int = int(os.getenv("PULLBACK_ENTRY_INTENT_QUEUE_MAXSIZE", "256"))
ENABLE_PULLBACK_DAILY_REFRESH_THREAD: bool = os.getenv(
    "ENABLE_PULLBACK_DAILY_REFRESH_THREAD",
    "false",
).lower() in (
    "true",
    "1",
    "yes",
)
DAILY_CONTEXT_REFRESH_SEC: int = int(os.getenv("DAILY_CONTEXT_REFRESH_SEC", "60"))
DAILY_CONTEXT_FORCE_REFRESH_ON_TRADE_DATE_CHANGE: bool = os.getenv(
    "DAILY_CONTEXT_FORCE_REFRESH_ON_TRADE_DATE_CHANGE",
    "true",
).lower() in (
    "true",
    "1",
    "yes",
)
DAILY_CONTEXT_STORE_MAX_SYMBOLS: int = int(os.getenv("DAILY_CONTEXT_STORE_MAX_SYMBOLS", "256"))
DAILY_CONTEXT_STALE_SEC: int = int(os.getenv("DAILY_CONTEXT_STALE_SEC", "180"))
ENABLE_RISK_SNAPSHOT_THREAD: bool = os.getenv(
    "ENABLE_RISK_SNAPSHOT_THREAD",
    "false",
).lower() in (
    "true",
    "1",
    "yes",
)
RISK_SNAPSHOT_REFRESH_SEC: int = int(os.getenv("RISK_SNAPSHOT_REFRESH_SEC", "30"))
RISK_SNAPSHOT_TTL_SEC: int = int(os.getenv("RISK_SNAPSHOT_TTL_SEC", "60"))
HOLDINGS_SNAPSHOT_TTL_SEC: int = int(os.getenv("HOLDINGS_SNAPSHOT_TTL_SEC", "30"))
ORDER_FINAL_VALIDATION_MODE: str = (
    str(os.getenv("ORDER_FINAL_VALIDATION_MODE", "light") or "light").strip().lower() or "light"
)

# 시장 레짐 필터 설정
# - 1차 버전은 대표 ETF 2개만으로 BUY 상위 필터를 제공합니다.
# - 기본값은 모두 기존 동작 유지(필터 OFF 또는 통과 허용)입니다.
# - 권장 운영 예시:
#   ENABLE_MARKET_REGIME_FILTER=true
#   MARKET_REGIME_KOSPI_SYMBOL=069500
#   MARKET_REGIME_KOSDAQ_SYMBOL=229200
#   MARKET_REGIME_MA_PERIOD=20
#   MARKET_REGIME_LOOKBACK_DAYS=3
#   MARKET_REGIME_BAD_3D_RETURN_PCT=-0.03
#   MARKET_REGIME_INTRADAY_DROP_PCT=-0.015
#   MARKET_REGIME_OPENING_GUARD_MINUTES=30
#   MARKET_REGIME_CACHE_TTL_SEC=60
#   MARKET_REGIME_OPENING_CACHE_TTL_SEC=60
#   MARKET_REGIME_STALE_MAX_SEC=180
#   MARKET_REGIME_FAIL_MODE=closed
#   MARKET_REGIME_REFRESH_BUDGET_SEC=1.5
#   MARKET_REGIME_BOOTSTRAP_BUDGET_SEC=3.0
#   ENABLE_MARKET_REGIME_REFRESH_THREAD=false
#   MARKET_REGIME_REFRESH_INTERVAL_SEC=30
#   MARKET_REGIME_DAILY_CONTEXT_REFRESH_SEC=600
#   MARKET_REGIME_INTRADAY_USE_WS_CACHE_ONLY=true
#   MARKET_REGIME_QUOTE_FALLBACK_MODE=skip
#   MARKET_REGIME_FORCE_DAILY_REFRESH_ON_TRADE_DATE_CHANGE=true
#   MARKET_REGIME_BACKGROUND_STALE_GRACE_SEC=180
#   MARKET_REGIME_QUOTE_MAX_AGE_SEC=15
#   ENABLE_MARKET_REGIME_WORKER_AUTO_RESTART=true
#   MARKET_REGIME_WORKER_RESTART_ERROR_THRESHOLD=3
#   MARKET_REGIME_WORKER_RESTART_BASE_BACKOFF_SEC=5
#   MARKET_REGIME_WORKER_RESTART_MAX_BACKOFF_SEC=60
#   MARKET_REGIME_WORKER_STALL_SEC=120
#   MARKET_REGIME_BAD_BLOCK_NEW_BUY=true
#   MARKET_REGIME_NEUTRAL_ALLOW_BUY=true
#   MARKET_REGIME_NEUTRAL_POSITION_SCALE=1.0
# - 기본 TTL은 60초입니다.
# - legacy main loop가 실제로 충분히 촘촘하게 돈다고 확인된 뒤에만
#   MARKET_REGIME_OPENING_CACHE_TTL_SEC=30 으로 낮추는 것을 권장합니다.
# - 일반 refresh budget과 first snapshot bootstrap budget은 다릅니다.
# - bootstrap budget은 previous snapshot이 없을 때만 적용됩니다.
ENABLE_MARKET_REGIME_FILTER: bool = os.getenv("ENABLE_MARKET_REGIME_FILTER", "false").lower() in (
    "true",
    "1",
    "yes",
)
MARKET_REGIME_KOSPI_SYMBOL: str = str(os.getenv("MARKET_REGIME_KOSPI_SYMBOL", "069500") or "069500").strip()
MARKET_REGIME_KOSDAQ_SYMBOL: str = str(os.getenv("MARKET_REGIME_KOSDAQ_SYMBOL", "229200") or "229200").strip()
MARKET_REGIME_MA_PERIOD: int = int(os.getenv("MARKET_REGIME_MA_PERIOD", "20"))
MARKET_REGIME_LOOKBACK_DAYS: int = int(os.getenv("MARKET_REGIME_LOOKBACK_DAYS", "3"))
MARKET_REGIME_BAD_3D_RETURN_PCT: float = float(os.getenv("MARKET_REGIME_BAD_3D_RETURN_PCT", "-0.03"))
MARKET_REGIME_INTRADAY_DROP_PCT: float = float(os.getenv("MARKET_REGIME_INTRADAY_DROP_PCT", "-0.015"))
MARKET_REGIME_OPENING_GUARD_MINUTES: int = int(os.getenv("MARKET_REGIME_OPENING_GUARD_MINUTES", "30"))
MARKET_REGIME_CACHE_TTL_SEC: int = int(os.getenv("MARKET_REGIME_CACHE_TTL_SEC", "60"))
MARKET_REGIME_OPENING_CACHE_TTL_SEC: int = int(
    os.getenv("MARKET_REGIME_OPENING_CACHE_TTL_SEC", "60")
)
MARKET_REGIME_STALE_MAX_SEC: int = int(os.getenv("MARKET_REGIME_STALE_MAX_SEC", "180"))
MARKET_REGIME_FAIL_MODE: str = (
    str(os.getenv("MARKET_REGIME_FAIL_MODE", "closed") or "closed").strip().lower() or "closed"
)
MARKET_REGIME_REFRESH_BUDGET_SEC: float = float(
    os.getenv("MARKET_REGIME_REFRESH_BUDGET_SEC", "1.5")
)
MARKET_REGIME_BOOTSTRAP_BUDGET_SEC: float = float(
    os.getenv("MARKET_REGIME_BOOTSTRAP_BUDGET_SEC", "3.0")
)
ENABLE_MARKET_REGIME_REFRESH_THREAD: bool = os.getenv(
    "ENABLE_MARKET_REGIME_REFRESH_THREAD", "false"
).lower() in ("true", "1", "yes")
MARKET_REGIME_REFRESH_INTERVAL_SEC: float = float(
    os.getenv("MARKET_REGIME_REFRESH_INTERVAL_SEC", "30")
)
MARKET_REGIME_DAILY_CONTEXT_REFRESH_SEC: float = float(
    os.getenv("MARKET_REGIME_DAILY_CONTEXT_REFRESH_SEC", "600")
)
MARKET_REGIME_INTRADAY_USE_WS_CACHE_ONLY: bool = os.getenv(
    "MARKET_REGIME_INTRADAY_USE_WS_CACHE_ONLY", "true"
).lower() in ("true", "1", "yes")
MARKET_REGIME_QUOTE_FALLBACK_MODE: str = (
    str(os.getenv("MARKET_REGIME_QUOTE_FALLBACK_MODE", "skip") or "skip").strip().lower()
    or "skip"
)
MARKET_REGIME_FORCE_DAILY_REFRESH_ON_TRADE_DATE_CHANGE: bool = os.getenv(
    "MARKET_REGIME_FORCE_DAILY_REFRESH_ON_TRADE_DATE_CHANGE", "true"
).lower() in ("true", "1", "yes")
MARKET_REGIME_BACKGROUND_STALE_GRACE_SEC: float = float(
    os.getenv("MARKET_REGIME_BACKGROUND_STALE_GRACE_SEC", "180")
)
MARKET_REGIME_QUOTE_MAX_AGE_SEC: float = float(
    os.getenv("MARKET_REGIME_QUOTE_MAX_AGE_SEC", "15")
)
ENABLE_MARKET_REGIME_WORKER_AUTO_RESTART: bool = os.getenv(
    "ENABLE_MARKET_REGIME_WORKER_AUTO_RESTART", "true"
).lower() in ("true", "1", "yes")
MARKET_REGIME_WORKER_RESTART_ERROR_THRESHOLD: int = int(
    os.getenv("MARKET_REGIME_WORKER_RESTART_ERROR_THRESHOLD", "3")
)
MARKET_REGIME_WORKER_RESTART_BASE_BACKOFF_SEC: float = float(
    os.getenv("MARKET_REGIME_WORKER_RESTART_BASE_BACKOFF_SEC", "5")
)
MARKET_REGIME_WORKER_RESTART_MAX_BACKOFF_SEC: float = float(
    os.getenv("MARKET_REGIME_WORKER_RESTART_MAX_BACKOFF_SEC", "60")
)
MARKET_REGIME_WORKER_STALL_SEC: float = float(
    os.getenv("MARKET_REGIME_WORKER_STALL_SEC", "120")
)
MARKET_REGIME_BAD_BLOCK_NEW_BUY: bool = os.getenv("MARKET_REGIME_BAD_BLOCK_NEW_BUY", "true").lower() in (
    "true",
    "1",
    "yes",
)
MARKET_REGIME_NEUTRAL_ALLOW_BUY: bool = os.getenv("MARKET_REGIME_NEUTRAL_ALLOW_BUY", "true").lower() in (
    "true",
    "1",
    "yes",
)
MARKET_REGIME_NEUTRAL_POSITION_SCALE: float = float(
    os.getenv("MARKET_REGIME_NEUTRAL_POSITION_SCALE", "1.0")
)

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
# 24/365 런타임 상태머신 설정
# ═══════════════════════════════════════════════════════════════════════════════

MARKET_TIMEZONE: str = os.getenv("MARKET_TIMEZONE", "Asia/Seoul")
DATA_FEED_DEFAULT: str = os.getenv("DATA_FEED_DEFAULT", "rest").strip().lower() or "rest"
RUNTIME_TIMEFRAME: str = os.getenv("RUNTIME_TIMEFRAME", "1m").strip().lower() or "1m"

PREOPEN_WARMUP_MIN: int = int(os.getenv("PREOPEN_WARMUP_MIN", "10"))
POSTCLOSE_MIN: int = int(os.getenv("POSTCLOSE_MIN", "10"))
AUCTION_GUARD_WINDOWS: List[str] = [
    token.strip() for token in os.getenv("AUCTION_GUARD_WINDOWS", "").split(",") if token.strip()
]
ALLOW_EXIT_IN_AUCTION: bool = os.getenv("ALLOW_EXIT_IN_AUCTION", "true").lower() in (
    "true",
    "1",
    "yes",
)
OFFSESSION_WS_ENABLED: bool = os.getenv("OFFSESSION_WS_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)

WS_START_GRACE_SEC: int = int(os.getenv("WS_START_GRACE_SEC", "30"))
WS_STALE_SEC: int = int(os.getenv("WS_STALE_SEC", "60"))
WS_RECONNECT_MAX_ATTEMPTS: int = int(os.getenv("WS_RECONNECT_MAX_ATTEMPTS", "5"))
WS_RECONNECT_BACKOFF_BASE_SEC: int = int(os.getenv("WS_RECONNECT_BACKOFF_BASE_SEC", "1"))
WS_RECOVER_POLICY: str = os.getenv("WS_RECOVER_POLICY", "auto").strip().lower() or "auto"
WS_RECOVER_STABLE_SEC: int = int(os.getenv("WS_RECOVER_STABLE_SEC", "30"))
WS_RECOVER_REQUIRED_BARS: int = int(os.getenv("WS_RECOVER_REQUIRED_BARS", "2"))
WS_MIN_DEGRADED_SEC: int = int(os.getenv("WS_MIN_DEGRADED_SEC", "120"))
WS_MIN_NORMAL_SEC: int = int(os.getenv("WS_MIN_NORMAL_SEC", "120"))
TELEGRAM_TRANSITION_COOLDOWN_SEC: int = int(
    os.getenv("TELEGRAM_TRANSITION_COOLDOWN_SEC", "600")
)

RUNTIME_STATUS_LOG_INTERVAL_SEC: int = int(os.getenv("RUNTIME_STATUS_LOG_INTERVAL_SEC", "300"))
RUNTIME_STATUS_TELEGRAM: bool = os.getenv("RUNTIME_STATUS_TELEGRAM", "false").lower() in (
    "true",
    "1",
    "yes",
)
RUNTIME_INSESSION_SLEEP_SEC: int = int(os.getenv("RUNTIME_INSESSION_SLEEP_SEC", "60"))
RUNTIME_OFFSESSION_SLEEP_SEC: int = int(os.getenv("RUNTIME_OFFSESSION_SLEEP_SEC", "600"))


# ═══════════════════════════════════════════════════════════════════════════════
# Fast Evaluation Scheduler (default OFF, rollback-safe)
# ═══════════════════════════════════════════════════════════════════════════════
#
# 목적:
#   - WS 정상 연결 시 quote event 기반으로 미보유 종목 평가 cadence를 낮춥니다.
#   - 기존 legacy completed-bar gate 경로는 유지되며, 아래 플래그가 OFF면 사용되지 않습니다.
#
# 원칙:
#   - 전략 임계값은 바꾸지 않고 평가 스케줄링/캐시 구조만 개선합니다.
#   - entry/exit cadence는 다르게 가져가되 주문/리스크 의미는 유지합니다.

ENABLE_FAST_EVAL_SCHEDULER: bool = os.getenv("ENABLE_FAST_EVAL_SCHEDULER", "false").lower() in (
    "true",
    "1",
    "yes",
)
FAST_EVAL_LOOP_SLEEP_SEC: float = float(os.getenv("FAST_EVAL_LOOP_SLEEP_SEC", "1"))
FAST_EVAL_ENTRY_COOLDOWN_SEC: float = float(os.getenv("FAST_EVAL_ENTRY_COOLDOWN_SEC", "12"))
FAST_EVAL_ENTRY_DEBOUNCE_SEC: float = float(os.getenv("FAST_EVAL_ENTRY_DEBOUNCE_SEC", "2"))
FAST_EVAL_EXIT_COOLDOWN_SEC: float = float(os.getenv("FAST_EVAL_EXIT_COOLDOWN_SEC", "5"))
FAST_EVAL_EXIT_DEBOUNCE_SEC: float = float(os.getenv("FAST_EVAL_EXIT_DEBOUNCE_SEC", "1"))
FAST_EVAL_REST_FALLBACK_COOLDOWN_SEC: float = float(
    os.getenv("FAST_EVAL_REST_FALLBACK_COOLDOWN_SEC", "30")
)
FAST_EVAL_RISK_SYNC_INTERVAL_SEC: float = float(
    os.getenv("FAST_EVAL_RISK_SYNC_INTERVAL_SEC", "30")
)
FAST_EVAL_METRIC_LOG_INTERVAL_SEC: float = float(
    os.getenv("FAST_EVAL_METRIC_LOG_INTERVAL_SEC", "60")
)
FAST_EVAL_DAILY_REFRESH_INTERVAL_SEC: float = float(
    os.getenv("FAST_EVAL_DAILY_REFRESH_INTERVAL_SEC", "300")
)
WS_QUOTE_STATIC_CACHE_TTL_SEC: float = float(os.getenv("WS_QUOTE_STATIC_CACHE_TTL_SEC", "900"))


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
