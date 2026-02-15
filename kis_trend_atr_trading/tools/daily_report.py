"""Wrapper entry for daily report tool."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT_TOOL = PROJECT_ROOT / "tools" / "daily_report.py"


def main() -> int:
    if not ROOT_TOOL.exists():
        print(f"daily_report tool not found: {ROOT_TOOL}")
        return 1
    namespace = {}
    exec(compile(ROOT_TOOL.read_text(encoding="utf-8"), str(ROOT_TOOL), "exec"), namespace)
    entry = namespace.get("main")
    if callable(entry):
        return int(entry())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

