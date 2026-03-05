import datetime as dt
import io
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
    candidates = []
    max_positions = 10
    out_of_universe_warn_days = 20
    out_of_universe_reduce_days = 30
    selection_method = "combined"
    selection_meta = {}

    def __init__(self, yaml_path, kis_client):
        self.yaml_path = yaml_path
        self.kis_client = kis_client
        self.policy = types.SimpleNamespace(
            selection_method=self.__class__.selection_method,
            cache_file=Path("/tmp/universe_cache.json"),
            max_positions=self.__class__.max_positions,
            out_of_universe_warn_days=self.__class__.out_of_universe_warn_days,
            out_of_universe_reduce_days=self.__class__.out_of_universe_reduce_days,
        )

    def load_holdings_symbols(self):
        return list(self.__class__.holdings)

    def get_or_create_todays_universe(self, trade_date):
        return list(self.__class__.universe)

    def compute_entry_candidates(self, holdings, todays_universe):
        h = set(holdings)
        return [s for s in todays_universe if s not in h]

    def get_todays_universe_snapshot(self, trade_date):
        candidates = list(self.__class__.candidates or self.__class__.universe)
        finals = list(self.__class__.universe)
        return {
            "trade_date": trade_date,
            "selection_method": self.policy.selection_method,
            "candidate_symbols": candidates,
            "universe_symbols": finals,
            "selection_meta": dict(self.__class__.selection_meta or {}),
        }

    @staticmethod
    def compute_entry_capacity(holdings, max_positions):
        return max(int(max_positions) - len(set(holdings or [])), 0)

    @staticmethod
    def limit_entry_candidates(entry_candidates, capacity):
        cap = max(int(capacity), 0)
        return list(entry_candidates[:cap])

    @staticmethod
    def compute_out_of_universe_ages(
        previous_ages, holdings, todays_universe, advance_day=True, advance_days=None
    ):
        universe_set = set(todays_universe or [])
        increment = int(advance_days) if advance_days is not None else (1 if advance_day else 0)
        increment = max(increment, 0)
        out = {}
        for symbol in holdings or []:
            if symbol in universe_set:
                out[symbol] = 0
            else:
                prev = int((previous_ages or {}).get(symbol, 0))
                out[symbol] = prev + increment if increment > 0 else prev
        return out

    @staticmethod
    def summarize_out_of_universe_aging(ages, warn_days, reduce_days):
        out_map = {k: int(v) for k, v in dict(ages or {}).items() if int(v) > 0}
        warn = [k for k, v in sorted(out_map.items(), key=lambda item: (-item[1], item[0])) if int(v) >= int(warn_days)]
        reduce = [k for k, v in sorted(out_map.items(), key=lambda item: (-item[1], item[0])) if int(v) >= int(reduce_days)]
        return {
            "tracked_count": len(dict(ages or {})),
            "out_of_universe_count": len(out_map),
            "warn_count": len(warn),
            "reduce_count": len(reduce),
            "warn_symbols": warn,
            "reduce_symbols": reduce,
            "out_of_universe_days": out_map,
        }

    @staticmethod
    def count_business_day_advances(previous_trade_date, current_trade_date):
        return 1 if str(previous_trade_date) != str(current_trade_date) else 0


class _DummyExecutor:
    created_symbols = []
    run_once_calls = []
    entry_controls = []
    holdings_symbols = set()
    restore_calls = []

    def __init__(self, *args, **kwargs):
        symbol = kwargs["stock_code"]
        self.stock_code = symbol
        self.strategy = types.SimpleNamespace(has_position=symbol in self.__class__.holdings_symbols)
        self.__class__.created_symbols.append(symbol)

    def set_entry_control(self, allow_entry, reason):
        self.__class__.entry_controls.append((self.stock_code, allow_entry, reason))

    def restore_position_on_start(self):
        self.__class__.restore_calls.append(self.stock_code)
        return self.strategy.has_position

    def run_once(self):
        self.__class__.run_once_calls.append(self.stock_code)

    def get_daily_summary(self):
        return {"total_trades": 0, "total_pnl": 0}

    def _save_position_on_exit(self):
        return None


class _DummyRiskManager:
    @staticmethod
    def check_kill_switch():
        return types.SimpleNamespace(passed=True, should_exit=False, reason="")


class _DummyNotifier:
    def __init__(self):
        self.enabled = True
        self.info_messages = []
        self.warning_messages = []

    def notify_info(self, message):
        self.info_messages.append(str(message))
        return True

    def notify_warning(self, message):
        self.warning_messages.append(str(message))
        return True


class _DummyPositionStoreNoState:
    def __init__(self, file_path):
        self.file_path = file_path

    def _load_raw_data(self):
        return {}


class _DummyPositionStoreMismatchedState:
    def __init__(self, file_path):
        self.file_path = file_path

    def _load_raw_data(self):
        return {
            "position": {
                "stock_code": "233740",
                "quantity": 1,
            }
        }


class TestMainMultidayUniversePolicy(unittest.TestCase):
    def setUp(self):
        _DummyExecutor.created_symbols = []
        _DummyExecutor.run_once_calls = []
        _DummyExecutor.entry_controls = []
        _DummyExecutor.holdings_symbols = set()
        _DummyExecutor.restore_calls = []
        _DummyUniverseService.candidates = []
        _DummyUniverseService.out_of_universe_warn_days = 20
        _DummyUniverseService.out_of_universe_reduce_days = 30
        _DummyUniverseService.selection_method = "combined"
        _DummyUniverseService.selection_meta = {}

    def test_holdings_are_managed_even_if_not_in_todays_universe(self):
        _DummyUniverseService.holdings = ["999999"]
        _DummyUniverseService.universe = ["111111"]
        _DummyUniverseService.max_positions = 10
        _DummyExecutor.holdings_symbols = {"999999"}

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "UniverseService", _DummyUniverseService), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "get_telegram_notifier", return_value=_DummyNotifier()), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday, "create_risk_manager_from_settings", return_value=_DummyRiskManager()), \
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
             patch.object(main_multiday, "get_telegram_notifier", return_value=_DummyNotifier()), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday, "create_risk_manager_from_settings", return_value=_DummyRiskManager()), \
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

    def test_entry_capacity_cutoff_allows_only_top_ranked_candidates(self):
        _DummyUniverseService.holdings = ["999999"]
        _DummyUniverseService.universe = ["111111", "222222", "333333"]
        _DummyUniverseService.max_positions = 2
        _DummyExecutor.holdings_symbols = {"999999"}

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "UniverseService", _DummyUniverseService), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "get_telegram_notifier", return_value=_DummyNotifier()), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday, "create_risk_manager_from_settings", return_value=_DummyRiskManager()), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        controls = {symbol: (allow, reason) for symbol, allow, reason in _DummyExecutor.entry_controls}
        self.assertTrue(controls["111111"][0])
        self.assertFalse(controls["222222"][0])
        self.assertFalse(controls["333333"][0])
        self.assertIn("capacity cutoff", controls["222222"][1])
        self.assertIn("capacity cutoff", controls["333333"][1])

    def test_daily_universe_selection_is_notified_to_telegram(self):
        _DummyUniverseService.holdings = []
        _DummyUniverseService.universe = ["111111", "222222"]
        _DummyUniverseService.candidates = ["333333", "444444", "555555"]
        _DummyUniverseService.max_positions = 10
        notifier = _DummyNotifier()

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "UniverseService", _DummyUniverseService), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "get_telegram_notifier", return_value=notifier), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday, "create_risk_manager_from_settings", return_value=_DummyRiskManager()), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        self.assertEqual(len(notifier.info_messages), 2)
        self.assertIn("후보 3개", notifier.info_messages[0])
        self.assertIn("333333", notifier.info_messages[0])
        self.assertIn("444444", notifier.info_messages[0])
        self.assertIn("555555", notifier.info_messages[0])
        self.assertIn("최종 선정 2개", notifier.info_messages[1])
        self.assertIn("111111", notifier.info_messages[1])
        self.assertIn("222222", notifier.info_messages[1])
        self.assertEqual(len(notifier.warning_messages), 0)

    def test_universe_anomaly_stage_zero_sends_warning(self):
        _DummyUniverseService.holdings = []
        _DummyUniverseService.universe = ["111111", "222222"]
        _DummyUniverseService.selection_method = "combined"
        _DummyUniverseService.selection_meta = {
            "strategy": "combined",
            "stage1_count": 0,
            "stage2_count": 0,
        }
        _DummyUniverseService.max_positions = 10
        notifier = _DummyNotifier()

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "UniverseService", _DummyUniverseService), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "get_telegram_notifier", return_value=notifier), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday, "create_risk_manager_from_settings", return_value=_DummyRiskManager()), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        self.assertEqual(len(notifier.warning_messages), 1)
        self.assertIn("stage1", notifier.warning_messages[0])

    def test_out_of_universe_aging_warn_is_notified(self):
        _DummyUniverseService.holdings = ["999999"]
        _DummyUniverseService.universe = ["111111"]
        _DummyUniverseService.max_positions = 10
        _DummyUniverseService.out_of_universe_warn_days = 1
        _DummyUniverseService.out_of_universe_reduce_days = 3
        _DummyExecutor.holdings_symbols = {"999999"}
        notifier = _DummyNotifier()

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "UniverseService", _DummyUniverseService), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "get_telegram_notifier", return_value=notifier), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday, "create_risk_manager_from_settings", return_value=_DummyRiskManager()), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        self.assertTrue(any("AGING" in msg for msg in notifier.info_messages))

    def test_startup_restore_skips_non_holding_symbols_without_state(self):
        _DummyUniverseService.holdings = ["233740"]
        _DummyUniverseService.universe = ["233740", "005930", "069500"]
        _DummyUniverseService.max_positions = 10
        _DummyExecutor.holdings_symbols = {"233740"}

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "UniverseService", _DummyUniverseService), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "PositionStore", _DummyPositionStoreNoState), \
             patch.object(main_multiday, "get_telegram_notifier", return_value=_DummyNotifier()), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday, "create_risk_manager_from_settings", return_value=_DummyRiskManager()), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        self.assertEqual(_DummyExecutor.created_symbols, ["233740", "005930", "069500"])
        self.assertEqual(_DummyExecutor.restore_calls, ["233740"])

    def test_startup_restore_skips_mismatched_symbol_state(self):
        _DummyUniverseService.holdings = []
        _DummyUniverseService.universe = ["005930"]
        _DummyUniverseService.max_positions = 10
        _DummyExecutor.holdings_symbols = set()

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "UniverseService", _DummyUniverseService), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "PositionStore", _DummyPositionStoreMismatchedState), \
             patch.object(main_multiday, "get_telegram_notifier", return_value=_DummyNotifier()), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday, "create_risk_manager_from_settings", return_value=_DummyRiskManager()), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        self.assertEqual(_DummyExecutor.created_symbols, ["005930"])
        self.assertEqual(_DummyExecutor.restore_calls, [])

    def test_print_banner_uses_real_emoji(self):
        with patch.object(main_multiday.settings, "TRADING_MODE", "REAL"), \
             patch("sys.stdout", new_callable=io.StringIO) as fake_out:
            main_multiday.print_banner()
            output = fake_out.getvalue()
        self.assertIn("🔴 현재 모드:", output)


if __name__ == "__main__":
    unittest.main()
