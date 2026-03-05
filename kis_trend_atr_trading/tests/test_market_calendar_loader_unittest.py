import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import market_hours


class MarketCalendarLoaderTests(unittest.TestCase):
    def test_load_holiday_calendar_from_file(self):
        with tempfile.TemporaryDirectory() as td:
            calendar_path = Path(td) / "market_calendar_krx.json"
            calendar_path.write_text(
                json.dumps(
                    {
                        "market": "KRX",
                        "version": "2099.01",
                        "coverage_from": "2099-01-01",
                        "coverage_to": "2099-12-31",
                        "source": "unit-test",
                        "holidays": ["2099-03-02"],
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"MARKET_CALENDAR_FILE": str(calendar_path)}, clear=False):
                holidays_set = market_hours.load_krx_holiday_calendar(force_reload=True)
                self.assertIn(date(2099, 3, 2), holidays_set)
                self.assertTrue(market_hours.is_holiday(date(2099, 3, 2)))
                meta = market_hours.get_holiday_calendar_metadata()
                self.assertEqual(meta.get("source"), "unit-test")

    def test_load_holiday_calendar_fallback_on_invalid_file(self):
        with tempfile.TemporaryDirectory() as td:
            calendar_path = Path(td) / "market_calendar_krx.json"
            calendar_path.write_text("{invalid json", encoding="utf-8")
            with patch.dict("os.environ", {"MARKET_CALENDAR_FILE": str(calendar_path)}, clear=False):
                holidays_set = market_hours.load_krx_holiday_calendar(force_reload=True)
                self.assertIn(date(2026, 3, 2), holidays_set)
                self.assertTrue(market_hours.is_holiday(date(2026, 3, 2)))


if __name__ == "__main__":
    unittest.main()
