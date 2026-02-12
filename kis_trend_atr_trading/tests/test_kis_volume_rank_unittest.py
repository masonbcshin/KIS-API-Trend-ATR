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
        fake_requests.get = lambda *args, **kwargs: _DummyResponse()
        fake_requests.post = lambda *args, **kwargs: _DummyResponse()
        sys.modules["requests"] = fake_requests
        sys.modules["requests.exceptions"] = fake_exceptions


_ensure_fake_dependencies()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.kis_api import KISApi  # type: ignore  # noqa: E402


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.ok = True
        self.text = ""

    def json(self):
        return self._payload


class TestKISVolumeRank(unittest.TestCase):
    def test_get_market_top_by_trade_value_parses_and_sorts(self):
        api = KISApi(app_key="k", app_secret="s", account_no="00000000", is_paper_trading=True)
        api.access_token = "token"

        payload = {
            "rt_cd": "0",
            "output": [
                {"mksc_shrn_iscd": "005930", "stck_prpr": "178100", "acml_vol": "1000"},
                {"mksc_shrn_iscd": "000660", "acml_tr_pbmn": "5000000000", "stck_prpr": "100000", "acml_vol": "300"},
                {"mksc_shrn_iscd": "035720", "stck_prpr": "50000", "acml_vol": "50"},
            ],
        }
        api._request_with_retry = lambda *args, **kwargs: _Response(payload)  # type: ignore

        rows = api.get_market_top_by_trade_value(top_n=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["code"], "000660")
        self.assertGreater(rows[0]["trade_value"], rows[1]["trade_value"])

    def test_get_market_universe_codes_returns_empty_on_error(self):
        api = KISApi(app_key="k", app_secret="s", account_no="00000000", is_paper_trading=True)
        api.access_token = "token"
        api.get_market_top_by_trade_value = lambda top_n=200: (_ for _ in ()).throw(Exception("blocked"))  # type: ignore
        codes = api.get_market_universe_codes(limit=10)
        self.assertEqual(codes, [])


if __name__ == "__main__":
    unittest.main()
