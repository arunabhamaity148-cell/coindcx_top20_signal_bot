# Troubleshooting

Work top-down: is the process up, is configuration valid, are the feeds healthy, are the
guards blocking, is delivery configured.

## 1. The process will not start

**`SafetyViolation: ... SIGNAL ENGINE MUST NOT START`**
An exchange trading credential is in the environment. Remove `BINANCE_API_KEY`,
`BINANCE_API_SECRET`, `COINDCX_API_KEY` or `COINDCX_API_SECRET` from `.env` *and* from the
shell (`env | grep -i -E 'binance|coindcx'`). This abort is deliberate and cannot be disabled
from configuration.

**`SafetyViolation` mentioning veto override or mode**
`config/system.yaml` has `mode` other than `signal_only`, or `config/veto.yaml` has
`override_allowed: true`. Both are non-negotiable invariants.

**`CONFIGURATION FAILED: AppConfig: unknown configuration keys ['x']`**
A typo or stray key in a YAML file. Unknown keys are rejected loudly so a
mistyped threshold can never be silently ignored.

**`missing configuration file: .../config/risk.yaml`**
Run from the project root, or set `CONFIG_DIR`. Docker mounts `config/` at `/app/config`.

## 2. It runs but never signals

**`NO TRADE` with `unhealthy feeds`**
No egress, a venue outage, or a stale feed. Diagnose with:

```bash
python scripts/healthcheck.py --live
curl -sS https://fapi.binance.com/fapi/v1/ping
curl -sS https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments
```

**`cross-venue normalization unavailable`**
Clock drift above 1500 ms, a missing/invalid orderbook, or no verified FX rate. Check NTP:
`timedatectl status` (Linux) / `w32tm /query /status` (Windows). The bot refuses to compare
misaligned venues by design.

**`insufficient basis history (<obs> < 60)`**
Normal behaviour for the first few minutes after boot. The basis history warms up from live
data; until it does, G2 is fail-closed.

**`news sources healthy 0 < required 2`**
Every RSS feed is unreachable. Verify egress to `cftc.gov`, `sec.gov`, `federalreserve.gov`,
`coindesk.com`, `api.gdeltproject.org`. A news outage blocks trading — deliberately.

**`only 1 engine(s) agree (minimum 2)`**
Working as intended. One engine alone is not a signal.

**`direction conflict with equal independent support`**
Two different channels disagree. The bot stands aside rather than picking a side.

**`ATR unavailable` / few candles**
Not enough history on the trigger timeframe yet. Confirm the data layer is feeding candles:
check `binance_ws` age in `healthcheck`.

## 3. Signals appear but are blocked

Each block writes a row to `vetoes` with a reason and evidence:

```bash
sqlite3 logs/signal_journal.sqlite \
  "select guard, severity, count(*) from vetoes where ts > strftime('%s','now','-1 day')*1000 group by 1,2;"
```

* **G1** — feed age/drift. Look at the evidence map.
* **G2 `divergence EXTREME`** — the venues are genuinely dislocated (a halt, a stale print, a
  liquidity gap). Stand aside until they re-converge; this is the guard doing its job.
* **G3 `spread ... > 12 bps`** — the book is wide. Check whether an event is unfolding.
* **G4 `news state BLOCK`** — read the blocking headline in `news` around that timestamp.
* **G5 `crowded book`** — funding z ≥ 2.5 *and* OI rank ≥ 0.97. The crowd is maximally
  positioned; reversal risk is two-sided.
* **G6/G10/G11/G12 (DEGRADE)** — confidence reduced by 0.15. A grade may drop from A to B; B
  signals are still emitted with a smaller advisory size.

## 4. Telegram problems

**Nothing arrives, logs show `telegram delivery failed`**
Check `TELEGRAM_DRY_RUN`. While `true`, messages are rendered and journalled but never sent.

**`HTTP 401` / `chat not found`**
Wrong token, or you never messaged the bot first. Send `/start` to the bot, re-read the chat id.

**`HTTP 429`**
Rate limited. The queue backs off and retries; it does not crash. If it persists, you are
sending to a group: the limit is 20 messages/min per group, and the queue honours 1 message/sec
per chat.

**The WHY block is late**
Check `max_why_latency_ms` in the queue snapshot (logged each cycle). The signal is always sent
first; WHY is a separate, lower-priority message. If latency exceeds the 10-second budget the
queue reports `why_budget_met: false`.

## 5. Backtest says `NOT RUN`

Expected without a dataset. The scripts never download data and never invent metrics:

```bash
python scripts/run_backtest.py --csv data/BTCUSDT_5m.csv --symbol B-BTC_USDT
```

If your CSV is rejected, check the required columns: `open_time_ms,open,high,low,close,volume`.

## 6. Gates fail

The report names the exact gate and the observed value. Common causes:

* **< 100 trades** — the test window is too short, or the universe is too narrow. Extend the
  dataset; do not lower the gate.
* **Profit factor < 1.25 / average R < 0.05** — costs are eating the edge. That is the finding.
* **Max drawdown > 15R** — the strategy clusters losses; check the per-regime table.
* **Fill rate < 0.35** — limits are too far from the touch. Review the entry offsets.
* **A regime worse than −0.15R** — the strategy is regime-dependent. Either restrict it to its
  documented regimes in `config/strategy.yaml`, or accept it is not deployable.

Never tune against the final holdout. If you changed parameters, re-run the walk-forward and
report the new OOS numbers.

## 7. Soak test says `PENDING`

Correct unless it ran for a full 72 hours. `soak_gate` requires ≥ 72 h of wall time **and** zero
unhandled exceptions. A short smoke soak (`--hours 0 --cycles 30`) is for wiring checks only.

## 8. Performance

High CPU is usually a tight poll interval or a chatty news cycle. Raise the CoinDCX poll
interval, or lower `max_items_per_source`. Memory growth beyond a few hundred MB suggests the
journals are being written somewhere unexpected — check `logs/` ownership and disk space, and
confirm `read_only` mounts are not silently buffering.

## 9. Collecting a support bundle

```bash
python scripts/healthcheck.py > health.txt
python scripts/validate_config.py >> health.txt
tail -n 200 logs/errors.jsonl >> health.txt
sqlite3 logs/signal_journal.sqlite "select * from vetoes order by ts desc limit 50;" >> health.txt
```

Redact `TELEGRAM_BOT_TOKEN` before sharing. The journals never contain secrets, but the console
log line that reports a failed HTTP call may include the request URL.
