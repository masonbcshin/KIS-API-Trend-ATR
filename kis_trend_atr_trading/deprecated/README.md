# Deprecated Entrypoints

This folder contains legacy entry modules preserved for backward compatibility.

- Existing `main*.py` commands remain executable.
- New entrypoints are introduced under `apps/`:
  - `python -m kis_trend_atr_trading.apps.kr_trade ...`
  - `python -m kis_trend_atr_trading.apps.kr_cbt ...`

Migration is staged and non-destructive; strategy/order/risk behavior remains unchanged.

