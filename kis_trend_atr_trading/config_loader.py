"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 설정 로딩 모듈
═══════════════════════════════════════════════════════════════════════════════

이 모듈은 환경별 설정 파일(dev.yaml, prod.yaml)을 로딩합니다.

★ 구조적 안전장치:
    1. DEV와 PROD 설정 파일이 완전히 분리되어 있습니다.
    2. 민감한 정보(API 키, 시크릿, 계좌번호)는 환경변수에서만 로드합니다.
    3. 설정 파일에는 민감 정보를 저장하지 않습니다.

★ 설정 파일 구조:
    config/
    ├── dev.yaml   # 모의투자 설정 (allow_order: true)
    └── prod.yaml  # 실계좌 설정 (allow_order: false 기본값)

⚠️ 주의사항:
    - 설정 파일에 API 키, 계좌번호를 직접 입력하지 마십시오.
    - 민감 정보는 반드시 환경변수로 전달하십시오.

═══════════════════════════════════════════════════════════════════════════════
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, Optional
import yaml

from env import get_environment, Environment, is_prod


# ═══════════════════════════════════════════════════════════════════════════════
# 설정 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class APIConfig:
    """API 설정"""
    base_url: str
    timeout: int = 10
    max_retries: int = 3
    retry_delay: float = 1.0
    rate_limit_delay: float = 0.1


@dataclass
class OrderConfig:
    """주문 설정"""
    allow_order: bool
    default_stock_code: str = "005930"
    default_quantity: int = 1
    tr_id: Dict[str, str] = field(default_factory=dict)


@dataclass
class StrategyConfig:
    """전략 설정"""
    atr_period: int = 14
    trend_ma_period: int = 50
    atr_multiplier_sl: float = 2.0
    atr_multiplier_tp: float = 3.0


@dataclass
class RiskConfig:
    """리스크 관리 설정"""
    max_loss_pct: float = 5.0
    atr_spike_threshold: float = 2.5
    adx_threshold: float = 25.0
    adx_period: int = 14
    daily_max_loss_percent: float = 3.0
    enable_kill_switch: bool = False


@dataclass
class LoggingConfig:
    """로깅 설정"""
    level: str = "INFO"
    log_dir: str = "logs"


@dataclass
class BacktestConfig:
    """백테스트 설정"""
    initial_capital: int = 10_000_000
    commission_rate: float = 0.00015


@dataclass
class MarketDataConfig:
    """시장데이터 피드 설정"""
    data_feed: str = "rest"  # rest|ws
    ws_timeframe: str = "1m"


@dataclass
class CredentialsConfig:
    """
    인증 정보 설정
    
    ★ 이 정보는 환경변수에서만 로드됩니다.
    ★ 설정 파일에 저장되지 않습니다.
    """
    app_key: str = ""
    app_secret: str = ""
    account_no: str = ""
    account_product_code: str = "01"


@dataclass
class Config:
    """
    전체 설정 클래스
    
    모든 설정값을 하나의 객체로 관리합니다.
    """
    environment: str
    description: str
    api: APIConfig
    order: OrderConfig
    strategy: StrategyConfig
    risk: RiskConfig
    logging: LoggingConfig
    backtest: BacktestConfig
    credentials: CredentialsConfig
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)


# ═══════════════════════════════════════════════════════════════════════════════
# 설정 로더
# ═══════════════════════════════════════════════════════════════════════════════

class ConfigLoader:
    """
    환경별 설정을 로드하는 클래스
    
    ★ 구조적 안전장치:
        - DEV/PROD별 완전히 분리된 설정 파일 사용
        - 민감 정보는 환경변수에서만 로드
    """
    
    # 설정 파일 경로
    CONFIG_DIR = Path(__file__).parent / "config"
    DEV_CONFIG_FILE = CONFIG_DIR / "dev.yaml"
    PROD_CONFIG_FILE = CONFIG_DIR / "prod.yaml"
    
    def __init__(self):
        """설정 로더 초기화"""
        self._config: Optional[Config] = None
        self._raw_config: Optional[Dict[str, Any]] = None
    
    def load(self) -> Config:
        """
        현재 환경에 맞는 설정을 로드합니다.
        
        ★ 환경별로 완전히 다른 설정 파일을 로드합니다.
        
        Returns:
            Config: 로드된 설정
        
        Raises:
            FileNotFoundError: 설정 파일이 없는 경우
            ValueError: 설정 파일 형식이 잘못된 경우
        """
        if self._config is not None:
            return self._config
        
        # 환경 판별
        env = get_environment()
        
        # ★ 환경별 설정 파일 선택 (완전 분리)
        if env == Environment.DEV:
            config_file = self.DEV_CONFIG_FILE
        else:
            config_file = self.PROD_CONFIG_FILE
        
        # 설정 파일 로드
        self._raw_config = self._load_yaml(config_file)
        
        # 설정 객체 생성
        self._config = self._create_config(self._raw_config)
        
        # 설정 검증
        self._validate_config(self._config)
        
        return self._config
    
    def _load_yaml(self, file_path: Path) -> Dict[str, Any]:
        """
        YAML 파일을 로드합니다.
        
        Args:
            file_path: YAML 파일 경로
        
        Returns:
            Dict: 로드된 설정 딕셔너리
        
        Raises:
            FileNotFoundError: 파일이 없는 경우
        """
        if not file_path.exists():
            raise FileNotFoundError(f"설정 파일이 없습니다: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    
    def _create_config(self, raw: Dict[str, Any]) -> Config:
        """
        원시 딕셔너리에서 Config 객체를 생성합니다.
        
        Args:
            raw: YAML에서 로드된 딕셔너리
        
        Returns:
            Config: 설정 객체
        """
        # API 설정
        api_raw = raw.get("api", {})
        api_config = APIConfig(
            base_url=api_raw.get("base_url", ""),
            timeout=api_raw.get("timeout", 10),
            max_retries=api_raw.get("max_retries", 3),
            retry_delay=api_raw.get("retry_delay", 1.0),
            rate_limit_delay=api_raw.get("rate_limit_delay", 0.1)
        )
        
        # 주문 설정
        order_raw = raw.get("order", {})
        order_config = OrderConfig(
            allow_order=order_raw.get("allow_order", False),
            default_stock_code=order_raw.get("default_stock_code", "005930"),
            default_quantity=order_raw.get("default_quantity", 1),
            tr_id=order_raw.get("tr_id", {})
        )
        
        # 전략 설정
        strategy_raw = raw.get("strategy", {})
        strategy_config = StrategyConfig(
            atr_period=strategy_raw.get("atr_period", 14),
            trend_ma_period=strategy_raw.get("trend_ma_period", 50),
            atr_multiplier_sl=strategy_raw.get("atr_multiplier_sl", 2.0),
            atr_multiplier_tp=strategy_raw.get("atr_multiplier_tp", 3.0)
        )
        
        # 리스크 설정
        risk_raw = raw.get("risk", {})
        risk_config = RiskConfig(
            max_loss_pct=risk_raw.get("max_loss_pct", 5.0),
            atr_spike_threshold=risk_raw.get("atr_spike_threshold", 2.5),
            adx_threshold=risk_raw.get("adx_threshold", 25.0),
            adx_period=risk_raw.get("adx_period", 14),
            daily_max_loss_percent=risk_raw.get("daily_max_loss_percent", 3.0),
            enable_kill_switch=risk_raw.get("enable_kill_switch", False)
        )
        
        # 로깅 설정
        logging_raw = raw.get("logging", {})
        logging_config = LoggingConfig(
            level=logging_raw.get("level", "INFO"),
            log_dir=logging_raw.get("log_dir", "logs")
        )
        
        # 백테스트 설정
        backtest_raw = raw.get("backtest", {})
        backtest_config = BacktestConfig(
            initial_capital=backtest_raw.get("initial_capital", 10_000_000),
            commission_rate=backtest_raw.get("commission_rate", 0.00015)
        )

        # 시장데이터 피드 설정
        market_data_raw = raw.get("market_data", {})
        market_data_config = MarketDataConfig(
            data_feed=str(market_data_raw.get("data_feed", "rest")),
            ws_timeframe=str(market_data_raw.get("ws_timeframe", "1m")),
        )
        
        # ★ 인증 정보는 환경변수에서만 로드 (보안)
        credentials_config = CredentialsConfig(
            app_key=os.getenv("KIS_APP_KEY", ""),
            app_secret=os.getenv("KIS_APP_SECRET", ""),
            account_no=os.getenv("KIS_ACCOUNT_NO", ""),
            account_product_code=os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "01")
        )
        
        return Config(
            environment=raw.get("environment", "DEV"),
            description=raw.get("description", ""),
            api=api_config,
            order=order_config,
            strategy=strategy_config,
            risk=risk_config,
            logging=logging_config,
            backtest=backtest_config,
            credentials=credentials_config,
            market_data=market_data_config,
        )
    
    def _validate_config(self, config: Config) -> None:
        """
        설정을 검증합니다.
        
        Args:
            config: 검증할 설정
        
        Raises:
            ValueError: 설정이 유효하지 않은 경우
        """
        errors = []
        
        # API URL 검증
        if not config.api.base_url:
            errors.append("API base_url이 설정되지 않았습니다.")
        
        # ★ PROD 환경에서 추가 검증
        if is_prod():
            # PROD 환경에서 DEV URL 사용 방지
            if "openapivts" in config.api.base_url:
                errors.append(
                    "⚠️ PROD 환경에서 모의투자 URL이 감지되었습니다. "
                    "설정 파일을 확인하세요."
                )
        else:
            # DEV 환경에서 PROD URL 사용 방지
            if "openapivts" not in config.api.base_url:
                errors.append(
                    "⚠️ DEV 환경에서 실계좌 URL이 감지되었습니다. "
                    "설정 파일을 확인하세요."
                )
        
        if errors:
            for error in errors:
                print(f"[설정 오류] {error}")
            raise ValueError("설정 검증 실패")
    
    def get_raw_config(self) -> Dict[str, Any]:
        """
        원시 설정 딕셔너리를 반환합니다.
        
        Returns:
            Dict: 원시 설정
        """
        if self._raw_config is None:
            self.load()
        return self._raw_config or {}


# ═══════════════════════════════════════════════════════════════════════════════
# 전역 설정 인스턴스
# ═══════════════════════════════════════════════════════════════════════════════

_config_loader: Optional[ConfigLoader] = None
_config: Optional[Config] = None


def get_config() -> Config:
    """
    전역 설정 객체를 반환합니다.
    
    ★ 싱글톤 패턴: 프로그램 전체에서 하나의 설정 인스턴스만 사용합니다.
    
    Returns:
        Config: 설정 객체
    
    Example:
        >>> config = get_config()
        >>> print(config.api.base_url)
    """
    global _config_loader, _config
    
    if _config is None:
        _config_loader = ConfigLoader()
        _config = _config_loader.load()
    
    return _config


def reload_config() -> Config:
    """
    설정을 다시 로드합니다.
    
    ⚠️ 주의: 일반적인 상황에서는 호출할 필요가 없습니다.
    테스트나 설정 변경 후 재로드가 필요한 경우에만 사용하세요.
    
    Returns:
        Config: 새로 로드된 설정
    """
    global _config_loader, _config
    _config_loader = None
    _config = None
    return get_config()


# ═══════════════════════════════════════════════════════════════════════════════
# 편의 함수
# ═══════════════════════════════════════════════════════════════════════════════

def is_order_allowed() -> bool:
    """
    주문이 허용되었는지 확인합니다.
    
    ★ 안전장치 1단계: 설정 파일의 allow_order 확인
    
    Returns:
        bool: 주문 허용 여부
    """
    config = get_config()
    return config.order.allow_order


def get_api_base_url() -> str:
    """
    API Base URL을 반환합니다.
    
    Returns:
        str: API Base URL
    """
    config = get_config()
    return config.api.base_url


def get_tr_id(order_type: str) -> str:
    """
    주문 유형별 TR_ID를 반환합니다.
    
    Args:
        order_type: 주문 유형 ("buy", "sell", "balance", "order_status")
    
    Returns:
        str: TR_ID
    """
    config = get_config()
    return config.order.tr_id.get(order_type, "")


def print_config_summary() -> None:
    """
    현재 설정 요약을 출력합니다.
    """
    config = get_config()
    
    print("\n" + "═" * 60)
    print("              현재 설정 요약")
    print("═" * 60)
    print(f"환경: {config.environment}")
    print(f"설명: {config.description}")
    print(f"\n[API 설정]")
    print(f"  - Base URL: {config.api.base_url}")
    print(f"  - Timeout: {config.api.timeout}초")
    print(f"\n[주문 설정]")
    print(f"  - 주문 허용: {'예' if config.order.allow_order else '아니오'}")
    print(f"  - 기본 종목: {config.order.default_stock_code}")
    print(f"  - 기본 수량: {config.order.default_quantity}주")
    print(f"\n[전략 설정]")
    print(f"  - ATR 기간: {config.strategy.atr_period}일")
    print(f"  - 추세 MA: {config.strategy.trend_ma_period}일")
    print(f"  - 손절 배수: {config.strategy.atr_multiplier_sl}x ATR")
    print(f"  - 익절 배수: {config.strategy.atr_multiplier_tp}x ATR")
    print(f"\n[인증 정보]")
    print(f"  - APP KEY: {'설정됨' if config.credentials.app_key else '미설정'}")
    print(f"  - APP SECRET: {'설정됨' if config.credentials.app_secret else '미설정'}")
    print(f"  - 계좌번호: {config.credentials.account_no[:4] + '****' if config.credentials.account_no else '미설정'}")
    print("═" * 60 + "\n")
