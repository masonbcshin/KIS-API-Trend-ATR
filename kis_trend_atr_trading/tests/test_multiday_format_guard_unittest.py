import unittest
from pathlib import Path


class TestMultidayFormatGuard(unittest.TestCase):
    def test_no_conditional_inside_format_specifier(self):
        """
        회귀 방지:
        f-string format specifier 내부 조건식은 ValueError를 유발할 수 있음.
        """
        strategy_path = (
            Path(__file__).resolve().parents[1]
            / "strategy"
            / "multiday_trend_atr.py"
        )
        source = strategy_path.read_text(encoding="utf-8")
        self.assertNotIn(
            "TP={take_profit:,.0f if take_profit else 'Trailing Only'}",
            source,
            "조건식이 format specifier 내부에 다시 들어가면 안 됩니다.",
        )


if __name__ == "__main__":
    unittest.main()

