"""
KIS Trend-ATR Trading System - PostgreSQL 데이터베이스 모듈

이 모듈은 트레이딩 시스템의 모든 데이터를 PostgreSQL에 영속화합니다.

★ 핵심 기능:
    1. 포지션 상태 관리 (서버 재시작 후에도 유지)
    2. 거래 기록 저장 (매수/매도 체결 내역)
    3. 계좌 스냅샷 (자산 변화 추적)
    4. 성과 측정 (수익률, 승률 등)

★ 설계 원칙:
    - ORM 사용 금지 (순수 SQL 기반)
    - 트랜잭션 실패 시 자동 롤백
    - 환경변수 기반 DB 접속 정보 관리
    - 중학생도 이해할 수 있는 명확한 구조

사용 예시:
    from db import get_db_manager
    
    db = get_db_manager()
    db.save_position(...)
    db.get_open_positions()
"""

from db.postgres import (
    PostgresManager,
    get_db_manager,
    DatabaseConfig,
    DatabaseError,
    ConnectionError,
    QueryError
)

from db.repository import (
    PositionRepository,
    TradeRepository,
    AccountSnapshotRepository,
    get_position_repository,
    get_trade_repository,
    get_account_snapshot_repository
)

__all__ = [
    # PostgreSQL Manager
    "PostgresManager",
    "get_db_manager",
    "DatabaseConfig",
    "DatabaseError",
    "ConnectionError",
    "QueryError",
    
    # Repositories
    "PositionRepository",
    "TradeRepository",
    "AccountSnapshotRepository",
    "get_position_repository",
    "get_trade_repository",
    "get_account_snapshot_repository"
]
