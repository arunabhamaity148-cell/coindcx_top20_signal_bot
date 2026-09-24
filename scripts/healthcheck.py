#!/usr/bin/env python3
"""Health probe used by Docker HEALTHCHECK and by operators.

Checks, in order: configuration load · safety invariants · journal writability ·
log directory · optional live feed reachability (only with --live).

Exit codes: 0 healthy · 1 unhealthy/config error. `--quiet` prints one line.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="health probe")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    problems: list[str] = []
    try:
        cfg = load_config(ROOT / "config")
    except Exception as exc:
        print(f"UNHEALTHY: configuration could not be loaded ({exc})")
        return 1

    from app.database.repository import JournalRepository
    from app.safety import safety_report

    report = safety_report(cfg)
    if not report["signal_only"]:
        problems.append("signal-only invariants violated")
    if report["trading_credentials_present"]:
        problems.append(f"trading credentials present: {report['trading_credentials_present']}")

    logs = ROOT / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        probe = logs / ".health_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        problems.append(f"log directory not writable: {exc}")

    repository = None
    try:
        repository = JournalRepository(
            sqlite_path=str(logs / "signal_journal.sqlite"), jsonl_dir=str(logs), jsonl_mirror=True
        )
        db_health = repository.health()
        if db_health.get("state") != "HEALTHY":
            problems.append(f"journal database: {db_health}")
    except Exception as exc:
        problems.append(f"journal database error: {exc}")
    finally:
        if repository is not None:
            repository.close()

    if args.live:
        from scripts._bootstrap import live_validation

        ok, detail = asyncio.run(live_validation(cfg))
        if not ok:
            problems.append(f"live feed probe: {detail}")

    status = "HEALTHY" if not problems else "UNHEALTHY"
    if args.quiet:
        print(f"{status} problems={len(problems)}")
    else:
        print(f"BOT HEALTH: {status}")
        print(
            f"  pairs: {len(cfg.pairs.pairs)}  mode: {cfg.system.mode}  "
            f"telegram_dry_run: {cfg.telegram.dry_run}"
        )
        print(
            f"  safety: signal_only={report['signal_only']} "
            f"forbidden_code={len(report['forbidden_code_violations'])}"
        )
        for problem in problems:
            print(f"  PROBLEM: {problem}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
