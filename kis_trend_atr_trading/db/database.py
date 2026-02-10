"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 데이터베이스 연결 및 세션 관리
═══════════════════════════════════════════════════════════════════════════════

SQLAlchemy를 사용하여 데이터베이스 엔진과 세션 풀을 관리합니다.

★ 핵심 기능:
    1. 중앙 집중식 DB 연결 관리
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

    # DB 설정이 비활성화면 초기화하지 않음
    if not config.db.enabled:
        print("[DATABASE] DB가 비활성화되어 있습니다.")
        return

    # DB 타입에 따른 드라이버 및 URL 구성
    if config.db.type == "mysql":
        # mysql-connector-python 사용
        driver = "mysql+mysqlconnector"
        db_url = (
            f"{driver}://{config.db.user}:{config.db.password}@"
            f"{config.db.host}:{config.db.port}/{config.db.name}"
        )
    elif config.db.type == "postgres":
        # psycopg2 드라이버 필요 (pip install psycopg2-binary)
        driver = "postgresql+psycopg2"
        db_url = (
            f"{driver}://{config.db.user}:{config.db.password}@"
            f"{config.db.host}:{config.db.port}/{config.db.name}"
        )
    elif config.db.type == "sqlite":
        # 파일 기반 SQLite
        db_path = config.db.path or "trading.db"
        db_url = f"sqlite:///{db_path}"
    else:
        raise ValueError(f"지원하지 않는 DB 타입입니다: {config.db.type}")

    try:
        # 데이터베이스 엔진 생성 (커넥션 풀 포함)
        # e2-micro 환경을 고려하여 pool_size를 작게 설정
        _engine = create_engine(
            db_url,
            pool_size=5,          # 풀에 유지할 최소 커넥션 수
            max_overflow=2,       # 풀 크기를 초과하여 생성할 수 있는 임시 커넥션 수
            pool_recycle=3600,    # 3600초(1시간)마다 커넥션 재활용 (MySQL 타임아웃 방지)
            echo=False            # SQL 쿼리 로깅 비활성화 (필요시 True로 변경)
        )

        # 세션 메이커 설정
        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=_engine
        )

        print(f"[DATABASE] DB 엔진 초기화 완료 (Type: {config.db.type}, Pool: 5+2)")

    except Exception as e:
        print(f"[DATABASE] DB 엔진 초기화 실패: {e}")
        _engine = None
        _SessionLocal = None


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """
    DB 세션을 제공하는 컨텍스트 관리자.

    Yields:
        Session: SQLAlchemy 세션 객체
    
    Raises:
        RuntimeError: DB가 초기화되지 않았을 경우
    """
    if _SessionLocal is None:
        # DB가 비활성화되었거나 초기화 실패 시
        # 실제 세션 대신 None을 제공하여 DB 작업이 실패하도록 함
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

# 모듈 임포트 시 자동 초기화
init_db()
