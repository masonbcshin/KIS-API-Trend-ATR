"""
KIS Trend-ATR Trading System - 성과 측정 모듈

═══════════════════════════════════════════════════════════════════════════════
⚠️ 이 모듈은 DRY_RUN, PAPER, REAL 모든 모드에서 동작합니다.
═══════════════════════════════════════════════════════════════════════════════

★ 주요 기능:
  - 종목별 수익률 계산
  - 포지션별 PnL 계산
  - 누적 수익 곡선 (Equity Curve)
  - 최대 낙폭 (MDD) 계산
  - 승률 / 손익비 계산

★ 지원 데이터 소스:
  - JSON 파일 (DRY_RUN 기본)
  - MySQL 데이터베이스
  - 가상 체결 데이터

사용 예시:
    from performance import PerformanceTracker
    
    tracker = PerformanceTracker()
    
    # 거래 기록
    tracker.record_trade(
        symbol="005930",
        side="BUY",
        price=70000,
        quantity=10
    )
    
    # 성과 요약
    summary = tracker.get_summary()
    print(f"승률: {summary['win_rate']}%")
    print(f"MDD: {summary['mdd']}%")

작성자: KIS Trend-ATR Trading System
버전: 2.0.0
"""

from performance.trade_record import TradeRecord
from performance.position_snapshot import PositionSnapshot
from performance.performance_tracker import PerformanceTracker, get_performance_tracker

__all__ = [
    "TradeRecord",
    "PositionSnapshot", 
    "PerformanceTracker",
    "get_performance_tracker"
]
