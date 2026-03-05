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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from universe.universe_service import UniverseService  # type: ignore


class _DummyKISWithBalance:
    def __init__(self, holdings=None):
        self._holdings = holdings or []

    def get_account_balance(self):
        return {"success": True, "holdings": list(self._holdings)}


class UniverseFlowIntegrationTests(unittest.TestCase):
    def _write_yaml(self, root: Path) -> Path:
        yaml_path = root / "universe.yaml"
        yaml_path.write_text(
            """
universe:
  selection_method: "fixed"
  max_stocks: 5
  universe_size: 3
  max_positions: 3
  universe_cache_file: "data/universe_cache.json"
stocks:
  - "005930"
  - "000660"
  - "035720"
  - "051910"
""".strip(),
            encoding="utf-8",
        )
        return yaml_path

    def test_end_to_end_daily_flow_reuses_cache_and_computes_entry_candidates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "positions_REAL_051910.json").write_text(
                json.dumps({"position": {"stock_code": "051910", "quantity": 1}}),
                encoding="utf-8",
            )
            yaml_path = self._write_yaml(root)
            service = UniverseService(
                str(yaml_path),
                _DummyKISWithBalance(holdings=[{"stock_code": "000660", "qty": 2}]),
                data_dir=data_dir,
            )
            service.policy.cache_file = data_dir / "universe_cache.json"
            trade_date = "2026-03-05"

            with patch.dict("os.environ", {"EXECUTION_MODE": "REAL", "TRADING_MODE": "REAL"}, clear=False):
                holdings = service.load_holdings_symbols()
                first = service.get_or_create_todays_universe(trade_date)
                cache_before = service.policy.cache_file.read_text(encoding="utf-8")
                second = service.get_or_create_todays_universe(trade_date)
                cache_after = service.policy.cache_file.read_text(encoding="utf-8")
                entries = service.compute_entry_candidates(holdings, second)

            self.assertEqual(holdings, ["000660", "051910"])
            self.assertEqual(first, ["005930", "000660", "035720"])
            self.assertEqual(second, first)
            self.assertEqual(entries, ["005930", "035720"])
            self.assertEqual(cache_before, cache_after)

    def test_end_to_end_cache_identity_changes_with_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            yaml_path = self._write_yaml(root)
            service = UniverseService(str(yaml_path), _DummyKISWithBalance(), data_dir=data_dir)
            service.policy.cache_file = data_dir / "universe_cache.json"
            trade_date = "2026-03-05"

            with patch.dict("os.environ", {"EXECUTION_MODE": "PAPER", "TRADING_MODE": "PAPER"}, clear=False):
                paper_symbols = service.get_or_create_todays_universe(trade_date)
            payload_paper = json.loads(service.policy.cache_file.read_text(encoding="utf-8"))

            with patch.dict("os.environ", {"EXECUTION_MODE": "REAL", "TRADING_MODE": "REAL"}, clear=False):
                real_symbols = service.get_or_create_todays_universe(trade_date)
            payload_real = json.loads(service.policy.cache_file.read_text(encoding="utf-8"))

            self.assertEqual(paper_symbols, ["005930", "000660", "035720"])
            self.assertEqual(real_symbols, paper_symbols)
            self.assertEqual(payload_paper.get("db_mode"), "PAPER")
            self.assertEqual(payload_real.get("db_mode"), "REAL")
            self.assertNotEqual(payload_paper.get("cache_key"), payload_real.get("cache_key"))


if __name__ == "__main__":
    unittest.main()
