import datetime as dt
import sys
import types
import unittest
from types import SimpleNamespace
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


class _DummyMarketChecker:
    def is_tradeable(self):
        return False, "폐장 - 주문 불가"


class TestMultidayMarketClosedSkip(unittest.TestCase):
    def test_run_once_skips_entry_signal_generation_when_market_closed_and_no_position(self):
        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.trading_mode = "PAPER"
        ex.stock_code = "005930"
        ex.market_checker = _DummyMarketChecker()
        ex.risk_manager = SimpleNamespace(
            check_kill_switch=lambda: SimpleNamespace(passed=True, should_exit=False, reason="")
        )
        ex.strategy = SimpleNamespace(has_position=False)
        ex._last_market_closed_skip_log_at = None

        def _unexpected_fetch_data():
            raise AssertionError("fetch_market_data should not be called when market is closed and no position")

        ex.fetch_market_data = _unexpected_fetch_data

        result = ex.run_once()
        self.assertIn("market_closed_skip", result["error"])


if __name__ == "__main__":
    unittest.main()
