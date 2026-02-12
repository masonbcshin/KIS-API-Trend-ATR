import unittest
import sys
from pathlib import Path
import types
import datetime as dt


def _ensure_fake_dependencies() -> None:
    if "pytz" not in sys.modules:
        class _FakeKST(dt.tzinfo):
            def utcoffset(self, _dt):
                return dt.timedelta(hours=9)

            def dst(self, _dt):
                return dt.timedelta(0)

            def tzname(self, _dt):
                return "KST"

        fake_pytz = types.ModuleType("pytz")
        fake_pytz.timezone = lambda _name: _FakeKST()
        sys.modules["pytz"] = fake_pytz

    if "pandas" not in sys.modules:
        fake_pd = types.ModuleType("pandas")
        fake_pd.DataFrame = object
        fake_pd.Series = object
        sys.modules["pandas"] = fake_pd

    if "numpy" not in sys.modules:
        fake_np = types.ModuleType("numpy")
        fake_np.nan = float("nan")
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
from engine.risk_manager import RiskManager


class TestRiskManagerAccountSnapshot(unittest.TestCase):
    def test_account_snapshot_is_reflected_in_status(self):
        rm = RiskManager(
            enable_kill_switch=False,
            daily_max_loss_percent=3.0,
            starting_capital=10_000_000,
        )

        rm.update_account_snapshot(
            {
                "success": True,
                "holdings": [{"stock_code": "005930", "quantity": 3}],
                "total_eval": 10_145_000,
                "cash_balance": 9_000_000,
                "total_pnl": 1451,
            }
        )

        status = rm.get_status()
        self.assertIn("account_snapshot", status)
        account = status["account_snapshot"]
        self.assertIsNotNone(account)
        self.assertEqual(account["holdings_count"], 1)
        self.assertAlmostEqual(account["total_pnl"], 1451.0)


if __name__ == "__main__":
    unittest.main()
