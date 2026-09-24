# Release Status — 2026-09-23

## Build
**1.1.0 — production-hardened candidate**

## What is fixed

The full audit remediation set is incorporated, including strategy-specific level preservation,
S3/S4 multi-timeframe requirements, fail-closed taker-flow requirements, Binance WebSocket state
ingestion and all-20 coverage, idempotent boot, bounded CoinDCX polling, partial-TP backtest
management, S5 cost/adverse-selection checks, risk lifecycle accounting, Telegram WHY dispatch
mechanics, news corroboration, timestamp-aware derivatives freshness, and current exchange endpoint
parsing/routing.

## Local verification

- 172 tests collected; 172 passed.
- `python -m compileall -q app scripts tests` — PASS.
- `python scripts/validate_config.py` — PASS.
- `python scripts/healthcheck.py` — HEALTHY.
- `python -m app.main --check` — CONFIG OK.
- `python scripts/smoke_test.py --cycles 10` — PASS, 0 errors.
- Signal-only static safety scan — PASS, zero forbidden-code violations.

## Not honestly certifiable inside this container

- Live Binance/CoinDCX endpoint probe: blocked by unavailable DNS/network.
- Live Telegram delivery: not exercised without operator credentials.
- Historical OOS / anchored walk-forward acceptance gates: require supplied real historical bars.
- Untouched holdout: pending the same empirical dataset and run.
- 72-hour paper soak: requires wall-clock deployment.

Therefore the release must not be described as profitability-validated or 72-hour production-validated
solely from unit tests. The binary safety contract remains signal-only: this package contains no
exchange trading credentials and no order-placement/account-mutation capability.
