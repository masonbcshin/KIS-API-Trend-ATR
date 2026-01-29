"""
KIS Trend-ATR Trading System - 설정 파일

이 파일은 시스템 전체에서 사용되는 설정값들을 관리합니다.
민감한 정보(API 키, 시크릿)는 .env 파일에서 로드합니다.

⚠️ 주의: 실계좌 사용 금지 - 모의투자 전용 설정입니다.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# .env 파일 로드
env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)


# ════════════════════════════════════════════════════════════════
# KIS API 기본 설정
# ════════════════════════════════════════════════════════════════

# 모의투자 API BASE URL (실계좌 URL 절대 사용 금지)
# 실전: https://openapi.koreainvestment.com:9443
# 모의: https://openapivts.koreainvestment.com:29443
KIS_BASE_URL = "https://openapivts.koreainvestment.com:29443"

# API 인증 정보 (.env 파일에서 로드)
APP_KEY = os.getenv("KIS_APP_KEY", "")
APP_SECRET = os.getenv("KIS_APP_SECRET", "")

# 계좌 정보
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")  # 계좌번호 (8자리)
ACCOUNT_PRODUCT_CODE = os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "01")  # 계좌상품코드

# ════════════════════════════════════════════════════════════════
# 거래 모드 설정 (중요!)
# ════════════════════════════════════════════════════════════════

# 트레이딩 모드 설정
# - "LIVE"  : 실계좌 주문 (실제 매매 발생)
# - "CBT"   : 종이 매매 (주문 금지, 텔레그램 알림만)
# - "PAPER" : 모의투자 (모의투자 서버 주문)
TRADING_MODE = os.getenv("TRADING_MODE", "PAPER")

# 모의투자 여부 (TRADING_MODE와 연동)
IS_PAPER_TRADING = TRADING_MODE in ("PAPER", "CBT")

# 기본 거래 종목 (삼성전자)
DEFAULT_STOCK_CODE = "005930"

# 1회 주문 수량
ORDER_QUANTITY = 1

# ════════════════════════════════════════════════════════════════
# Trend-ATR 전략 파라미터
# ════════════════════════════════════════════════════════════════

# ATR(Average True Range) 계산 기간
ATR_PERIOD = 14

# 추세 판단용 이동평균 기간
TREND_MA_PERIOD = 50

# 손절 배수 (ATR 기준)
# 손절가 = 진입가 - (ATR * ATR_MULTIPLIER_SL)
ATR_MULTIPLIER_SL = 2.0

# 익절 배수 (ATR 기준)
# 익절가 = 진입가 + (ATR * ATR_MULTIPLIER_TP)
ATR_MULTIPLIER_TP = 3.0

# ════════════════════════════════════════════════════════════════
# 리스크 관리 파라미터
# ════════════════════════════════════════════════════════════════

# 최대 손실 비율 (진입가 대비, %)
# ATR 기반 손절가가 이 비율을 초과하면 강제로 제한
MAX_LOSS_PCT = 5.0

# ATR 급등 임계값 (평균 대비 배수)
# 현재 ATR이 최근 평균의 N배를 초과하면 진입 거부
ATR_SPIKE_THRESHOLD = 2.5

# ADX 임계값 (추세 강도)
# ADX가 이 값 미만이면 횡보장으로 판단하여 진입 거부
ADX_THRESHOLD = 25.0

# ADX 계산 기간
ADX_PERIOD = 14

# ════════════════════════════════════════════════════════════════
# 트레일링 스탑 설정 (멀티데이 전략 필수)
# ════════════════════════════════════════════════════════════════

# 트레일링 스탑 활성화 여부
ENABLE_TRAILING_STOP = True

# 트레일링 스탑 ATR 배수 (진입 시 ATR 기준)
# 트레일링 스탑 = 최고가 - (진입ATR * 배수)
TRAILING_STOP_ATR_MULTIPLIER = 2.0

# 트레일링 스탑 활성화 기준 (진입가 대비 수익률 %)
# 이 수익률 이상이면 트레일링 스탑이 작동 시작
TRAILING_STOP_ACTIVATION_PCT = 1.0

# ════════════════════════════════════════════════════════════════
# 갭 리스크 관리 (멀티데이 필수 옵션)
# ════════════════════════════════════════════════════════════════

# 갭 리스크 완화 활성화 여부 (기본 OFF)
ENABLE_GAP_PROTECTION = False

# 최대 갭 손실 허용 비율 (%)
# 시가가 손절가보다 이 비율 이상 불리하면 즉시 청산
MAX_GAP_LOSS_PCT = 3.0

# ════════════════════════════════════════════════════════════════
# 이벤트 리스크 관리
# ════════════════════════════════════════════════════════════════

# 고위험 이벤트일 신규 진입 제한 활성화
ENABLE_EVENT_RISK_CHECK = False

# 고위험 이벤트 날짜 목록 (YYYY-MM-DD 형식)
# 예: FOMC, 옵션 만기일, 중요 경제지표 발표일 등
HIGH_RISK_EVENT_DATES = []

# ════════════════════════════════════════════════════════════════
# 손익 알림 임계값
# ════════════════════════════════════════════════════════════════

# 손절/익절 근접 알림 비율 (%)
# 손절선까지 이 비율에 도달하면 텔레그램 알림 전송
ALERT_NEAR_STOPLOSS_PCT = 80.0

# 익절선 근접 알림 비율 (%)
ALERT_NEAR_TAKEPROFIT_PCT = 80.0

# ════════════════════════════════════════════════════════════════
# 긴급 정지 및 일일 손실 제한 (Kill Switch & Daily Loss Limit)
# ════════════════════════════════════════════════════════════════

# 킬 스위치 (긴급 정지)
# True로 설정하면 모든 신규 주문이 즉시 차단되고 프로그램이 안전 종료됩니다.
# 긴급 상황 발생 시 이 값을 True로 변경하세요.
ENABLE_KILL_SWITCH = False

# 일일 최대 손실 허용 비율 (%)
# 당일 누적 손실이 이 비율을 초과하면 신규 주문이 차단됩니다.
# 예: 3.0 = 시작 자본금의 3% 손실 시 거래 중단
# 주의: 기존 포지션의 청산은 허용됩니다 (추가 손실 방지 목적)
DAILY_MAX_LOSS_PERCENT = 3.0

# ════════════════════════════════════════════════════════════════
# 일일 리스크 한도 (계좌 보호) - 상세 설정
# ════════════════════════════════════════════════════════════════

# 일일 최대 손실 비율 (%) - position_store 용
DAILY_MAX_LOSS_PCT = 10.0

# 일일 최대 거래 횟수
DAILY_MAX_TRADES = 5

# 연속 손실 허용 횟수
MAX_CONSECUTIVE_LOSSES = 3

# ════════════════════════════════════════════════════════════════
# 긴급 손절 설정
# ════════════════════════════════════════════════════════════════

# 긴급 손절 최대 재시도 횟수
EMERGENCY_SELL_MAX_RETRIES = 10

# 긴급 손절 재시도 간격 (초)
EMERGENCY_SELL_RETRY_INTERVAL = 3

# 체결 확인 최대 대기 시간 (초)
ORDER_EXECUTION_TIMEOUT = 30

# 체결 확인 간격 (초)
ORDER_CHECK_INTERVAL = 2

# ════════════════════════════════════════════════════════════════
# API 호출 설정
# ════════════════════════════════════════════════════════════════

# API 호출 타임아웃 (초)
API_TIMEOUT = 10

# API 호출 실패 시 최대 재시도 횟수
MAX_RETRIES = 3

# 재시도 간 대기 시간 (초)
RETRY_DELAY = 1.0

# Rate Limit 대기 시간 (초) - KIS API는 초당 20회 제한
RATE_LIMIT_DELAY = 0.1

# ════════════════════════════════════════════════════════════════
# 백테스트 설정
# ════════════════════════════════════════════════════════════════

# 백테스트 시작 자본금 (원)
BACKTEST_INITIAL_CAPITAL = 10_000_000

# 백테스트 수수료율 (0.015% = 0.00015)
BACKTEST_COMMISSION_RATE = 0.00015

# ════════════════════════════════════════════════════════════════
# CBT (Closed Beta Test) 모드 설정
# ════════════════════════════════════════════════════════════════
# CBT 모드: 실계좌 주문 없이 가상 체결로 성과 측정
# TRADING_MODE = "CBT"로 설정하면 활성화됩니다.

# CBT 초기 자본금 (원)
CBT_INITIAL_CAPITAL = int(os.getenv("CBT_INITIAL_CAPITAL", "10000000"))

# CBT 데이터 저장 경로
CBT_DATA_DIR = Path(__file__).parent.parent / "cbt_data"

# CBT 거래 기록 저장 방식 ("json" 또는 "sqlite")
CBT_STORAGE_TYPE = os.getenv("CBT_STORAGE_TYPE", "json")

# CBT 가상 수수료율 (0.015% = 0.00015, 실제 거래와 동일)
CBT_COMMISSION_RATE = float(os.getenv("CBT_COMMISSION_RATE", "0.00015"))

# CBT 일일 리포트 자동 전송 활성화
CBT_AUTO_REPORT_ENABLED = os.getenv("CBT_AUTO_REPORT_ENABLED", "true").lower() in ("true", "1", "yes")

# CBT Equity Curve 저장 간격 (분)
CBT_EQUITY_SAVE_INTERVAL = int(os.getenv("CBT_EQUITY_SAVE_INTERVAL", "60"))

# ════════════════════════════════════════════════════════════════
# 로깅 설정
# ════════════════════════════════════════════════════════════════

# 로그 레벨: DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_LEVEL = "INFO"

# 로그 파일 저장 경로
LOG_DIR = Path(__file__).parent.parent / "logs"

# ════════════════════════════════════════════════════════════════
# 텔레그램 알림 설정
# ════════════════════════════════════════════════════════════════

# 텔레그램 봇 토큰 (@BotFather에서 발급)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# 텔레그램 채팅 ID (1:1 또는 그룹)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 텔레그램 알림 활성화 여부
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "true").lower() in ("true", "1", "yes")

# ════════════════════════════════════════════════════════════════
# 설정 검증 함수
# ════════════════════════════════════════════════════════════════

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
    
    # 모의투자 URL 검증 (실계좌 URL 사용 방지)
    if "openapi.koreainvestment.com:9443" in KIS_BASE_URL:
        errors.append("⚠️ 실계좌 URL이 감지되었습니다. 모의투자 URL을 사용하세요.")
    
    # 모의투자 플래그 검증
    if not IS_PAPER_TRADING:
        errors.append("⚠️ IS_PAPER_TRADING이 False입니다. True로 설정하세요.")
    
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
        "LIVE": "🔴 실계좌",
        "CBT": "🟡 종이매매",
        "PAPER": "🟢 모의투자"
    }
    mode_display = mode_emoji.get(TRADING_MODE, f"❓ {TRADING_MODE}")
    
    return f"""
═══════════════════════════════════════════════════
KIS Trend-ATR Trading System - 설정 요약
═══════════════════════════════════════════════════
[트레이딩 모드]
- 모드: {mode_display}
- 실주문 여부: {'예' if TRADING_MODE == 'LIVE' else '아니오' if TRADING_MODE == 'CBT' else '모의투자만'}

[API 설정]
- BASE URL: {KIS_BASE_URL}
- 계좌번호: {ACCOUNT_NO[:4]}****

[전략 파라미터]
- 종목코드: {DEFAULT_STOCK_CODE}
- ATR 기간: {ATR_PERIOD}일
- 추세 MA: {TREND_MA_PERIOD}일
- 손절 배수: {ATR_MULTIPLIER_SL}x ATR
- 익절 배수: {ATR_MULTIPLIER_TP}x ATR

[멀티데이 설정]
- 트레일링 스탑: {'✅ ON' if ENABLE_TRAILING_STOP else '❌ OFF'}
- 갭 보호: {'✅ ON' if ENABLE_GAP_PROTECTION else '❌ OFF'}
- 이벤트 체크: {'✅ ON' if ENABLE_EVENT_RISK_CHECK else '❌ OFF'}

[주문 설정]
- 주문 수량: {ORDER_QUANTITY}주

[텔레그램 알림]
- 상태: {telegram_status}
═══════════════════════════════════════════════════
"""


def is_cbt_mode() -> bool:
    """CBT(종이매매) 모드인지 확인합니다."""
    return TRADING_MODE == "CBT"


def is_live_mode() -> bool:
    """LIVE(실계좌) 모드인지 확인합니다."""
    return TRADING_MODE == "LIVE"


def is_paper_mode() -> bool:
    """PAPER(모의투자) 모드인지 확인합니다."""
    return TRADING_MODE == "PAPER"


def can_place_orders() -> bool:
    """실제 주문이 가능한 모드인지 확인합니다."""
    return TRADING_MODE in ("LIVE", "PAPER")


def get_cbt_settings_summary() -> str:
    """
    CBT 모드 설정 요약을 반환합니다.
    
    Returns:
        str: CBT 설정 요약 문자열
    """
    if TRADING_MODE != "CBT":
        return ""
    
    return f"""
═══════════════════════════════════════════════════
🧪 CBT (Closed Beta Test) 모드 설정
═══════════════════════════════════════════════════
• 초기 자본금: {CBT_INITIAL_CAPITAL:,}원
• 저장 방식: {CBT_STORAGE_TYPE.upper()}
• 수수료율: {CBT_COMMISSION_RATE * 100:.3f}%
• 자동 리포트: {'✅ 활성화' if CBT_AUTO_REPORT_ENABLED else '❌ 비활성화'}
• 데이터 저장 경로: {CBT_DATA_DIR}
═══════════════════════════════════════════════════
⚠️ CBT 모드: 실계좌 주문이 발생하지 않습니다.
   모든 체결은 가상으로 처리됩니다.
═══════════════════════════════════════════════════
"""
