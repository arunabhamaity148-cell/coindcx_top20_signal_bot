# Backtesting & Validation

Everything in this document is simulated from historical bars **you supply**. The engine never
downloads data and never invents a result. Without a dataset the scripts print `NOT RUN` and
exit 3 — that is the correct output, not a failure.

## 1. Data format

CSV, one row per bar:

```csv
open_time_ms,open,high,low,close,volume,taker_buy_quote
1695000000000,86120.5,86240.0,86010.0,86180.0,1240.5,620.1
```

`taker_buy_quote` is optional; when present it powers the taker-flow confirmations used by
S1/S2/S4. Export Binance USDⓈ-M klines for the timeframe your strategies read (5 m is the
trigger timeframe; supply 15 m/1 h/4 h series too for faithful multi-timeframe behaviour).

## 2. Execution model

| Aspect | Guarantee |
|---|---|
| **Look-ahead** | eliminated by construction: a signal computed from bar `i` can only fill from bar `i + latency` onward |
| **Limit fills** | simulated with an explicit fill-probability model driven by distance to the limit; never assumed |
| **Missed fills** | counted and reported as `MISSED`/`INVALIDATED`, and excluded from trade statistics |
| **Expiry** | a signal that neither fills nor invalidates inside its window expires and is counted as `EXPIRED`, not as a trade |
| **Ambiguous bars** | a bar whose range touches both the stop and a TP resolves **pessimistically — the stop is taken first** |
| **Fees** | verified CoinDCX maker 0.0236 % / taker 0.059 %, applied per leg |
| **Spread** | charged on entry and exit |
| **Slippage** | 3 bps, always adverse |
| **Latency** | 250 ms between decision and fill eligibility |

Costs are applied on both legs; the net R of every trade therefore already includes the
round-trip cost of the venue. A strategy that is only profitable gross will fail the gates —
that is the point.

## 3. Metrics

Reported per run and per regime: win rate · profit factor · expectancy · average R · max
drawdown (in R) · fill rate · TP1–TP4 reach rates · SL rate · expiry rate · veto rate · signal
frequency · per-pair performance · per-strategy performance · per-regime performance.

Drawdown is computed on the cumulative net-R curve, so it is expressed in the same unit as the
risk you actually take.

## 4. Acceptance gates

A run is **not** production-ready unless every gate passes out-of-sample:

| Gate | Requirement |
|---|---|
| Minimum OOS trades | ≥ 100 |
| Profit factor | ≥ 1.25 |
| Average R | ≥ 0.05 |
| Max drawdown | ≤ 15R |
| Fill rate | ≥ 0.35 |
| Worst regime | no regime worse than −0.15R (regimes with no trades do not count) |

A failing run reports the exact gate and the observed value. `scripts/run_backtest.py` exits
2 when gates fail; it never rounds a failure up to a pass.

## 5. Walk-forward validation

```
[-------- train (expanding) --------][embargo][-- test fold --]
                                    1 % of bars
```

* **6 anchored folds**, expanding training window, no shuffling.
* **1 % embargo** between train and test to kill serial-correlation leakage.
* **Regime buckets:** COMPRESSION · RANGE · TREND_UP · TREND_DOWN · HIGH_VOL · POST_EVENT.
* **Untouched final holdout**, reported but **never** used for parameter selection — the
  runner takes it as a separate callable precisely so it cannot be tuned against.

```bash
python scripts/run_walkforward.py --csv data/BTCUSDT_5m.csv --folds 6 --json-out wf.json
```

The JSON output records the OOS aggregate, the holdout, every gate with its observed and
required values, and the notes (including the holdout disclaimer).

## 6. Bias controls

| Bias | Control |
|---|---|
| Look-ahead | next-bar execution, no same-bar fills, indicator warm-up enforced |
| Survivorship | the universe is the *configured* top-20 at the time of the run; delisted pairs stay in the CSV |
| Data leakage | embargo gap; holdout never feeds tuning; no feature computed across the boundary |
| Overfitting | a fixed, small parameter set per engine; six independent folds must all survive; no per-fold re-tuning |
| Optimistic fills | simulated fills with a probability model; unfilled orders excluded, not silently filled |

## 7. Interpreting the output

A run that produces 12 trades and a profit factor of 3.0 has not validated anything — the
trade-count gate exists for exactly this reason. Read the gates first, the regime table
second, the aggregate metrics last. If the strategy only works in one regime, the
worst-regime gate will tell you.

## 8. Reproducing a run

```bash
python scripts/run_backtest.py --csv data/BTCUSDT_5m.csv --symbol B-BTC_USDT --seed 11 --json-out bt.json
python scripts/run_walkforward.py --csv data/BTCUSDT_5m.csv --symbol B-BTC_USDT --folds 6 --json-out wf.json
```

Both scripts are deterministic for a given `--seed` and dataset. Keep the CSV, the seed and
the config commit together — that triple is the complete reproduction recipe.


## Position-management model

Filled signals are modeled with partial exits: 40% at TP1, 30% at TP2, 20% at TP3, and 10% at TP4. After TP1, the remaining position's stop is moved to breakeven. A candle that touches an active stop and a target is resolved stop-first (pessimistic). Each exit leg accrues the appropriate maker/taker fee; unfilled limits remain outside the trade denominator and are reported as fill-rate misses/expiries.
