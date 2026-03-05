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


class _DummyKISWithBalance:
    def __init__(self, holdings=None):
        self._holdings = holdings or []

    def get_account_balance(self):
        return {"success": True, "holdings": list(self._holdings)}


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
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("schema_version"), service.CACHE_SCHEMA_VERSION)

    def test_legacy_cache_payload_is_migrated_and_reused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            yaml_path = self._write_yaml(root)
            service = UniverseService(str(yaml_path), _DummyKIS(), data_dir=root / "data")
            cache_file = root / "data" / "universe_cache.json"
            service.policy.cache_file = cache_file
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(
                    {
                        "date": "2026-02-12",
                        "selection_method": "combined",
                        "universe_size": 3,
                        "max_positions": 2,
                        "params": {
                            "min_volume": None,
                            "min_market_cap": None,
                            "min_atr_pct": None,
                            "max_atr_pct": None,
                            "candidate_pool_mode": None,
                            "market_scan_size": None,
                        },
                        "candidate_symbols": ["005930", "000660", "035720"],
                        "stocks": ["005930", "000660", "035720"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            with patch(
                "universe.universe_service.UniverseSelector.from_yaml",
                return_value=_DummySelector(["069500", "229200", "114800"]),
            ) as mocked:
                symbols = service.get_or_create_todays_universe("2026-02-12")

            self.assertEqual(symbols, ["005930", "000660", "035720"])
            self.assertEqual(mocked.call_count, 0)
            migrated = json.loads(cache_file.read_text(encoding="utf-8"))
            expected_identity = service._build_cache_identity("2026-02-12")
            self.assertEqual(migrated.get("schema_version"), service.CACHE_SCHEMA_VERSION)
            self.assertEqual(migrated.get("db_mode"), expected_identity["db_mode"])
            self.assertEqual(migrated.get("policy_signature"), expected_identity["policy_signature"])
            self.assertEqual(migrated.get("cache_key"), expected_identity["cache_key"])

    def test_policy_change_invalidates_same_day_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            yaml_path = self._write_yaml(root)
            service = UniverseService(str(yaml_path), _DummyKIS(), data_dir=root / "data")
            cache_file = root / "data" / "universe_cache.json"
            service.policy.cache_file = cache_file

            with patch(
                "universe.universe_service.UniverseSelector.from_yaml",
                side_effect=[
                    _DummySelector(["005930", "000660", "035720"]),
                    _DummySelector(["069500", "229200", "114800"]),
                ],
            ) as mocked:
                first = service.get_or_create_todays_universe("2026-02-12")
                service.policy.universe_size = 2
                second = service.get_or_create_todays_universe("2026-02-12")

            self.assertEqual(first, ["005930", "000660", "035720"])
            self.assertEqual(second, ["069500", "229200", "114800"])
            self.assertEqual(mocked.call_count, 2)

    def test_unsupported_schema_version_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            yaml_path = self._write_yaml(root)
            service = UniverseService(str(yaml_path), _DummyKIS(), data_dir=root / "data")
            cache_file = root / "data" / "universe_cache.json"
            service.policy.cache_file = cache_file
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(
                    {
                        "schema_version": 999,
                        "date": "2026-02-12",
                        "stocks": ["005930", "000660", "035720"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            with patch(
                "universe.universe_service.UniverseSelector.from_yaml",
                return_value=_DummySelector(["069500", "229200", "114800"]),
            ) as mocked:
                symbols = service.get_or_create_todays_universe("2026-02-12")

            self.assertEqual(symbols, ["069500", "229200", "114800"])
            self.assertEqual(mocked.call_count, 1)
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("schema_version"), service.CACHE_SCHEMA_VERSION)

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

    def test_load_holdings_symbols_uses_mode_scoped_files_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            yaml_path = self._write_yaml(root)

            (data_dir / "positions_REAL_005930.json").write_text(
                json.dumps({"position": {"stock_code": "005930", "quantity": 2}}),
                encoding="utf-8",
            )
            (data_dir / "positions_DRY_RUN_000660.json").write_text(
                json.dumps({"position": {"stock_code": "000660", "quantity": 3}}),
                encoding="utf-8",
            )

            service = UniverseService(str(yaml_path), _DummyKIS(), data_dir=data_dir)
            with patch.dict("os.environ", {"EXECUTION_MODE": "REAL", "TRADING_MODE": "REAL"}, clear=False):
                symbols = service.load_holdings_symbols()

            self.assertEqual(symbols, ["005930"])

    def test_load_holdings_symbols_includes_api_holdings_for_real(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            yaml_path = self._write_yaml(root)
            kis = _DummyKISWithBalance(
                holdings=[
                    {"stock_code": "069500", "qty": 1},
                    {"stock_code": "233740", "quantity": 2},
                    {"stock_code": "005930", "qty": 0},
                ]
            )
            service = UniverseService(str(yaml_path), kis, data_dir=root / "data")

            with patch.dict("os.environ", {"EXECUTION_MODE": "REAL", "TRADING_MODE": "REAL"}, clear=False):
                symbols = service.load_holdings_symbols()

            self.assertEqual(symbols, ["069500", "233740"])

    def test_get_todays_universe_snapshot_prefers_cached_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            yaml_path = self._write_yaml(root)
            service = UniverseService(str(yaml_path), _DummyKIS(), data_dir=root / "data")
            cache_file = root / "data" / "universe_cache.json"
            service.policy.cache_file = cache_file
            identity = service._build_cache_identity("2026-02-12")
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(
                    {
                        "schema_version": service.CACHE_SCHEMA_VERSION,
                        "date": "2026-02-12",
                        "db_mode": identity["db_mode"],
                        "policy_signature": identity["policy_signature"],
                        "cache_key": identity["cache_key"],
                        "selection_method": "combined",
                        "candidate_symbols": ["005930", "000660", "035720"],
                        "universe_symbols": ["005930", "035720"],
                        "stocks": ["005930", "035720"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            snapshot = service.get_todays_universe_snapshot("2026-02-12")

            self.assertEqual(snapshot["candidate_symbols"], ["005930", "000660", "035720"])
            self.assertEqual(snapshot["universe_symbols"], ["005930", "035720"])


if __name__ == "__main__":
    unittest.main()
