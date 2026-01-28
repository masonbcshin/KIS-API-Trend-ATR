"""
config/settings.py - 환경변수 및 전략 설정 관리

모든 민감 정보는 .env 파일에서 로딩합니다.
전략 파라미터는 투자 성향(MODE)에 따라 자동 설정됩니다.
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any
from dotenv import load_dotenv

# .env 파일 로드
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)


@dataclass
class StrategyParams:
    """전략 파라미터 데이터 클래스"""
    # 리스크 관리
    risk_per_trade: float = 0.02          # 1회 거래 리스크 (총 자산 대비 %)
    stop_loss_pct: float = -2.5           # 손절 기준 (%)
    take_profit_pct: float = 5.0          # 익절 기준 (%)
    
    # 변동성 필터
    atr_period: int = 14                  # ATR 계산 기간
    volatility_threshold: float = 1.5     # 변동성 임계값 (ATR 배수)
    
    # 거래 제한
    max_positions: int = 5                # 최대 동시 보유 종목 수
    min_order_amount: int = 100000        # 최소 주문 금액 (원)
    
    # 시간 제한
    trading_start_hour: int = 9           # 거래 시작 시간
    trading_start_minute: int = 5         # 거래 시작 분
    trading_end_hour: int = 15            # 신규 진입 마감 시간
    trading_end_minute: int = 20          # 신규 진입 마감 분


# 투자 성향별 전략 설정
STRATEGY_PRESETS: Dict[str, Dict[str, Any]] = {
    "stable": {
        "risk_per_trade": 0.01,
        "stop_loss_pct": -1.5,
        "take_profit_pct": 3.0,
        "volatility_threshold": 1.0,
        "max_positions": 3,
    },
    "normal": {
        "risk_per_trade": 0.02,
        "stop_loss_pct": -2.5,
        "take_profit_pct": 5.0,
        "volatility_threshold": 1.5,
        "max_positions": 5,
    },
    "aggressive": {
        "risk_per_trade": 0.03,
        "stop_loss_pct": -4.0,
        "take_profit_pct": 8.0,
        "volatility_threshold": 2.0,
        "max_positions": 8,
    },
}


class Settings:
    """
    전역 설정 관리 클래스
    
    환경변수에서 민감 정보를 로딩하고,
    투자 성향에 따라 전략 파라미터를 자동 설정합니다.
    """
    
    def __init__(self):
        # ═══════════════════════════════════════════════════════════════
        # API 인증 정보 (환경변수에서 로딩)
        # ═══════════════════════════════════════════════════════════════
        self.KIS_APP_KEY: str = os.getenv("KIS_APP_KEY", "")
        self.KIS_APP_SECRET: str = os.getenv("KIS_APP_SECRET", "")
        self.KIS_ACCOUNT_NO: str = os.getenv("KIS_ACCOUNT_NO", "")
        
        # ═══════════════════════════════════════════════════════════════
        # 텔레그램 설정
        # ═══════════════════════════════════════════════════════════════
        self.TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
        
        # ═══════════════════════════════════════════════════════════════
        # 거래 모드 설정
        # ═══════════════════════════════════════════════════════════════
        self.MODE: str = os.getenv("MODE", "normal")
        self.TRADING_MODE: str = os.getenv("TRADING_MODE", "paper")
        self.IS_PAPER_TRADING: bool = self.TRADING_MODE.lower() != "live"
        
        # ═══════════════════════════════════════════════════════════════
        # KIS API URL 설정
        # ═══════════════════════════════════════════════════════════════
        if self.IS_PAPER_TRADING:
            self.KIS_BASE_URL = "https://openapivts.koreainvestment.com:29443"
        else:
            self.KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
        
        # ═══════════════════════════════════════════════════════════════
        # 전략 파라미터 (MODE에 따라 자동 설정)
        # ═══════════════════════════════════════════════════════════════
        self.strategy = self._load_strategy_params()
        
        # ═══════════════════════════════════════════════════════════════
        # API 설정
        # ═══════════════════════════════════════════════════════════════
        self.API_TIMEOUT: int = 10
        self.API_MAX_RETRIES: int = 3
        self.API_RETRY_DELAY: float = 1.0
    
    def _load_strategy_params(self) -> StrategyParams:
        """투자 성향에 따른 전략 파라미터 로딩"""
        preset = STRATEGY_PRESETS.get(self.MODE, STRATEGY_PRESETS["normal"])
        return StrategyParams(**preset)
    
    def validate(self) -> tuple[bool, list[str]]:
        """
        설정 유효성 검증
        
        Returns:
            tuple: (검증 성공 여부, 오류 메시지 리스트)
        """
        errors = []
        
        if not self.KIS_APP_KEY:
            errors.append("KIS_APP_KEY가 설정되지 않았습니다.")
        if not self.KIS_APP_SECRET:
            errors.append("KIS_APP_SECRET이 설정되지 않았습니다.")
        if not self.KIS_ACCOUNT_NO:
            errors.append("KIS_ACCOUNT_NO가 설정되지 않았습니다.")
        if not self.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")
        if not self.TELEGRAM_CHAT_ID:
            errors.append("TELEGRAM_CHAT_ID가 설정되지 않았습니다.")
        
        # 손익비 검증 (최소 1:2)
        if abs(self.strategy.take_profit_pct) < abs(self.strategy.stop_loss_pct) * 2:
            errors.append(
                f"손익비가 1:2 미만입니다. "
                f"(손절: {self.strategy.stop_loss_pct}%, 익절: {self.strategy.take_profit_pct}%)"
            )
        
        return len(errors) == 0, errors
    
    def get_summary(self) -> str:
        """설정 요약 문자열 반환"""
        return f"""
═══════════════════════════════════════════════════════════════
KIS Auto Trader - 설정 요약
═══════════════════════════════════════════════════════════════
[거래 모드]
- 투자 성향: {self.MODE}
- 거래 환경: {'모의투자' if self.IS_PAPER_TRADING else '실계좌'}
- API URL: {self.KIS_BASE_URL}

[전략 파라미터]
- 1회 거래 리스크: {self.strategy.risk_per_trade * 100:.1f}%
- 손절 기준: {self.strategy.stop_loss_pct}%
- 익절 기준: +{self.strategy.take_profit_pct}%
- 손익비(R:R): 1:{abs(self.strategy.take_profit_pct / self.strategy.stop_loss_pct):.1f}

[거래 제한]
- 최대 보유 종목: {self.strategy.max_positions}개
- 최소 주문 금액: {self.strategy.min_order_amount:,}원
- 거래 시간: {self.strategy.trading_start_hour:02d}:{self.strategy.trading_start_minute:02d} ~ {self.strategy.trading_end_hour:02d}:{self.strategy.trading_end_minute:02d}

[텔레그램]
- 알림: {'✅ 활성화' if self.TELEGRAM_BOT_TOKEN else '❌ 비활성화'}
═══════════════════════════════════════════════════════════════
"""


# 전역 싱글톤 인스턴스
_settings_instance: Settings = None


def get_settings() -> Settings:
    """싱글톤 Settings 인스턴스 반환"""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance
