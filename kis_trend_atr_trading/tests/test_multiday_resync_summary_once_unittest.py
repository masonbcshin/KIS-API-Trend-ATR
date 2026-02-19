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
        fake_exceptions = types.ModuleType("requests.exceptions")
        fake_exceptions.RequestException = Exception
        fake_exceptions.Timeout = Exception
        fake_exceptions.ConnectionError = Exception
        fake_requests.exceptions = fake_exceptions
        fake_requests.Session = object
        fake_requests.Response = object
        sys.modules["requests"] = fake_requests
        sys.modules["requests.exceptions"] = fake_exceptions


_ensure_fake_dependencies()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.multiday_executor import MultidayExecutor  # type: ignore  # noqa: E402


class _DummyPosResync:
    def synchronize_on_startup(self):
        return {
            "success": True,
            "position": None,
            "action": "NO_POSITION",
            "warnings": [],
            "recoveries": [],
            "allow_new_entries": True,
            "summary": {"total_holdings": 2, "updated": 2, "created": 0, "zombies": 0},
            "holdings": [
                {"stock_code": "000660", "qty": 1, "avg_price": "897000"},
                {"stock_code": "005930", "qty": 3, "avg_price": "178116.3"},
            ],
        }


class _DummyTelegram:
    def __init__(self):
        self.info_messages = []

    def notify_warning(self, _message):
        return True

    def notify_error(self, *_args, **_kwargs):
        return True

    def notify_info(self, message):
        self.info_messages.append(message)
        return True


class TestMultidayResyncSummaryOnce(unittest.TestCase):
    def test_summary_notified_once_and_avg_has_comma(self):
        MultidayExecutor._startup_resync_summary_notified = False
        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.position_resync = _DummyPosResync()
        ex.telegram = _DummyTelegram()
        ex.stock_code = "005930"
        ex._entry_allowed = True
        ex._entry_block_reason = ""
        ex._pending_exit_state = None
        ex.order_synchronizer = types.SimpleNamespace(recover_pending_orders=lambda: [])
        ex._drop_pending_exit_state = lambda _reason: None
        ex._calculate_holding_days = lambda _entry_date: 1
        ex.strategy = types.SimpleNamespace(has_position=False, restore_position=lambda _p: None)
        ex.position_store = types.SimpleNamespace(clear_position=lambda: None, save_pending_exit=lambda _p: None)

        with patch.object(MultidayExecutor, "_pending_recovery_done", False):
            with patch.object(MultidayExecutor, "_pending_recovery_count", 0):
                assert ex.restore_position_on_start() is False
                assert ex.restore_position_on_start() is False

        assert len(ex.telegram.info_messages) == 1
        msg = ex.telegram.info_messages[0]
        assert "복원 완료:" in msg
        assert "avg=897,000.00원" in msg
        assert "avg=178,116.30원" in msg


if __name__ == "__main__":
    unittest.main()
