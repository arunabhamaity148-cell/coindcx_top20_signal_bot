#!/usr/bin/env python3
"""End-to-end smoke test with NO network access.

Drives the real pipeline (strategies -> consensus -> veto -> risk -> TP/SL -> formatter ->
dry-run queue -> journal) over deterministic synthetic snapshots, then prints the outcome.
This is the fastest way to prove the wiring is intact after a deployment.

Exit codes: 0 = pipeline produced at least one fully-formed decision (signal or NO TRADE)
            without an exception · 1 = the pipeline raised or produced nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.logging_setup import setup_logging  # noqa: E402


async def main_async(cycles: int, seed: int) -> int:
    from app.backtest.harness import SyntheticSnapshotFactory, synthetic_candles
    from app.config import load_config
    from app.database.repository import JournalRepository
    from app.telegram.formatter import MessageFormatter
    from app.telegram.queue import TelegramQueue
    from app.telegram.sender import TelegramSender
    from scripts._bootstrap import build_signal_engine, instrument_for

    cfg = load_config(ROOT / "config")
    pair = cfg.pairs.pairs[0].coindcx
    instrument = instrument_for(cfg, pair)
    engine, _ = build_signal_engine(cfg)
    series = synthetic_candles(600, seed=seed)
    factory = SyntheticSnapshotFactory(cfg=cfg, series=series, instrument=instrument)
    formatter = MessageFormatter(cfg)
    sender = TelegramSender(cfg)
    queue = TelegramQueue(cfg=cfg, formatter=formatter, sender=sender)
    repository = JournalRepository(
        sqlite_path=str(ROOT / "logs" / "smoke.sqlite"),
        jsonl_dir=str(ROOT / "logs"),
        jsonl_mirror=True,
    )

    await queue.start()
    outcomes = {"signal": 0, "no_trade": 0, "error": 0}
    try:
        for index in range(cycles):
            snap = factory(pair, 250 + index)
            if snap is None:
                continue
            try:
                decision = engine.generate(snap=snap)
            except Exception as exc:
                outcomes["error"] += 1
                print(f"  cycle {index}: EXCEPTION {type(exc).__name__}: {exc}")
                continue
            if decision.signal is not None:
                outcomes["signal"] += 1
                repository.record_signal(decision.signal.to_row())
                await queue.publish_signal(decision.signal, engine.why_bullets(decision.signal))
            else:
                outcomes["no_trade"] += 1
            if decision.veto is not None:
                repository.record_vetoes(decision.veto.rows(snap.symbol, snap.ts_ms))
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
        repository.close()

    print(f"SMOKE TEST ({cycles} cycles, seed={seed}, pair={pair})")
    print(f"  signals   : {outcomes['signal']}")
    print(f"  no-trades : {outcomes['no_trade']}")
    print(f"  errors    : {outcomes['error']}")
    print(f"  telegram  : {queue.snapshot()}")
    decision = (
        "PASS"
        if outcomes["error"] == 0 and outcomes["signal"] + outcomes["no_trade"] > 0
        else "FAIL"
    )
    print(f"  decision  : {decision}")

    return 0 if outcomes["error"] == 0 and outcomes["signal"] + outcomes["no_trade"] > 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="offline end-to-end smoke test")
    parser.add_argument("--cycles", type=int, default=40)
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()
    setup_logging(level="WARNING")
    return asyncio.run(main_async(args.cycles, args.seed))


if __name__ == "__main__":
    raise SystemExit(main())
