import datetime as dt
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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
        fake_pd.concat = lambda *_args, **_kwargs: []
        sys.modules["pandas"] = fake_pd

    if "numpy" not in sys.modules:
        fake_np = types.ModuleType("numpy")
        fake_np.where = lambda cond, a, b: a
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
        fake_requests.post = lambda *args, **kwargs: None
        fake_requests.get = lambda *args, **kwargs: None

        fake_exceptions = types.ModuleType("requests.exceptions")
        fake_exceptions.RequestException = Exception
        fake_exceptions.Timeout = Exception
        fake_exceptions.ConnectionError = Exception

        fake_requests.exceptions = fake_exceptions
        sys.modules["requests"] = fake_requests
        sys.modules["requests.exceptions"] = fake_exceptions

    if "yaml" not in sys.modules:
        fake_yaml = types.ModuleType("yaml")
        fake_yaml.safe_load = lambda *_args, **_kwargs: {}
        fake_yaml.safe_dump = lambda *_args, **_kwargs: ""
        sys.modules["yaml"] = fake_yaml


_ensure_fake_dependencies()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main_multiday  # type: ignore  # noqa: E402


class _DummyAPI:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def get_access_token(self):
        return "token"


class _DummySelector:
    def __init__(self):
        self.config = types.SimpleNamespace(selection_method="combined")
        self.cache_file = Path("/tmp/universe_cache.json")

    def select(self):
        return ["005930", "000660"]


class _DummyLock:
    is_acquired = False

    def release(self):
        return None


class _DummyExecutor:
    created_symbols = []
    run_once_calls = []

    def __init__(self, *args, **kwargs):
        self.stock_code = kwargs["stock_code"]
        _DummyExecutor.created_symbols.append(self.stock_code)

    def restore_position_on_start(self):
        return False

    def run_once(self):
        _DummyExecutor.run_once_calls.append(self.stock_code)

    def get_daily_summary(self):
        return {"total_trades": 0, "total_pnl": 0}

    def _save_position_on_exit(self):
        return None


class TestMainMultidayMultiSymbols(unittest.TestCase):
    def test_run_trade_executes_all_selected_symbols_once(self):
        _DummyExecutor.created_symbols = []
        _DummyExecutor.run_once_calls = []
        selector = _DummySelector()

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday.UniverseSelector, "from_yaml", return_value=selector), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        self.assertEqual(_DummyExecutor.created_symbols, ["005930", "000660"])
        self.assertEqual(_DummyExecutor.run_once_calls, ["005930", "000660"])


if __name__ == "__main__":
    unittest.main()
