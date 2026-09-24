# Veto System

Capital protection is architecturally senior to signal generation. Twelve guards run **after**
strategy consensus and can only reduce the outcome: a guard can block a signal or degrade its
confidence — never approve one the previous stage refused.

Three structural guarantees hold for every guard:

1. **A hard block is non-overridable.** `override_allowed` is `false` in configuration and the
   boot assertion aborts if it is ever set to `true`. No strategy score, no confidence value,
   no operator flag in the config can bypass a hard block.
2. **A guard exception is a BLOCK.** Each guard is invoked through a wrapper that converts any
   exception into a `BLOCK` row with the exception text as evidence. A crashing guard is never
   a silent pass.
3. **Missing data is a BLOCK.** A guard that cannot obtain the input it needs — a basis
   snapshot, funding z, OI rank, news health — blocks rather than assuming "probably fine".

---

## Hard blocks (non-overridable)

### G1 — Data Integrity

| Condition | Threshold |
|---|---|
| Any tracked feed not HEALTHY | state ≠ HEALTHY ⇒ block |
| Healthy feed count | < `min_sources_healthy` (2) ⇒ block |
| Required feed missing from the registry | block (`binance_rest`, `binance_ws`, `coindcx_rest`) |
| Tick staleness | > 3000 ms ⇒ block |
| Clock drift | > 1500 ms ⇒ block |
| Orderbook invalid | empty or crossed ⇒ block |
| Instrument metadata | tick size ≤ 0 ⇒ block |
| Rate-limit pressure | weight budget > 80 % ⇒ block |

Evidence recorded: per-feed state map, ages, the offending value.

### G2 — Cross-Exchange Divergence

| Condition | Action |
|---|---|
| Basis snapshot unavailable | block (normalization failed) |
| History < 60 observations | block — "insufficient history", never NORMAL |
| Classification EXTREME (\|z\| ≥ 3.0) | **block** |
| \|z\| ≥ `block_z` (3.0) with a numeric z | block |

Divergence bands: NORMAL < 1.0 · ELEVATED 1–2 · ABNORMAL 2–3 · EXTREME ≥ 3.0.
S5 is only eligible in the ABNORMAL band; EXTREME is a hard stop, not an opportunity.

### G3 — Liquidity / Slippage

| Condition | Threshold |
|---|---|
| Spread | > 12 bps ⇒ block |
| Depth within ±50 bps | < $150 000 ⇒ block |
| \|Book imbalance\| | > 0.85 ⇒ block |

### G4 — News Shock

| Condition | Action |
|---|---|
| News feed not HEALTHY | block |
| News state BLOCK (CRITICAL tier ≤ 2 with corroboration) | block |
| Blackout window active (120 s after a CRITICAL release) | block |
| News health absent from the registry | block |

A feed outage therefore blocks trading rather than silently removing the news filter.

### G5 — Crowding

| Condition | Action |
|---|---|
| \|funding z\| ≥ 2.5 **and** OI percentile ≥ 0.97 | **block** |
| Either input missing | block (an absent input is not a pass) |
| Only one condition met | pass |

---

## Degrade tier (confidence −0.15, never a block)

| Guard | Trigger | Note |
|---|---|---|
| **G6** Structure Invalidation | price has already breached the candidate's invalidation | the thesis is objectively false |
| **G7** Extreme Volatility | realized vol > 1.50 or ATR percentile > 0.995 | signal still allowed, at reduced confidence |
| **G8** BTC Regime Conflict | BTC regime argues against the candidate direction | BTC is the market-wide filter |
| **G10** Orderbook Instability | mid jump > 20 bps in-window | unstable tape |
| **G11** Duplicate / Anti-Chase | a live signal already exists for the pair, or price is outside the entry zone | the anti-chase rule: expire, never chase |
| **G12** Spread Expansion | spread > 1.5 × the configured limit | escalates to a block when configured to |

`escalation.spread_expansion_to_block` promotes G12 from DEGRADE to BLOCK. It is enabled by
default: a spread blowing through the limit is a liquidity event, not a discount.

---

## Journal

Every non-PASS result is written to the `vetoes` table and the `vetoes.jsonl` mirror with the
guard id, severity, a human-readable reason and a structured evidence payload — so a refused
signal is as auditable as an emitted one.

```sql
select ts, symbol, guard, severity, reason from vetoes order by ts desc limit 20;
```

## Recalibration

Thresholds are reviewed monthly against realised slippage and fill data (see
`docs/DEPLOYMENT.md` §Maintenance). Thresholds live in `config/veto.yaml`; the guard code
contains no numbers of its own.
