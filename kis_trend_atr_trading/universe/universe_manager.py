"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 유니버스(종목 선정) 관리 모듈
═══════════════════════════════════════════════════════════════════════════════

자동매매 대상 종목을 관리합니다.

★ 종목 선정 방식:
    1. YAML 고정 종목 리스트
    2. 거래대금 상위 N개 자동 선정
    3. ATR/변동성 필터 통과 종목

★ 안전장치:
    - 최대 종목 수 제한
    - 최소 거래대금/시가총액 필터
    - 관리종목/정리매매 제외
"""

import yaml
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from enum import Enum

from utils.logger import get_logger

logger = get_logger("universe_manager")


# ═══════════════════════════════════════════════════════════════════════════════
# 데이터 클래스 및 열거형
# ═══════════════════════════════════════════════════════════════════════════════

class SelectionMethod(Enum):
    """종목 선정 방식"""
    FIXED = "fixed"                    # 고정 종목 리스트
    VOLUME_TOP = "volume_top"          # 거래대금 상위
    ATR_FILTER = "atr_filter"          # ATR 필터 통과
    COMBINED = "combined"              # 복합 조건


@dataclass
class StockInfo:
    """종목 정보"""
    code: str
    name: str = ""
    market: str = "KOSPI"
    avg_volume: float = 0.0            # 평균 거래대금 (원)
    avg_atr: float = 0.0               # 평균 ATR
    atr_pct: float = 0.0               # ATR 비율 (%)
    market_cap: float = 0.0            # 시가총액 (억원)
    is_management: bool = False        # 관리종목 여부
    is_valid: bool = True              # 유효성 (거래 가능)
    added_date: str = ""
    last_updated: str = ""
    
    def __post_init__(self):
        if not self.added_date:
            self.added_date = datetime.now().strftime("%Y-%m-%d")
        self.last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class UniverseConfig:
    """유니버스 설정"""
    selection_method: SelectionMethod = SelectionMethod.FIXED
    max_stocks: int = 10               # 최대 종목 수
    min_volume: float = 1_000_000_000  # 최소 거래대금 (10억)
    min_market_cap: float = 1000       # 최소 시가총액 (억원)
    min_atr_pct: float = 1.0           # 최소 ATR 비율 (%)
    max_atr_pct: float = 10.0          # 최대 ATR 비율 (%)
    exclude_management: bool = True    # 관리종목 제외
    volume_top_n: int = 50             # 거래대금 상위 N개 후보
    fixed_stocks: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════════
# 유니버스 매니저 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class UniverseManager:
    """
    유니버스(거래 종목) 관리자
    
    자동매매 대상 종목을 관리하고 선정합니다.
    
    Usage:
        # 1. 고정 종목 리스트 사용
        manager = UniverseManager.from_yaml("config/universe.yaml")
        stocks = manager.get_universe()
        
        # 2. 동적 선정
        manager = UniverseManager(config=UniverseConfig(
            selection_method=SelectionMethod.VOLUME_TOP,
            volume_top_n=50
        ))
        stocks = manager.refresh_universe(api_client)
    """
    
    def __init__(
        self,
        config: UniverseConfig = None,
        data_dir: Path = None
    ):
        """
        유니버스 매니저 초기화
        
        Args:
            config: 유니버스 설정
            data_dir: 데이터 저장 경로
        """
        self.config = config or UniverseConfig()
        self.data_dir = data_dir or Path(__file__).parent.parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # 현재 유니버스
        self._universe: Dict[str, StockInfo] = {}
        
        # 캐시 파일
        self._cache_file = self.data_dir / "universe_cache.yaml"
        
        logger.info(
            f"[UNIVERSE] 매니저 초기화: "
            f"방식={self.config.selection_method.value}, "
            f"최대종목={self.config.max_stocks}"
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 유니버스 조회
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_universe(self) -> List[StockInfo]:
        """
        현재 유니버스 종목 목록을 반환합니다.
        
        Returns:
            List[StockInfo]: 종목 목록
        """
        return list(self._universe.values())
    
    def get_stock_codes(self) -> List[str]:
        """
        유니버스 종목 코드만 반환합니다.
        
        Returns:
            List[str]: 종목 코드 목록
        """
        return list(self._universe.keys())
    
    def get_stock(self, code: str) -> Optional[StockInfo]:
        """
        특정 종목 정보를 반환합니다.
        
        Args:
            code: 종목 코드
            
        Returns:
            Optional[StockInfo]: 종목 정보
        """
        return self._universe.get(code)
    
    def is_in_universe(self, code: str) -> bool:
        """
        종목이 유니버스에 포함되어 있는지 확인합니다.
        
        Args:
            code: 종목 코드
            
        Returns:
            bool: 포함 여부
        """
        return code in self._universe
    
    def count(self) -> int:
        """유니버스 종목 수"""
        return len(self._universe)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 유니버스 관리
    # ═══════════════════════════════════════════════════════════════════════════
    
    def add_stock(self, stock: StockInfo) -> bool:
        """
        종목을 유니버스에 추가합니다.
        
        Args:
            stock: 종목 정보
            
        Returns:
            bool: 추가 성공 여부
        """
        if len(self._universe) >= self.config.max_stocks:
            logger.warning(
                f"[UNIVERSE] 최대 종목 수({self.config.max_stocks}) 도달, "
                f"추가 불가: {stock.code}"
            )
            return False
        
        if not self._validate_stock(stock):
            return False
        
        self._universe[stock.code] = stock
        logger.info(f"[UNIVERSE] 종목 추가: {stock.code} ({stock.name})")
        return True
    
    def remove_stock(self, code: str) -> bool:
        """
        종목을 유니버스에서 제거합니다.
        
        Args:
            code: 종목 코드
            
        Returns:
            bool: 제거 성공 여부
        """
        if code in self._universe:
            stock = self._universe.pop(code)
            logger.info(f"[UNIVERSE] 종목 제거: {code} ({stock.name})")
            return True
        return False
    
    def clear(self) -> None:
        """유니버스를 초기화합니다."""
        self._universe.clear()
        logger.info("[UNIVERSE] 유니버스 초기화됨")
    
    def _validate_stock(self, stock: StockInfo) -> bool:
        """
        종목 유효성을 검증합니다.
        
        Args:
            stock: 종목 정보
            
        Returns:
            bool: 유효 여부
        """
        # 관리종목 체크
        if self.config.exclude_management and stock.is_management:
            logger.warning(f"[UNIVERSE] 관리종목 제외: {stock.code}")
            return False
        
        # 거래대금 체크
        if stock.avg_volume < self.config.min_volume:
            logger.debug(
                f"[UNIVERSE] 거래대금 부족: {stock.code} "
                f"({stock.avg_volume:,.0f} < {self.config.min_volume:,.0f})"
            )
            return False
        
        # 시가총액 체크
        if stock.market_cap > 0 and stock.market_cap < self.config.min_market_cap:
            logger.debug(
                f"[UNIVERSE] 시가총액 부족: {stock.code} "
                f"({stock.market_cap:,.0f} < {self.config.min_market_cap:,.0f})"
            )
            return False
        
        # ATR 비율 체크
        if stock.atr_pct > 0:
            if stock.atr_pct < self.config.min_atr_pct:
                logger.debug(
                    f"[UNIVERSE] ATR 비율 부족: {stock.code} "
                    f"({stock.atr_pct:.2f}% < {self.config.min_atr_pct:.2f}%)"
                )
                return False
            if stock.atr_pct > self.config.max_atr_pct:
                logger.debug(
                    f"[UNIVERSE] ATR 비율 초과: {stock.code} "
                    f"({stock.atr_pct:.2f}% > {self.config.max_atr_pct:.2f}%)"
                )
                return False
        
        return True
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 유니버스 갱신
    # ═══════════════════════════════════════════════════════════════════════════
    
    def refresh_universe(self, api_client=None) -> List[StockInfo]:
        """
        유니버스를 갱신합니다.
        
        설정된 선정 방식에 따라 종목을 선정합니다.
        
        Args:
            api_client: KIS API 클라이언트 (동적 선정 시 필요)
            
        Returns:
            List[StockInfo]: 갱신된 종목 목록
        """
        method = self.config.selection_method
        
        if method == SelectionMethod.FIXED:
            return self._select_fixed_stocks()
        elif method == SelectionMethod.VOLUME_TOP:
            return self._select_volume_top(api_client)
        elif method == SelectionMethod.ATR_FILTER:
            return self._select_atr_filtered(api_client)
        elif method == SelectionMethod.COMBINED:
            return self._select_combined(api_client)
        else:
            logger.warning(f"[UNIVERSE] 알 수 없는 선정 방식: {method}")
            return self._select_fixed_stocks()
    
    def _select_fixed_stocks(self) -> List[StockInfo]:
        """고정 종목 리스트 선정"""
        self.clear()
        
        for code in self.config.fixed_stocks[:self.config.max_stocks]:
            stock = StockInfo(code=code)
            self.add_stock(stock)
        
        logger.info(
            f"[UNIVERSE] 고정 종목 선정 완료: {len(self._universe)}개"
        )
        return self.get_universe()
    
    def _select_volume_top(self, api_client) -> List[StockInfo]:
        """
        거래대금 상위 N개 선정
        
        Args:
            api_client: KIS API 클라이언트
            
        Returns:
            List[StockInfo]: 선정된 종목 목록
        """
        self.clear()
        
        if api_client is None:
            logger.warning("[UNIVERSE] API 클라이언트 없음, 고정 목록 사용")
            return self._select_fixed_stocks()
        
        try:
            # TODO: KIS API로 거래대금 상위 종목 조회
            # 현재는 고정 목록 대체
            logger.info("[UNIVERSE] 거래대금 상위 선정 (API 연동 필요)")
            return self._select_fixed_stocks()
            
        except Exception as e:
            logger.error(f"[UNIVERSE] 거래대금 조회 실패: {e}")
            return self._select_fixed_stocks()
    
    def _select_atr_filtered(self, api_client) -> List[StockInfo]:
        """
        ATR 필터 통과 종목 선정
        
        Args:
            api_client: KIS API 클라이언트
            
        Returns:
            List[StockInfo]: 선정된 종목 목록
        """
        self.clear()
        
        if api_client is None:
            logger.warning("[UNIVERSE] API 클라이언트 없음, 고정 목록 사용")
            return self._select_fixed_stocks()
        
        try:
            # TODO: 각 종목의 ATR 계산 후 필터링
            logger.info("[UNIVERSE] ATR 필터 선정 (API 연동 필요)")
            return self._select_fixed_stocks()
            
        except Exception as e:
            logger.error(f"[UNIVERSE] ATR 필터링 실패: {e}")
            return self._select_fixed_stocks()
    
    def _select_combined(self, api_client) -> List[StockInfo]:
        """
        복합 조건 선정 (거래대금 + ATR)
        
        Args:
            api_client: KIS API 클라이언트
            
        Returns:
            List[StockInfo]: 선정된 종목 목록
        """
        # 1단계: 거래대금 상위 후보
        candidates = self._select_volume_top(api_client)
        
        # 2단계: ATR 필터 적용
        filtered = []
        for stock in candidates:
            if self._validate_stock(stock):
                filtered.append(stock)
        
        logger.info(
            f"[UNIVERSE] 복합 조건 선정: {len(candidates)} → {len(filtered)}"
        )
        return filtered
    
    # ═══════════════════════════════════════════════════════════════════════════
    # YAML 파일 연동
    # ═══════════════════════════════════════════════════════════════════════════
    
    @classmethod
    def from_yaml(cls, yaml_path: str) -> "UniverseManager":
        """
        YAML 파일에서 유니버스 매니저를 생성합니다.
        
        Args:
            yaml_path: YAML 파일 경로
            
        Returns:
            UniverseManager: 유니버스 매니저
        """
        path = Path(yaml_path)
        
        if not path.exists():
            logger.warning(f"[UNIVERSE] YAML 파일 없음: {yaml_path}")
            return cls()
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            
            # 설정 파싱
            config_data = data.get("universe", {})
            
            method_str = config_data.get("selection_method", "fixed")
            try:
                method = SelectionMethod(method_str)
            except ValueError:
                method = SelectionMethod.FIXED
            
            config = UniverseConfig(
                selection_method=method,
                max_stocks=config_data.get("max_stocks", 10),
                min_volume=config_data.get("min_volume", 1_000_000_000),
                min_market_cap=config_data.get("min_market_cap", 1000),
                min_atr_pct=config_data.get("min_atr_pct", 1.0),
                max_atr_pct=config_data.get("max_atr_pct", 10.0),
                exclude_management=config_data.get("exclude_management", True),
                volume_top_n=config_data.get("volume_top_n", 50),
                fixed_stocks=config_data.get("stocks", [])
            )
            
            manager = cls(config=config)
            manager.refresh_universe()
            
            logger.info(f"[UNIVERSE] YAML 로드 완료: {yaml_path}")
            return manager
            
        except Exception as e:
            logger.error(f"[UNIVERSE] YAML 로드 실패: {e}")
            return cls()
    
    def save_to_yaml(self, yaml_path: str) -> bool:
        """
        현재 유니버스를 YAML 파일에 저장합니다.
        
        Args:
            yaml_path: YAML 파일 경로
            
        Returns:
            bool: 저장 성공 여부
        """
        try:
            data = {
                "universe": {
                    "selection_method": self.config.selection_method.value,
                    "max_stocks": self.config.max_stocks,
                    "min_volume": self.config.min_volume,
                    "min_market_cap": self.config.min_market_cap,
                    "min_atr_pct": self.config.min_atr_pct,
                    "max_atr_pct": self.config.max_atr_pct,
                    "exclude_management": self.config.exclude_management,
                    "volume_top_n": self.config.volume_top_n,
                    "stocks": self.get_stock_codes()
                },
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            with open(yaml_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
            
            logger.info(f"[UNIVERSE] YAML 저장 완료: {yaml_path}")
            return True
            
        except Exception as e:
            logger.error(f"[UNIVERSE] YAML 저장 실패: {e}")
            return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 유틸리티
    # ═══════════════════════════════════════════════════════════════════════════
    
    def print_universe(self) -> None:
        """현재 유니버스를 출력합니다."""
        print("\n" + "═" * 60)
        print("               [TRADING UNIVERSE]")
        print("═" * 60)
        print(f"  선정 방식: {self.config.selection_method.value}")
        print(f"  최대 종목: {self.config.max_stocks}개")
        print(f"  현재 종목: {len(self._universe)}개")
        print("-" * 60)
        
        for i, (code, stock) in enumerate(self._universe.items(), 1):
            name = stock.name or "N/A"
            print(f"  {i:2}. {code} ({name})")
        
        print("═" * 60 + "\n")
    
    def get_summary(self) -> Dict[str, Any]:
        """유니버스 요약 정보를 반환합니다."""
        return {
            "selection_method": self.config.selection_method.value,
            "max_stocks": self.config.max_stocks,
            "current_count": len(self._universe),
            "stocks": [
                {"code": s.code, "name": s.name}
                for s in self._universe.values()
            ],
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 편의 함수
# ═══════════════════════════════════════════════════════════════════════════════

_universe_manager: Optional[UniverseManager] = None


def get_universe_manager(yaml_path: str = None) -> UniverseManager:
    """
    싱글톤 UniverseManager를 반환합니다.
    
    Args:
        yaml_path: YAML 파일 경로 (최초 생성 시)
        
    Returns:
        UniverseManager: 유니버스 매니저
    """
    global _universe_manager
    
    if _universe_manager is None:
        if yaml_path:
            _universe_manager = UniverseManager.from_yaml(yaml_path)
        else:
            # 기본 설정으로 생성
            _universe_manager = UniverseManager(
                config=UniverseConfig(
                    selection_method=SelectionMethod.FIXED,
                    fixed_stocks=["005930"]  # 삼성전자 기본
                )
            )
            _universe_manager.refresh_universe()
    
    return _universe_manager


def create_universe_from_config(config: Dict[str, Any]) -> UniverseManager:
    """
    설정 딕셔너리에서 유니버스 매니저를 생성합니다.
    
    Args:
        config: 설정 딕셔너리
        
    Returns:
        UniverseManager: 유니버스 매니저
    """
    method_str = config.get("selection_method", "fixed")
    try:
        method = SelectionMethod(method_str)
    except ValueError:
        method = SelectionMethod.FIXED
    
    universe_config = UniverseConfig(
        selection_method=method,
        max_stocks=config.get("max_stocks", 10),
        min_volume=config.get("min_volume", 1_000_000_000),
        min_atr_pct=config.get("min_atr_pct", 1.0),
        max_atr_pct=config.get("max_atr_pct", 10.0),
        fixed_stocks=config.get("stocks", [])
    )
    
    manager = UniverseManager(config=universe_config)
    manager.refresh_universe()
    
    return manager
