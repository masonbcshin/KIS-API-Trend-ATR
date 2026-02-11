"""
KIS Trend-ATR Trading System - MySQL 연결 관리자

이 모듈은 MySQL(InnoDB) 데이터베이스와의 연결을 관리합니다.
Oracle Cloud Infrastructure Free Tier MySQL 호환.

★ 핵심 기능:
    1. 환경변수 기반 DB 접속 정보 관리
    2. 커넥션 풀링 (여러 연결 효율적 관리)
    3. 트랜잭션 관리 (자동 커밋/롤백)
    4. 쿼리 실행 헬퍼 함수

★ 설계 원칙:
    - ORM 사용 금지 (순수 SQL 기반)
    - 트랜잭션 실패 시 자동 롤백
    - 모든 쿼리 로깅 (디버그용)
    - PostgreSQL 전용 문법 제거 (표준 SQL만 사용)

★ 환경변수:
    DB_TYPE     : 데이터베이스 유형 (기본: mysql)
    DB_HOST     : MySQL 호스트 (기본: localhost)
    DB_PORT     : MySQL 포트 (기본: 3306)
    DB_NAME     : 데이터베이스 이름 (기본: kis_trading)
    DB_USER     : 사용자명 (기본: root)
    DB_PASSWORD : 비밀번호
    DB_ENABLED  : DB 사용 여부 (기본: true)

사용 예시:
    from db.mysql import get_db_manager
    
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
    import mysql.connector
    from mysql.connector import pooling, Error as MySQLError
    MYSQL_AVAILABLE = True
except ImportError:
    MYSQL_AVAILABLE = False
    MySQLError = Exception  # Fallback

from utils.logger import get_logger

logger = get_logger("mysql")


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
    db_type: str = None  # mysql
    host: str = None
    port: int = None
    database: str = None
    user: str = None
    password: str = None
    enabled: bool = True
    
    # 커넥션 풀 설정
    pool_name: str = "kis_trading_pool"
    pool_size: int = 5
    pool_reset_session: bool = True
    
    # 타임아웃 설정 (초)
    connect_timeout: int = 10
    
    # 추가 연결 옵션
    charset: str = "utf8mb4"
    collation: str = "utf8mb4_unicode_ci"
    autocommit: bool = False  # 명시적 커밋 사용
    
    def __post_init__(self):
        """환경변수에서 설정 로드"""
        self.db_type = self.db_type or os.getenv("DB_TYPE", "mysql")
        self.host = self.host or os.getenv("DB_HOST", "localhost")
        self.port = self.port or int(os.getenv("DB_PORT", "3306"))
        self.database = self.database or os.getenv("DB_NAME", "kis_trading")
        self.user = self.user or os.getenv("DB_USER", "root")
        self.password = self.password or os.getenv("DB_PASSWORD", "")
        
        env_enabled = os.getenv("DB_ENABLED", "true").lower()
        self.enabled = env_enabled in ("true", "1", "yes")
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환 (mysql.connector.connect용)"""
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "password": self.password,
            "charset": self.charset,
            "collation": self.collation,
            "autocommit": self.autocommit,
            "connection_timeout": self.connect_timeout
        }
    
    def to_pool_config(self) -> Dict[str, Any]:
        """커넥션 풀 설정 딕셔너리"""
        config = self.to_dict()
        config.update({
            "pool_name": self.pool_name,
            "pool_size": self.pool_size,
            "pool_reset_session": self.pool_reset_session
        })
        return config
    
    def __repr__(self) -> str:
        """비밀번호 마스킹된 문자열 표현"""
        masked_pw = "****" if self.password else "(없음)"
        return (
            f"DatabaseConfig(type={self.db_type}, host={self.host}, port={self.port}, "
            f"database={self.database}, user={self.user}, password={masked_pw})"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# MySQL 관리자 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class MySQLManager:
    """
    MySQL 데이터베이스 관리자
    
    ★ 중학생도 이해할 수 있는 설명:
        - 이 클래스는 "데이터베이스와 대화하는 통역사" 역할을 합니다.
        - 우리가 "이 데이터 저장해줘" 라고 말하면
        - MySQL이 이해하는 말로 번역해서 전달합니다.
        - 문제가 생기면 자동으로 되돌리기(롤백)를 해줍니다.
    
    ★ 핵심 기능:
        1. connect(): 데이터베이스에 연결
        2. execute_query(): SQL 실행 (SELECT)
        3. execute_command(): SQL 실행 (INSERT/UPDATE/DELETE)
        4. transaction(): 여러 작업을 하나로 묶기 (트랜잭션)
    
    ★ PostgreSQL과의 차이점:
        - RETURNING 절 미지원 → LAST_INSERT_ID() 사용
        - ON CONFLICT → INSERT ... ON DUPLICATE KEY UPDATE
        - SERIAL → AUTO_INCREMENT
    
    사용 예시:
        db = MySQLManager()
        
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
        MySQL 관리자 초기화
        
        Args:
            config: 데이터베이스 설정 (미입력 시 환경변수에서 로드)
        """
        if not MYSQL_AVAILABLE:
            logger.warning(
                "[DB] mysql-connector-python 라이브러리가 설치되지 않았습니다. "
                "pip install mysql-connector-python 를 실행하세요."
            )
        
        self.config = config or DatabaseConfig()
        self._pool: Optional[pooling.MySQLConnectionPool] = None
        self._lock = threading.Lock()
        self._connected = False
        
        logger.info(f"[DB] MySQL 관리자 초기화: {self.config}")
    
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
        if not MYSQL_AVAILABLE:
            logger.error("[DB] mysql-connector-python이 설치되지 않아 연결할 수 없습니다.")
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
                self._pool = pooling.MySQLConnectionPool(
                    **self.config.to_pool_config()
                )
                
                # 연결 테스트
                test_conn = self._pool.get_connection()
                
                with test_conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                
                test_conn.close()
                
                self._connected = True
                logger.info(f"[DB] MySQL 연결 성공: {self.config.host}:{self.config.port}")
                
                return True
                
            except MySQLError as e:
                logger.error(f"[DB] MySQL 연결 실패: {e}")
                self._pool = None
                self._connected = False
                raise ConnectionError(f"데이터베이스 연결 실패: {e}")
    
    def close(self) -> None:
        """
        모든 연결을 종료합니다.
        
        ★ MySQL 커넥션 풀은 명시적 closeall이 없으므로
           풀 객체를 None으로 설정하면 GC가 정리합니다.
        """
        with self._lock:
            if self._pool:
                # MySQL 커넥션 풀은 자동으로 정리됨
                self._pool = None
            self._connected = False
            logger.info("[DB] MySQL 연결 종료")
    
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
        
        conn = self._pool.get_connection()

        # 세션 타임존을 KST로 고정하여 CURDATE/NOW 등 DB 날짜 함수의 기준을 일치시킵니다.
        try:
            with conn.cursor() as cursor:
                cursor.execute("SET time_zone = '+09:00'")
        except MySQLError as e:
            conn.close()
            raise ConnectionError(f"세션 타임존 설정 실패: {e}")

        return conn
    
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
            # dictionary=True로 결과를 딕셔너리로 받음
            with conn.cursor(dictionary=True) as cursor:
                cursor.execute(query, params)
                
                if fetch_one:
                    result = cursor.fetchone()
                    return dict(result) if result else None
                else:
                    results = cursor.fetchall()
                    return [dict(row) for row in results]
                    
        except MySQLError as e:
            logger.error(f"[DB] 쿼리 실행 실패: {e}\nQuery: {query}")
            raise QueryError(f"쿼리 실행 실패: {e}")
        finally:
            conn.close()
    
    def execute_command(
        self,
        command: str,
        params: Tuple = None
    ) -> int:
        """
        INSERT/UPDATE/DELETE 명령을 실행합니다.
        
        ★ 데이터를 "저장/수정/삭제"할 때 사용합니다.
        ★ 자동으로 커밋됩니다.
        
        Args:
            command: SQL 명령문
            params: 파라미터 (튜플)
        
        Returns:
            int: 영향받은 행 수
        
        Example:
            # 포지션 저장
            db.execute_command(
                "INSERT INTO positions (symbol, entry_price, quantity) VALUES (%s, %s, %s)",
                ("005930", 70000, 10)
            )
        """
        if not self.config.enabled:
            logger.debug("[DB] 데이터베이스 비활성화 - 명령 건너뜀")
            return 0
        
        conn = self._get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(command, params)
                affected = cursor.rowcount
                conn.commit()
                return affected
                    
        except MySQLError as e:
            conn.rollback()
            logger.error(f"[DB] 명령 실행 실패 (롤백됨): {e}\nCommand: {command}")
            raise QueryError(f"명령 실행 실패: {e}")
        finally:
            conn.close()
    
    def execute_insert(
        self,
        command: str,
        params: Tuple = None
    ) -> int:
        """
        INSERT 명령을 실행하고 생성된 ID를 반환합니다.
        
        ★ PostgreSQL의 RETURNING 대체
        ★ AUTO_INCREMENT ID를 반환
        
        Args:
            command: INSERT SQL 명령문
            params: 파라미터 (튜플)
        
        Returns:
            int: 생성된 AUTO_INCREMENT ID (없으면 0)
        
        Example:
            # 거래 기록 저장 후 ID 반환
            trade_id = db.execute_insert(
                "INSERT INTO trades (symbol, side, price) VALUES (%s, %s, %s)",
                ("005930", "BUY", 70000)
            )
        """
        if not self.config.enabled:
            logger.debug("[DB] 데이터베이스 비활성화 - 명령 건너뜀")
            return 0
        
        conn = self._get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(command, params)
                last_id = cursor.lastrowid
                conn.commit()
                return last_id or 0
                    
        except MySQLError as e:
            conn.rollback()
            logger.error(f"[DB] INSERT 실행 실패 (롤백됨): {e}\nCommand: {command}")
            raise QueryError(f"INSERT 실행 실패: {e}")
        finally:
            conn.close()
    
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
                
        except MySQLError as e:
            conn.rollback()
            logger.error(f"[DB] 일괄 명령 실행 실패 (롤백됨): {e}")
            raise QueryError(f"일괄 명령 실행 실패: {e}")
        finally:
            conn.close()
    
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
        
        ★ MySQL InnoDB 트랜잭션:
            - autocommit=False로 설정되어 있음
            - 명시적 commit() 호출 필요
            - 오류 시 rollback() 자동 호출
        
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
                @property
                def lastrowid(self): return 0
                @property
                def rowcount(self): return 0
            yield DummyCursor()
            return
        
        conn = self._get_connection()
        try:
            # 트랜잭션 시작 (autocommit=False이므로 자동 시작)
            with conn.cursor(dictionary=True) as cursor:
                yield cursor
                conn.commit()
                logger.debug("[DB] 트랜잭션 커밋 완료")
                
        except Exception as e:
            conn.rollback()
            logger.error(f"[DB] 트랜잭션 롤백: {e}")
            raise
        finally:
            conn.close()
    
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
                # MySQL에서는 여러 문장을 한번에 실행할 수 없으므로 분리
                for statement in schema_sql.split(';'):
                    statement = statement.strip()
                    if statement:
                        cursor.execute(statement)
                conn.commit()
                
            logger.info("[DB] 스키마 초기화 완료")
            return True
            
        except MySQLError as e:
            conn.rollback()
            logger.error(f"[DB] 스키마 초기화 실패: {e}")
            raise QueryError(f"스키마 초기화 실패: {e}")
        finally:
            conn.close()
    
    def _get_schema_sql(self) -> str:
        """
        스키마 생성 SQL을 반환합니다.
        
        ★ 주요 테이블:
            1. positions: 현재 보유 포지션
            2. trades: 매매 기록
            3. account_snapshots: 계좌 스냅샷
        """
        return """
        CREATE TABLE IF NOT EXISTS positions (
            symbol VARCHAR(20) NOT NULL PRIMARY KEY,
            entry_price DECIMAL(15, 2) NOT NULL,
            quantity INT NOT NULL,
            entry_time DATETIME NOT NULL,
            atr_at_entry DECIMAL(15, 2) NOT NULL,
            stop_price DECIMAL(15, 2) NOT NULL,
            take_profit_price DECIMAL(15, 2) NULL,
            trailing_stop DECIMAL(15, 2) NULL,
            highest_price DECIMAL(15, 2) NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        
        CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
        
        CREATE TABLE IF NOT EXISTS trades (
            id INT AUTO_INCREMENT PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            side VARCHAR(10) NOT NULL,
            price DECIMAL(15, 2) NOT NULL,
            quantity INT NOT NULL,
            executed_at DATETIME NOT NULL,
            reason VARCHAR(50) NULL,
            pnl DECIMAL(15, 2) NULL,
            pnl_percent DECIMAL(8, 4) NULL,
            entry_price DECIMAL(15, 2) NULL,
            holding_days INT NULL,
            order_no VARCHAR(50) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        
        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
        CREATE INDEX IF NOT EXISTS idx_trades_executed_at ON trades(executed_at);
        CREATE INDEX IF NOT EXISTS idx_trades_side ON trades(side);
        
        CREATE TABLE IF NOT EXISTS account_snapshots (
            snapshot_time DATETIME NOT NULL PRIMARY KEY,
            total_equity DECIMAL(15, 2) NOT NULL,
            cash DECIMAL(15, 2) NOT NULL,
            unrealized_pnl DECIMAL(15, 2) DEFAULT 0,
            realized_pnl DECIMAL(15, 2) DEFAULT 0,
            position_count INT DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        
        CREATE INDEX IF NOT EXISTS idx_snapshots_time ON account_snapshots(snapshot_time)
        """
    
    def table_exists(self, table_name: str) -> bool:
        """테이블 존재 여부 확인"""
        result = self.execute_query(
            """
            SELECT COUNT(*) as cnt 
            FROM information_schema.tables 
            WHERE table_schema = %s AND table_name = %s
            """,
            (self.config.database, table_name),
            fetch_one=True
        )
        return result and result.get("cnt", 0) > 0
    
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
            "type": "mysql",
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
                    WHERE table_schema = %s
                    """,
                    (self.config.database,)
                )
                status["tables"] = [t["table_name"] for t in tables]
                
                # 각 테이블 행 수
                for table in status["tables"]:
                    count = self.execute_query(
                        f"SELECT COUNT(*) as cnt FROM `{table}`",
                        fetch_one=True
                    )
                    status[f"{table}_count"] = count.get("cnt", 0) if count else 0
                    
            except Exception as e:
                status["error"] = str(e)
        
        return status


# ═══════════════════════════════════════════════════════════════════════════════
# 싱글톤 인스턴스
# ═══════════════════════════════════════════════════════════════════════════════

_db_manager: Optional[MySQLManager] = None


def get_db_manager(config: DatabaseConfig = None) -> MySQLManager:
    """
    싱글톤 MySQLManager 인스턴스를 반환합니다.
    
    ★ 앱 전체에서 하나의 DB 연결만 사용합니다.
    
    Args:
        config: 데이터베이스 설정 (최초 호출 시에만 적용)
    
    Returns:
        MySQLManager: DB 관리자 인스턴스
    """
    global _db_manager
    
    if _db_manager is None:
        _db_manager = MySQLManager(config)
    
    return _db_manager


def close_db_manager() -> None:
    """싱글톤 DB 관리자 연결 종료"""
    global _db_manager
    
    if _db_manager is not None:
        _db_manager.close()
        _db_manager = None
