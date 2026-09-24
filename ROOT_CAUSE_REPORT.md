# Root Cause Report — 2026-09-23

## Fixed issues

1. **Critical — strategy levels overwritten**: `SignalEngine.generate()` rebuilt strategy-specific LevelPlans with the generic ATR builder. Fixed by validating and preserving the selected `StrategyCandidate` entry, zone, invalidation, SL and TP1-TP4 levels; strategy expiry is also preserved subject to the grade risk ceiling.
2. **Critical — S4 pullback not enforced**: added required 15m EMA21 pullback-touch + close-back filter before the 5m trigger can qualify.
3. **Major — S3 1h context not enforced**: added required 1h EMA21/55 context guard against counter-context reversals.
4. **Major — missing taker flow accepted in S2/S4**: missing taker flow now rejects the candidate instead of treating it as neutral.
5. **Critical — Binance WS was telemetry-only**: depth and kline messages now update the live market-state caches; 1m WS candles are rolled into 5m/15m/1h/4h caches.
6. **Critical — only first 5 symbols used for Binance WS**: boot now subscribes to every validated Top-20 Binance symbol.
7. **Critical — double boot**: boot is idempotent and `run()` only boots when the bot is not already booted.
8. **Major — CoinDCX polling serialized**: polling now uses bounded async concurrency (default 5) plus per-pair timeouts.
9. **Critical — backtest exited fully at TP1**: backtest now models the documented 40/30/20/10 partial exits, breakeven after TP1, structural trailing after TP2/TP3, and leg-level costs.
10. **Major — S5 edge test too weak**: expected convergence edge must exceed effective costs plus a latency/adverse-selection buffer; S5 is documented and implemented as a convergence signal, not risk-free arbitrage.

## Additional verification bugs fixed while validating the remediation build

11. **Config mismatch — `poll_concurrency`**: the new YAML setting was not represented in `ExchangeConfig`; the typed config now accepts and validates it.
12. **Soak journal API mismatch**: `scripts/soak_test.py` called a nonexistent `record_no_trade()` method; no-trade outcomes now use the existing journal error/audit path while the soak telemetry still counts them separately.
13. **Structured logging redaction broke printf-style arguments**: redaction converted argument tuples into lists, causing `LogRecord.getMessage()` formatting errors; the filter now preserves tuple semantics.

## Current validation status

See `TEST_RESULTS.md` for the exact offline verification results.
