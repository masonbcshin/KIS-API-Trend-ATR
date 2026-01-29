"""
KIS Trend-ATR Trading System - Universe Package

종목 선정 및 관리 모듈
"""

from .universe_manager import (
    UniverseManager,
    UniverseConfig,
    StockInfo,
    SelectionMethod,
    get_universe_manager,
    create_universe_from_config
)

__all__ = [
    "UniverseManager",
    "UniverseConfig",
    "StockInfo",
    "SelectionMethod",
    "get_universe_manager",
    "create_universe_from_config"
]
