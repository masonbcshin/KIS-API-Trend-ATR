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
        sys.modules["pandas"] = fake_pd

    if "numpy" not in sys.modules:
        fake_np = types.ModuleType("numpy")
        fake_np.nan = float("nan")
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
from engine.order_synchronizer import PositionResynchronizer  # type: ignore  # noqa: E402
from utils.position_store import StoredPosition  # type: ignore  # noqa: E402


class _DummyApi:
    def __init__(self, holdings=None):
        self.holdings = holdings or []
        self.token_calls = 0
        self.balance_calls = 0

    def get_access_token(self):
        self.token_calls += 1
        return "dummy-token"

    def get_account_balance(self):
        self.balance_calls += 1
        return {"success": True, "holdings": self.holdings}


class _DummyStore:
    def __init__(self, stored=None):
        self._stored = stored
        self.cleared = False

    def load_position(self):
        return self._stored

    def clear_position(self):
        self.cleared = True
        self._stored = None

    def save_position(self, position):
        self._stored = position


class TestPositionResynchronizerModes(unittest.TestCase):
    def test_paper_mode_sync_uses_api_on_startup(self):
        api = _DummyApi(holdings=[])
        store = _DummyStore(stored=None)
        syncer = PositionResynchronizer(
            api=api,
            position_store=store,
            db_repository=None,
            trading_mode="PAPER",
        )

        result = syncer.synchronize_on_startup()

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "NO_POSITION")
        self.assertEqual(api.token_calls, 1)
        self.assertEqual(api.balance_calls, 1)

    def test_paper_mode_force_sync_not_skipped(self):
        api = _DummyApi(
            holdings=[
                {"stock_code": "005930", "quantity": 2, "avg_price": 70000, "current_price": 70500}
            ]
        )
        store = _DummyStore(stored=None)
        syncer = PositionResynchronizer(
            api=api,
            position_store=store,
            db_repository=None,
            trading_mode="PAPER",
        )

        result = syncer.force_sync_from_api()

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "SYNCED")
        self.assertEqual(len(result["holdings"]), 1)
        self.assertEqual(api.token_calls, 1)
        self.assertEqual(api.balance_calls, 1)

    def test_cbt_mode_startup_sync_does_not_call_api(self):
        api = _DummyApi(holdings=[])
        store = _DummyStore(stored=None)
        syncer = PositionResynchronizer(
            api=api,
            position_store=store,
            db_repository=None,
            trading_mode="CBT",
        )

        result = syncer.synchronize_on_startup()

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "NO_POSITION")
        self.assertEqual(api.token_calls, 0)
        self.assertEqual(api.balance_calls, 0)

    def test_real_mode_auto_recovers_from_api_when_stored_missing(self):
        api = _DummyApi(
            holdings=[
                {"stock_code": "005930", "quantity": 3, "avg_price": 71000, "current_price": 71500}
            ]
        )
        store = _DummyStore(stored=None)
        syncer = PositionResynchronizer(
            api=api,
            position_store=store,
            db_repository=None,
            trading_mode="REAL",
            target_symbol="005930",
        )

        result = syncer.synchronize_on_startup()

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "AUTO_RECOVERED_FROM_API")
        self.assertIsNotNone(result["position"])
        self.assertEqual(result["position"].stock_code, "005930")
        self.assertEqual(result["position"].quantity, 3)
        self.assertTrue(result["recoveries"])

    def test_real_mode_auto_clears_stale_stored_position(self):
        stale = StoredPosition(
            stock_code="005930",
            entry_price=70000,
            quantity=1,
            stop_loss=68000,
            take_profit=73000,
            entry_date="2026-02-01",
            atr_at_entry=1200,
        )
        api = _DummyApi(holdings=[])
        store = _DummyStore(stored=stale)
        syncer = PositionResynchronizer(
            api=api,
            position_store=store,
            db_repository=None,
            trading_mode="REAL",
            target_symbol="005930",
        )

        result = syncer.synchronize_on_startup()

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "AUTO_RECOVERED_CLEARED")
        self.assertTrue(store.cleared)
        self.assertIsNone(store.load_position())

    def test_real_mode_auto_replaces_mismatched_symbol(self):
        stale = StoredPosition(
            stock_code="000660",
            entry_price=120000,
            quantity=1,
            stop_loss=115000,
            take_profit=130000,
            entry_date="2026-02-01",
            atr_at_entry=2500,
        )
        api = _DummyApi(
            holdings=[
                {"stock_code": "005930", "quantity": 2, "avg_price": 70500, "current_price": 71000}
            ]
        )
        store = _DummyStore(stored=stale)
        syncer = PositionResynchronizer(
            api=api,
            position_store=store,
            db_repository=None,
            trading_mode="REAL",
            target_symbol="005930",
        )

        result = syncer.synchronize_on_startup()

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "AUTO_RECOVERED_REPLACED")
        self.assertEqual(result["position"].stock_code, "005930")
        self.assertEqual(result["position"].quantity, 2)
        self.assertTrue(result["recoveries"])

    def test_real_mode_aggregates_duplicate_rows_for_same_symbol_quantity(self):
        stale = StoredPosition(
            stock_code="005930",
            entry_price=70000,
            quantity=1,
            stop_loss=68000,
            take_profit=73000,
            entry_date="2026-02-01",
            atr_at_entry=1200,
        )
        api = _DummyApi(
            holdings=[
                {"stock_code": "005930", "quantity": 1, "avg_price": 70500, "current_price": 71000},
                {"stock_code": "005930", "quantity": 2, "avg_price": 70600, "current_price": 71100},
            ]
        )
        store = _DummyStore(stored=stale)
        syncer = PositionResynchronizer(
            api=api,
            position_store=store,
            db_repository=None,
            trading_mode="REAL",
            target_symbol="005930",
        )

        result = syncer.synchronize_on_startup()

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "QTY_ADJUSTED")
        self.assertEqual(result["position"].stock_code, "005930")
        self.assertEqual(result["position"].quantity, 3)


if __name__ == "__main__":
    unittest.main()
