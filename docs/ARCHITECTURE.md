# Architecture

## 1. Purpose and the hard boundary

The system is a **signal-only** intelligence layer for the top 20 CoinDCX USDT-margined
perpetual futures. It observes the market, evaluates five proprietary strategies, refuses to
emit anything that fails a veto or a risk gate, and hands a human a fully-specified limit
plan with a short, quantified rationale.

The boundary is architectural, not configurational:

* there is no order-placement, cancellation, modification, close, leverage or transfer code
  anywhere in `app/`;
* `app/safety.py` scans the tree at boot and in the test suite, and fails closed if any such
  call site appears;
* the boot assertion aborts the process when a Binance/CoinDCX trading credential exists in
  the environment, when `mode != signal_only`, or when veto overrides are enabled.

If the process cannot prove those three things, it does not start.

## 2. Component map

```
                    ┌───────────────────── feeds ─────────────────────┐
                    │ news RSS/JSON   Binance WS+REST   CoinDCX REST  │
                    └───────┬───────────────┬───────────────┬─────────┘
                            │               │               │
                     news/engine.py   data/health.py  (staleness, gaps, rate budget)
                            │               │               │
                            └───────► data/normalization.py ◄────────┘
                                       quote/contract/time alignment,
                                       basis_bps, z, net_basis, divergence class
                                              │
                       data/orderbook.py ────┼──── data/derivatives.py
                       spread, depth,        │     OI, funding z, taker flow,
                       imbalance, mid jump   │     quadrant, crowding, cascade risk
                                              ▼
                                  strategies/registry.py
                              ┌───────────┬───────────┬───────────┐
                             S1          S2          S3       S4/S5
                              └───────────┴───────────┴───────────┘
                                              │  StrategyCandidate[]
                                       risk/consensus.py
                              (independent-channel agreement + grading)
                                              │
                                       risk/veto_engine.py
                        G1..G5 HARD BLOCK (non-overridable) · G6..G12 DEGRADE
                                    guard exception ⇒ BLOCK
                                              │
                                       risk/risk_engine.py
                             R:R ≥ 1.8 · ≤3 concurrent · ≤6/day · ≤2R/day · 60m cooldown
                                              │
                                       signals/signal_engine.py
                            limit entry (tick-snapped) · SL · TP1–TP4 · expiry · WHY
                                              │
                        signals/lifecycle.py ── signals/danger.py (advisory only)
                                              │
                                   telegram/{formatter,queue,sender}
                                              │
                                   database/repository.py (audit)
```

## 3. Per-cycle decision order

The sequence is fixed and mirrors the specification; each step can only make the outcome
*more* conservative.

1. **News state** — BLOCKED / stale news layer ⇒ NO TRADE.
2. **Macro/BTC regime** — a regime that invalidates the proposed direction ⇒ BLOCK or DEGRADE.
3. **Data integrity** — any unhealthy feed, stale tick (> 3 s), clock drift > 1500 ms,
   missing instrument metadata ⇒ NO TRADE.
4. **Normalization** — basis, z, percentile, net basis. Unavailable ⇒ NO TRADE.
5. **Liquidity** — spread, depth within ±50 bps, imbalance, mid jump.
6. **Derivatives** — funding z, OI percentile, taker flow, price/OI quadrant.
7. **Market structure** — swing extremes, range bounds, EMA trend, ATR percentile.
8. **Strategies** — S1–S5 run independently; an engine that raises abstains.
9. **Consensus** — direction with the most *independent* groups wins; ties and conflicts are
   NO TRADE; grading by confidence and group count.
10. **Veto** — 12 guards; any hard block stops the signal and is journalled.
11. **Risk** — R:R, concurrency, daily caps, cooldown, advisory size.
12. **Signal** — limit entry snapped to the CoinDCX tick, SL, TP1–TP4, expiry, WHY bullets.
13. **Delivery** — async Telegram queue: signal first, WHY within the 10-second budget.
14. **Audit** — signal, veto rows, news rows, performance facts written to SQLite + JSONL.

## 4. Failure model (fail-closed everywhere)

| Failure | Detection | Response |
|---|---|---|
| Binance WS gap / disconnect | sequence gap, heartbeat timeout, staleness budget | feed marked DEGRADED/STALE → G1 blocks → NO TRADE; REST fallback; reconnect with jittered backoff |
| CoinDCX poll timeout | per-source latency/error tracking | feed unhealthy → G1 blocks |
| Clock drift | venue timestamp vs local clock | > 1500 ms ⇒ normalization raises ⇒ NO TRADE |
| Cross-venue dislocation | basis z | \|z\| ≥ 3.0 or EXTREME ⇒ **G2 hard block** |
| Spread blow-out / thin book | orderbook metrics | G3 block, or G12 degrade → escalation |
| News outage | healthy source count | < `min_sources_healthy` ⇒ news state BLOCK ⇒ NO TRADE |
| Critical news | severity + decay | blackout window; G4 block |
| Extreme crowding | \|funding z\| ≥ 2.5 **and** OI rank ≥ 0.97 | **G5 hard block** |
| Veto guard exception | try/except per guard | converted to a BLOCK (never a silent pass) |
| Missing OI/funding for G5 | `None` inputs | BLOCK — an absent input is not a pass |
| Limit not filled in time | entry-zone/expiry clock | signal EXPIRES; never chased |
| Telegram 429 / outage | HTTP status + retry policy | backoff and retry; queue counts the failure; the bot keeps running |
| Rate-limit pressure | weight budget ≥ 80 % | G1 blocks; the client refuses further spend |

## 5. Audit and reconstructability

`database/repository.py` writes five tables — `signals`, `vetoes`, `news`, `errors`,
`performance` — with a rotating JSONL mirror beside them. A signal row stores the venue
prices, spread, basis, news state, strategy votes and veto status that produced it, so
`repository.reconstruct_signal(signal_id)` returns the complete decision context. The
journals contain no secrets: Telegram tokens are redacted by a logging filter.

## 6. Extensibility

* **New pair** — add it to `config/top20_pairs.yaml`; the loader validates the symbol shape
  and the live metadata check rejects anything CoinDCX does not list.
* **New strategy** — subclass `Strategy`, register an id in `config/strategy.yaml` with its
  correlation group and expiry, then add it to the registry. It inherits the risk floor,
  tick snapping, consensus and veto treatment automatically.
* **New news source** — add it to `config/news.yaml` with a tier. Give it
  `status: UNVERIFIED` until a probe proves it; unverified sources are skipped and reported,
  never trusted.
* **New veto guard** — add the function to `app/risk/veto.py`, register it in
  `VetoEngine.run` and document the threshold in `config/veto.yaml`.
