import unittest
from pathlib import Path


class TestGapNotificationAlignment(unittest.TestCase):
    def test_gap_notification_uses_same_open_and_base_as_calculation(self):
        strategy_path = (
            Path(__file__).resolve().parents[1]
            / "strategy"
            / "multiday_trend_atr.py"
        )
        executor_path = (
            Path(__file__).resolve().parents[1]
            / "engine"
            / "multiday_executor.py"
        )

        strategy_source = strategy_path.read_text(encoding="utf-8")
        executor_source = executor_path.read_text(encoding="utf-8")

        self.assertIn(
            "gap_open_price=float(open_price) if open_price is not None else None",
            strategy_source,
            "갭 시그널에는 계산에 사용한 open_price가 반드시 저장되어야 합니다.",
        )
        self.assertIn(
            "reference_price=gap_ref_price",
            strategy_source,
            "갭 시그널에는 계산에 사용한 base(reference_price)가 저장되어야 합니다.",
        )
        self.assertIn(
            "open_price=gap_open_price",
            executor_source,
            "텔레그램 갭 메시지는 exit_price가 아닌 계산에 사용한 open_price를 써야 합니다.",
        )
        self.assertIn(
            "reference_price=gap_reference_price",
            executor_source,
            "텔레그램 갭 메시지 base_price는 계산에 사용한 reference_price와 동일해야 합니다.",
        )


if __name__ == "__main__":
    unittest.main()
