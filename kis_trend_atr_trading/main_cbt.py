#!/usr/bin/env python3
"""Deprecated compatibility wrapper.

Use: python -m kis_trend_atr_trading.apps.kr_cbt
"""

from __future__ import annotations

from pathlib import Path

_LEGACY_FILE = Path(__file__).resolve().parent / "deprecated" / "legacy_main_cbt.py"
_ORIG_NAME = __name__
globals()["__name__"] = "kis_trend_atr_trading.main_cbt_legacy"
exec(compile(_LEGACY_FILE.read_text(encoding="utf-8"), str(_LEGACY_FILE), "exec"), globals())
globals()["__name__"] = _ORIG_NAME
_legacy_main = globals().get("main")


def main() -> None:
    print("[DEPRECATED] main_cbt.py -> use `python -m kis_trend_atr_trading.apps.kr_cbt`")
    _legacy_main()


if __name__ == "__main__":
    main()
