"""
KIS Trend-ATR Trading System - 트레이딩 모듈

이 모듈은 PostgreSQL 기반의 매수/매도 로직을 담당합니다.

★ 핵심 기능:
    1. 매수 체결 시 positions + trades 테이블 동시 저장
    2. 매도 체결 시 positions 업데이트 + trades 기록
    3. 동일 종목 중복 진입 방지 (DB 기준)
    4. 신호 전용 모드 (SIGNAL_ONLY) 지원
    5. 프로그램 재시작 시 DB에서 포지션 복원

★ 트레이딩 모드:
    - LIVE: 실계좌 주문 + DB 기록
    - PAPER: 모의투자 주문 + DB 기록
    - CBT: 가상 체결 + DB 기록 (주문 API 호출 안 함)
    - SIGNAL_ONLY: 신호 알림만 + DB 기록 (체결 없음)

사용 예시:
    from trading import DatabaseTrader, get_db_trader
    
    trader = get_db_trader()
    
    # 매수
    result = trader.buy(
        symbol="005930",
        price=70000,
        quantity=10,
        stop_loss=67000,
        take_profit=75000,
        atr=1500
    )
    
    # 매도
    result = trader.sell(
        symbol="005930",
        price=72000,
        reason="TAKE_PROFIT"
    )
"""

from trading.trader import (
    DatabaseTrader,
    TradingMode,
    TradeResult,
    get_db_trader
)

__all__ = [
    "DatabaseTrader",
    "TradingMode",
    "TradeResult",
    "get_db_trader"
]
