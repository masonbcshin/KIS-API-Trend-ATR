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
    risk_manager_ids = []

    def __init__(self, *args, **kwargs):
        self.stock_code = kwargs["stock_code"]
        self.strategy = types.SimpleNamespace(has_position=False)
        _DummyExecutor.created_symbols.append(self.stock_code)
        _DummyExecutor.risk_manager_ids.append(id(kwargs.get("risk_manager")))

    def set_entry_control(self, allow_entry, reason):
        return None

    def restore_position_on_start(self):
        return False

    def set_market_regime_snapshot(self, snapshot):
        return None

    def run_once(self):
        _DummyExecutor.run_once_calls.append(self.stock_code)

    def get_daily_summary(self):
        return {"total_trades": 0, "total_pnl": 0}

    def _save_position_on_exit(self):
        return None


class _DummyMarketRegimeWorker:
    started = 0
    joined = 0
    status_sequence = []
    status = {
        "snapshot": None,
        "refresh_state": "bootstrap_pending",
        "market_regime_background_refresh_ms": 0.0,
        "market_regime_daily_context_refresh_ms": 0.0,
        "market_regime_intraday_guard_ms": 0.0,
        "market_regime_quote_source": "skip",
        "market_regime_quote_state": "absent",
        "market_regime_daily_context_state": "absent",
        "market_regime_background_last_success_age_sec": -1.0,
        "market_regime_background_refresh_fail_count": 0,
        "market_regime_worker_error_streak": 0,
        "market_regime_worker_heartbeat_age_sec": 0.0,
        "market_regime_worker_error_state": "",
    }

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self._alive = False
        if self.__class__.status_sequence:
            self._status = dict(self.__class__.status_sequence.pop(0))
        else:
            self._status = dict(self.__class__.status)

    def start(self):
        self.__class__.started += 1
        self._alive = True

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.__class__.joined += 1
        self._alive = False

    def get_status(self, now=None):
        return dict(self._status)


class TestMainMultidayMultiSymbols(unittest.TestCase):
    def test_run_trade_executes_all_selected_symbols_once(self):
        _DummyExecutor.created_symbols = []
        _DummyExecutor.run_once_calls = []
        _DummyExecutor.risk_manager_ids = []
        selector = _DummySelector()

        with self.assertLogs("main", level="INFO") as captured:
            with patch.object(main_multiday, "KISApi", _DummyAPI), \
                 patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
                 patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
                 patch.object(main_multiday.UniverseSelector, "from_yaml", return_value=selector), \
                 patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
                 patch.object(
                     main_multiday,
                     "get_market_session_state",
                     return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
                 ), \
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
        self.assertEqual(len(set(_DummyExecutor.risk_manager_ids)), 1)
        self.assertTrue(any("[LOOP_METRIC]" in line for line in captured.output))

    def test_market_regime_background_mode_keeps_main_loop_read_only(self):
        _DummyExecutor.created_symbols = []
        _DummyExecutor.run_once_calls = []
        _DummyExecutor.risk_manager_ids = []
        _DummyMarketRegimeWorker.started = 0
        _DummyMarketRegimeWorker.joined = 0
        _DummyMarketRegimeWorker.status_sequence = []
        _DummyMarketRegimeWorker.status = {
            "snapshot": None,
            "refresh_state": "bootstrap_pending",
            "market_regime_background_refresh_ms": 12.0,
            "market_regime_daily_context_refresh_ms": 5.0,
            "market_regime_intraday_guard_ms": 1.0,
            "market_regime_quote_source": "skip",
            "market_regime_quote_state": "absent",
            "market_regime_daily_context_state": "fresh",
            "market_regime_background_last_success_age_sec": -1.0,
            "market_regime_background_refresh_fail_count": 0,
            "market_regime_worker_error_streak": 0,
            "market_regime_worker_heartbeat_age_sec": 0.0,
            "market_regime_worker_error_state": "",
        }
        selector = _DummySelector()

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "MarketRegimeRefreshThread", _DummyMarketRegimeWorker), \
             patch.object(main_multiday, "refresh_shared_market_regime_snapshot", side_effect=AssertionError("sync refresh should not be called")), \
             patch.object(main_multiday.UniverseSelector, "from_yaml", return_value=selector), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday.settings, "ENABLE_MARKET_REGIME_FILTER", True), \
             patch.object(main_multiday.settings, "ENABLE_MARKET_REGIME_REFRESH_THREAD", True), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        self.assertEqual(_DummyMarketRegimeWorker.started, 1)
        self.assertEqual(_DummyMarketRegimeWorker.joined, 1)

    def test_market_regime_worker_error_does_not_trigger_sync_refresh_fallback(self):
        _DummyExecutor.created_symbols = []
        _DummyExecutor.run_once_calls = []
        _DummyExecutor.risk_manager_ids = []
        _DummyMarketRegimeWorker.started = 0
        _DummyMarketRegimeWorker.joined = 0
        _DummyMarketRegimeWorker.status_sequence = []
        _DummyMarketRegimeWorker.status = {
            "snapshot": None,
            "refresh_state": "refresh_fail",
            "market_regime_background_refresh_ms": 30.0,
            "market_regime_daily_context_refresh_ms": 0.0,
            "market_regime_intraday_guard_ms": 0.0,
            "market_regime_quote_source": "skip",
            "market_regime_quote_state": "absent",
            "market_regime_daily_context_state": "absent",
            "market_regime_background_last_success_age_sec": -1.0,
            "market_regime_background_refresh_fail_count": 3,
            "market_regime_worker_error_streak": 1,
            "market_regime_worker_heartbeat_age_sec": 0.0,
            "market_regime_worker_error_state": "boom",
        }
        selector = _DummySelector()

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "MarketRegimeRefreshThread", _DummyMarketRegimeWorker), \
             patch.object(main_multiday, "refresh_shared_market_regime_snapshot", side_effect=AssertionError("sync refresh should not be called")), \
             patch.object(main_multiday.UniverseSelector, "from_yaml", return_value=selector), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday.settings, "ENABLE_MARKET_REGIME_FILTER", True), \
             patch.object(main_multiday.settings, "ENABLE_MARKET_REGIME_REFRESH_THREAD", True), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        self.assertEqual(_DummyMarketRegimeWorker.started, 1)
        self.assertEqual(_DummyMarketRegimeWorker.joined, 1)

    def test_market_regime_worker_auto_restarts_on_error_streak_without_sync_fallback(self):
        _DummyExecutor.created_symbols = []
        _DummyExecutor.run_once_calls = []
        _DummyExecutor.risk_manager_ids = []
        _DummyMarketRegimeWorker.started = 0
        _DummyMarketRegimeWorker.joined = 0
        _DummyMarketRegimeWorker.status_sequence = [
            {
                "snapshot": None,
                "refresh_state": "refresh_fail",
                "market_regime_background_refresh_ms": 30.0,
                "market_regime_daily_context_refresh_ms": 0.0,
                "market_regime_intraday_guard_ms": 0.0,
                "market_regime_quote_source": "skip",
                "market_regime_quote_state": "absent",
                "market_regime_daily_context_state": "absent",
                "market_regime_background_last_success_age_sec": -1.0,
                "market_regime_background_refresh_fail_count": 3,
                "market_regime_worker_error_streak": 3,
                "market_regime_worker_heartbeat_age_sec": 1.0,
                "market_regime_worker_error_state": "boom",
            },
            {
                "snapshot": None,
                "refresh_state": "bootstrap_pending",
                "market_regime_background_refresh_ms": 0.0,
                "market_regime_daily_context_refresh_ms": 0.0,
                "market_regime_intraday_guard_ms": 0.0,
                "market_regime_quote_source": "skip",
                "market_regime_quote_state": "absent",
                "market_regime_daily_context_state": "fresh",
                "market_regime_background_last_success_age_sec": -1.0,
                "market_regime_background_refresh_fail_count": 0,
                "market_regime_worker_error_streak": 0,
                "market_regime_worker_heartbeat_age_sec": 0.0,
                "market_regime_worker_error_state": "",
            },
        ]
        selector = _DummySelector()

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday, "MarketRegimeRefreshThread", _DummyMarketRegimeWorker), \
             patch.object(main_multiday, "refresh_shared_market_regime_snapshot", side_effect=AssertionError("sync refresh should not be called")), \
             patch.object(main_multiday.UniverseSelector, "from_yaml", return_value=selector), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday.settings, "ENABLE_MARKET_REGIME_FILTER", True), \
             patch.object(main_multiday.settings, "ENABLE_MARKET_REGIME_REFRESH_THREAD", True), \
             patch.object(main_multiday.settings, "ENABLE_MARKET_REGIME_WORKER_AUTO_RESTART", True), \
             patch.object(main_multiday.settings, "MARKET_REGIME_WORKER_RESTART_ERROR_THRESHOLD", 3), \
             patch.object(main_multiday.settings, "MARKET_REGIME_WORKER_RESTART_BASE_BACKOFF_SEC", 5.0), \
             patch.object(main_multiday.settings, "MARKET_REGIME_WORKER_RESTART_MAX_BACKOFF_SEC", 60.0), \
             patch.object(main_multiday.settings, "MARKET_REGIME_WORKER_STALL_SEC", 120.0), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        self.assertEqual(_DummyMarketRegimeWorker.started, 2)
        self.assertEqual(_DummyMarketRegimeWorker.joined, 2)

    def test_market_regime_sync_refresh_path_remains_when_background_flag_is_off(self):
        _DummyExecutor.created_symbols = []
        _DummyExecutor.run_once_calls = []
        _DummyExecutor.risk_manager_ids = []
        _DummyMarketRegimeWorker.status_sequence = []
        selector = _DummySelector()
        refresh_calls = {"count": 0}

        def _refresh(*args, **kwargs):
            refresh_calls["count"] += 1
            return types.SimpleNamespace(
                snapshot=None,
                total_refresh_elapsed_sec=0.0,
                refreshed=False,
                budget_exceeded=False,
                error=None,
                refresh_skipped_reason="unit_test_sync_path",
            )

        with patch.object(main_multiday, "KISApi", _DummyAPI), \
             patch.object(main_multiday, "MultidayExecutor", _DummyExecutor), \
             patch.object(main_multiday, "MultidayTrendATRStrategy", lambda: object()), \
             patch.object(main_multiday.UniverseSelector, "from_yaml", return_value=selector), \
             patch.object(main_multiday, "refresh_shared_market_regime_snapshot", side_effect=_refresh), \
             patch.object(main_multiday, "get_trading_mode", return_value="PAPER"), \
             patch.object(
                 main_multiday,
                 "get_market_session_state",
                 return_value=(main_multiday.MarketSessionState.IN_SESSION, "regular_session_open"),
             ), \
             patch.object(main_multiday, "get_instance_lock", return_value=_DummyLock()), \
             patch.object(main_multiday.settings, "ENABLE_MARKET_REGIME_FILTER", True), \
             patch.object(main_multiday.settings, "ENABLE_MARKET_REGIME_REFRESH_THREAD", False), \
             patch.object(main_multiday.settings, "validate_settings", return_value=True), \
             patch.object(main_multiday.settings, "get_settings_summary", return_value=""):
            main_multiday.run_trade(
                stock_code=main_multiday.settings.DEFAULT_STOCK_CODE,
                interval=0,
                max_runs=1,
            )

        self.assertGreater(refresh_calls["count"], 0)


if __name__ == "__main__":
    unittest.main()
