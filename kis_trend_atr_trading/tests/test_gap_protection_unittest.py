import unittest
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "utils" / "gap_protection.py"
SPEC = importlib.util.spec_from_file_location("gap_protection", MODULE_PATH)
GAP_PROTECTION = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(GAP_PROTECTION)

GAP_REASON_DISABLED = GAP_PROTECTION.GAP_REASON_DISABLED
GAP_REASON_OTHER = GAP_PROTECTION.GAP_REASON_OTHER
GAP_REASON_TRIGGERED = GAP_PROTECTION.GAP_REASON_TRIGGERED
should_trigger_gap_protection = GAP_PROTECTION.should_trigger_gap_protection


class TestGapProtectionDecision(unittest.TestCase):
    def setUp(self) -> None:
        self.position = None

    def test_raw_gap_zero_does_not_trigger(self) -> None:
        triggered, reason, raw_gap = should_trigger_gap_protection(
            position=self.position,
            open_price=100.0,
            reference_price=100.0,
            threshold_pct=0.3,
            epsilon_pct=0.001,
        )
        self.assertFalse(triggered)
        self.assertEqual(reason, GAP_REASON_OTHER)
        self.assertAlmostEqual(raw_gap, 0.0, places=9)

    def test_near_equal_open_and_entry_keeps_small_gap_scale(self) -> None:
        triggered, reason, raw_gap = should_trigger_gap_protection(
            position=self.position,
            open_price=178000.0,
            reference_price=178100.0,
            threshold_pct=2.0,
            epsilon_pct=0.001,
        )
        self.assertFalse(triggered)
        self.assertEqual(reason, GAP_REASON_OTHER)
        self.assertAlmostEqual(raw_gap, -0.056148, places=6)

    def test_small_loss_gap_below_threshold_does_not_trigger(self) -> None:
        triggered, reason, raw_gap = should_trigger_gap_protection(
            position=self.position,
            open_price=99.95,  # -0.05%
            reference_price=100.0,
            threshold_pct=0.3,
            epsilon_pct=0.001,
        )
        self.assertFalse(triggered)
        self.assertEqual(reason, GAP_REASON_OTHER)
        self.assertAlmostEqual(raw_gap, -0.05, places=6)

    def test_meaningful_loss_gap_triggers(self) -> None:
        triggered, reason, raw_gap = should_trigger_gap_protection(
            position=self.position,
            open_price=99.4,  # -0.6%
            reference_price=100.0,
            threshold_pct=0.3,
            epsilon_pct=0.001,
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, GAP_REASON_TRIGGERED)
        self.assertAlmostEqual(raw_gap, -0.6, places=6)

    def test_positive_gap_never_triggers(self) -> None:
        triggered, reason, raw_gap = should_trigger_gap_protection(
            position=self.position,
            open_price=100.5,  # +0.5%
            reference_price=100.0,
            threshold_pct=0.3,
            epsilon_pct=0.001,
        )
        self.assertFalse(triggered)
        self.assertEqual(reason, GAP_REASON_OTHER)
        self.assertAlmostEqual(raw_gap, 0.5, places=6)

    def test_threshold_zero_or_missing_disables_gap_protection(self) -> None:
        for threshold in (0.0, None):
            triggered, reason, raw_gap = should_trigger_gap_protection(
                position=self.position,
                open_price=99.0,
                reference_price=100.0,
                threshold_pct=threshold,
                epsilon_pct=0.001,
            )
            self.assertFalse(triggered)
            self.assertEqual(reason, GAP_REASON_DISABLED)
            self.assertAlmostEqual(raw_gap, 0.0, places=9)


if __name__ == "__main__":
    unittest.main()
