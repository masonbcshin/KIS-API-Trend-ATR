import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "universe"))
from universe_selector import UniverseSelectionConfig, UniverseSelector  # type: ignore


class _DummyKIS:
    def get_current_price(self, stock_code):
        return {"current_price": 70000, "open_price": 69000, "volume": 100000}

    def get_daily_ohlcv(self, stock_code, period_type="D"):
        import pandas as pd
        rows = []
        price = 70000
        for i in range(30):
            rows.append(
                {
                    "date": f"2026-01-{i+1:02d}",
                    "open": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "close": price,
                    "volume": 100000,
                }
            )
        return pd.DataFrame(rows)


class UniverseSelectorFixedTests(unittest.TestCase):
    def test_fixed_mode_preserves_order_and_max(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = UniverseSelectionConfig(
                selection_method="fixed",
                max_stocks=3,
                stocks=["005930", "000660", "035420", "051910"],
                universe_cache_file=str(Path(td) / "universe_cache.json"),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selected = selector.select()
            self.assertEqual(selected, ["005930", "000660", "035420"])

    def test_cache_schema_saved(self):
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "universe_cache.json"
            cfg = UniverseSelectionConfig(
                selection_method="fixed",
                max_stocks=1,
                stocks=["005930"],
                universe_cache_file=str(cache_path),
            )
            selector = UniverseSelector(config=cfg, kis_client=_DummyKIS(), db=None)
            selected = selector.select()
            self.assertEqual(selected, ["005930"])
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("date", payload)
            self.assertEqual(payload["stocks"], ["005930"])
            self.assertIn("selection_method", payload)


if __name__ == "__main__":
    unittest.main()
