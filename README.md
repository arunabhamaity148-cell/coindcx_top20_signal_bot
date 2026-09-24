# CoinDCX TOP-20 — Signal-Only Crypto Futures Intelligence Bot

A production-oriented, **signal-only** intelligence system for the **top 20 CoinDCX
USDT-margined perpetual futures** pairs, validated against **Binance USDⓈ-M** market data.

It emits human-executable Telegram signals — limit entry, structure-based stop, four
take-profits, expiry, R:R and a ≤10-second *WHY* block — and nothing else.

> ## 🛑 SIGNAL ONLY — READ THIS FIRST
> This bot **holds no exchange trading key and cannot place, cancel, modify or close any
> order.** It cannot change leverage and cannot transfer funds. The capability does not
> exist anywhere in the source tree, and the static scanner in `app/safety.py` fails the
> test suite if such a call site is ever introduced.
>
> At boot the process asserts the invariants and **aborts** if it finds a Binance/CoinDCX
> trading credential in the environment, if `mode != signal_only`, or if veto overrides are
> enabled. Missing, stale or contradictory data always produces **NO TRADE** — never a guess.
>
> Nothing here is financial advice. Crypto derivatives can lose you all of your capital.

---

## 1. What it does

| Layer | Behaviour |
|---|---|
| **Data** | Binance USDⓈ-M public REST + WebSocket (mark price, depth, klines, open interest, funding, taker flow) and CoinDCX Futures public REST (instruments, ticker, orderbook) |
| **Normalization** | Quote/contract/timestamp alignment; the raw Binance USDT mid is **never** compared to a raw CoinDCX figure. Clock drift > 1500 ms and unavailable FX are **fail-closed** |
| **Divergence** | Basis in bps, z-score, percentile, vol-adjusted z, net basis after fees. Bands: NORMAL < 1.0, ELEVATED 1–2, ABNORMAL 2–3, **EXTREME ≥ 3.0 = hard block** |
| **Strategies** | Exactly five research-selected strategy modules: **S1** Liquidity Sweep & Reclaim · **S2** Volatility Compression → Range Expansion · **S3** Funding/Crowding Exhaustion Reversal · **S4** OI-Confirmed Trend Continuation · **S5** Cross-Venue Basis Convergence |
| **Consensus** | Agreement counted by *information channel*, not by engine count — correlated engines are never double-counted. Grades: **A+** ≥0.82 & ≥3 groups, **A** ≥0.66 & ≥2, **B** ≥0.55 & ≥1, else **NO TRADE** |
| **Veto** | 12 guards. **G1** Data Integrity · **G2** Cross-Exchange Divergence · **G3** Liquidity/Slippage · **G4** News Shock · **G5** Crowding are **HARD BLOCKS, non-overridable**. A guard that raises becomes a BLOCK |
| **Risk** | R:R ≥ 1.8 · max 3 concurrent · max 6/day · max 2R daily loss · 60-min cooldown — all evaluated **before** any publication |
| **Signals** | LIMIT entry only, snapped to the CoinDCX tick. Price runs away → the signal **EXPIRES**, it is never chased |
| **News** | Free RSS/JSON only (CFTC, SEC, Federal Reserve, CoinDesk, GDELT). Credibility hierarchy + mandatory half-life decay. A single unverified rumour can **never** produce HIGH or CRITICAL |
| **DANGER** | Continuous re-evaluation of live signals → advisory alert only. **NO AUTO-CLOSE. MANUAL ACTION REQUIRED.** |
| **Audit** | SQLite + rotating JSONL journals: `signals`, `vetoes`, `news`, `errors`, `performance`. Any signal is fully reconstructable |
| **Validation** | Real event-driven backtester (next-bar fills, simulated limit fills, fees + spread + slippage + latency), 6-fold anchored walk-forward with a 1 % embargo, untouched final holdout, and the documented acceptance gates |

---

## 2. Requirements

* **Python 3.11 or newer** (developed on 3.11/3.12)
* ~500 MB RAM, 1 CPU core, a few hundred MB of disk for journals
* Outbound HTTPS/WSS to Binance and CoinDCX public endpoints
* Optional: Docker 24+ and Docker Compose v2
* Optional: a Telegram bot token + chat id (the bot runs fine without one, in dry-run)

No exchange account, no API key, and no paid data subscription is required.

---

## 3. Quick start (Ubuntu / macOS)

```bash
git clone <your-repo> coindcx_top20_signal_bot
cd coindcx_top20_signal_bot

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env          # fill in Telegram values later; dry-run is default
```

Validate, smoke-test, then run:

```bash
python scripts/validate_config.py          # config + pair universe + safety invariants
python -m pytest -q                        # full test suite (no network needed)
python scripts/smoke_test.py               # offline end-to-end pipeline check
python scripts/healthcheck.py              # journal/telegram/config health
python -m app.main                         # start the signal loop (dry-run Telegram)
```

---

## 4. Quick start (Windows 10/11, PowerShell)

```powershell
git clone <your-repo> coindcx_top20_signal_bot
cd coindcx_top20_signal_bot

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

Copy-Item .env.example .env

python scripts/validate_config.py
python -m pytest -q
python scripts/smoke_test.py
python -m app.main
```

If PowerShell blocks the activation script, run once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

---

## 5. Quick start (Docker)

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f signal-bot
docker compose down
```

The compose file mounts `config/` **read-only**, drops all Linux capabilities, runs as a
non-root user (uid 10001) and pins a health check. A one-shot validation job is available:

```bash
docker compose run --rm validate
```

---

## 6. Configuration

Everything tunable lives in `config/*.yaml` — there are no magic numbers in the engine.

| File | Contents |
|---|---|
| `config/top20_pairs.yaml` | The 20 CoinDCX pairs + their Binance counterparts, fee defaults, precision overrides, validation rules |
| `config/system.yaml` | Mode (`signal_only`), fail-closed flag, forbidden capabilities/credentials, exchange bases, rate-limit budget, staleness budgets, Telegram limits, database paths, boot checks |
| `config/strategy.yaml` | Shared thresholds + per-strategy parameters (S1–S5), regimes, expiry, cooldown |
| `config/veto.yaml` | Hard-block thresholds (G1–G5), degrade tier (G6–G12), escalation rules |
| `config/news.yaml` | Sources with VERIFIED/UNPROVEN status, credibility hierarchy, impact weights, half-lives, severity thresholds, state mapping |
| `config/risk.yaml` | R:R floor, TP R-multiples, SL buffers, expiry per grade, concurrency/daily caps, advisory sizing |

**To change the tradable universe**, edit `config/top20_pairs.yaml` only — pairs are never
hard-coded in modules. Every pair is then validated against live CoinDCX instrument
metadata:

```bash
python scripts/validate_config.py --live
```

A pair that fails validation is **rejected**. It is never silently replaced.

---

## 7. Validation, smoke test and the test suite

```bash
python scripts/validate_config.py            # exit 0 = pass, 1 = config/safety, 3 = live unavailable
python scripts/smoke_test.py --cycles 40     # offline end-to-end pipeline; writes logs/smoke.sqlite
python -m pytest -q                          # 172 deterministic tests, zero network access
python -m pytest tests/safety -q             # fail-closed / no-trading-capability proofs only
```

The suite covers: normalization, basis/z-score, stale detection, orderbook, funding, OI,
**S1–S5**, **every veto guard**, risk, TP/SL, expiry, duplicate prevention, Telegram
formatting, the news engine, fail-closed behaviour, and the full
`DATA → NORMALIZATION → STRATEGY → VETO → RISK → SIGNAL → TELEGRAM` integration path.

---

## 8. Backtesting

The backtester consumes **real historical bars you supply** — it never downloads data and
never invents results.

```bash
# export Binance USDⓈ-M klines to CSV with columns:
#   open_time_ms,open,high,low,close,volume[,taker_buy_quote]
python scripts/run_backtest.py     --csv data/BTCUSDT_5m.csv --symbol B-BTC_USDT
python scripts/run_walkforward.py  --csv data/BTCUSDT_5m.csv --symbol B-BTC_USDT --folds 6
```


### Current release boundary

This build is **production-hardened / production-candidate**, not empirically production-validated.
The software enforces signal-only operation and fail-closed safety, but deployment acceptance still
requires real Binance/CoinDCX connectivity, real historical OOS/holdout evaluation, and a 72-hour
paper observation. No claim of profitability or live-feed validation is made from the offline suite.

Binance USDⓈ-M WebSocket routing is implemented for the current `/public` and `/market` routed
endpoints. CoinDCX Futures orderbook and candlestick polling uses the currently documented public
market-data endpoints, while exchange trading APIs remain intentionally unused.

Without `--csv`, both scripts print **`NOT RUN`** and exit 3. That is the correct, honest
output: a profitability figure you did not compute does not exist.

Modelling guarantees: next-bar execution only, simulated limit fills with a fill-probability
model, missed fills and expiries counted, verified CoinDCX maker/taker fees + spread +
slippage + latency applied, ambiguous bars resolved pessimistically (stop first).

**Acceptance gates (all must pass, out-of-sample):** ≥100 trades · profit factor ≥1.25 ·
average R ≥0.05 · max drawdown ≤15R · fill rate ≥0.35 · no regime worse than −0.15R.

---

## 9. Walk-forward validation

6 anchored (expanding-window) folds, a 1 % embargo between train and test, regime buckets
(compression / range / trend / high-volatility / post-event) and an **untouched final
holdout** that is reported but never used for parameter selection.

```bash
python scripts/run_walkforward.py --csv data/BTCUSDT_5m.csv --folds 6 --json-out wf.json
```

Exit codes: `0` gates passed · `2` gates failed · `3` not run.

---

## 10. Dry-run and paper observation

`TELEGRAM_DRY_RUN=true` (the default) renders and journals every message but sends nothing.
Watch the formatted output in the logs before enabling delivery.

For the 72-hour paper/soak observation window:

```bash
python scripts/soak_test.py --hours 72     # paper mode; no orders, no keys
```

It records feed stability, stale-data events, signal/WHY latency, veto rate, duplicates,
exceptions, CPU/RSS and restart recovery, and prints **PENDING** until a full 72 hours of
wall time have elapsed. A short run can never be reported as a completed soak test.

---

## 11. Running in production

```bash
# Ubuntu: systemd unit (a hardened template ships in docs/DEPLOYMENT.md)
sudo cp deploy/coindcx-signal-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now coindcx-signal-bot
journalctl -u coindcx-signal-bot -f
```

A production host should run the bot as an unprivileged user with `config/` read-only, and
should never carry an exchange trading credential.

---

## 12. Telegram setup

1. Talk to **@BotFather** → `/newbot` → copy the token.
2. Send your new bot any message, then find your chat id (e.g. via `@userinfobot`, or the
   `getUpdates` endpoint).
3. Put both values in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=987654321
   TELEGRAM_DRY_RUN=false
   ```
4. Restart. Delivery is queued asynchronously: the **signal** goes first, the **WHY** block
   follows within the 10-second budget, and a 429 is retried with backoff. Telegram being
   down degrades delivery — it never crashes the bot.

Delivery limits honoured by the queue: 1 message/sec per chat, 20 messages/min per group.
Messages are capped at 1024 characters.

Format:

```
🚨 SIGNAL | B-BTC_USDT — LONG
🧠 Grade: A   📊 Confidence: 84%
🎯 LIMIT ENTRY: 86120 – 86180
🛑 SL: 85620
🎯 TP1: 86680   🎯 TP2: 87240
🎯 TP3: 88020   🎯 TP4: 88980
📐 R:R: 1 : 2.1   ⏳ Expiry: 45m
📰 News: no blocking event   🏦 Binance: healthy   🏦 CoinDCX: healthy
🛡️ Veto: PASS
ID: CSB-20260922-4F1A2C
```

---

## 13. Logs and journal

```
logs/
├── signal_journal.sqlite     # SQLite: signals, vetoes, news, errors, performance
├── signals.jsonl             # rotating line-delimited mirror (grep/jq friendly)
├── vetoes.jsonl
├── news.jsonl
├── errors.jsonl
└── bot.log                   # structured JSON logs (human-readable when LOG_JSON=false)
```

Every signal row carries the inputs it was built from — prices, basis, spread, derivatives
state, strategy votes, veto states, news state — so any signal can be **fully reconstructed**
after the fact:

```bash
sqlite3 logs/signal_journal.sqlite \
  "select signal_id, symbol, grade, confidence, entry, sl, tp2, reason from signals order by ts desc limit 5;"
```

---

## 14. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `SafetyViolation: ... SIGNAL ENGINE MUST NOT START` | an exchange key is in the environment | remove `BINANCE_*` / `COINDCX_*` from `.env` and the shell |
| `CONFIGURATION FAILED: ... unknown configuration keys` | a typo in a YAML file | fix the key — unknown keys are rejected loudly, never ignored |
| Everything is `NO TRADE` with `unhealthy feeds` | no outbound network, or a venue is down | `python scripts/healthcheck.py --live` |
| `news sources healthy 0 < required 2` | all RSS feeds unreachable | check egress; the news layer is fail-closed by design |
| `cross-venue normalization unavailable` | clock drift > 1500 ms, or no verified FX | enable NTP; the bot refuses to compare misaligned venues |
| `NOT RUN` from a backtest script | no `--csv` supplied | export real klines — results are never fabricated |
| Telegram silent | dry-run is on | set `TELEGRAM_DRY_RUN=false` and restart |
| `HARD BLOCK G2 divergence EXTREME` | venues genuinely dislocated | correct behaviour — stand aside until they re-converge |

More detail in [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

---

## 15. Project layout

```
coindcx_top20_signal_bot/
├── app/
│   ├── main.py                 # boot checks -> async signal loop
│   ├── bot.py                  # orchestration (feeds, pipeline, lifecycle, DANGER)
│   ├── config.py               # typed config loading (fail-closed)
│   ├── safety.py               # boot assertions + forbidden-capability scanner
│   ├── core/                   # errors, logging, math, models, time utils
│   ├── data/                   # binance/, coindcx/, normalization, orderbook, derivatives, health
│   ├── news/                   # collectors, parser, deduper, credibility, impact, decay, correlation, engine
│   ├── strategies/             # base, registry, s1..s5
│   ├── risk/                   # veto, veto_engine, consensus, risk_engine, btc_regime
│   ├── signals/                # signal_engine, models, tpsl, expiry, lifecycle, danger
│   ├── telegram/               # formatter, queue, sender
│   ├── database/               # models, repository, migrations
│   ├── backtest/               # engine, fills, costs, metrics, walk_forward, harness
│   └── monitoring/             # health, metrics, journals, soak
├── config/                     # top20_pairs, system, strategy, veto, news, risk
├── tests/                      # unit, integration, strategies, veto, news, safety
├── scripts/                    # validate_config, smoke_test, healthcheck, run_backtest,
│                               # run_walkforward, soak_test
├── docs/                       # ARCHITECTURE, STRATEGIES, VETO_SYSTEM, NEWS_ENGINE,
│                               # BACKTESTING, DEPLOYMENT, TROUBLESHOOTING
├── data/  logs/                # runtime directories (.gitkeep'd)
├── .env.example  .gitignore  Dockerfile  docker-compose.yml
├── requirements.txt  pyproject.toml  README.md  LICENSE
```

---

## 16. Safety restrictions (non-negotiable)

* No order placement, cancellation, modification, close, leverage change or transfer.
* No exchange private/trading API is used, referenced or required.
* Every veto is a **hard block** — no strategy score can override it.
* Missing, stale or contradictory data ⇒ **NO TRADE**.
* Limit-order planning only. Price runs away ⇒ the signal **expires**; it is never chased.
* **NO TRADE is a successful output.** There is no signal quota and no pressure to emit one.
* A single unverified news rumour can never produce a HIGH or CRITICAL severity.
* The DANGER monitor is advisory only: it never closes anything.

---

## 17. Documentation

* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components, data flow, failure modes
* [`docs/STRATEGIES.md`](docs/STRATEGIES.md) — S1–S5 trigger logic, invalidation, expiry
* [`docs/VETO_SYSTEM.md`](docs/VETO_SYSTEM.md) — the 12 guards and their thresholds
* [`docs/NEWS_ENGINE.md`](docs/NEWS_ENGINE.md) — sources, credibility, impact, decay
* [`docs/BACKTESTING.md`](docs/BACKTESTING.md) — execution model, gates, walk-forward
* [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — Ubuntu/Windows/Docker, systemd, hardening
* [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — symptom → cause → fix

---

## 18. Licence

MIT — see [`LICENSE`](LICENSE). Includes an additional notice: this software is
informational tooling, not financial advice.
