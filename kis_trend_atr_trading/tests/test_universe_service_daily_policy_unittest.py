import json
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    fake_pytz = type(sys)("pytz")
    fake_pytz.timezone = lambda _name: _FakeKST()
    sys.modules["pytz"] = fake_pytz

if "yaml" not in sys.modules:
    fake_yaml = type(sys)("yaml")
    fake_yaml.safe_load = lambda *_args, **_kwargs: {
        "universe": {
            "selection_method": "combined",
            "max_stocks": 5,
            "universe_size": 3,
            "max_positions": 2,
            "universe_cache_file": "data/universe_cache.json",
        },
        "stocks": ["005930", "000660", "035720"],
    }
    fake_yaml.safe_dump = lambda *_args, **_kwargs: ""
    sys.modules["yaml"] = fake_yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from universe.universe_service import UniverseService  # type: ignore


class _DummyKIS:
    pass


class _DummySelector:
    def __init__(self, symbols):
        self._symbols = list(symbols)
        self.config = type("Cfg", (), {"max_stocks": len(self._symbols)})()

    def select(self):
        return list(self._symbols)


class _FailSelector:
    def __init__(self):
        self.config = type("Cfg", (), {"max_stocks": 0})()

    def select(self):
        raise RuntimeError("selector failed")


class UniverseServiceDailyPolicyTests(unittest.TestCase):
    def _write_yaml(self, root: Path) -> Path:
        yaml_path = root / "universe.yaml"
        yaml_path.write_text(
            """
universe:
  selection_method: "combined"
  max_stocks: 5
  universe_size: 3
  max_positions: 2
  universe_cache_file: "data/universe_cache.json"
stocks:
  - "005930"
  - "000660"
  - "035720"
""".strip(),
            encoding="utf-8",
        )
        return yaml_path

    def test_compute_entry_candidates_subtracts_holdings(self):
        got = UniverseService.compute_entry_candidates(
            holdings=["005930", "000660"],
            todays_universe=["005930", "035720", "051910"],
        )
        self.assertEqual(got, ["035720", "051910"])

    def test_reuse_today_universe_cache_on_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            yaml_path = self._write_yaml(root)
            service = UniverseService(str(yaml_path), _DummyKIS(), data_dir=root / "data")
            cache_file = root / "data" / "universe_cache.json"
            service.policy.cache_file = cache_file

            with patch("universe.universe_service.UniverseSelector.from_yaml", return_value=_DummySelector(["005930", "000660", "035720"])) as mocked:
                first = service.get_or_create_todays_universe("2026-02-12")
                second = service.get_or_create_todays_universe("2026-02-12")

            self.assertEqual(first, ["005930", "000660", "035720"])
            self.assertEqual(second, first)
            self.assertEqual(mocked.call_count, 1)
            self.assertTrue(cache_file.exists())

    def test_refresh_failure_fallback_to_fixed_stocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            yaml_path = self._write_yaml(root)
            service = UniverseService(str(yaml_path), _DummyKIS(), data_dir=root / "data")
            cache_file = root / "data" / "universe_cache.json"
            service.policy.cache_file = cache_file

            with patch("universe.universe_service.UniverseSelector.from_yaml", return_value=_FailSelector()):
                symbols = service.get_or_create_todays_universe("2026-02-12")

            self.assertEqual(symbols, ["005930", "000660", "035720"])
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("selection_method"), "fixed_fallback")


if __name__ == "__main__":
    unittest.main()
