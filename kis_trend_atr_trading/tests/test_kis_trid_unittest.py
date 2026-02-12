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
from api.kis_api import KISApi  # type: ignore  # noqa: E402


class TestKISTrIdMapping(unittest.TestCase):
    def test_paper_tr_ids(self):
        api = KISApi(app_key="k", app_secret="s", account_no="00000000", is_paper_trading=True)
        self.assertEqual(api._resolve_tr_id("order_buy"), "VTTC0802U")
        self.assertEqual(api._resolve_tr_id("order_sell"), "VTTC0801U")
        self.assertEqual(api._resolve_tr_id("order_status"), "VTTC8001R")
        self.assertEqual(api._resolve_tr_id("order_cancel"), "VTTC0803U")
        self.assertEqual(api._resolve_tr_id("balance"), "VTTC8434R")

    def test_real_tr_ids(self):
        api = KISApi(app_key="k", app_secret="s", account_no="00000000", is_paper_trading=False)
        self.assertEqual(api._resolve_tr_id("order_buy"), "TTTC0802U")
        self.assertEqual(api._resolve_tr_id("order_sell"), "TTTC0801U")
        self.assertEqual(api._resolve_tr_id("order_status"), "TTTC8001R")
        self.assertEqual(api._resolve_tr_id("order_cancel"), "TTTC0803U")
        self.assertEqual(api._resolve_tr_id("balance"), "TTTC8434R")


if __name__ == "__main__":
    unittest.main()
