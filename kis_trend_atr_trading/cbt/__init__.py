"""
KIS Trend-ATR Trading System - CBT (Closed Beta Test) 모듈

CBT 모드는 실계좌 주문 없이 가상 체결로 전략 성과를 측정합니다.

주요 기능:
    1. 가상 체결 처리 (KIS 시세 API 기준 현재가 사용)
    2. Trade Log 저장 (JSON/SQLite)
    3. 가상 계좌 자본금 관리
    4. 성과 지표 자동 계산
    5. 텔레그램 CBT 리포트 전송

사용법:
    TRADING_MODE = "CBT" 설정 후 시스템 실행

⚠️ 주의: CBT 모드에서는 실제 주문이 발생하지 않습니다.
"""

from .virtual_account import VirtualAccount
from .trade_store import TradeStore, Trade
from .metrics import CBTMetrics, PerformanceReport
from .cbt_executor import CBTExecutor

__all__ = [
    "VirtualAccount",
    "TradeStore",
    "Trade",
    "CBTMetrics",
    "PerformanceReport",
    "CBTExecutor",
]
