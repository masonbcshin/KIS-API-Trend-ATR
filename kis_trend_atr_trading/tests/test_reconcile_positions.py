from dataclasses import dataclass
from decimal import Decimal

from engine.order_synchronizer import PositionResynchronizer
from utils.position_store import StoredPosition


class _DummyApi:
    def __init__(self, holdings):
        self._holdings = holdings

    def get_access_token(self):
        return "token"

    def get_holdings(self):
        return self._holdings


class _DummyStore:
    def __init__(self, stored=None):
        self._stored = stored
        self.cleared = False

    def load_position(self):
        return self._stored

    def save_position(self, position):
        self._stored = position

    def clear_position(self):
        self.cleared = True
        self._stored = None


@dataclass
class _DbPosition:
    symbol: str
    atr_at_entry: float = 1000.0
    stop_price: float = 65000.0
    take_profit_price: float = 80000.0
    trailing_stop: float = 65000.0
    highest_price: float = 70000.0


class _DummyDbRepo:
    def __init__(self, open_positions):
        self._open_positions = open_positions
        self.upsert_calls = []
        self.closed_symbols = []

    def get_open_positions(self):
        return list(self._open_positions)

    def upsert_from_account_holding(self, **kwargs):
        self.upsert_calls.append(kwargs)
        return True

    def close_position(self, symbol):
        self.closed_symbols.append(symbol)
        return True


def test_reconcile_overwrites_stored_entry_price_and_quantity_from_holdings():
    PositionResynchronizer._startup_holdings_cache = None
    PositionResynchronizer._startup_holdings_cached_at = None
    PositionResynchronizer._startup_db_sync_applied = False
    stored = StoredPosition(
        stock_code="005930",
        entry_price=60000.0,
        quantity=1,
        stop_loss=58000.0,
        take_profit=70000.0,
        entry_date="2026-02-10",
        atr_at_entry=1200.0,
    )
    api = _DummyApi(
        [
            {
                "stock_code": "005930",
                "qty": 3,
                "avg_price": Decimal("70123.45"),
                "stock_name": "Samsung",
            }
        ]
    )
    db_repo = _DummyDbRepo(open_positions=[_DbPosition(symbol="005930")])
    store = _DummyStore(stored=stored)
    syncer = PositionResynchronizer(
        api=api,
        position_store=store,
        db_repository=db_repo,
        trading_mode="PAPER",
        target_symbol="005930",
    )

    result = syncer.synchronize_on_startup()

    assert result["success"] is True
    assert result["action"] == "QTY_ADJUSTED"
    assert result["position"].quantity == 3
    assert result["position"].entry_price == 70123.45
    assert db_repo.upsert_calls
    assert db_repo.upsert_calls[0]["quantity"] == 3
    assert db_repo.upsert_calls[0]["entry_price"] == 70123.45


def test_reconcile_closes_db_open_positions_missing_from_holdings():
    PositionResynchronizer._startup_holdings_cache = None
    PositionResynchronizer._startup_holdings_cached_at = None
    PositionResynchronizer._startup_db_sync_applied = False
    api = _DummyApi(
        [
            {
                "stock_code": "005930",
                "qty": 2,
                "avg_price": Decimal("70000"),
            }
        ]
    )
    db_repo = _DummyDbRepo(open_positions=[_DbPosition("005930"), _DbPosition("000660")])
    store = _DummyStore(stored=None)
    syncer = PositionResynchronizer(
        api=api,
        position_store=store,
        db_repository=db_repo,
        trading_mode="PAPER",
        target_symbol="005930",
    )

    result = syncer.synchronize_on_startup()

    assert result["success"] is True
    assert "000660" in db_repo.closed_symbols
    assert result["summary"]["zombies"] == 1


def test_reconcile_avg_adjusted_action_when_only_avg_changes():
    PositionResynchronizer._startup_holdings_cache = None
    PositionResynchronizer._startup_holdings_cached_at = None
    PositionResynchronizer._startup_db_sync_applied = False
    stored = StoredPosition(
        stock_code="005930",
        entry_price=70000.0,
        quantity=3,
        stop_loss=68000.0,
        take_profit=73000.0,
        entry_date="2026-02-10",
        atr_at_entry=1200.0,
    )
    api = _DummyApi(
        [
            {
                "stock_code": "005930",
                "qty": 3,
                "avg_price": Decimal("70100.11"),
            }
        ]
    )
    db_repo = _DummyDbRepo(open_positions=[_DbPosition(symbol="005930")])
    store = _DummyStore(stored=stored)
    syncer = PositionResynchronizer(
        api=api,
        position_store=store,
        db_repository=db_repo,
        trading_mode="PAPER",
        target_symbol="005930",
    )

    result = syncer.synchronize_on_startup()

    assert result["success"] is True
    assert result["action"] == "AVG_ADJUSTED"
    assert result["position"].quantity == 3
    assert result["position"].entry_price == 70100.11
