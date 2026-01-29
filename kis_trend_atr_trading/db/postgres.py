"""
KIS Trend-ATR Trading System - PostgreSQL 연결 관리자

이 모듈은 PostgreSQL 데이터베이스와의 연결을 관리합니다.

★ 핵심 기능:
    1. 환경변수 기반 DB 접속 정보 관리
    2. 커넥션 풀링 (여러 연결 효율적 관리)
    3. 트랜잭션 관리 (자동 커밋/롤백)
    4. 쿼리 실행 헬퍼 함수

★ 설계 원칙:
    - ORM 사용 금지 (순수 SQL 기반)
    - 트랜잭션 실패 시 자동 롤백
    - 모든 쿼리 로깅 (디버그용)

★ 환경변수:
    DB_HOST     : PostgreSQL 호스트 (기본: localhost)
    DB_PORT     : PostgreSQL 포트 (기본: 5432)
    DB_NAME     : 데이터베이스 이름 (기본: kis_trading)
    DB_USER     : 사용자명 (기본: postgres)
    DB_PASSWORD : 비밀번호
    DB_ENABLED  : DB 사용 여부 (기본: true)

사용 예시:
    from db.postgres import get_db_manager
    
    db = get_db_manager()
    
    # 단일 쿼리
    result = db.execute_query("SELECT * FROM positions WHERE status = %s", ("OPEN",))
    
    # 트랜잭션
    with db.transaction() as cursor:
        cursor.execute("INSERT INTO trades ...")
        cursor.execute("UPDATE positions ...")
"""

import os
import time
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple, Generator
from dataclasses import dataclass
from contextlib import contextmanager
import threading

try:
    import psycopg2
    from psycopg2 import pool, sql
    from psycopg2.extras import RealDictCursor, execute_values
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

from utils.logger import get_logger

logger = get_logger("postgres")


# ═══════════════════════════════════════════════════════════════════════════════
# 예외 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class DatabaseError(Exception):
    """데이터베이스 일반 오류"""
    pass


class ConnectionError(DatabaseError):
    """연결 오류"""
    pass


class QueryError(DatabaseError):
    """쿼리 실행 오류"""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# 설정 클래스
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DatabaseConfig:
    """
    데이터베이스 설정 클래스
    
    ★ 환경변수에서 자동으로 로드됩니다.
    ★ 직접 값을 전달하면 환경변수보다 우선합니다.
    """
    host: str = None
    port: int = None
    database: str = None
    user: str = None
    password: str = None
    enabled: bool = True
    
    # 커넥션 풀 설정
    min_connections: int = 1
    max_connections: int = 10
    
    # 타임아웃 설정 (초)
    connect_timeout: int = 10
    query_timeout: int = 30
    
    def __post_init__(self):
        """환경변수에서 설정 로드"""
        self.host = self.host or os.getenv("DB_HOST", "localhost")
        self.port = self.port or int(os.getenv("DB_PORT", "5432"))
        self.database = self.database or os.getenv("DB_NAME", "kis_trading")
        self.user = self.user or os.getenv("DB_USER", "postgres")
        self.password = self.password or os.getenv("DB_PASSWORD", "")
        
        env_enabled = os.getenv("DB_ENABLED", "true").lower()
        self.enabled = env_enabled in ("true", "1", "yes")
    
    def to_dsn(self) -> str:
        """DSN 문자열 생성"""
        return (
            f"host={self.host} "
            f"port={self.port} "
            f"dbname={self.database} "
            f"user={self.user} "
            f"password={self.password} "
            f"connect_timeout={self.connect_timeout}"
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환 (psycopg2.connect용)"""
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "password": self.password,
            "connect_timeout": self.connect_timeout
        }
    
    def __repr__(self) -> str:
        """비밀번호 마스킹된 문자열 표현"""
        masked_pw = "****" if self.password else "(없음)"
        return (
            f"DatabaseConfig(host={self.host}, port={self.port}, "
            f"database={self.database}, user={self.user}, password={masked_pw})"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# PostgreSQL 관리자 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class PostgresManager:
    """
    PostgreSQL 데이터베이스 관리자
    
    ★ 중학생도 이해할 수 있는 설명:
        - 이 클래스는 "데이터베이스와 대화하는 통역사" 역할을 합니다.
        - 우리가 "이 데이터 저장해줘" 라고 말하면
        - PostgreSQL이 이해하는 말로 번역해서 전달합니다.
        - 문제가 생기면 자동으로 되돌리기(롤백)를 해줍니다.
    
    ★ 핵심 기능:
        1. connect(): 데이터베이스에 연결
        2. execute_query(): SQL 실행 (SELECT)
        3. execute_command(): SQL 실행 (INSERT/UPDATE/DELETE)
        4. transaction(): 여러 작업을 하나로 묶기 (트랜잭션)
    
    사용 예시:
        db = PostgresManager()
        
        # 연결
        db.connect()
        
        # 데이터 조회
        positions = db.execute_query(
            "SELECT * FROM positions WHERE status = %s", 
            ("OPEN",)
        )
        
        # 데이터 저장 (트랜잭션)
        with db.transaction() as cursor:
            cursor.execute("INSERT INTO trades VALUES (...)")
            cursor.execute("UPDATE positions SET status = 'CLOSED' ...")
        # 문제 없으면 자동 저장(커밋)
        # 문제 생기면 자동 되돌리기(롤백)
        
        # 연결 종료
        db.close()
    """
    
    def __init__(self, config: DatabaseConfig = None):
        """
        PostgreSQL 관리자 초기화
        
        Args:
            config: 데이터베이스 설정 (미입력 시 환경변수에서 로드)
        """
        if not PSYCOPG2_AVAILABLE:
            logger.warning(
                "[DB] psycopg2 라이브러리가 설치되지 않았습니다. "
                "pip install psycopg2-binary 를 실행하세요."
            )
        
        self.config = config or DatabaseConfig()
        self._pool: Optional[pool.ThreadedConnectionPool] = None
        self._lock = threading.Lock()
        self._connected = False
        
        logger.info(f"[DB] PostgreSQL 관리자 초기화: {self.config}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 연결 관리
    # ═══════════════════════════════════════════════════════════════════════════
    
    def connect(self) -> bool:
        """
        데이터베이스에 연결합니다.
        
        ★ 커넥션 풀을 생성하여 효율적으로 연결을 관리합니다.
        
        Returns:
            bool: 연결 성공 여부
        """
        if not PSYCOPG2_AVAILABLE:
            logger.error("[DB] psycopg2가 설치되지 않아 연결할 수 없습니다.")
            return False
        
        if not self.config.enabled:
            logger.warning("[DB] 데이터베이스가 비활성화되어 있습니다.")
            return False
        
        if self._connected:
            logger.debug("[DB] 이미 연결되어 있습니다.")
            return True
        
        with self._lock:
            try:
                # 커넥션 풀 생성
                self._pool = pool.ThreadedConnectionPool(
                    minconn=self.config.min_connections,
                    maxconn=self.config.max_connections,
                    **self.config.to_dict()
                )
                
                # 연결 테스트
                test_conn = self._pool.getconn()
                test_conn.autocommit = True
                
                with test_conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                
                self._pool.putconn(test_conn)
                
                self._connected = True
                logger.info(f"[DB] PostgreSQL 연결 성공: {self.config.host}:{self.config.port}")
                
                return True
                
            except psycopg2.Error as e:
                logger.error(f"[DB] PostgreSQL 연결 실패: {e}")
                self._pool = None
                self._connected = False
                raise ConnectionError(f"데이터베이스 연결 실패: {e}")
    
    def close(self) -> None:
        """
        모든 연결을 종료합니다.
        """
        with self._lock:
            if self._pool:
                self._pool.closeall()
                self._pool = None
            self._connected = False
            logger.info("[DB] PostgreSQL 연결 종료")
    
    def is_connected(self) -> bool:
        """연결 상태 확인"""
        return self._connected and self._pool is not None
    
    def _get_connection(self):
        """
        커넥션 풀에서 연결을 가져옵니다.
        
        ★ 내부 사용 전용
        """
        if not self.is_connected():
            self.connect()
        
        if not self._pool:
            raise ConnectionError("데이터베이스에 연결되어 있지 않습니다.")
        
        return self._pool.getconn()
    
    def _release_connection(self, conn) -> None:
        """
        연결을 풀에 반환합니다.
        
        ★ 내부 사용 전용
        """
        if self._pool:
            self._pool.putconn(conn)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 쿼리 실행
    # ═══════════════════════════════════════════════════════════════════════════
    
    def execute_query(
        self,
        query: str,
        params: Tuple = None,
        fetch_one: bool = False
    ) -> List[Dict] | Dict | None:
        """
        SELECT 쿼리를 실행합니다.
        
        ★ 데이터를 "조회"할 때 사용합니다.
        ★ 결과는 딕셔너리 리스트로 반환됩니다.
        
        Args:
            query: SQL 쿼리문
            params: 쿼리 파라미터 (튜플)
            fetch_one: True면 첫 번째 결과만 반환
        
        Returns:
            List[Dict] | Dict | None: 조회 결과
        
        Example:
            # 열린 포지션 조회
            positions = db.execute_query(
                "SELECT * FROM positions WHERE status = %s",
                ("OPEN",)
            )
            
            # 특정 포지션 조회
            position = db.execute_query(
                "SELECT * FROM positions WHERE symbol = %s",
                ("005930",),
                fetch_one=True
            )
        """
        if not self.config.enabled:
            logger.debug("[DB] 데이터베이스 비활성화 - 빈 결과 반환")
            return None if fetch_one else []
        
        conn = self._get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, params)
                
                if fetch_one:
                    result = cursor.fetchone()
                    return dict(result) if result else None
                else:
                    results = cursor.fetchall()
                    return [dict(row) for row in results]
                    
        except psycopg2.Error as e:
            logger.error(f"[DB] 쿼리 실행 실패: {e}\nQuery: {query}")
            raise QueryError(f"쿼리 실행 실패: {e}")
        finally:
            self._release_connection(conn)
    
    def execute_command(
        self,
        command: str,
        params: Tuple = None,
        returning: bool = False
    ) -> int | Dict | None:
        """
        INSERT/UPDATE/DELETE 명령을 실행합니다.
        
        ★ 데이터를 "저장/수정/삭제"할 때 사용합니다.
        ★ 자동으로 커밋됩니다.
        
        Args:
            command: SQL 명령문
            params: 파라미터 (튜플)
            returning: RETURNING 절 사용 시 True
        
        Returns:
            int | Dict | None: 
                - returning=False: 영향받은 행 수
                - returning=True: 반환된 행 (딕셔너리)
        
        Example:
            # 포지션 저장
            db.execute_command(
                "INSERT INTO positions (symbol, entry_price, quantity) VALUES (%s, %s, %s)",
                ("005930", 70000, 10)
            )
            
            # 포지션 저장 후 결과 반환
            result = db.execute_command(
                "INSERT INTO positions (...) VALUES (...) RETURNING *",
                (...),
                returning=True
            )
        """
        if not self.config.enabled:
            logger.debug("[DB] 데이터베이스 비활성화 - 명령 건너뜀")
            return None if returning else 0
        
        conn = self._get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(command, params)
                
                if returning:
                    result = cursor.fetchone()
                    conn.commit()
                    return dict(result) if result else None
                else:
                    affected = cursor.rowcount
                    conn.commit()
                    return affected
                    
        except psycopg2.Error as e:
            conn.rollback()
            logger.error(f"[DB] 명령 실행 실패 (롤백됨): {e}\nCommand: {command}")
            raise QueryError(f"명령 실행 실패: {e}")
        finally:
            self._release_connection(conn)
    
    def execute_many(
        self,
        command: str,
        params_list: List[Tuple]
    ) -> int:
        """
        여러 개의 INSERT/UPDATE/DELETE 명령을 일괄 실행합니다.
        
        ★ 많은 데이터를 한번에 저장할 때 사용합니다.
        ★ 성능이 훨씬 좋습니다.
        
        Args:
            command: SQL 명령문
            params_list: 파라미터 리스트
        
        Returns:
            int: 영향받은 총 행 수
        
        Example:
            # 여러 거래 일괄 저장
            db.execute_many(
                "INSERT INTO trades (symbol, side, price, quantity) VALUES (%s, %s, %s, %s)",
                [
                    ("005930", "BUY", 70000, 10),
                    ("005930", "SELL", 72000, 10),
                ]
            )
        """
        if not self.config.enabled:
            return 0
        
        conn = self._get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.executemany(command, params_list)
                affected = cursor.rowcount
                conn.commit()
                return affected
                
        except psycopg2.Error as e:
            conn.rollback()
            logger.error(f"[DB] 일괄 명령 실행 실패 (롤백됨): {e}")
            raise QueryError(f"일괄 명령 실행 실패: {e}")
        finally:
            self._release_connection(conn)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 트랜잭션 관리
    # ═══════════════════════════════════════════════════════════════════════════
    
    @contextmanager
    def transaction(self) -> Generator:
        """
        트랜잭션 컨텍스트 매니저
        
        ★ 중학생도 이해할 수 있는 설명:
            - 여러 작업을 "한 묶음"으로 실행합니다.
            - 모든 작업이 성공하면 저장(커밋)합니다.
            - 하나라도 실패하면 모두 되돌립니다(롤백).
        
        ★ 왜 필요한가?
            - 예: 주식 매도 시
                1. trades 테이블에 매도 기록 추가
                2. positions 테이블에서 포지션 종료
            - 1번은 성공하고 2번이 실패하면?
            - 데이터가 꼬입니다!
            - 트랜잭션을 쓰면 둘 다 성공하거나 둘 다 실패합니다.
        
        Example:
            with db.transaction() as cursor:
                # 매도 기록
                cursor.execute(
                    "INSERT INTO trades (symbol, side, price) VALUES (%s, %s, %s)",
                    ("005930", "SELL", 72000)
                )
                # 포지션 종료
                cursor.execute(
                    "UPDATE positions SET status = 'CLOSED' WHERE symbol = %s",
                    ("005930",)
                )
            # with 블록을 나가면 자동 커밋
            # 에러 발생 시 자동 롤백
        """
        if not self.config.enabled:
            # DB 비활성화 시 아무것도 하지 않는 더미 커서 반환
            class DummyCursor:
                def execute(self, *args, **kwargs): pass
                def executemany(self, *args, **kwargs): pass
                def fetchone(self): return None
                def fetchall(self): return []
            yield DummyCursor()
            return
        
        conn = self._get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                yield cursor
                conn.commit()
                logger.debug("[DB] 트랜잭션 커밋 완료")
                
        except Exception as e:
            conn.rollback()
            logger.error(f"[DB] 트랜잭션 롤백: {e}")
            raise
        finally:
            self._release_connection(conn)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 스키마 관리
    # ═══════════════════════════════════════════════════════════════════════════
    
    def initialize_schema(self) -> bool:
        """
        데이터베이스 스키마(테이블)를 초기화합니다.
        
        ★ 처음 실행할 때 테이블들을 생성합니다.
        ★ 이미 테이블이 있으면 건너뜁니다.
        
        Returns:
            bool: 초기화 성공 여부
        """
        if not self.config.enabled:
            logger.warning("[DB] 데이터베이스 비활성화 - 스키마 초기화 건너뜀")
            return False
        
        schema_sql = self._get_schema_sql()
        
        conn = self._get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(schema_sql)
                conn.commit()
                
            logger.info("[DB] 스키마 초기화 완료")
            return True
            
        except psycopg2.Error as e:
            conn.rollback()
            logger.error(f"[DB] 스키마 초기화 실패: {e}")
            raise QueryError(f"스키마 초기화 실패: {e}")
        finally:
            self._release_connection(conn)
    
    def _get_schema_sql(self) -> str:
        """
        스키마 생성 SQL을 반환합니다.
        
        ★ 주요 테이블:
            1. positions: 현재 보유 포지션
            2. trades: 매매 기록
            3. account_snapshots: 계좌 스냅샷
        """
        return """
        -- ═══════════════════════════════════════════════════════════════════════════════
        -- KIS Trend-ATR Trading System - 데이터베이스 스키마
        -- ═══════════════════════════════════════════════════════════════════════════════
        
        -- ───────────────────────────────────────────────────────────────────────────────
        -- 1. positions 테이블 (현재 보유 포지션)
        -- ───────────────────────────────────────────────────────────────────────────────
        -- ★ 역할: 현재 내가 어떤 주식을 얼마에 몇 주 가지고 있는지 저장
        -- ★ 서버가 재시작되어도 이 테이블을 보면 포지션을 복구할 수 있음
        
        CREATE TABLE IF NOT EXISTS positions (
            -- 기본 키: 종목 코드 (한 종목에 하나의 포지션만)
            symbol VARCHAR(20) PRIMARY KEY,
            
            -- 진입 정보
            entry_price DECIMAL(15, 2) NOT NULL,    -- 매수한 가격
            quantity INTEGER NOT NULL,               -- 보유 수량
            entry_time TIMESTAMP NOT NULL,           -- 매수한 시간
            
            -- ATR 관련 (★ 중요: 진입 시 고정, 절대 재계산 금지)
            atr_at_entry DECIMAL(15, 2) NOT NULL,   -- 진입 시 ATR 값
            
            -- 손절/익절 가격
            stop_price DECIMAL(15, 2) NOT NULL,     -- 손절가 (이 가격 이하면 팔아야 함)
            take_profit_price DECIMAL(15, 2),       -- 익절가 (이 가격 이상이면 팔아도 됨)
            trailing_stop DECIMAL(15, 2),           -- 트레일링 스탑 가격
            highest_price DECIMAL(15, 2),           -- 보유 중 최고가
            
            -- 상태
            status VARCHAR(20) NOT NULL DEFAULT 'OPEN',  -- OPEN(보유중) / CLOSED(청산완료)
            
            -- 메타 정보
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        
        -- status 컬럼에 인덱스 (열린 포지션 빠르게 조회)
        CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
        
        -- ───────────────────────────────────────────────────────────────────────────────
        -- 2. trades 테이블 (매매 기록)
        -- ───────────────────────────────────────────────────────────────────────────────
        -- ★ 역할: 모든 매수/매도 기록을 저장
        -- ★ 나중에 성과 분석, 승률 계산 등에 활용
        
        CREATE TABLE IF NOT EXISTS trades (
            -- 기본 키: 자동 증가 ID
            id SERIAL PRIMARY KEY,
            
            -- 거래 정보
            symbol VARCHAR(20) NOT NULL,            -- 종목 코드
            side VARCHAR(10) NOT NULL,              -- BUY(매수) / SELL(매도)
            price DECIMAL(15, 2) NOT NULL,          -- 체결 가격
            quantity INTEGER NOT NULL,              -- 거래 수량
            executed_at TIMESTAMP NOT NULL,         -- 체결 시간
            
            -- 청산 사유 (매도 시에만 기록)
            -- ATR_STOP: ATR 손절
            -- TAKE_PROFIT: 익절
            -- TRAILING_STOP: 트레일링 스탑
            -- TREND_BROKEN: 추세 붕괴
            -- GAP_PROTECTION: 갭 보호
            -- MANUAL: 수동 청산
            -- SIGNAL_ONLY: 신호만 (실매매 없음)
            reason VARCHAR(50),
            
            -- 손익 정보 (매도 시에만 기록)
            pnl DECIMAL(15, 2),                     -- 손익 금액
            pnl_percent DECIMAL(8, 4),              -- 손익률 (%)
            
            -- 진입가 정보 (매도 시 손익 계산용)
            entry_price DECIMAL(15, 2),
            
            -- 메타 정보
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        
        -- 종목별, 날짜별 조회를 위한 인덱스
        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
        CREATE INDEX IF NOT EXISTS idx_trades_executed_at ON trades(executed_at);
        CREATE INDEX IF NOT EXISTS idx_trades_side ON trades(side);
        
        -- ───────────────────────────────────────────────────────────────────────────────
        -- 3. account_snapshots 테이블 (계좌 스냅샷)
        -- ───────────────────────────────────────────────────────────────────────────────
        -- ★ 역할: 특정 시점의 계좌 상태를 저장
        -- ★ 자산 변화 추적, 일별 손익 계산에 활용
        
        CREATE TABLE IF NOT EXISTS account_snapshots (
            -- 기본 키: 스냅샷 시간
            snapshot_time TIMESTAMP PRIMARY KEY,
            
            -- 자산 정보
            total_equity DECIMAL(15, 2) NOT NULL,   -- 총 평가금액 (현금 + 주식)
            cash DECIMAL(15, 2) NOT NULL,           -- 현금
            unrealized_pnl DECIMAL(15, 2),          -- 미실현 손익 (아직 안 판 주식의 손익)
            realized_pnl DECIMAL(15, 2),            -- 실현 손익 (판 주식의 손익 합계)
            
            -- 메타 정보
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        
        -- 날짜별 조회를 위한 인덱스
        CREATE INDEX IF NOT EXISTS idx_snapshots_time ON account_snapshots(snapshot_time);
        
        -- ───────────────────────────────────────────────────────────────────────────────
        -- 4. 업데이트 트리거 함수
        -- ───────────────────────────────────────────────────────────────────────────────
        -- ★ 역할: positions 테이블이 수정될 때 updated_at 자동 갱신
        
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$ language 'plpgsql';
        
        -- positions 테이블에 트리거 적용
        DROP TRIGGER IF EXISTS update_positions_updated_at ON positions;
        CREATE TRIGGER update_positions_updated_at
            BEFORE UPDATE ON positions
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
        """
    
    def table_exists(self, table_name: str) -> bool:
        """테이블 존재 여부 확인"""
        result = self.execute_query(
            """
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = %s
            )
            """,
            (table_name,),
            fetch_one=True
        )
        return result.get("exists", False) if result else False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 상태 확인
    # ═══════════════════════════════════════════════════════════════════════════
    
    def health_check(self) -> Dict[str, Any]:
        """
        데이터베이스 상태 확인
        
        Returns:
            Dict: 상태 정보
        """
        status = {
            "connected": self.is_connected(),
            "enabled": self.config.enabled,
            "host": self.config.host,
            "database": self.config.database,
            "tables": []
        }
        
        if self.is_connected():
            try:
                # 테이블 목록 조회
                tables = self.execute_query(
                    """
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = 'public'
                    """
                )
                status["tables"] = [t["table_name"] for t in tables]
                
                # 각 테이블 행 수
                for table in status["tables"]:
                    count = self.execute_query(
                        f"SELECT COUNT(*) as cnt FROM {table}",
                        fetch_one=True
                    )
                    status[f"{table}_count"] = count.get("cnt", 0) if count else 0
                    
            except Exception as e:
                status["error"] = str(e)
        
        return status


# ═══════════════════════════════════════════════════════════════════════════════
# 싱글톤 인스턴스
# ═══════════════════════════════════════════════════════════════════════════════

_db_manager: Optional[PostgresManager] = None


def get_db_manager(config: DatabaseConfig = None) -> PostgresManager:
    """
    싱글톤 PostgresManager 인스턴스를 반환합니다.
    
    ★ 앱 전체에서 하나의 DB 연결만 사용합니다.
    
    Args:
        config: 데이터베이스 설정 (최초 호출 시에만 적용)
    
    Returns:
        PostgresManager: DB 관리자 인스턴스
    """
    global _db_manager
    
    if _db_manager is None:
        _db_manager = PostgresManager(config)
    
    return _db_manager


def close_db_manager() -> None:
    """싱글톤 DB 관리자 연결 종료"""
    global _db_manager
    
    if _db_manager is not None:
        _db_manager.close()
        _db_manager = None
