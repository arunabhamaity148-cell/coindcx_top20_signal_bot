# Deployment

Three supported environments: Ubuntu (recommended), Windows, Docker. The bot is a single
asyncio process — no message broker, no database server, no privileged ports.

## 0. Before you deploy

* Python **3.11+** (Ubuntu 22.04 ships 3.10 — install 3.11+ from deadsnakes or pyenv).
* Outbound HTTPS/WSS to Binance and CoinDCX public endpoints; DNS working.
* **NTP time sync enabled.** Clock drift above 1500 ms makes normalization fail closed, and
  the bot will simply stop emitting signals. That is intentional — but it looks like a
  mystery outage if you do not know about it.
* No exchange trading credential anywhere in the environment. If one exists, the process
  aborts by design.

---

## 1. Ubuntu VPS (systemd)

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin signalbot
sudo mkdir -p /opt/coindcx_top20_signal_bot && sudo chown -R signalbot:signalbot /opt/coindcx_top20_signal_bot

# as root, from your checkout
sudo cp -r . /opt/coindcx_top20_signal_bot/
cd /opt/coindcx_top20_signal_bot
sudo -u signalbot python3.11 -m venv .venv
sudo -u signalbot .venv/bin/pip install --upgrade pip
sudo -u signalbot .venv/bin/pip install -r requirements.txt
sudo -u signalbot cp .env.example .env
sudo -u signalbot chmod 600 .env
```

Validate before installing the unit:

```bash
sudo -u signalbot .venv/bin/python scripts/validate_config.py
sudo -u signalbot .venv/bin/python -m pytest -q
sudo -u signalbot .venv/bin/python scripts/smoke_test.py
```

`/etc/systemd/system/coindcx-signal-bot.service`:

```ini
[Unit]
Description=CoinDCX TOP-20 signal-only futures intelligence bot
After=network-online.target time-sync.target
Wants=network-online.target time-sync.target

[Service]
Type=simple
User=signalbot
Group=signalbot
WorkingDirectory=/opt/coindcx_top20_signal_bot
EnvironmentFile=/opt/coindcx_top20_signal_bot/.env
ExecStartPre=/opt/coindcx_top20_signal_bot/.venv/bin/python scripts/validate_config.py
ExecStart=/opt/coindcx_top20_signal_bot/.venv/bin/python -m app.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=coindcx-signal-bot

# hardening: the process needs no privileges at all
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
ReadWritePaths=/opt/coindcx_top20_signal_bot/logs /opt/coindcx_top20_signal_bot/data
CapabilityBoundingSet=
SystemCallFilter=@system-service
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now coindcx-signal-bot
systemctl status coindcx-signal-bot
journalctl -u coindcx-signal-bot -f
```

Note the asymmetry in the hardening block: `config/` stays writable by the service user only
because it lives inside `WorkingDirectory`. If you prefer immutability, mount it read-only and
add it to `ReadOnlyPaths`.

---

## 2. Windows 10/11

```powershell
py -3.11 -m venv C:\bots\signalbot\.venv
C:\bots\signalbot\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python scripts\validate_config.py
python -m pytest -q
python scripts\smoke_test.py
python -m app.main
```

Run it as a scheduled task so it restarts automatically:

* Program: `C:\bots\signalbot\.venv\Scripts\python.exe`
* Arguments: `-m app.main`
* Start in: `C:\bots\signalbot`
* Trigger: *At startup*; Settings: *Restart the task if it fails*, every 1 minute, up to 3 times.
* Enable *Run whether user is logged on or not*.

Windows Defender Firewall needs no inbound rule — the bot only makes outbound connections.
Windows Time service must be running (`w32tm /resync`) so clock drift stays inside budget.

---

## 3. Docker

```bash
cp .env.example .env       # fill in Telegram values; leave dry-run true at first
docker compose up -d --build
docker compose logs -f signal-bot
docker compose run --rm validate        # one-shot live pair validation
docker compose down
```

The image runs as uid 10001, drops all Linux capabilities, mounts `config/` read-only and
declares a 5-minute health check. Journals persist in `./logs`; `data/` is available for
historical CSVs mounted for backtests.

---

## 4. Startup safety check

`python -m app.main` performs, in order, before any signal can be produced:

1. **configuration validation** — all YAML loaded, unknown keys rejected, invariants checked;
2. **safety assertion** — mode, veto overrides, forbidden capabilities, and the credential scan;
3. **TOP-20 validation** — exactly 20 pairs, symbol shape, no duplicates;
4. **CoinDCX instrument validation** — live metadata for every pair (tick, quantity increment,
   minimum size and notional);
5. **Binance market validation** — the mapped USDⓈ-M symbol exists;
6. **feed health** — REST/WS reachability and freshness;
7. **news health** — at least `min_sources_healthy` responding sources;
8. **database health** — the journal is writable;
9. **Telegram health** — a dry-run or a configured token.

Any critical failure prints the blockers and **refuses to start signal generation**. Run the
same checks on demand with `python scripts/healthcheck.py [--live]`.

---

## 5. 72-hour soak test

Before you trust a deployment, observe it:

```bash
python scripts/soak_test.py --hours 72 --json-out soak.json
```

Telemetry: feed stability · stale-data events · signal latency · veto rate · news latency ·
WHY latency · duplicate signals · exceptions · CPU · RSS · restart recovery. The gate prints
**PASSED** only after 72 hours of wall time with zero unhandled exceptions; anything shorter is
**PENDING**. No auto-trading occurs at any point.

Rehearse a restart during the soak (`systemctl restart coindcx-signal-bot`) and confirm the
journal re-opens and the live-signal registry rebuilds.

---

## 6. Maintenance

| Cadence | Task |
|---|---|
| Daily | skim `logs/errors.jsonl`; confirm no feed has been flapping |
| Weekly | review delivery latency and WHY budget adherence; check disk usage of journals |
| Monthly | recalibrate veto thresholds against realised spread/slippage; re-run `validate_config.py --live` (venues change tick sizes and minimums) |
| Quarterly | re-run the full backtest + walk-forward on fresh data and audit strategy performance per regime |
| Quarterly | rotate the Telegram bot token (`/revoke` in BotFather), then update `.env` and restart |
| On change | `pip-audit` / dependency bump, re-run the suite, then redeploy |

Host hardening checklist: NTP enabled · no exchange key on the box · `.env` mode 600 owned by
the service user · `logs/` outside version control · OS security updates applied · outbound
egress restricted to the venue and Telegram domains if the host allows it.

---

## 7. Monitoring

Expose the health surface however you prefer — `scripts/healthcheck.py` is exit-code driven, so
any supervisor, cron mailer or uptime monitor can consume it:

```bash
* * * * * /opt/coindcx_top20_signal_bot/.venv/bin/python \
            /opt/coindcx_top20_signal_bot/scripts/healthcheck.py --quiet \
            || logger -t signalbot "healthcheck FAILED"
```

Key dashboard metrics: feed ages · divergence z · spread bps · open signals · veto counts by
guard · grade mix · fill rate · average R · drawdown · WHY latency. All are logged; the
journals are the source of truth.
