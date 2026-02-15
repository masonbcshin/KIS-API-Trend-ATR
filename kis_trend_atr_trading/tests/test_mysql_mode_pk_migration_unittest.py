"""Unit tests for MySQL mode-separated primary key migration logic."""

import sys
from pathlib import Path
from unittest.mock import Mock

# Add project root for local imports.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db.mysql import DatabaseConfig, MySQLManager


def _new_manager() -> MySQLManager:
    return MySQLManager(DatabaseConfig(database="kis_trading_test"))


def test_ensure_primary_keys_updates_legacy_single_pk_tables():
    manager = _new_manager()
    cursor = Mock()

    manager.table_exists = Mock(return_value=True)
    manager._column_exists = Mock(return_value=True)
    manager._has_duplicate_composite_key = Mock(return_value=False)
    manager._get_primary_key_columns = Mock(
        side_effect=lambda table_name: {
            "positions": ["symbol"],
            "account_snapshots": ["snapshot_time"],
            "daily_summary": ["trade_date", "mode"],
        }[table_name]
    )

    manager._ensure_primary_keys(cursor)

    executed_sql = [args[0] for args, _ in cursor.execute.call_args_list]
    assert any("ALTER TABLE `positions` DROP PRIMARY KEY, ADD PRIMARY KEY (`symbol`, `mode`)" in sql for sql in executed_sql)
    assert any(
        "ALTER TABLE `account_snapshots` DROP PRIMARY KEY, ADD PRIMARY KEY (`snapshot_time`, `mode`)" in sql
        for sql in executed_sql
    )
    assert all("daily_summary" not in sql for sql in executed_sql)


def test_ensure_primary_keys_skips_when_duplicate_composite_key_exists():
    manager = _new_manager()
    cursor = Mock()

    manager.table_exists = Mock(side_effect=lambda table_name: table_name == "positions")
    manager._column_exists = Mock(return_value=True)
    manager._get_primary_key_columns = Mock(return_value=["symbol"])
    manager._has_duplicate_composite_key = Mock(return_value=True)

    manager._ensure_primary_keys(cursor)

    cursor.execute.assert_not_called()
