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
        fake_pd.isna = lambda _x: False
        sys.modules["pandas"] = fake_pd

    if "numpy" not in sys.modules:
        fake_np = types.ModuleType("numpy")
        fake_np.nan = float("nan")
        fake_np.where = lambda cond, a, b: a
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
from engine.order_synchronizer import OrderSynchronizer  # type: ignore  # noqa: E402


class _DummyApi:
    pass


class _DummyDb:
    def __init__(self, affected=3):
        self.affected = affected
        self.last_command = None
        self.last_params = None

    def execute_command(self, command, params=None):
        self.last_command = command
        self.last_params = params
        return self.affected


class TestOrderSynchronizerStaleCleanup(unittest.TestCase):
    def test_cleanup_stale_pending_orders_updates_cancelled(self):
        syncer = OrderSynchronizer(api=_DummyApi())
        syncer._db = _DummyDb(affected=2)
        syncer.mode = "PAPER"
        affected = syncer._cleanup_stale_pending_orders()

        self.assertEqual(affected, 2)
        self.assertIn("UPDATE order_state", syncer._db.last_command)
        self.assertEqual(syncer._db.last_params[0], "PAPER")


if __name__ == "__main__":
    unittest.main()
