# KIS KR Trading Architecture Refactor Plan

## 1) Current-State Scan

### A. Folder Purpose / Overlap

| Folder | Main Purpose | Overlap / Risk |
|---|---|---|
| `kis_trend_atr_trading` | Primary trading system (strategy, order sync, risk, DB, reporting) | Multiple entrypoints (`main.py`, `main_v2.py`, `main_v3.py`, `main_multiday.py`, `main_cbt.py`) and mixed generations of runtime paths |
| `kis_websocket_trader` | Real-time quote consumption with standalone strategy/controller/notifier | Duplicated strategy/notifier/runtime loop outside SSOT; should be reduced to market-data adapter only |
| `kis_auto_trader` | Separate legacy auto-trader with own broker/strategy/risk/notifier | Full duplicate of strategy/order/risk concerns and separate config/runtime path |

### B. Entrypoints and Roles

| Entrypoint | Current Role | Main Mode/Options (high-level) |
|---|---|---|
| `kis_trend_atr_trading/main.py` | Early single-symbol REST polling engine | `--mode backtest|trade`, `--stock`, `--interval`, `--max-runs`, `--days` |
| `kis_trend_atr_trading/main_v2.py` | DEV/PROD split runtime with `trader.py` path | `--mode trade|backtest`, plus stock/interval variants |
| `kis_trend_atr_trading/main_v3.py` | Extended automation + scheduler path | `--mode trade|scheduler|verify`, plus interval/stock style options |
| `kis_trend_atr_trading/main_multiday.py` | Active multiday engine entry (order sync/risk/universe) | `--mode backtest|trade|verify`, `--stock`, `--interval`, `--max-runs`, real-mode guards |
| `kis_trend_atr_trading/main_cbt.py` | CBT virtual-account workflow | `--mode cbt|report|reset|export`, `--stock`, `--interval`, `--max-runs` |
| `kis_websocket_trader/main.py` | WS-only standalone controller | No unified core integration, own strategy/state flow |
| `kis_auto_trader/main.py` | Separate scheduler loop with own broker/risk/strategy | Independent runtime and config |

### C. WebSocket Implementation Map

| File | Class/Function | Responsibility |
|---|---|---|
| `kis_websocket_trader/websocket_client.py` | `KISWebSocketClient` | WS connect/auth/subscribe/message parsing/reconnect |
| `kis_websocket_trader/websocket_client.py` | `TickData` | Tick payload object |
| `kis_websocket_trader/main.py` | `TradingController.on_price_update` | Tick callback driving standalone strategy |
| `kis_websocket_trader/strategy.py` | `ATRStrategy` | Duplicate signal generation and state transitions |

### D. REST Market-Data Implementation Map

| File | Class/Function | Responsibility |
|---|---|---|
| `kis_trend_atr_trading/api/kis_api.py` | `KISApi.get_current_price` | Current quote (REST) |
| `kis_trend_atr_trading/api/kis_api.py` | `KISApi.get_daily_ohlcv` | Historical OHLCV (REST) |
| `kis_trend_atr_trading/engine/executor.py` | `fetch_market_data/fetch_current_price` | REST polling usage in single-day executor |
| `kis_trend_atr_trading/engine/multiday_executor.py` | `fetch_market_data/fetch_current_price` | REST polling usage in multiday executor |

### E. Order/Fill/State Sync Map

| File | Class | Responsibility |
|---|---|---|
| `kis_trend_atr_trading/engine/order_synchronizer.py` | `OrderSynchronizer` | Idempotent order execution and fill-state persistence (`order_state`) |
| `kis_trend_atr_trading/engine/order_synchronizer.py` | `PositionResynchronizer` | Startup resync between broker/store/DB |
| `kis_trend_atr_trading/engine/multiday_executor.py` | `execute_buy/execute_sell` | Core strategy-to-order bridge (must remain SSOT) |

### F. Risk / Strategy / Reporting Map (SSOT scope)

| Domain | Primary Location |
|---|---|
| Strategy (Trend-ATR) | `kis_trend_atr_trading/strategy/` |
| Risk | `kis_trend_atr_trading/engine/risk_manager.py` |
| Reporting/DB summary | `kis_trend_atr_trading/reporting/`, `kis_trend_atr_trading/db/` |
| Telegram notify | `kis_trend_atr_trading/utils/telegram_notifier.py` |

## 2) Move vs Keep Decision

### Move into Adapters (market-data only)

- `kis_websocket_trader/websocket_client.py` logic (connection/auth/subscribe/parse/reconnect)
- Tick payload conversion and WS message parsing utilities
- Tick -> 1m bar aggregation as adapter utility (`bar_aggregator`)

### Keep in Core (no behavior change)

- Trend-ATR signal logic (`strategy/*`)
- Order parameters/order execution path (`engine/order_synchronizer.py`, executor order calls)
- Risk rules (`engine/risk_manager.py`)
- MySQL schema and repositories (`db/*`)
- Reporting/Telegram reporting (`reporting/*`, `tools/daily_report.py`)

### Explicitly Remove as duplicate responsibilities (by deprecation path)

- `kis_websocket_trader/strategy.py` strategy logic
- `kis_websocket_trader/main.py` trading controller runtime
- `kis_auto_trader/trader/strategy.py`, `risk_manager.py`, trading loop runtime

## 3) Target Structure (staged)

```text
kis_trend_atr_trading/
  apps/
    kr_trade.py
    kr_cbt.py
  core/
    engine/
    strategy/
    risk/
    reporting/
    notify/
    storage/
  adapters/
    kis_rest/
      market_data.py
    kis_ws/
      ws_client.py
      bar_aggregator.py
      market_data.py
  config/
    dev.yaml
    prod.yaml
    universe.yaml
  tools/
    daily_report.py
    verify_system.py
  deprecated/
    (legacy main wrappers and moved legacy entry modules)
```

## 4) Compatibility Policy

- Existing `main*.py` commands must remain executable.
- Phase-1/2 keep behavior via compatibility wrappers and delegated legacy entry modules.
- If deprecation happens, wrapper prints replacement command and forwards arguments.

## 5) WS Minimal Recovery Policy (fixed)

- Policy: `rest_fallback`
- Behavior:
  1. detect disconnect
  2. exponential backoff reconnect (max 5 attempts)
  3. if still failed, switch provider decision to REST fallback (or safe stop in strict mode)

## 6) PR Split (execution plan)

- PR1: analysis document + target scaffolding + deprecated wrapper skeleton (no strategy/order/risk changes)
- PR2: `MarketDataProvider` protocol + REST adapter + executor provider injection (default REST, behavior preserved)
- PR3: WS adapter (market-data only) + 1m completed-bar aggregator + reconnect<=5 and fallback policy
- PR4: `apps/kr_trade.py`, `apps/kr_cbt.py`, feed switch CLI/config wiring, smoke tests, deprecation guide updates

