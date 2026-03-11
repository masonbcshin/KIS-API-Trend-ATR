"""Repo-root wrapper for the deterministic threaded pipeline replay harness."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = PROJECT_ROOT / "kis_trend_atr_trading"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

try:
    from tools.threaded_pipeline_replay import main
except ModuleNotFoundError as exc:
    missing_name = getattr(exc, "name", "unknown")

    def main() -> int:
        sys.stderr.write(
            "threaded_pipeline_replay requires the project virtualenv dependencies. "
            f"Missing module: {missing_name}. "
            "Run with `.venv/bin/python tools/threaded_pipeline_replay.py ...`.\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
