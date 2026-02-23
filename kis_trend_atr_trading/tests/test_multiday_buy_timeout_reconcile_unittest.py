import datetime as dt
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def _ensure_fake_dependencies() -> None:
    if "pytz" not in sys.modules:
        class _FakeKST(dt.tzinfo):
            def utcoffset(self, _dt):
                return dt.timedelta(hours=9)

            def dst(self, _dt):
                return dt.timedelta(0)

            def tzname(self, _dt):
                return "KST"

            def localize(self, value):
                return value.replace(tzinfo=self)

        fake_pytz = types.ModuleType("pytz")
        fake_pytz.timezone = lambda _name: _FakeKST()
        sys.modules["pytz"] = fake_pytz

    if "pandas" not in sys.modules:
        fake_pd = types.ModuleType("pandas")
        fake_pd.DataFrame = object
        fake_pd.Series = object
        fake_pd.isna = lambda _x: False
        sys.modules["pandas"] = fake_pd

    if "numpy" not in sys.modules:
        fake_np = types.ModuleType("numpy")
        fake_np.nan = float("nan")
        fake_np.where = lambda cond, a, b: a
        sys.modules["numpy"] = fake_np

    if "dotenv" not in sys.modules:
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *args, **kwargs: None
        sys.modules["dotenv"] = fake_dotenv

    if "requests" not in sys.modules:
        fake_requests = types.ModuleType("requests")

        class _DummySession:
            def request(self, *args, **kwargs):
                raise RuntimeError("dummy requests session should not be used in this test")

        class _DummyResponse:
            status_code = 200

        fake_requests.Session = _DummySession
        fake_requests.Response = _DummyResponse
        fake_exceptions = types.ModuleType("requests.exceptions")
        fake_exceptions.RequestException = Exception
        fake_exceptions.Timeout = Exception
        fake_exceptions.ConnectionError = Exception
        fake_requests.exceptions = fake_exceptions
        sys.modules["requests"] = fake_requests
        sys.modules["requests.exceptions"] = fake_exceptions


_ensure_fake_dependencies()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.multiday_executor import MultidayExecutor  # type: ignore  # noqa: E402
from engine.order_synchronizer import OrderExecutionResult  # type: ignore  # noqa: E402


class _DummyPositionStore:
    def __init__(self):
        self.save_position_calls = 0
        self.saved_positions = []
        self.clear_position_calls = 0

    def save_position(self, position):
        self.save_position_calls += 1
        self.saved_positions.append(position)
        return True

    def clear_position(self):
        self.clear_position_calls += 1
        return True


class _DummyTelegram:
    def __init__(self):
        self.warning_messages = []

    def notify_warning(self, message):
        self.warning_messages.append(message)
        return True

    def notify_info(self, *_args, **_kwargs):
        return True

    def notify_error(self, *_args, **_kwargs):
        return True


class _DummyStrategy:
    def __init__(self):
        self._position = None

    @property
    def has_position(self):
        return self._position is not None

    @property
    def position(self):
        return self._position

    def open_position(self, symbol, entry_price, quantity, atr, stop_loss, take_profit=None):
        self._position = SimpleNamespace(
            symbol=symbol,
            position="LONG",
            entry_price=float(entry_price),
            quantity=int(quantity),
            atr_at_entry=float(atr),
            stop_loss=float(stop_loss),
            take_profit=float(take_profit) if take_profit is not None else None,
            trailing_stop=float(stop_loss),
            highest_price=float(entry_price),
            entry_date=dt.date.today().isoformat(),
            entry_time="09:00:00",
            state=SimpleNamespace(value="ENTERED"),
        )
        return self._position

    def reset_to_wait(self):
        self._position = None


class TestMultidayBuyTimeoutReconcile(unittest.TestCase):
    def _make_executor(self, holdings, failure_message="타임아웃 - 마지막 확인 체결수량: 0주"):
        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.trading_mode = "PAPER"
        ex.stock_code = "000660"
        ex.order_quantity = 1
        ex.strategy = _DummyStrategy()
        ex.position_store = _DummyPositionStore()
        ex.telegram = _DummyTelegram()
        ex._pending_exit_state = None
        ex.risk_manager = SimpleNamespace(
            check_order_allowed=lambda is_closing_position: SimpleNamespace(
                passed=True, should_exit=False, reason=""
            )
        )
        ex.order_synchronizer = SimpleNamespace(
            execute_buy_order=lambda **_kwargs: SimpleNamespace(
                success=False,
                result_type=OrderExecutionResult.CANCELLED,
                order_no="B-001",
                message=failure_message,
            )
        )
        ex.api = SimpleNamespace(get_holdings=lambda: holdings)
        ex._can_place_orders = lambda: False

        ex.db_sync_calls = 0
        ex.snapshot_calls = 0
        ex._sync_db_position_from_strategy = (
            lambda: setattr(ex, "db_sync_calls", ex.db_sync_calls + 1)
        )
        ex._persist_account_snapshot = (
            lambda force=False: setattr(
                ex,
                "snapshot_calls",
                ex.snapshot_calls + (1 if force else 0),
            )
        )
        return ex

    @staticmethod
    def _make_buy_signal(price=957000.0, atr=12000.0):
        return SimpleNamespace(
            price=price,
            atr=atr,
            stop_loss=0.0,
            take_profit=0.0,
            trend=SimpleNamespace(value="UPTREND"),
            reason="UNITTEST",
        )

    def test_execute_buy_timeout_auto_reconciles_when_api_confirms_holding(self):
        ex = self._make_executor(
            holdings=[{"stock_code": "000660", "qty": 2, "avg_price": 955500.0}]
        )
        signal = self._make_buy_signal()

        result = ex.execute_buy(signal)

        self.assertTrue(result["success"])
        self.assertTrue(result.get("reconciled"))
        self.assertTrue(ex.strategy.has_position)
        self.assertEqual(ex.strategy.position.quantity, 2)
        self.assertAlmostEqual(ex.strategy.position.entry_price, 955500.0)
        self.assertEqual(ex.position_store.save_position_calls, 1)
        self.assertEqual(ex.db_sync_calls, 1)
        self.assertEqual(ex.snapshot_calls, 1)
        self.assertEqual(len(ex.telegram.warning_messages), 1)

    def test_execute_buy_timeout_keeps_no_position_when_api_has_no_holding(self):
        ex = self._make_executor(holdings=[])
        signal = self._make_buy_signal()

        result = ex.execute_buy(signal)

        self.assertFalse(result["success"])
        self.assertFalse(ex.strategy.has_position)
        self.assertEqual(ex.position_store.save_position_calls, 0)
        self.assertEqual(ex.db_sync_calls, 0)
        self.assertEqual(ex.snapshot_calls, 0)


if __name__ == "__main__":
    unittest.main()
