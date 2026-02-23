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
        self.clear_position_calls = 0
        self.clear_pending_calls = 0

    def clear_position(self):
        self.clear_position_calls += 1
        return True

    def clear_pending_exit(self):
        self.clear_pending_calls += 1
        return True


class _DummyTelegram:
    def __init__(self):
        self.warning_messages = []
        self.info_messages = []

    def notify_warning(self, message):
        self.warning_messages.append(message)
        return True

    def notify_info(self, message):
        self.info_messages.append(message)
        return True

    def notify_error(self, *_args, **_kwargs):
        return True


class _DummyStrategy:
    def __init__(self, quantity: int = 1, entry_price: float = 897000.0):
        self._position = SimpleNamespace(quantity=quantity, entry_price=entry_price)

    @property
    def has_position(self):
        return self._position is not None

    @property
    def position(self):
        return self._position

    def reset_to_wait(self):
        self._position = None


class TestMultidaySellNoHoldingReconcile(unittest.TestCase):
    def _make_executor(self, holdings):
        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.trading_mode = "PAPER"
        ex.stock_code = "000660"
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
            execute_sell_order=lambda **_kwargs: SimpleNamespace(
                success=False,
                result_type=OrderExecutionResult.FAILED,
                order_no="",
                message="주문 전송 실패: 모의투자 잔고내역이 없습니다.",
            )
        )
        ex.api = SimpleNamespace(get_holdings=lambda: holdings)

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

    def test_execute_sell_auto_reconciles_when_api_confirms_no_holding(self):
        ex = self._make_executor(holdings=[])
        signal = SimpleNamespace(exit_reason=None, price=953000.0)

        result = ex.execute_sell(signal)

        self.assertTrue(result["success"])
        self.assertTrue(result.get("reconciled"))
        self.assertFalse(ex.strategy.has_position)
        self.assertEqual(ex.position_store.clear_position_calls, 1)
        self.assertEqual(ex.db_sync_calls, 1)
        self.assertEqual(ex.snapshot_calls, 1)
        self.assertEqual(len(ex.telegram.warning_messages), 1)

    def test_execute_sell_keeps_position_when_api_still_has_holding(self):
        ex = self._make_executor(holdings=[{"stock_code": "000660", "qty": 1}])
        signal = SimpleNamespace(exit_reason=None, price=953000.0)

        result = ex.execute_sell(signal)

        self.assertFalse(result["success"])
        self.assertTrue(ex.strategy.has_position)
        self.assertEqual(ex.position_store.clear_position_calls, 0)
        self.assertEqual(ex.db_sync_calls, 0)
        self.assertEqual(ex.snapshot_calls, 0)


if __name__ == "__main__":
    unittest.main()
