# Repository Audit & Fix Report — 2026-09-24

## Scope
Complete repository-level audit of the CoinDCX TOP-20 signal-only futures system.
Signal-only architecture preserved. No auto-trading capability added.

## Test evidence
- Full suite: **184 passed** (172 prior + 12 new regression tests)
- New file: `tests/unit/test_timestamp_and_ws_regression.py`

---

## Issue 1 — CoinDCX startup health
**STATUS:** PARTIAL / prior remediation retained  
Prior build introduced first-poll warm cache and DRY_RUN startup path. Poller uses
bounded concurrency (default 5) and per-pair timeouts. Health grades by worst pair age.
**NOT VERIFIED live** in this environment (no exchange network in container).

## Issue 2 — Runtime clock drift >1500ms (ROOT CAUSE FIXED)
**BEFORE:** `Normalizer.basis` compared raw `binance_book.ts_ms` vs `coindcx_book.ts_ms`.
CoinDCX REST books often used local `now_ms()` at poll time; Binance WS books used
exchange event time. Under a ~2s poll cycle this produced 1.7–2.4s pairwise “drift”
even when both feeds were usable (user diagnostic: venue drift 73ms after NTP).

**ROOT CAUSE:** Treating REST local-receipt clocks and WS exchange event clocks as the
same market clock.

**CHANGE:**
- `OrderBook` now carries `received_ts_ms` and `event_ts_ms` (+ `has_exchange_event_ts`).
- CoinDCX / Binance parsers set both fields explicitly.
- `Normalizer.basis`:
  - When `now_ms` is provided: freshness gate on receipt age (2× budget).
  - Event-time pairwise drift enforced **only** when both books have exchange event timestamps.
  - Mixed clocks: no false event-drift; freshness still fail-closed.
- CoinDCX poller `age_ms` / `last_ts` prefer `received_ts_ms`.

**TEST:** `test_normalization_mixed_clocks_does_not_false_drift_on_poll_cadence` PASS  
**RESULT:** PASS

## Issue 3 — Binance WS simultaneous sequence gaps (ROOT CAUSE FIXED)
**BEFORE:** On reconnect, `_last_depth_update_id` was retained. First events of the new
connection were compared against the previous connection’s last update id → simultaneous
gap alarms on every depth stream + REST resync storm.

**CHANGE:** `_consume` clears per-stream `_last_depth_update_id` and `_last_event_time_ms`
on every (re)connect before accepting messages.

**TEST:** contiguous / gap / reconnect / duplicate regressions PASS  
**RESULT:** PASS

## Issue 4 — CoinDCX timestamp semantics
**FIXED** as part of Issue 2. Event vs receipt explicitly separated.

## Issue 5 — CoinDCX polling architecture
Prior remediation: concurrency=5, per-pair timeout, failures tracked per pair.
Health uses worst age (entire feed can go DEGRADED/STALE). Rationale: basis and
cross-venue signals need a coherent CoinDCX surface; pair-level veto remains via
missing book on snapshot build. Unchanged.

## Issue 6 — 20 configured / 19 valid
Rejection is live validation against CoinDCX active_instruments + instrument metadata
+ Binance exchangeInfo. **NOT VERIFIED** without live endpoints. Pair is never silently
substituted (`on_failure: NO_SIGNAL`).

## Issue 7 — News (CFTC TLS, GDELT 429, unproven sources)
Prior design marks unproven sources disabled; TLS verification not disabled.
**NOT re-verified live** here. Existing unit coverage in `tests/news/`.

## Issue 8 — Duplicate shutdown
Prior remediation: boot idempotent; stop paths cancel tasks best-effort.
Existing tests cover double boot. **RESULT:** retained.

## Issues 9–15 — Strategy TP/SL, S3/S4 context, taker flow, WS state, partial exits
Prior final-remediation build + `tests/test_final_remediation.py` cover these.
Re-run: PASS.

## Issue 16 — Strategy independence
S1–S5 share price/volume/OI/funding/orderbook inputs. Common-factor risk exists;
no statistical independence claim is asserted by this audit.

## Issue 17 — “Top 5” research claims
**NOT VERIFIED** as measured superiority. Treat as design choices, not empirical ranking.

## Issue 18 — Backtest acceptance gates
**NOT VERIFIED — REQUIRES LIVE/OOS/PAPER TESTING**  
No OOS ≥100 trades / PF≥1.25 / etc. evidence package in-repo for this environment.

## Issue 19 — S5 convergence risk
Prior remediation: expected edge must exceed costs + latency buffer; documented as
convergence with residual basis risk, not risk-free arb. Retained.

## Issue 20 — Professional market-structure filters
Classification from code/docs (no marketing features added):
| Feature | Status |
|---------|--------|
| HTF Alignment 4H+1H+15m | PARTIAL (used in S4 trend path; not global gate) |
| Order Block Detection | MISSING / UNUSED |
| Liquidity Sweep Detection | IMPLEMENTED + USED (S1) |
| Fair Value Gap | MISSING |
| Change of Character | MISSING |
| Point of Control | MISSING |
| Fibonacci Confluence | MISSING |

## Security
- No trading keys required; `app/safety.py` aborts if trading credentials present.
- `.env.example` has empty Telegram placeholders only.
- TLS verification not disabled.
- No secrets exposed in this report.

## Production readiness
**NOT production-validated** against live Binance/CoinDCX or 72h paper soak in this
container. Unit/integration suite PASS. Deploy with dry-run + paper soak before live
signal delivery.
