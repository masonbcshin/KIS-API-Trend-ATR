#!/usr/bin/env python3
"""Deprecated compatibility wrapper.

Use: python -m kis_trend_atr_trading.apps.kr_trade
"""

from __future__ import annotations

from pathlib import Path

_LEGACY_FILE = Path(__file__).resolve().parent / "deprecated" / "legacy_main_v3.py"
_ORIG_NAME = __name__
globals()["__name__"] = "kis_trend_atr_trading.main_v3_legacy"
exec(compile(_LEGACY_FILE.read_text(encoding="utf-8"), str(_LEGACY_FILE), "exec"), globals())
globals()["__name__"] = _ORIG_NAME
_legacy_main = globals().get("main")


def main() -> None:
    print("[DEPRECATED] main_v3.py -> use `python -m kis_trend_atr_trading.apps.kr_trade`")
    _legacy_main()


if __name__ == "__main__":
    main()
