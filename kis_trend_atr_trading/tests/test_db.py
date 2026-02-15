"""
KIS Trend-ATR Trading System - 데이터베이스 모듈 테스트

이 모듈은 MySQL 연동 기능을 테스트합니다.

★ 테스트 환경:
    - DB_ENABLED=false일 때 테스트 (실제 DB 연결 없이)
    - Mock을 사용하여 DB 연결 시뮬레이션

실행 방법:
    pytest tests/test_db.py -v
"""

import os
import pytest
from datetime import datetime, date
from unittest.mock import Mock, patch, MagicMock

# DB_ENABLED를 false로 설정하여 실제 연결 방지
os.environ["DB_ENABLED"] = "false"

from db.mysql import DatabaseConfig, MySQLManager, get_db_manager, QueryError
from db.repository import (
    PositionRepository,
    TradeRepository,
    AccountSnapshotRepository,
    PositionRecord,
    TradeRecord,
    AccountSnapshotRecord
)
from trading.trader import DatabaseTrader, TradingMode, TradeResult


class TestDatabaseConfig:
    """DatabaseConfig 테스트"""
    
    def test_default_config(self):
        """기본 설정값 테스트"""
        config = DatabaseConfig()
        
        assert config.host == "localhost"
        assert config.port == 3306  # MySQL 기본 포트
        assert config.database == "kis_trading"
        assert config.user == "root"  # MySQL 기본 사용자
        assert config.db_type == "mysql"
        # DB_ENABLED=false로 설정했으므로
        assert config.enabled == False
    
    def test_custom_config(self):
        """커스텀 설정값 테스트"""
        config = DatabaseConfig(
            host="custom-host",
            port=3307,
            database="custom_db",
            user="custom_user",
            password="secret"
        )
        
        assert config.host == "custom-host"
        assert config.port == 3307
        assert config.database == "custom_db"
        assert config.user == "custom_user"
        assert config.password == "secret"
    
    def test_to_dict(self):
        """딕셔너리 변환 테스트"""
        config = DatabaseConfig(
            host="localhost",
            port=3306,
            database="test_db"
        )
        
        d = config.to_dict()
        
        assert d["host"] == "localhost"
        assert d["port"] == 3306
        assert d["database"] == "test_db"
        assert d["charset"] == "utf8mb4"
    
    def test_repr_masks_password(self):
        """비밀번호 마스킹 테스트"""
        config = DatabaseConfig(password="secret123")
        
        repr_str = repr(config)
        
        assert "secret123" not in repr_str
        assert "****" in repr_str


class TestMySQLManager:
    """MySQLManager 테스트"""
    
    def test_manager_initialization(self):
        """매니저 초기화 테스트"""
        manager = MySQLManager()
        
        assert manager.config is not None
        assert manager.is_connected() == False
    
    def test_disabled_manager_returns_empty(self):
        """비활성화된 매니저는 빈 결과 반환"""
        manager = MySQLManager()
        
        # 비활성화 상태에서 쿼리 실행
        result = manager.execute_query("SELECT 1")
        
        assert result == []
    
    def test_disabled_manager_command_returns_zero(self):
        """비활성화된 매니저는 0 반환"""
        manager = MySQLManager()
        
        result = manager.execute_command("INSERT INTO test VALUES (1)")
        
        assert result == 0
    
    def test_disabled_manager_insert_returns_zero(self):
        """비활성화된 매니저의 INSERT는 0 반환"""
        manager = MySQLManager()
        
        result = manager.execute_insert("INSERT INTO test VALUES (1)")
        
        assert result == 0


class TestPositionRecord:
    """PositionRecord 테스트"""
    
    def test_to_dict(self):
        """딕셔너리 변환 테스트"""
        record = PositionRecord(
            symbol="005930",
            entry_price=70000,
            quantity=10,
            entry_time=datetime(2025, 1, 15, 9, 30, 0),
            atr_at_entry=1500,
            stop_price=67000,
            take_profit_price=75000,
            trailing_stop=67500,
            highest_price=71000,
            status="OPEN"
        )
        
        d = record.to_dict()
        
        assert d["symbol"] == "005930"
        assert d["entry_price"] == 70000.0
        assert d["quantity"] == 10
        assert d["atr_at_entry"] == 1500.0
        assert d["status"] == "OPEN"
    
    def test_from_dict(self):
        """딕셔너리에서 생성 테스트"""
        data = {
            "symbol": "005930",
            "entry_price": 70000,
            "quantity": 10,
            "entry_time": datetime(2025, 1, 15, 9, 30, 0),
            "atr_at_entry": 1500,
            "stop_price": 67000,
            "take_profit_price": 75000,
            "trailing_stop": 67500,
            "highest_price": 71000,
            "status": "OPEN"
        }
        
        record = PositionRecord.from_dict(data)
        
        assert record.symbol == "005930"
        assert record.entry_price == 70000.0
        assert record.quantity == 10

    def test_from_dict_with_stock_code(self):
        """구스키마(stock_code) 딕셔너리 생성 테스트"""
        data = {
            "stock_code": "005930",
            "entry_price": 70000,
            "quantity": 10,
            "entry_time": datetime(2025, 1, 15, 9, 30, 0),
            "atr_at_entry": 1500,
            "stop_price": 67000,
            "take_profit_price": 75000,
            "trailing_stop": 67500,
            "highest_price": 71000,
            "status": "OPEN"
        }

        record = PositionRecord.from_dict(data)

        assert record.symbol == "005930"
        assert record.entry_price == 70000.0
        assert record.quantity == 10


class TestPositionRepositoryCompatibility:
    """positions symbol/stock_code 호환 테스트"""

    def test_upsert_uses_stock_code_when_symbol_missing(self):
        mock_db = Mock()
        mock_db.config = Mock(database="kis_trading")
        mock_db.execute_query.side_effect = [
            [{"column_name": "stock_code"}],  # 컬럼 탐지
            None,  # position_id 메타(없음)
            {
                "stock_code": "005930",
                "entry_price": 70000,
                "quantity": 10,
                "entry_time": datetime(2025, 1, 15, 9, 30, 0),
                "atr_at_entry": 1500,
                "stop_price": 67000,
                "take_profit_price": 75000,
                "trailing_stop": 67500,
                "highest_price": 71000,
                "mode": "PAPER",
                "status": "OPEN",
            },
        ]
        mock_db.execute_command.return_value = 1

        repo = PositionRepository(db=mock_db)
        result = repo.upsert_from_account_holding(
            symbol="005930",
            entry_price=70000,
            quantity=10,
            atr_at_entry=1500,
            stop_price=67000,
            take_profit_price=75000,
            trailing_stop=67500,
            highest_price=71000,
            entry_time=datetime(2025, 1, 15, 9, 30, 0),
        )

        assert result is not None
        assert result.symbol == "005930"
        insert_sql = mock_db.execute_command.call_args[0][0]
        assert "`stock_code`" in insert_sql
        select_sql = mock_db.execute_query.call_args_list[2][0][0]
        assert "`stock_code`" in select_sql

    def test_upsert_generates_position_id_when_required(self):
        mock_db = Mock()
        mock_db.config = Mock(database="kis_trading")
        mock_db.execute_query.side_effect = [
            [{"column_name": "stock_code"}],  # 컬럼 탐지
            {  # position_id 메타(필수, 문자열)
                "data_type": "varchar",
                "is_nullable": "NO",
                "column_default": None,
                "extra": "",
            },
            None,  # existing 조회: 없음
            {  # 최종 get_by_symbol 결과
                "position_id": "P20250115093000000000_005930",
                "stock_code": "005930",
                "entry_price": 70000,
                "quantity": 10,
                "entry_time": datetime(2025, 1, 15, 9, 30, 0),
                "atr_at_entry": 1500,
                "stop_price": 67000,
                "take_profit_price": 75000,
                "trailing_stop": 67500,
                "highest_price": 71000,
                "mode": "PAPER",
                "status": "OPEN",
            },
        ]
        mock_db.execute_command.return_value = 1

        repo = PositionRepository(db=mock_db)
        result = repo.upsert_from_account_holding(
            symbol="005930",
            entry_price=70000,
            quantity=10,
            atr_at_entry=1500,
            stop_price=67000,
            take_profit_price=75000,
            trailing_stop=67500,
            highest_price=71000,
            entry_time=datetime(2025, 1, 15, 9, 30, 0),
        )

        assert result is not None
        insert_sql = mock_db.execute_command.call_args[0][0]
        insert_params = mock_db.execute_command.call_args[0][1]
        assert "position_id" in insert_sql
        assert str(insert_params[0]).startswith("P")

    def test_detect_position_id_requirement_with_uppercase_metadata(self):
        mock_db = Mock()
        mock_db.config = Mock(database="kis_trading")
        mock_db.execute_query.side_effect = [
            [{"column_name": "stock_code"}],  # 컬럼 탐지
            {
                "DATA_TYPE": "varchar",
                "IS_NULLABLE": "NO",
                "COLUMN_DEFAULT": None,
                "EXTRA": "",
            },
        ]

        repo = PositionRepository(db=mock_db)
        assert repo._position_id_required is True
        assert repo._position_id_is_numeric is False

    def test_upsert_retries_when_position_id_default_error_occurs(self):
        mock_db = Mock()
        mock_db.config = Mock(database="kis_trading")
        mock_db.execute_query.side_effect = [
            [{"column_name": "stock_code"}],  # __init__: symbol 컬럼 탐지
            None,  # __init__: position_id 메타 탐지 실패(미탐지)
            {  # retry 시 position_id 메타 탐지(대문자 키)
                "DATA_TYPE": "varchar",
                "IS_NULLABLE": "NO",
                "COLUMN_DEFAULT": None,
                "EXTRA": "",
            },
            None,  # retry attempt: existing 조회
            {  # 최종 get_by_symbol
                "position_id": "P20250115093000000000_005930",
                "stock_code": "005930",
                "entry_price": 70000,
                "quantity": 10,
                "entry_time": datetime(2025, 1, 15, 9, 30, 0),
                "atr_at_entry": 1500,
                "stop_price": 67000,
                "take_profit_price": 75000,
                "trailing_stop": 67500,
                "highest_price": 71000,
                "mode": "PAPER",
                "status": "OPEN",
            },
        ]
        mock_db.execute_command.side_effect = [
            QueryError("1364 (HY000): Field 'position_id' doesn't have a default value"),
            1,
        ]

        repo = PositionRepository(db=mock_db)
        result = repo.upsert_from_account_holding(
            symbol="005930",
            entry_price=70000,
            quantity=10,
            atr_at_entry=1500,
            stop_price=67000,
            take_profit_price=75000,
            trailing_stop=67500,
            highest_price=71000,
            entry_time=datetime(2025, 1, 15, 9, 30, 0),
        )

        assert result is not None
        assert mock_db.execute_command.call_count == 2
        second_sql = mock_db.execute_command.call_args_list[1][0][0]
        assert "position_id" in second_sql


class TestTradeRecord:
    """TradeRecord 테스트"""
    
    def test_to_dict(self):
        """딕셔너리 변환 테스트"""
        record = TradeRecord(
            symbol="005930",
            side="BUY",
            price=70000,
            quantity=10,
            executed_at=datetime(2025, 1, 15, 9, 30, 0)
        )
        
        d = record.to_dict()
        
        assert d["symbol"] == "005930"
        assert d["side"] == "BUY"
        assert d["price"] == 70000.0
        assert d["quantity"] == 10
    
    def test_sell_with_pnl(self):
        """손익 포함 매도 기록 테스트"""
        record = TradeRecord(
            symbol="005930",
            side="SELL",
            price=72000,
            quantity=10,
            executed_at=datetime(2025, 1, 16, 14, 30, 0),
            reason="TAKE_PROFIT",
            pnl=20000,
            pnl_percent=2.86,
            entry_price=70000,
            holding_days=2
        )
        
        d = record.to_dict()
        
        assert d["side"] == "SELL"
        assert d["reason"] == "TAKE_PROFIT"
        assert d["pnl"] == 20000.0
        assert d["pnl_percent"] == 2.86


class TestTradingMode:
    """TradingMode 테스트"""
    
    def test_mode_values(self):
        """모드 값 테스트"""
        assert TradingMode.LIVE.value == "LIVE"
        assert TradingMode.PAPER.value == "PAPER"
        assert TradingMode.CBT.value == "CBT"
        assert TradingMode.SIGNAL_ONLY.value == "SIGNAL_ONLY"


class TestTradeResult:
    """TradeResult 테스트"""
    
    def test_success_result(self):
        """성공 결과 테스트"""
        result = TradeResult(
            success=True,
            message="매수 완료",
            symbol="005930",
            side="BUY",
            price=70000,
            quantity=10,
            order_no="KIS123456",
            mode="PAPER"
        )
        
        assert result.success == True
        assert result.symbol == "005930"
        assert result.side == "BUY"
    
    def test_failure_result(self):
        """실패 결과 테스트"""
        result = TradeResult(
            success=False,
            message="중복 진입 차단",
            symbol="005930",
            side="BUY",
            mode="PAPER"
        )
        
        assert result.success == False
        assert "중복" in result.message
    
    def test_to_dict(self):
        """딕셔너리 변환 테스트"""
        result = TradeResult(
            success=True,
            message="매도 완료",
            symbol="005930",
            side="SELL",
            price=72000,
            quantity=10,
            pnl=20000,
            pnl_percent=2.86,
            mode="PAPER"
        )
        
        d = result.to_dict()
        
        assert d["success"] == True
        assert d["pnl"] == 20000
        assert d["pnl_percent"] == 2.86


class TestDatabaseTrader:
    """DatabaseTrader 테스트"""
    
    def test_trader_initialization(self):
        """트레이더 초기화 테스트"""
        # 환경변수 설정
        os.environ["TRADING_MODE"] = "CBT"
        
        trader = DatabaseTrader()
        
        assert trader.mode == TradingMode.CBT
    
    def test_signal_only_mode(self):
        """SIGNAL_ONLY 모드 테스트"""
        os.environ["TRADING_MODE"] = "SIGNAL_ONLY"
        
        trader = DatabaseTrader()
        
        assert trader.mode == TradingMode.SIGNAL_ONLY
    
    @patch('trading.trader.get_db_manager')
    @patch('trading.trader.get_telegram_notifier')
    def test_can_place_orders(self, mock_telegram, mock_db):
        """주문 가능 여부 테스트"""
        mock_db.return_value.is_connected.return_value = False
        mock_telegram.return_value = Mock()
        
        # SIGNAL_ONLY 모드에서는 실주문 안 함
        os.environ["TRADING_MODE"] = "SIGNAL_ONLY"
        trader = DatabaseTrader()
        
        # mode가 SIGNAL_ONLY인지 확인
        assert trader.mode == TradingMode.SIGNAL_ONLY


class TestRepositoryDisabled:
    """비활성화된 Repository 테스트"""
    
    def test_position_repo_returns_empty_list(self):
        """비활성화 시 빈 리스트 반환"""
        repo = PositionRepository()
        
        positions = repo.get_open_positions()
        
        assert positions == []
    
    def test_trade_repo_returns_empty_list(self):
        """비활성화 시 빈 리스트 반환"""
        repo = TradeRepository()
        
        trades = repo.get_recent_trades()
        
        assert trades == []
    
    def test_snapshot_repo_returns_none(self):
        """비활성화 시 None 반환"""
        repo = AccountSnapshotRepository()
        
        latest = repo.get_latest()
        
        assert latest is None


class TestAccountSnapshotRecord:
    """AccountSnapshotRecord 테스트"""
    
    def test_to_dict(self):
        """딕셔너리 변환 테스트"""
        record = AccountSnapshotRecord(
            snapshot_time=datetime(2025, 1, 15, 15, 30, 0),
            total_equity=10000000,
            cash=5000000,
            unrealized_pnl=100000,
            realized_pnl=200000,
            position_count=2
        )
        
        d = record.to_dict()
        
        assert d["total_equity"] == 10000000.0
        assert d["cash"] == 5000000.0
        assert d["unrealized_pnl"] == 100000.0
        assert d["realized_pnl"] == 200000.0
        assert d["position_count"] == 2


class TestMySQLSpecificFeatures:
    """MySQL 특화 기능 테스트"""
    
    def test_pool_config(self):
        """커넥션 풀 설정 테스트"""
        config = DatabaseConfig(
            pool_size=10,
            pool_name="test_pool"
        )
        
        pool_config = config.to_pool_config()
        
        assert pool_config["pool_size"] == 10
        assert pool_config["pool_name"] == "test_pool"
    
    def test_charset_config(self):
        """문자셋 설정 테스트"""
        config = DatabaseConfig()
        
        assert config.charset == "utf8mb4"
        assert config.collation == "utf8mb4_unicode_ci"
    
    def test_autocommit_disabled_by_default(self):
        """자동 커밋이 기본으로 비활성화"""
        config = DatabaseConfig()
        
        assert config.autocommit == False

    def test_positions_compat_columns_migrate_stop_loss(self):
        """구스키마 stop_loss -> stop_price 보정 SQL 실행 테스트"""
        manager = MySQLManager()
        manager.table_exists = Mock(return_value=True)

        existing_cols = {
            "stop_loss",
            "take_profit",
            "entry_date",
            "entry_price",
            "atr_value",
        }

        def _column_exists(table_name, column_name):
            return table_name == "positions" and column_name in existing_cols

        manager._column_exists = Mock(side_effect=_column_exists)

        executed_sql = []
        cursor = Mock()

        def _execute(sql):
            executed_sql.append(sql)
            sql_upper = " ".join(str(sql).upper().split())
            if "ALTER TABLE POSITIONS ADD COLUMN" in sql_upper:
                # "ADD COLUMN <name> ..." 패턴에서 컬럼명 추출
                parts = str(sql).replace("\n", " ").split()
                if "COLUMN" in parts:
                    idx = parts.index("COLUMN")
                    if idx + 1 < len(parts):
                        existing_cols.add(parts[idx + 1].strip("`"))

        cursor.execute.side_effect = _execute

        manager._ensure_positions_compat_columns(cursor)

        assert any("ADD COLUMN stop_price" in sql for sql in executed_sql)
        assert any("SET `stop_price` = `stop_loss`" in sql for sql in executed_sql)


# 실행 시 환경 복원
@pytest.fixture(autouse=True)
def cleanup_env():
    """테스트 후 환경변수 정리"""
    yield
    # 테스트 후 원래 값으로 복원
    if "TRADING_MODE" in os.environ:
        del os.environ["TRADING_MODE"]
