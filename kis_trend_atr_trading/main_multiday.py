#!/usr/bin/env python3
"""Deprecated compatibility wrapper.

Use: python -m kis_trend_atr_trading.apps.kr_trade
"""

from __future__ import annotations

from pathlib import Path
import sys

_LEGACY_FILE = Path(__file__).resolve().parent / "deprecated" / "legacy_main_multiday.py"
_PKG_DIR = Path(__file__).resolve().parent
_ORIG_NAME = __name__

# Legacy modules still use script-era imports like `from config import ...`.
# Ensure package directory is importable as a top-level module namespace.
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

globals()["__name__"] = "kis_trend_atr_trading.main_multiday_legacy"
exec(compile(_LEGACY_FILE.read_text(encoding="utf-8"), str(_LEGACY_FILE), "exec"), globals())
globals()["__name__"] = _ORIG_NAME
_legacy_main = globals().get("main")


def main() -> None:
    print("[DEPRECATED] main_multiday.py -> use `python -m kis_trend_atr_trading.apps.kr_trade`")
    _legacy_main()


if __name__ == "__main__":
    main()
