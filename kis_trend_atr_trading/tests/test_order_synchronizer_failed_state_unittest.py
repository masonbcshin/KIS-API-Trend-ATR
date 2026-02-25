import datetime as dt
import sys
import types
import unittest
from pathlib import Path


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

    if "dotenv" not in sys.modules:
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *args, **kwargs: None
        sys.modules["dotenv"] = fake_dotenv

    if "pandas" not in sys.modules:
        fake_pd = types.ModuleType("pandas")
        fake_pd.DataFrame = object
        fake_pd.Series = object
        sys.modules["pandas"] = fake_pd

    if "numpy" not in sys.modules:
        fake_np = types.ModuleType("numpy")
        fake_np.nan = float("nan")
        sys.modules["numpy"] = fake_np

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
from engine.order_synchronizer import OrderSynchronizer, OrderExecutionResult  # type: ignore  # noqa: E402


class _FailingApi:
    def place_sell_order(self, *args, **kwargs):
        return {"success": False, "message": "모의투자 장종료 입니다."}


class _BuyTimeoutWithHoldingIncreaseApi:
    def __init__(self):
        self._holding_call_count = 0

    def get_holdings(self):
        self._holding_call_count += 1
        # 1st call: 주문 전 무보유, 2nd call: 주문 후 1주 보유
        if self._holding_call_count == 1:
            return []
        return [{"stock_code": "024060", "qty": 1, "avg_price": 21900.0}]

    def place_buy_order(self, *args, **kwargs):
        return {"success": True, "order_no": "0000014023", "message": "ok"}

    def wait_for_execution(self, *args, **kwargs):
        return {
            "success": False,
            "status": "TIMEOUT",
            "exec_qty": 0,
            "exec_price": 0.0,
            "message": "타임아웃 - 마지막 확인 체결수량: 0주",
            "fills": [],
        }


class TestOrderSynchronizerFailedState(unittest.TestCase):
    def test_sell_send_failure_marks_failed_state(self):
        syncer = OrderSynchronizer(api=_FailingApi())
        statuses = []

        syncer._get_order_state = lambda _k: None  # type: ignore

        def _capture_upsert(**kwargs):
            statuses.append(kwargs.get("status"))

        syncer._upsert_order_state = _capture_upsert  # type: ignore
        result = syncer.execute_sell_order(
            stock_code="005930",
            quantity=1,
            signal_id="unit-test",
            skip_market_check=True,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.result_type, OrderExecutionResult.FAILED)
        self.assertEqual(statuses, ["PENDING", "FAILED"])

    def test_buy_timeout_reconciles_from_holdings_delta(self):
        syncer = OrderSynchronizer(api=_BuyTimeoutWithHoldingIncreaseApi())
        syncer.mode = "PAPER"
        statuses = []

        syncer._get_order_state = lambda _k: None  # type: ignore

        def _capture_upsert(**kwargs):
            statuses.append(kwargs.get("status"))

        syncer._upsert_order_state = _capture_upsert  # type: ignore

        result = syncer.execute_buy_order(
            stock_code="024060",
            quantity=1,
            signal_id="unit-test-buy-timeout-holding",
            skip_market_check=True,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.result_type, OrderExecutionResult.SUCCESS)
        self.assertEqual(result.exec_qty, 1)
        self.assertGreater(float(result.exec_price), 0.0)
        self.assertIn("보유수량 변동", result.message)
        self.assertEqual(statuses, ["PENDING", "SUBMITTED", "FILLED"])

    def test_buy_timeout_reconciles_from_holdings_delta_in_real_mode(self):
        syncer = OrderSynchronizer(api=_BuyTimeoutWithHoldingIncreaseApi())
        syncer.mode = "REAL"
        statuses = []

        syncer._get_order_state = lambda _k: None  # type: ignore

        def _capture_upsert(**kwargs):
            statuses.append(kwargs.get("status"))

        syncer._upsert_order_state = _capture_upsert  # type: ignore

        result = syncer.execute_buy_order(
            stock_code="024060",
            quantity=1,
            signal_id="unit-test-buy-timeout-holding-real",
            skip_market_check=True,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.result_type, OrderExecutionResult.SUCCESS)
        self.assertEqual(result.exec_qty, 1)
        self.assertGreater(float(result.exec_price), 0.0)
        self.assertIn("보유수량 변동", result.message)
        self.assertEqual(statuses, ["PENDING", "SUBMITTED", "FILLED"])


if __name__ == "__main__":
    unittest.main()
