"""
KIS WebSocket 자동매매 시스템 - 설정 모듈

환경변수 및 시스템 상수를 관리합니다.

환경변수 설정:
    .env 파일에 다음 값들을 설정하세요:
    - KIS_APP_KEY: 한국투자증권 앱 키
    - KIS_APP_SECRET: 한국투자증권 앱 시크릿
    - KIS_ACCOUNT_NO: 계좌번호
    - TELEGRAM_BOT_TOKEN: 텔레그램 봇 토큰
    - TELEGRAM_CHAT_ID: 텔레그램 채팅 ID
    - TRADE_MODE: CBT 또는 LIVE
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# .env 파일 로드
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")


# ════════════════════════════════════════════════════════════════
# 거래 모드 열거형
# ════════════════════════════════════════════════════════════════

class TradeMode(Enum):
    """
    거래 모드
    
    - CBT: 모의 백테스트 모드 (주문 없이 알림만)
    - LIVE: 실거래 모드 (실제 주문 가능)
    """
    CBT = "CBT"    # 주문 없이 텔레그램 알림만
    LIVE = "LIVE"  # 실제 주문 실행


# ════════════════════════════════════════════════════════════════
# 종목 상태 열거형
# ════════════════════════════════════════════════════════════════

class StockState(Enum):
    """
    종목별 상태
    
    - WAIT: 진입 대기 (아직 진입하지 않음)
    - ENTERED: 진입 완료 (포지션 보유 중)
    - EXITED: 청산 완료 (손절/익절 후 더 이상 감시 안함)
    """
    WAIT = "WAIT"         # 진입 대기
    ENTERED = "ENTERED"   # 포지션 보유 중
    EXITED = "EXITED"     # 청산 완료


# ════════════════════════════════════════════════════════════════
# 설정 데이터 클래스
# ════════════════════════════════════════════════════════════════

@dataclass
class KISConfig:
    """한국투자증권 API 설정"""
    app_key: str
    app_secret: str
    account_no: str
    account_product_code: str = "01"
    
    # WebSocket URL (실전/모의투자 동일)
    ws_url: str = "ws://ops.koreainvestment.com:21000"
    
    # REST API URL
    base_url: str = "https://openapi.koreainvestment.com:9443"
    paper_base_url: str = "https://openapivts.koreainvestment.com:29443"


@dataclass
class TelegramConfig:
    """텔레그램 설정"""
    bot_token: str
    chat_id: str
    enabled: bool = True


@dataclass
class TradingConfig:
    """거래 설정"""
    mode: TradeMode
    
    # 운영 시간 (HH:MM 형식)
    entry_start_time: str = "09:00"   # 진입 시작 시간
    entry_end_time: str = "15:20"     # 진입 마감 시간
    close_time: str = "15:30"         # WebSocket 종료 시간
    
    # 데이터 파일 경로
    universe_file: str = "data/trade_universe.json"


# ════════════════════════════════════════════════════════════════
# 환경변수에서 설정 로드
# ════════════════════════════════════════════════════════════════

def get_kis_config() -> KISConfig:
    """
    환경변수에서 KIS API 설정을 로드합니다.
    
    Returns:
        KISConfig: KIS API 설정 객체
        
    Raises:
        ValueError: 필수 환경변수가 누락된 경우
    """
    app_key = os.getenv("KIS_APP_KEY", "")
    app_secret = os.getenv("KIS_APP_SECRET", "")
    account_no = os.getenv("KIS_ACCOUNT_NO", "")
    account_product_code = os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "01")
    
    # 필수값 검증
    if not app_key:
        raise ValueError("KIS_APP_KEY 환경변수가 설정되지 않았습니다.")
    if not app_secret:
        raise ValueError("KIS_APP_SECRET 환경변수가 설정되지 않았습니다.")
    if not account_no:
        raise ValueError("KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다.")
    
    return KISConfig(
        app_key=app_key,
        app_secret=app_secret,
        account_no=account_no,
        account_product_code=account_product_code
    )


def get_telegram_config() -> TelegramConfig:
    """
    환경변수에서 텔레그램 설정을 로드합니다.
    
    Returns:
        TelegramConfig: 텔레그램 설정 객체
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enabled = os.getenv("TELEGRAM_ENABLED", "true").lower() in ("true", "1", "yes")
    
    return TelegramConfig(
        bot_token=bot_token,
        chat_id=chat_id,
        enabled=enabled and bool(bot_token) and bool(chat_id)
    )


def get_trading_config() -> TradingConfig:
    """
    환경변수에서 거래 설정을 로드합니다.
    
    Returns:
        TradingConfig: 거래 설정 객체
    """
    mode_str = os.getenv("TRADE_MODE", "CBT").upper()
    
    try:
        mode = TradeMode(mode_str)
    except ValueError:
        print(f"[경고] 알 수 없는 TRADE_MODE: {mode_str}, CBT로 설정됩니다.")
        mode = TradeMode.CBT
    
    return TradingConfig(
        mode=mode,
        entry_start_time=os.getenv("ENTRY_START_TIME", "09:00"),
        entry_end_time=os.getenv("ENTRY_END_TIME", "15:20"),
        close_time=os.getenv("CLOSE_TIME", "15:30"),
        universe_file=os.getenv("UNIVERSE_FILE", "data/trade_universe.json")
    )


# ════════════════════════════════════════════════════════════════
# 전역 설정 인스턴스
# ════════════════════════════════════════════════════════════════

# 설정이 필요할 때 lazy loading으로 생성
_kis_config: Optional[KISConfig] = None
_telegram_config: Optional[TelegramConfig] = None
_trading_config: Optional[TradingConfig] = None


def get_config():
    """
    모든 설정을 로드하여 반환합니다.
    
    Returns:
        tuple: (KISConfig, TelegramConfig, TradingConfig)
    """
    global _kis_config, _telegram_config, _trading_config
    
    if _kis_config is None:
        _kis_config = get_kis_config()
    if _telegram_config is None:
        _telegram_config = get_telegram_config()
    if _trading_config is None:
        _trading_config = get_trading_config()
    
    return _kis_config, _telegram_config, _trading_config


# ════════════════════════════════════════════════════════════════
# 상수 정의
# ════════════════════════════════════════════════════════════════

# WebSocket 재연결 설정
WS_RECONNECT_DELAY = 5          # 재연결 대기 시간 (초)
WS_MAX_RECONNECT_ATTEMPTS = 10  # 최대 재연결 시도 횟수
WS_PING_INTERVAL = 30           # 핑 전송 간격 (초)

# API Rate Limit
RATE_LIMIT_DELAY = 0.1          # API 호출 간 최소 대기 시간 (초)
API_TIMEOUT = 10                # API 요청 타임아웃 (초)
MAX_RETRIES = 3                 # API 재시도 횟수

# 로깅 설정
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
