from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal

from db.repository import TradeRepository
from utils.avg_price import calc_weighted_avg
from utils.market_hours import KST


class _FakeCursor:
    def __init__(self, db):
        self._db = db
        self.rowcount = 0
        self.lastrowid = 0

    def execute(self, _sql, params):
        idem = params[-1]
        if idem in self._db._rows:
            self.rowcount = 0
            self.lastrowid = 0
            return
        self._db._auto_id += 1
        self._db._rows[idem] = {
            "id": self._db._auto_id,
            "symbol": params[0],
            "side": params[1],
            "price": params[2],
            "quantity": params[3],
            "executed_at": params[4],
            "reason": params[5],
            "pnl": params[6],
            "pnl_percent": params[7],
            "entry_price": params[8],
            "holding_days": params[9],
            "order_no": params[10],
            "mode": params[11],
            "idempotency_key": params[12],
        }
        self.rowcount = 1
        self.lastrowid = self._db._auto_id


class _FakeDb:
    def __init__(self):
        self._rows = {}
        self._auto_id = 0

    @contextmanager
    def transaction(self):
        yield _FakeCursor(self)

    def execute_query(self, _query, params, fetch_one=False):
        idem = params[0]
        row = self._rows.get(idem)
        if fetch_one:
            return row
        return [row] if row else []


def test_same_fill_applied_twice_updates_position_once():
    repo = TradeRepository(db=_FakeDb())
    executed_at = datetime.now(KST)

    _, created1 = repo.save_execution_fill(
        symbol="005930",
        side="BUY",
        price=70000,
        quantity=1,
        executed_at=executed_at,
        order_no="A0001",
        exec_id="E001",
    )
    _, created2 = repo.save_execution_fill(
        symbol="005930",
        side="BUY",
        price=70000,
        quantity=1,
        executed_at=executed_at,
        order_no="A0001",
        exec_id="E001",
    )

    qty = 0
    avg = Decimal("0")
    for created in (created1, created2):
        if not created:
            continue
        avg = calc_weighted_avg(avg, qty, Decimal("70000"), 1)
        qty += 1

    assert created1 is True
    assert created2 is False
    assert qty == 1
    assert avg == Decimal("70000.00")
