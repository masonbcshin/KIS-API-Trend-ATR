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
# 거래 설정
# ════════════════════════════════════════════════════════════════

# 모의투자 여부 (항상 True 유지)
IS_PAPER_TRADING = True

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
# 로깅 설정
# ════════════════════════════════════════════════════════════════

# 로그 레벨: DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_LEVEL = "INFO"

# 로그 파일 저장 경로
LOG_DIR = Path(__file__).parent.parent / "logs"

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
    return f"""
═══════════════════════════════════════════════════
KIS Trend-ATR Trading System - 설정 요약
═══════════════════════════════════════════════════
[API 설정]
- BASE URL: {KIS_BASE_URL}
- 모의투자: {'예' if IS_PAPER_TRADING else '아니오 (⚠️ 위험!)'}
- 계좌번호: {ACCOUNT_NO[:4]}****

[전략 파라미터]
- 종목코드: {DEFAULT_STOCK_CODE}
- ATR 기간: {ATR_PERIOD}일
- 추세 MA: {TREND_MA_PERIOD}일
- 손절 배수: {ATR_MULTIPLIER_SL}x ATR
- 익절 배수: {ATR_MULTIPLIER_TP}x ATR

[주문 설정]
- 주문 수량: {ORDER_QUANTITY}주
═══════════════════════════════════════════════════
"""
