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


class TestMultidayRiskStartCapital(unittest.TestCase):
    def test_sync_starting_capital_only_once_per_day(self):
        captured = []
        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.risk_manager = SimpleNamespace(set_starting_capital=lambda v: captured.append(v))
        ex._report_mode = "PAPER"
        ex._risk_start_capital_sync_date = None
        ex._risk_start_capital_synced = False

        ex._sync_risk_starting_capital_from_equity(12_345_678, "TEST")
        ex._sync_risk_starting_capital_from_equity(22_222_222, "TEST")

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0], 12_345_678)

    def test_sync_starting_capital_resets_on_new_day(self):
        captured = []
        ex = MultidayExecutor.__new__(MultidayExecutor)
        ex.risk_manager = SimpleNamespace(set_starting_capital=lambda v: captured.append(v))
        ex._report_mode = "PAPER"
        ex._risk_start_capital_sync_date = "1900-01-01"
        ex._risk_start_capital_synced = True

        ex._sync_risk_starting_capital_from_equity(15_000_000, "TEST")

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0], 15_000_000)

    def test_build_dry_run_virtual_snapshot_uses_virtual_account_when_available(self):
        ex = MultidayExecutor.__new__(MultidayExecutor)

        fake_pkg = types.ModuleType("cbt")
        fake_va_mod = types.ModuleType("cbt.virtual_account")

        class _FakeVirtualAccount:
            def __init__(self, load_existing=True):
                self.load_existing = load_existing

            def get_account_summary(self):
                return {
                    "total_equity": 11_100_000,
                    "cash": 9_900_000,
                    "total_pnl": 1_100_000,
                    "has_position": True,
                }

        fake_va_mod.VirtualAccount = _FakeVirtualAccount

        old_pkg = sys.modules.get("cbt")
        old_mod = sys.modules.get("cbt.virtual_account")
        sys.modules["cbt"] = fake_pkg
        sys.modules["cbt.virtual_account"] = fake_va_mod
        try:
            snapshot = ex._build_dry_run_virtual_snapshot()
        finally:
            if old_pkg is not None:
                sys.modules["cbt"] = old_pkg
            else:
                sys.modules.pop("cbt", None)
            if old_mod is not None:
                sys.modules["cbt.virtual_account"] = old_mod
            else:
                sys.modules.pop("cbt.virtual_account", None)

        self.assertTrue(snapshot["success"])
        self.assertEqual(snapshot["total_eval"], 11_100_000)
        self.assertEqual(snapshot["cash_balance"], 9_900_000)
        self.assertEqual(snapshot["total_pnl"], 1_100_000)
        self.assertEqual(len(snapshot["holdings"]), 1)


if __name__ == "__main__":
    unittest.main()
