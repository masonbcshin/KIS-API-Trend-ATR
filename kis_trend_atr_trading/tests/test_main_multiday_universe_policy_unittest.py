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


class _DummyLock:
    is_acquired = False

    def release(self):
        return None


class _DummyUniverseService:
    holdings = []
    universe = []
    max_positions = 10

    def __init__(self, yaml_path, kis_client):
        self.yaml_path = yaml_path
        self.kis_client = kis_client
        self.policy = types.SimpleNamespace(
            selection_method="combined",
            cache_file=Path("/tmp/universe_cache.json"),
            max_positions=self.__class__.max_positions,
        )

    def load_holdings_symbols(self):
        return list(self.__class__.holdings)

    def get_or_create_todays_universe(self, trade_date):
        return list(self.__class__.universe)

    def compute_entry_candidates(self, holdings, todays_universe):
        h = set(holdings)
        return [s for s in todays_universe if s not in h]


class _DummyExecutor:
    created_symbols = []
    run_once_calls = []
    entry_controls = []
    holdings_symbols = set()

    def __init__(self, *args, **kwargs):
        symbol = kwargs["stock_code"]
        self.stock_code = symbol
        self.strategy = types.SimpleNamespace(has_position=symbol in self.__class__.holdings_symbols)
        self.__class__.created_symbols.append(symbol)

    def set_entry_control(self, allow_entry, reason):
        self.__class__.entry_controls.append((self.stock_code, allow_entry, reason))

    def restore_position_on_start(self):
        return self.strategy.has_position

    def run_once(self):
        self.__class__.run_once_calls.append(self.stock_code)

    def get_daily_summary(self):
        return {"total_trades": 0, "total_pnl": 0}

    def _save_position_on_exit(self):
        return None


class TestMainMultidayUniversePolicy(unittest.TestCase):
    def setUp(self):
        _DummyExecutor.created_symbols = []
        _DummyExecutor.run_once_calls = []
        _DummyExecutor.entry_controls = []
        _DummyExecutor.holdings_symbols = set()

    def test_holdings_are_managed_even_if_not_in_todays_universe(self):
        _DummyUniverseService.holdings = ["999999"]
        _DummyUniverseService.universe = ["111111"]
        _DummyUniverseService.max_positions = 10
        _DummyExecutor.holdings_symbols = {"999999"}

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "UniverseService", _DummyUniverseService), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday, "create_risk_manager_from_settings", return_value=object()), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        self.assertEqual(_DummyExecutor.created_symbols, ["999999", "111111"])
        self.assertEqual(_DummyExecutor.run_once_calls, ["999999", "111111"])

    def test_entry_is_blocked_when_max_positions_reached(self):
        _DummyUniverseService.holdings = ["999999"]
        _DummyUniverseService.universe = ["111111"]
        _DummyUniverseService.max_positions = 1
        _DummyExecutor.holdings_symbols = {"999999"}

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "UniverseService", _DummyUniverseService), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday, "create_risk_manager_from_settings", return_value=object()), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        blocked = [x for x in _DummyExecutor.entry_controls if x[0] == "111111"]
        self.assertTrue(blocked)
        self.assertFalse(blocked[-1][1])
        self.assertIn("max_positions reached", blocked[-1][2])


if __name__ == "__main__":
    unittest.main()
