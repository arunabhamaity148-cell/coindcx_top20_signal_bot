# Changelog — 2026-09-23

## Final remediation build

- Preserved strategy-specific LevelPlans and strategy expiry end-to-end.
- Enforced S3 1h context and S4 15m pullback.
- Made missing taker flow fail-closed for S2/S4.
- Wired Binance WS depth/kline into live state and higher-timeframe caches.
- Expanded Binance WS coverage to all validated Top-20 pairs.
- Made bot boot idempotent.
- Bounded CoinDCX REST polling concurrency with per-pair timeout handling.
- Added partial-exit, breakeven and structural-trailing backtest management.
- Hardened S5 expected-edge economics.
- Fixed typed config support for `poll_concurrency`.
- Fixed soak-test journal API usage.
- Fixed structured logging argument redaction/formatting.
- Added dedicated regression coverage for the remediation set.


## Exchange protocol verification hardening

- Updated Binance USDⓈ-M WebSocket routing to the current routed `/public` and `/market` endpoints; kline/markPrice/ticker are now on the market route and depth remains on the public route.
- Added current-route regression tests and all-20-symbol coverage assertions.
- Updated CoinDCX Futures REST market-data integration to the documented public orderbook and candlestick endpoints.
- Updated CoinDCX instrument parser for the documented `instrument` response wrapper and mapping-shaped orderbook payload.
- Added `market_data_base` to typed exchange configuration.
- Removed runtime caches/journals from the release package; logs are created at runtime only.

- Added explicit gross-profit/gross-loss fields to the backtest `Metrics` dataclass so walk-forward aggregation is typed rather than relying on dynamic attributes.
