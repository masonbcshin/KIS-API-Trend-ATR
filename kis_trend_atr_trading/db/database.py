"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 데이터베이스 연결 및 세션 관리
═══════════════════════════════════════════════════════════════════════════════

SQLAlchemy를 사용하여 데이터베이스 엔진과 세션 풀을 관리합니다.

★ 핵심 기능:
    1. 중앙 집중식 DB 연결 관리 (타임존 `Asia/Seoul` 명시)
    2. 커넥션 풀링을 통한 리소스 효율화 (e2-micro 환경 최적화)
    3. 스레드 안전(Thread-safe)한 세션 제공
    4. 설정 파일 기반으로 DB 정보 자동 로드

★ 사용 방법:
    from db.database import get_db_session

    with get_db_session() as session:
        # session을 사용하여 DB 작업 수행
        session.query(...)

"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from typing import Optional, Generator

from config_loader import get_config, Config

# 전역 변수
_engine = None
_SessionLocal: Optional[sessionmaker] = None


def init_db():
    """
    데이터베이스 엔진과 세션을 초기화합니다.
    이 함수는 애플리케이션 시작 시 한 번만 호출되어야 합니다.
    """
    global _engine, _SessionLocal

    if _engine is not None:
        return

    config: Config = get_config()

    if not config.db.enabled:
        print("[DATABASE] DB가 비활성화되어 있습니다.")
        return

    db_url: str
    connect_args = {}

    if config.db.type == "mysql":
        driver = "mysql+mysqlconnector"
        db_url = (
            f"{driver}://{config.db.user}:{config.db.password}@"
            f"{config.db.host}:{config.db.port}/{config.db.name}"
        )
        # MySQL 연결 시 세션 타임존을 KST로 설정
        connect_args['time_zone'] = 'Asia/Seoul'
        
    elif config.db.type == "postgres":
        driver = "postgresql+psycopg2"
        db_url = (
            f"{driver}://{config.db.user}:{config.db.password}@"
            f"{config.db.host}:{config.db.port}/{config.db.name}"
        )
        # PostgreSQL 연결 시 세션 타임존을 KST로 설정
        connect_args['options'] = f"-c TimeZone=Asia/Seoul"
        
    elif config.db.type == "sqlite":
        db_path = config.db.path or "trading.db"
        db_url = f"sqlite:///{db_path}"
        # SQLite는 별도 타임존 설정을 지원하지 않음 (DateTime(timezone=True)에 의존)
        
    else:
        raise ValueError(f"지원하지 않는 DB 타입입니다: {config.db.type}")

    try:
        _engine = create_engine(
            db_url,
            connect_args=connect_args,  # 타임존 설정 추가
            pool_size=5,          
            max_overflow=2,       
            pool_recycle=3600,    
            echo=False            
        )

        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=_engine
        )

        print(f"[DATABASE] DB 엔진 초기화 완료 (Type: {config.db.type}, TimeZone: Asia/Seoul)")

    except Exception as e:
        print(f"[DATABASE] DB 엔진 초기화 실패: {e}")
        _engine = None
        _SessionLocal = None


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    if _SessionLocal is None:
        yield None
        return

    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

init_db()
