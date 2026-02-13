"""
SymbolResolver 단위 테스트
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

# 프로젝트 루트를 path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from db.repository import SymbolCacheRecord
from utils.market_hours import KST
from utils.symbol_resolver import SymbolResolver


class InMemorySymbolCacheRepo:
    """테스트용 in-memory symbol_cache 저장소"""

    def __init__(self, initial=None):
        self._data = initial or {}
        self.upsert_calls = []

    def get(self, stock_code: str):
        return self._data.get(stock_code)

    def upsert(self, stock_code: str, stock_name: str, updated_at=None):
        ts = updated_at or datetime.now(KST)
        self._data[stock_code] = SymbolCacheRecord(
            stock_code=stock_code,
            stock_name=stock_name,
            updated_at=ts,
        )
        self.upsert_calls.append((stock_code, stock_name))
        return True


def test_format_symbol_uses_cache_without_api_call():
    repo = InMemorySymbolCacheRepo(
        initial={
            "005930": SymbolCacheRecord(
                stock_code="005930",
                stock_name="삼성전자",
                updated_at=datetime.now(KST) - timedelta(days=1),
            )
        }
    )
    api_client = MagicMock()
    resolver = SymbolResolver(cache_repo=repo, api_client=api_client)

    formatted = resolver.format_symbol("005930")

    assert formatted == "삼성전자(005930)"
    api_client.get_account_balance.assert_not_called()


def test_format_symbol_cache_miss_api_success_upserts_cache():
    repo = InMemorySymbolCacheRepo()
    api_client = MagicMock()
    api_client.get_account_balance.return_value = {
        "success": True,
        "holdings": [
            {"stock_code": "005930", "stock_name": "삼성전자"},
            {"stock_code": "000660", "stock_name": "SK하이닉스"},
        ],
    }
    resolver = SymbolResolver(cache_repo=repo, api_client=api_client)

    formatted = resolver.format_symbol("005930")

    assert formatted == "삼성전자(005930)"
    assert ("005930", "삼성전자") in repo.upsert_calls


def test_format_symbol_api_failure_returns_stale_cache_if_exists():
    stale_time = datetime.now(KST) - timedelta(days=31)
    repo = InMemorySymbolCacheRepo(
        initial={
            "005930": SymbolCacheRecord(
                stock_code="005930",
                stock_name="삼성전자",
                updated_at=stale_time,
            )
        }
    )
    api_client = MagicMock()
    api_client.get_account_balance.side_effect = RuntimeError("api down")
    resolver = SymbolResolver(cache_repo=repo, api_client=api_client)

    formatted = resolver.format_symbol("005930")

    assert formatted == "삼성전자(005930)"
    api_client.get_account_balance.assert_called_once()


def test_format_symbol_api_failure_without_cache_returns_unknown():
    repo = InMemorySymbolCacheRepo()
    api_client = MagicMock()
    api_client.get_account_balance.side_effect = RuntimeError("api down")
    resolver = SymbolResolver(cache_repo=repo, api_client=api_client)

    formatted = resolver.format_symbol("005930")

    assert formatted == "UNKNOWN(005930)"
