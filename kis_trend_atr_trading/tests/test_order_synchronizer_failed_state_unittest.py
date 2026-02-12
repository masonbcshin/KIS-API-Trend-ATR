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


if __name__ == "__main__":
    unittest.main()
