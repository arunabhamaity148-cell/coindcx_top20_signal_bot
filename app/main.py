"""Entry point.

`main.py` asserts signal-only at boot: it exits fatally if `mode != signal_only`, if any
forbidden capability symbol is present in the source tree, or if ANY exchange trading API
key is configured in the environment (FINAL_DELIVERABLE §V).

Usage:
    python -m app.main --check          # config + safety audit only, then exit
    python -m app.main --dry-run        # run the loop with Telegram delivery simulated
    python -m app.main                  # run the live signal loop (no trading capability exists)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.bot import SignalBot
from app.config import load_config
from app.core.errors import FailClosedError, SafetyViolation
from app.core.logging_setup import get_logger, setup_logging
from app.safety import assert_signal_only, safety_report

log = get_logger("app.main")

EXIT_SAFETY = 3
EXIT_CONFIG = 4
EXIT_HEALTH = 5


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CoinDCX TOP-20 futures SIGNAL-ONLY intelligence bot"
    )
    parser.add_argument("--config-dir", default=None, help="override the config directory")
    parser.add_argument(
        "--check", action="store_true", help="validate config + safety invariants, then exit"
    )
    parser.add_argument("--dry-run", action="store_true", help="force Telegram dry-run mode")
    parser.add_argument(
        "--cycles", type=int, default=None, help="run a bounded number of scan cycles"
    )
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between scan cycles")
    parser.add_argument("--json-logs", action="store_true", help="emit structured JSON logs")
    parser.add_argument("--log-level", default=None)
    return parser.parse_args(argv)


def boot_assert() -> None:
    """The single, non-negotiable gate. Anything unexpected here is fatal."""
    from app.config import load_config as _load

    cfg = _load()
    assert_signal_only(cfg)
    report = safety_report(cfg)
    if not report["signal_only"]:
        raise SafetyViolation(f"signal-only audit failed: {report}")


async def _run(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config_dir)
    except Exception as exc:
        print(f"CONFIG ERROR: {exc}")
        return EXIT_CONFIG

    try:
        assert_signal_only(cfg)
    except SafetyViolation as exc:
        print(f"SAFETY VIOLATION: {exc}")
        print("SIGNAL ENGINE MUST NOT START.")
        return EXIT_SAFETY

    if args.dry_run:
        object.__setattr__(cfg.telegram, "dry_run", True)

    if args.check:
        report = safety_report(cfg)
        print("SIGNAL-ONLY AUDIT")
        for key, value in report.items():
            print(f"  {key}: {value}")
        print(f"pairs configured: {len(cfg.pairs.pairs)}")
        print(f"strategies enabled: {list(cfg.strategy.enabled)}")
        print("CONFIG OK")
        return 0

    bot = SignalBot(cfg, scan_interval_sec=args.interval)
    try:
        report = await bot.boot()
    except FailClosedError as exc:
        await bot.shutdown()
        print(f"STARTUP HEALTH FAILED: {exc}")
        return EXIT_HEALTH
    except Exception as exc:
        await bot.shutdown()
        print(f"STARTUP ERROR: {exc}")
        return EXIT_HEALTH
    print(f"startup health: {report.status}")
    print(report.render())
    try:
        await bot.run(cycles=args.cycles)
    except KeyboardInterrupt:  # pragma: no cover - operator action
        print("interrupted")
    finally:
        await bot.shutdown()
    print(bot.status())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level, json_output=args.json_logs)
    try:
        boot_assert()
    except SafetyViolation as exc:
        print(f"SAFETY VIOLATION AT BOOT: {exc}")
        print("SIGNAL ENGINE MUST NOT START.")
        return EXIT_SAFETY
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
