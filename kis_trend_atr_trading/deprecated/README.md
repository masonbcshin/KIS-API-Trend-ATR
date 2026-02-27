# Deprecated Entrypoints

This folder contains legacy entry modules preserved for backward compatibility.

- Existing `main*.py` commands remain executable.
- Recommended entrypoints are under `apps/`:
  - `python3 -m kis_trend_atr_trading.apps.kr_trade ...`
  - `python3 -m kis_trend_atr_trading.apps.kr_cbt ...`
- `python3 -m kis_trend_atr_trading.main_multiday ...` is a compatibility wrapper:
  - prints `[DEPRECATED] main_multiday.py -> use ...`
  - then executes `deprecated/legacy_main_multiday.py`
  - multi-symbol runtime behavior is preserved (including `positions_{mode}_{symbol}.json` files)

Migration is staged and non-destructive; strategy/order/risk behavior remains unchanged.
