"""Compatibility verification tool wrapper."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "kis_trend_atr_trading"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from main_multiday import run_verification


def main() -> int:
    return 0 if run_verification() else 1


if __name__ == "__main__":
    raise SystemExit(main())

