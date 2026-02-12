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


class _DummyStore:
    def __init__(self):
        self.pending = None
        self.save_calls = 0
        self.clear_calls = 0

    def save_pending_exit(self, pending):
        self.pending = pending
        self.save_calls += 1
        return True

    def clear_pending_exit(self):
        self.pending = None
        self.clear_calls += 1
        return True


class _DummyTelegram:
    def __init__(self):
        self.warning_calls = 0
        self.info_calls = 0

    def notify_warning(self, _msg):
        self.warning_calls += 1
        return True

    def notify_info(self, _msg):
        self.info_calls += 1
        return True


class _DummyMarketChecker:
    def __init__(self):
        self.tradeable = False
        self.reason = "장종료"

    def is_tradeable(self):
        return self.tradeable, self.reason


class TestPendingExitBackoff(unittest.TestCase):
    def _make_executor(self):
        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.stock_code = "005930"
        ex._pending_exit_backoff_minutes = 5
        ex._pending_exit_state = None
        ex.position_store = _DummyStore()
        ex.telegram = _DummyTelegram()
        ex.market_checker = _DummyMarketChecker()
        return ex

    def _make_signal(self):
        return SimpleNamespace(
            exit_reason=SimpleNamespace(value="GAP_PROTECTION"),
            reason_code="GAP_PROTECTION_TRIGGERED",
        )

    def test_pending_exit_blocks_repeated_market_closed_retries_and_retries_once_open(self):
        ex = self._make_executor()
        signal = self._make_signal()
        calls = {"sell": 0}

        def _sell_fail(_signal):
            calls["sell"] += 1
            return {"success": False, "message": "모의투자 장종료 입니다."}

        ex.execute_sell = _sell_fail

        # 1st loop: failure -> pending_exit activation
        first = ex._execute_exit_with_pending_control(signal)
        self.assertFalse(first["success"])
        self.assertEqual(calls["sell"], 1)
        self.assertIsNotNone(ex._pending_exit_state)
        self.assertEqual(ex.telegram.warning_calls, 1)

        # 2nd loop immediately: should skip actual order call due to backoff
        second = ex._execute_exit_with_pending_control(signal)
        self.assertFalse(second["success"])
        self.assertTrue(second.get("pending_exit"))
        self.assertEqual(calls["sell"], 1)

        # backoff elapsed and market open -> one retry, success, pending cleared
        ex._pending_exit_state["next_retry_at"] = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        ).astimezone().isoformat()
        ex.market_checker.tradeable = True

        def _sell_success(_signal):
            calls["sell"] += 1
            return {"success": True, "message": "ok"}

        ex.execute_sell = _sell_success
        third = ex._execute_exit_with_pending_control(signal)
        self.assertTrue(third["success"])
        self.assertEqual(calls["sell"], 2)
        self.assertIsNone(ex._pending_exit_state)
        self.assertGreaterEqual(ex.telegram.info_calls, 1)


if __name__ == "__main__":
    unittest.main()
