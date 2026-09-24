#!/usr/bin/env python3
"""72-hour paper / soak observation run (master prompt §24).

    python scripts/soak_test.py --hours 72            # full paper observation window
    python scripts/soak_test.py --hours 0 --cycles 30 # short smoke soak (reports PENDING)

PAPER MODE ONLY: drives the real pipeline over synthetic snapshots (or a live feed when
--live is given) and records stability telemetry. It places nothing, holds no exchange key,
and cannot close anything - that capability does not exist in this codebase.

Exit codes: 0 = soak gate PASSED · 2 = FAILED (unhandled exceptions) · 3 = PENDING
            (the window has not been observed for the full 72 hours).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.logging_setup import setup_logging  # noqa: E402
from app.core.timeutils import now_ms  # noqa: E402


async def run(args) -> int:
    from app.backtest.harness import SyntheticSnapshotFactory, synthetic_candles
    from app.config import load_config
    from app.database.repository import JournalRepository
    from app.monitoring.soak import SoakHarness, SoakSample
    from scripts._bootstrap import build_signal_engine, instrument_for

    cfg = load_config(ROOT / "config")
    pair = cfg.pairs.pairs[0].coindcx
    instrument = instrument_for(cfg, pair)
    engine, _ = build_signal_engine(cfg)
    series = synthetic_candles(900, seed=args.seed)
    factory = SyntheticSnapshotFactory(cfg=cfg, series=series, instrument=instrument)
    repository = JournalRepository(
        sqlite_path=str(ROOT / "logs" / "soak.sqlite"),
        jsonl_dir=str(ROOT / "logs"),
        jsonl_mirror=True,
    )

    if args.hours > 0:
        cycles = max(1, int(args.hours * 3600 / max(1.0, args.poll_sec)))
    else:
        cycles = args.cycles

    def cycle(index: int) -> SoakSample:
        started = now_ms()
        snap = factory(pair, 250 + (index % 600))
        sample = SoakSample(index=index, ts_ms=started)
        if snap is None:
            return sample
        decision = engine.generate(snap=snap)
        sample.latency_ms = now_ms() - started
        if decision.signal is not None:
            sample.signals = 1
            repository.record_signal(decision.signal.to_row())
        else:
            sample.no_trades = 1
            if decision.no_trade is not None:
                repository.record_error("no_trade", decision.no_trade.reason, decision.no_trade.to_row())
        if decision.veto is not None and decision.veto.blocked:
            sample.veto_blocks = 1
            repository.record_vetoes(decision.veto.rows(snap.symbol, snap.ts_ms))
        stale = [n for n, h in snap.feed_health.items() if h.state.value != "HEALTHY"]
        sample.stale_feeds = len(stale)
        return sample

    harness = SoakHarness(
        cycle=cycle, cycles=cycles, interval_sec=args.poll_sec if args.hours > 0 else 0.0
    )
    print(
        f"soak: {cycles} cycles, interval {harness.interval_sec}s, pair {pair}, "
        f"target {args.hours or 0:.0f}h"
    )
    try:
        report = await harness.run()
    finally:
        repository.close()

    harness.simulate_restart()  # rehearse restart recovery from the journal
    report = harness.report(started_ms=report.started_ms, peak_rss_mb=report.peak_rss_mb)
    print(report.render())
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "cycles": report.cycles,
                    "duration_hours": report.duration_hours,
                    "signals": report.signals,
                    "no_trades": report.no_trades,
                    "veto_blocks": report.veto_blocks,
                    "veto_rate": report.veto_rate,
                    "stale_feed_events": report.stale_feed_events,
                    "exceptions": list(report.exceptions),
                    "max_signal_latency_ms": report.max_signal_latency_ms,
                    "peak_rss_mb": report.peak_rss_mb,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"written: {args.json_out}")

    from app.monitoring.soak import soak_gate

    gate = soak_gate(report)
    return 0 if gate.passed else (2 if gate.status == "FAILED" else 3)


def main() -> int:
    parser = argparse.ArgumentParser(description="72-hour paper/soak observation")
    parser.add_argument(
        "--hours",
        type=float,
        default=72.0,
        help="observation window; 0 = use --cycles for a short smoke soak",
    )
    parser.add_argument("--cycles", type=int, default=60)
    parser.add_argument("--poll-sec", type=float, default=60.0)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--json-out")
    args = parser.parse_args()
    setup_logging(level="WARNING")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
