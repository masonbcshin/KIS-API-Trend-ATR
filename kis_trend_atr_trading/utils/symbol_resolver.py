"""
KIS Trend-ATR Trading System - 종목명 Resolver

알림 메시지에서 종목코드를 `종목명(종목코드)` 형태로 통일하기 위한 모듈입니다.
"""

import threading
from datetime import datetime, timedelta
from typing import Dict, Optional

from api.kis_api import KISApi
from db.repository import (
    SymbolCacheRecord,
    SymbolCacheRepository,
    get_symbol_cache_repository,
)
from env import get_trading_mode
from utils.logger import get_logger
from utils.market_hours import KST

logger = get_logger("symbol_resolver")

DEFAULT_SYMBOL_CACHE_TTL_DAYS = 30


class SymbolResolver:
    """
    종목코드를 종목명으로 해석하는 Resolver.

    해석 순서:
        1) 메모리 캐시
        2) SSOT DB symbol_cache
        3) KIS API(get_account_balance -> holdings[].stock_name)
        4) 실패 시 UNKNOWN(code)
    """

    def __init__(
        self,
        cache_repo: SymbolCacheRepository = None,
        api_client: KISApi = None,
        ttl_days: int = DEFAULT_SYMBOL_CACHE_TTL_DAYS,
    ):
        self._cache_repo = cache_repo or get_symbol_cache_repository()
        self._api_client = api_client
        self._ttl = timedelta(days=max(int(ttl_days), 1))

        self._memory_cache: Dict[str, SymbolCacheRecord] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def format_symbol(self, stock_code: str, refresh: bool = True) -> str:
        """
        종목코드를 `종목명(종목코드)` 형태로 반환합니다.

        실패 시 예외를 던지지 않고 `UNKNOWN(code)`를 반환합니다.
        """
        code = self._normalize_stock_code(stock_code)
        if not code:
            raw = str(stock_code or "").strip()
            return f"UNKNOWN({raw})"

        try:
            stock_name = self._resolve_stock_name(code, refresh=refresh)
            if not stock_name:
                stock_name = "UNKNOWN"
        except Exception as e:
            logger.warning(f"[SYMBOL] 종목명 해석 실패: code={code}, err={e}")
            stock_name = "UNKNOWN"

        return f"{stock_name}({code})"

    def _resolve_stock_name(self, stock_code: str, refresh: bool = True) -> Optional[str]:
        mem = self._memory_cache.get(stock_code)
        if mem:
            if refresh and self._is_stale(mem.updated_at):
                refreshed = self._refresh_with_singleflight(
                    stock_code=stock_code,
                    fallback_name=mem.stock_name,
                )
                return refreshed or mem.stock_name
            return mem.stock_name

        persisted = self._load_from_persistent_cache(stock_code)
        if persisted:
            self._memory_cache[stock_code] = persisted
            if refresh and self._is_stale(persisted.updated_at):
                refreshed = self._refresh_with_singleflight(
                    stock_code=stock_code,
                    fallback_name=persisted.stock_name,
                )
                return refreshed or persisted.stock_name
            return persisted.stock_name

        if not refresh:
            return None

        refreshed = self._refresh_with_singleflight(stock_code=stock_code, fallback_name=None)
        return refreshed

    def _load_from_persistent_cache(self, stock_code: str) -> Optional[SymbolCacheRecord]:
        try:
            return self._cache_repo.get(stock_code)
        except Exception as e:
            logger.warning(f"[SYMBOL] 영속 캐시 조회 실패: code={stock_code}, err={e}")
            return None

    def _refresh_with_singleflight(
        self,
        stock_code: str,
        fallback_name: Optional[str],
    ) -> Optional[str]:
        lock = self._get_code_lock(stock_code)

        with lock:
            latest = self._memory_cache.get(stock_code)
            if latest and not self._is_stale(latest.updated_at):
                return latest.stock_name

            try:
                resolved = self._lookup_name_via_api(stock_code)
                if resolved:
                    self._save_cache(stock_code, resolved)
                    return resolved
            except Exception as e:
                logger.warning(f"[SYMBOL] API 갱신 실패: code={stock_code}, err={e}")

            if latest and latest.stock_name:
                return latest.stock_name
            return fallback_name

    def _lookup_name_via_api(self, stock_code: str) -> Optional[str]:
        client = self._get_api_client()
        if client is None:
            return None

        balance = client.get_account_balance()
        if not isinstance(balance, dict) or not balance.get("success"):
            return None

        matched_name: Optional[str] = None
        now = datetime.now(KST)
        holdings = balance.get("holdings") or []

        for item in holdings:
            code = str(item.get("stock_code", "")).strip()
            name = str(item.get("stock_name", "")).strip()
            if not code or not name:
                continue

            # 한번의 API 호출 결과로 보유종목명을 모두 캐시에 반영합니다.
            self._save_cache(code, name, updated_at=now)
            if code == stock_code:
                matched_name = name

        return matched_name

    def _save_cache(
        self,
        stock_code: str,
        stock_name: str,
        updated_at: datetime = None,
    ) -> None:
        now = updated_at or datetime.now(KST)
        record = SymbolCacheRecord(
            stock_code=stock_code,
            stock_name=stock_name,
            updated_at=now,
        )
        self._memory_cache[stock_code] = record

        try:
            self._cache_repo.upsert(stock_code=stock_code, stock_name=stock_name, updated_at=now)
        except Exception as e:
            logger.warning(f"[SYMBOL] 영속 캐시 저장 실패: code={stock_code}, err={e}")

    def _get_code_lock(self, stock_code: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(stock_code)
            if lock is None:
                lock = threading.Lock()
                self._locks[stock_code] = lock
            return lock

    def _is_stale(self, updated_at: datetime) -> bool:
        if not isinstance(updated_at, datetime):
            return True
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=KST)
        return (datetime.now(KST) - updated_at) > self._ttl

    def _get_api_client(self) -> Optional[KISApi]:
        if self._api_client is not None:
            return self._api_client

        try:
            mode = get_trading_mode()
            is_paper = mode != "REAL"
            self._api_client = KISApi(is_paper_trading=is_paper)
            return self._api_client
        except Exception as e:
            logger.warning(f"[SYMBOL] KIS API 클라이언트 초기화 실패: {e}")
            return None

    @staticmethod
    def _normalize_stock_code(stock_code: str) -> str:
        return str(stock_code or "").strip()


_resolver_instance: Optional[SymbolResolver] = None


def get_symbol_resolver() -> SymbolResolver:
    """싱글톤 SymbolResolver 인스턴스를 반환합니다."""
    global _resolver_instance
    if _resolver_instance is None:
        _resolver_instance = SymbolResolver()
    return _resolver_instance
