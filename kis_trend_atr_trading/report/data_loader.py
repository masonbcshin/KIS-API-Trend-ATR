"""
KIS Trend-ATR Trading System - 거래 데이터 로더

거래 결과 데이터를 CSV 파일 또는 데이터베이스에서 로드합니다.

데이터 스키마:
    - trade_date: 거래일 (YYYY-MM-DD)
    - symbol: 종목코드
    - side: 매수/매도 (BUY/SELL)
    - entry_price: 진입가
    - exit_price: 청산가
    - quantity: 수량
    - pnl: 실현손익 (원화)
    - holding_minutes: 보유 시간 (분)
"""

import os
import sqlite3
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

import pandas as pd

from utils.logger import get_logger

logger = get_logger("data_loader")


# ════════════════════════════════════════════════════════════════
# 데이터 스키마 정의
# ════════════════════════════════════════════════════════════════

REQUIRED_COLUMNS = [
    "trade_date",
    "symbol",
    "side",
    "entry_price",
    "exit_price",
    "quantity",
    "pnl",
    "holding_minutes",
]

COLUMN_DTYPES = {
    "trade_date": str,
    "symbol": str,
    "side": str,
    "entry_price": float,
    "exit_price": float,
    "quantity": int,
    "pnl": float,
    "holding_minutes": float,
}


# ════════════════════════════════════════════════════════════════
# 추상 데이터 로더 클래스
# ════════════════════════════════════════════════════════════════

class DataLoader(ABC):
    """
    거래 데이터 로더 추상 클래스
    
    CSV, DB 등 다양한 데이터 소스에서 거래 데이터를 로드합니다.
    """
    
    @abstractmethod
    def load_trades(
        self,
        target_date: date,
        include_mtd: bool = True
    ) -> pd.DataFrame:
        """
        거래 데이터를 로드합니다.
        
        Args:
            target_date: 대상 날짜
            include_mtd: MTD(월초~대상일) 데이터 포함 여부
        
        Returns:
            pd.DataFrame: 거래 데이터
        """
        pass
    
    @abstractmethod
    def load_daily_trades(self, target_date: date) -> pd.DataFrame:
        """
        특정 날짜의 거래 데이터만 로드합니다.
        
        Args:
            target_date: 대상 날짜
        
        Returns:
            pd.DataFrame: 해당 날짜 거래 데이터
        """
        pass
    
    def _validate_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        데이터프레임의 스키마를 검증하고 정제합니다.
        
        Args:
            df: 원본 데이터프레임
        
        Returns:
            pd.DataFrame: 검증 및 정제된 데이터프레임
        
        Raises:
            ValueError: 필수 컬럼이 누락된 경우
        """
        if df.empty:
            return df
        
        # 필수 컬럼 검증
        missing_cols = set(REQUIRED_COLUMNS) - set(df.columns)
        if missing_cols:
            raise ValueError(f"필수 컬럼 누락: {missing_cols}")
        
        # 데이터 타입 변환
        for col, dtype in COLUMN_DTYPES.items():
            if col in df.columns:
                try:
                    if dtype == str:
                        df[col] = df[col].astype(str)
                    elif dtype == float:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                    elif dtype == int:
                        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
                except Exception as e:
                    logger.warning(f"컬럼 '{col}' 타입 변환 실패: {e}")
        
        # trade_date를 datetime으로 변환
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        
        # side 값 정규화 (대문자로)
        df["side"] = df["side"].str.upper()
        
        # 결측값이 있는 행 로깅
        null_rows = df[df[REQUIRED_COLUMNS].isnull().any(axis=1)]
        if not null_rows.empty:
            logger.warning(f"결측값이 포함된 {len(null_rows)}개 행이 있습니다.")
        
        return df


# ════════════════════════════════════════════════════════════════
# CSV 데이터 로더
# ════════════════════════════════════════════════════════════════

class CSVDataLoader(DataLoader):
    """
    CSV 파일에서 거래 데이터를 로드하는 클래스
    
    Usage:
        loader = CSVDataLoader("/path/to/trades.csv")
        df = loader.load_daily_trades(date.today())
    """
    
    def __init__(
        self,
        csv_path: str,
        encoding: str = "utf-8",
        date_column: str = "trade_date"
    ):
        """
        CSV 데이터 로더 초기화
        
        Args:
            csv_path: CSV 파일 경로
            encoding: 파일 인코딩
            date_column: 날짜 컬럼명
        """
        self.csv_path = Path(csv_path)
        self.encoding = encoding
        self.date_column = date_column
        
        if not self.csv_path.exists():
            logger.warning(f"CSV 파일이 존재하지 않습니다: {self.csv_path}")
    
    def _load_csv(self) -> pd.DataFrame:
        """CSV 파일 전체를 로드합니다."""
        if not self.csv_path.exists():
            logger.warning(f"CSV 파일 없음: {self.csv_path}")
            return pd.DataFrame(columns=REQUIRED_COLUMNS)
        
        try:
            df = pd.read_csv(
                self.csv_path,
                encoding=self.encoding,
                parse_dates=[self.date_column]
            )
            return self._validate_dataframe(df)
        except Exception as e:
            logger.error(f"CSV 로드 실패: {e}")
            return pd.DataFrame(columns=REQUIRED_COLUMNS)
    
    def load_trades(
        self,
        target_date: date,
        include_mtd: bool = True
    ) -> pd.DataFrame:
        """
        거래 데이터를 로드합니다.
        
        Args:
            target_date: 대상 날짜
            include_mtd: MTD 데이터 포함 여부
        
        Returns:
            pd.DataFrame: 거래 데이터
        """
        df = self._load_csv()
        
        if df.empty:
            return df
        
        target_dt = pd.Timestamp(target_date)
        
        if include_mtd:
            # 월초부터 대상일까지
            month_start = target_dt.replace(day=1)
            mask = (df["trade_date"] >= month_start) & (df["trade_date"] <= target_dt)
        else:
            # 대상일만
            mask = df["trade_date"].dt.date == target_date
        
        return df[mask].copy()
    
    def load_daily_trades(self, target_date: date) -> pd.DataFrame:
        """특정 날짜의 거래 데이터만 로드합니다."""
        df = self._load_csv()
        
        if df.empty:
            return df
        
        mask = df["trade_date"].dt.date == target_date
        return df[mask].copy()


# ════════════════════════════════════════════════════════════════
# 데이터베이스 데이터 로더
# ════════════════════════════════════════════════════════════════

class DBDataLoader(DataLoader):
    """
    데이터베이스에서 거래 데이터를 로드하는 클래스
    
    SQLite를 기본으로 지원하며, 확장하여 다른 DB도 지원 가능합니다.
    
    Usage:
        loader = DBDataLoader("/path/to/trades.db", table_name="trades")
        df = loader.load_daily_trades(date.today())
    """
    
    def __init__(
        self,
        db_path: str,
        table_name: str = "trades",
        date_column: str = "trade_date"
    ):
        """
        DB 데이터 로더 초기화
        
        Args:
            db_path: 데이터베이스 파일 경로
            table_name: 테이블명
            date_column: 날짜 컬럼명
        """
        self.db_path = Path(db_path)
        self.table_name = table_name
        self.date_column = date_column
        
        if not self.db_path.exists():
            logger.warning(f"데이터베이스 파일이 존재하지 않습니다: {self.db_path}")
    
    def _get_connection(self) -> sqlite3.Connection:
        """데이터베이스 연결을 반환합니다."""
        return sqlite3.connect(str(self.db_path))
    
    def _execute_query(self, query: str, params: tuple = ()) -> pd.DataFrame:
        """SQL 쿼리를 실행하고 결과를 DataFrame으로 반환합니다."""
        if not self.db_path.exists():
            logger.warning(f"DB 파일 없음: {self.db_path}")
            return pd.DataFrame(columns=REQUIRED_COLUMNS)
        
        try:
            with self._get_connection() as conn:
                df = pd.read_sql_query(query, conn, params=params)
                return self._validate_dataframe(df)
        except Exception as e:
            logger.error(f"DB 쿼리 실패: {e}")
            return pd.DataFrame(columns=REQUIRED_COLUMNS)
    
    def load_trades(
        self,
        target_date: date,
        include_mtd: bool = True
    ) -> pd.DataFrame:
        """
        거래 데이터를 로드합니다.
        
        Args:
            target_date: 대상 날짜
            include_mtd: MTD 데이터 포함 여부
        
        Returns:
            pd.DataFrame: 거래 데이터
        """
        target_str = target_date.strftime("%Y-%m-%d")
        
        if include_mtd:
            month_start = target_date.replace(day=1).strftime("%Y-%m-%d")
            query = f"""
                SELECT {', '.join(REQUIRED_COLUMNS)}
                FROM {self.table_name}
                WHERE {self.date_column} >= ? AND {self.date_column} <= ?
                ORDER BY {self.date_column}
            """
            return self._execute_query(query, (month_start, target_str))
        else:
            query = f"""
                SELECT {', '.join(REQUIRED_COLUMNS)}
                FROM {self.table_name}
                WHERE DATE({self.date_column}) = ?
                ORDER BY {self.date_column}
            """
            return self._execute_query(query, (target_str,))
    
    def load_daily_trades(self, target_date: date) -> pd.DataFrame:
        """특정 날짜의 거래 데이터만 로드합니다."""
        target_str = target_date.strftime("%Y-%m-%d")
        
        query = f"""
            SELECT {', '.join(REQUIRED_COLUMNS)}
            FROM {self.table_name}
            WHERE DATE({self.date_column}) = ?
            ORDER BY {self.date_column}
        """
        return self._execute_query(query, (target_str,))


# ════════════════════════════════════════════════════════════════
# 팩토리 함수
# ════════════════════════════════════════════════════════════════

def create_data_loader(
    source_type: str = "csv",
    source_path: Optional[str] = None,
    **kwargs
) -> DataLoader:
    """
    데이터 소스 유형에 따라 적절한 DataLoader를 생성합니다.
    
    Args:
        source_type: 데이터 소스 유형 ("csv" 또는 "db")
        source_path: 데이터 소스 경로
        **kwargs: 추가 설정
    
    Returns:
        DataLoader: 데이터 로더 인스턴스
    
    Raises:
        ValueError: 지원하지 않는 소스 유형인 경우
    """
    # 환경변수에서 기본 경로 로드
    if source_path is None:
        source_path = os.getenv("TRADE_DATA_PATH", "data/trades.csv")
    
    source_type = source_type.lower()
    
    if source_type == "csv":
        return CSVDataLoader(source_path, **kwargs)
    elif source_type in ("db", "sqlite", "database"):
        table_name = kwargs.pop("table_name", "trades")
        return DBDataLoader(source_path, table_name=table_name, **kwargs)
    else:
        raise ValueError(f"지원하지 않는 데이터 소스: {source_type}")
