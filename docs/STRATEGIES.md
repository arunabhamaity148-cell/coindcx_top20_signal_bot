# Strategies (S1–S5)

Five engines, five distinct information channels. They are deliberately **not** RSI/MACD
variants, and none of them may bypass the VetoEngine or the RiskEngine.

Each engine is a pure function of a `MarketSnapshot` and returns a `StrategyCandidate`
(direction, entry, zone, invalidation, stop, ATR, expiry, reasons, correlation group) or
`None`. An engine that raises abstains — the registry records the failure and the other
engines continue.

| Id | Name | Channel | Timeframes | Expiry | Correlation group |
|---|---|---|---|---|---|
| S1 | Liquidity Sweep & Reclaim | micro-liquidity / stop-runs | 5 m structure, 1 m trigger | 45 m | `MICRO_LIQUIDITY` |
| S2 | Volatility Compression → Range Expansion | volatility structure | 15 m compression, 5 m trigger | 45 m | `VOLATILITY` |
| S3 | Funding / Crowding Exhaustion Reversal | positioning / derivatives | 1 h context, 5 m trigger | 90 m | `POSITIONING` |
| S4 | OI-Confirmed Trend Continuation | trend + derivatives confirmation | 4 h trend, 5 m trigger | 45 m | `TREND_DERIVATIVES` |
| S5 | Cross-Venue Basis Convergence | cross-venue execution reality | 1–5 m | 20 m | `CROSS_VENUE` |

Grading counts **groups**, not engines, so S1 and S2 can never masquerade as two
independent confirmations of one micro-liquidity idea.

---

## S1 — Liquidity Sweep & Reclaim (`app/strategies/s1_liquidity_sweep.py`)

**Thesis.** A stop-run below/above a recent swing that is immediately reclaimed is liquidity
being taken, not information being revealed. Fade the sweep back into the range.

* **Regime:** RANGE / CHOP / POST_EVENT.
* **Setup:** the most recent bar trades beyond a prior 3-bar swing extreme (low for longs,
  high for shorts) **and** closes back inside it.
* **Confirmation:** taker-buy ratio > 0.55 for longs (< 0.45 for shorts), and `oi_chg_pct`
  ≤ +0.5 % — a genuine OI *build* means breakout, so the engine stands aside.
* **Entry (limit):** `ref_low + 0.15 × ATR` (long) / `ref_high − 0.15 × ATR` (short).
* **Invalidation:** the swept extreme itself.
* **Stop:** invalidation ∓ 0.5 × ATR, with the shared ATR risk floor applied.
* **TP1 clamp:** never beyond the sweep extreme ± 1 × ATR beyond the entry, so TP1 stays
  reachable.
* **Refusals:** no sweep; flow not aligned; an OI build (real breakout); fewer than the
  configured minimum candles.

## S2 — Volatility Compression → Range Expansion (`s2_volatility_compression.py`)

**Thesis.** Volatility is mean-reverting. A compressed range that expands on an OI build
tends to continue, and the *retest of the boundary* is the low-risk entry.

* **Regime:** COMPRESSION / RANGE (ATR percentile ≤ 0.25).
* **Setup:** the last 60 compression bars form a range no wider than 6 × ATR.
* **Trigger:** an expansion bar with body > 1.2 × ATR closing outside the boundary, with
  `oi_chg_pct ≥ +1.0 %` and taker flow aligned with the break.
* **Entry (limit):** the retest of the compression boundary (`hi` for longs, `lo` for shorts).
* **Invalidation:** 0.9 × ATR back inside the range.
* **Stop:** the opposite boundary ∓/± 0.1 × ATR, never tighter than the risk floor.
* **Refusals:** volatility not actually compressed; no OI confirmation; expansion without a
  close beyond the boundary.

## S3 — Funding / Crowding Exhaustion Reversal (`s3_funding_crowding.py`)

**Thesis.** When funding and open interest are simultaneously extreme, the crowd is paying
to hold a position that is already failing. The reversal is the trade.

* **Regime:** POST_EVENT / EXTREME (funding distribution tails).
* **Setup:** \|funding z\| ≥ 2.5 **and** OI percentile ≥ 0.90, with the price failing to make
  a new extreme and taker flow flipping against the crowd.
* **Entry (limit):** the retracement into the failed extreme (not a market chase).
* **Invalidation:** a decisive new extreme beyond the crowded level.
* **Stop:** invalidation ± 0.5 × ATR (risk-floored).
* **Refusals:** funding not extreme; book not crowded; flow not flipping.
* **Note:** the *hard block* for crowding is G5 (funding z ≥ 2.5 **and** OI rank ≥ 0.97). S3
  operates in the 0.90–0.97 band; beyond 0.97 the veto refuses the trade entirely, by design.

## S4 — OI-Confirmed Trend Continuation (`s4_oi_trend.py`)

**Thesis.** A trend confirmed by rising open interest and aligned taker flow is real
positioning; buying the pullback into the fast EMA is the disciplined entry.

* **Regime:** TREND_UP / TREND_DOWN.
* **Setup:** EMA21/EMA55 stack with positive slope, price above (long) the fast EMA on the
  4 h series.
* **Confirmation:** `oi_chg_pct ≥ +0.5 %`, taker-buy ratio ≥ 0.55 (long).
* **Entry (limit):** the pullback into the fast EMA, i.e. at the EMA — never at market.
* **Anti-chase:** if price is already more than the configured ATR multiple beyond the EMA,
  the engine abstains (no extended entries).
* **Invalidation:** the slow EMA.
* **Stop:** invalidation ∓ 0.5 × ATR (risk-floored).
* **Refusals:** OI missing (an absent input is not a pass); EMAs not stacked; price extended.

## S5 — Cross-Venue Basis Convergence (`s5_basis_convergence.py`)

**Thesis.** CoinDCX and Binance price the same contract. When the CoinDCX mark is rich
relative to Binance, a limit at the dislocated venue converges against the Binance
reference — but only if the edge survives fees.

* **Regime:** any, provided S5 is eligible (divergence ABNORMAL, not EXTREME).
* **Setup:** \|basis z\| between 2.0 and 3.0 (2–3 σ) with `net_basis_bps > 0` *after*
  round-trip cost.
* **Entry (limit):** the dislocated venue's touchable price (tick-snapped best bid for a
  short, best ask for a long).
* **Stop:** 1 % adverse move, which guarantees R:R ≥ 2 against the convergence distance. A
  wider dislocation would trip G2, so the geometry stays inside the acceptance envelope.
* **Invalidation:** a 1 % adverse move *widening* the basis.
* **Refusals:** \|z\| ≥ 3.0 (that is a veto, not a trade); negative net edge after fees; news
  state not CLEAR — a dislocation during a news event is a repricing, not a divergence.

---

## Shared construction rules (all engines)

* `risk = |entry − stop|`, **floored at 0.25 × ATR**, so a three-tick stop cannot manufacture
  a 100R ladder.
* TP ladder = 1R / 2R / 3R / 5R, tick-snapped and monotonicity-checked; a ladder that cannot
  be constructed raises `FailClosedError` and the signal is dropped.
* An impossible ladder (SL on the wrong side of entry, non-positive risk, ATR of 0) is a
  hard failure — the engine emits nothing.
* A candidate whose TP2 R:R is below `min_rr_tp2` (1.8) is discarded before consensus.
* Every WHY bullet must contain a measured number (percentile, ratio, z-score, percentage
  change). No adjectives, no narrative.


## Remediation notes (2026-09-23)

- Signal generation preserves the selected strategy candidate's exact entry/zone/SL/TP1-TP4/R:R plan; the generic TP/SL builder is no longer allowed to overwrite strategy-specific levels.
- S3 now requires a verified 1h EMA context; missing OI percentile is fail-closed.
- S4 now requires a real 15m pullback-to-EMA21 with close-back confirmation and verified taker flow; missing taker flow is NO TRADE.
- S5 remains a single-venue convergence signal, not risk-free arbitrage, and requires expected convergence edge to exceed effective costs plus a latency/adverse-selection buffer.
- Binance WS depth/kline messages update live state, including rolling 5m/15m/1h/4h aggregates from 1m events.
- CoinDCX REST polling uses bounded async concurrency so slow symbols do not serialize the whole watchlist.
