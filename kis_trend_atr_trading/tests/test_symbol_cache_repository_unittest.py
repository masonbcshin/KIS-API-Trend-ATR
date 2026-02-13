"""
SymbolCacheRepository 단위 테스트
"""

import sys
from datetime import datetime
from pathlib import Path

# 프로젝트 루트를 path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from db.repository import SymbolCacheRepository
from utils.market_hours import KST


class FakeDB:
    def __init__(self):
        self.commands = []
        self.query_calls = 0

    def execute_command(self, command, params=None):
        self.commands.append(command)
        return 1

    def execute_query(self, query, params=None, fetch_one=False):
        self.query_calls += 1
        # 첫 조회는 table missing, 두 번째는 정상값 반환
        if self.query_calls == 1:
            raise Exception("1146 (42S02): Table 'kis_trading.symbol_cache' doesn't exist")
        return {
            "stock_code": "005930",
            "stock_name": "삼성전자",
            "updated_at": datetime.now(KST),
        }


def test_symbol_cache_repository_ensures_table_on_init():
    db = FakeDB()
    SymbolCacheRepository(db=db)

    ddl = "\n".join(db.commands)
    assert "CREATE TABLE IF NOT EXISTS symbol_cache" in ddl


def test_symbol_cache_repository_recovers_from_missing_table_on_get():
    db = FakeDB()
    repo = SymbolCacheRepository(db=db)

    record = repo.get("005930")

    assert record is not None
    assert record.stock_name == "삼성전자"
    assert db.query_calls == 2
