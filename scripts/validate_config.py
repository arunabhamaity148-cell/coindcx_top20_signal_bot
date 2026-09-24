#!/usr/bin/env python3
"""Validate configuration, the TOP-20 pair universe, and the safety invariants.

    python scripts/validate_config.py            # static validation (no network)
    python scripts/validate_config.py --live     # additionally probe both venues

Exit codes: 0 = all checks passed · 1 = configuration/safety failure · 2 = pair validation
failure · 3 = live probe unavailable (offline, or an endpoint marked UNVERIFIED).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.core.errors import ConfigError, SafetyViolation  # noqa: E402
from app.core.logging_setup import setup_logging  # noqa: E402


def static_checks(config_dir: Path) -> tuple[object, list[str]]:
    problems: list[str] = []
    cfg = load_config(config_dir)
    if cfg.system.mode != "signal_only":
        problems.append(f"system.mode is '{cfg.system.mode}', must be 'signal_only'")
    if not cfg.system.fail_closed:
        problems.append("system.fail_closed is false - fail-closed is mandatory")
    if cfg.veto.override_allowed:
        problems.append("veto.override_allowed is true - every veto must be a hard block")
    if not cfg.veto.hard_block:
        problems.append("veto.hard_block is false")
    if len(cfg.pairs.pairs) != 20:
        problems.append(f"expected exactly 20 pairs, found {len(cfg.pairs.pairs)}")
    seen: set[str] = set()
    for pair in cfg.pairs.pairs:
        if pair.coindcx in seen:
            problems.append(f"duplicate pair {pair.coindcx}")
        seen.add(pair.coindcx)
    if cfg.system.forbidden_credential_env == ():
        problems.append("system.forbidden_credential_env is empty - the boot assertion cannot work")
    for strategy_id in ("S1", "S2", "S3", "S4", "S5"):
        if cfg.strategy.params(strategy_id) is None:
            problems.append(f"strategy {strategy_id} has no configuration block")
    if cfg.risk.min_rr_tp2 < 1.8:
        problems.append(f"risk.min_rr_tp2 {cfg.risk.min_rr_tp2} is below the documented 1.8 floor")
    return cfg, problems


def main() -> int:
    parser = argparse.ArgumentParser(description="validate the bot configuration")
    parser.add_argument("--config-dir", default=str(ROOT / "config"))
    parser.add_argument(
        "--live",
        action="store_true",
        help="also probe CoinDCX/Binance public endpoints (needs network)",
    )
    args = parser.parse_args()
    setup_logging()

    try:
        cfg, problems = static_checks(Path(args.config_dir))
    except (ConfigError, SafetyViolation) as exc:
        print(f"CONFIGURATION FAILED: {exc}")
        return 1

    print(
        f"config        : {len(cfg.pairs.pairs)} pairs, mode={cfg.system.mode}, "
        f"fail_closed={cfg.system.fail_closed}"
    )
    print(f"strategies    : {', '.join(cfg.strategy.enabled)}")
    print(f"hard vetoes   : G1..G5 (override_allowed={cfg.veto.override_allowed})")
    print(
        f"news sources  : {len(cfg.news.verified_sources)} VERIFIED, "
        f"{len(cfg.news.sources) - len(cfg.news.verified_sources)} UNPROVEN (disabled)"
    )

    from app.safety import safety_report

    report = safety_report(cfg)
    print(
        f"safety        : signal_only={report['signal_only']} "
        f"credentials={report['trading_credentials_present'] or 'none'} "
        f"forbidden_code={len(report['forbidden_code_violations'])}"
    )
    if not report["signal_only"] or report["forbidden_code_violations"]:
        problems.append("safety invariants violated")

    if problems:
        print("\nPROBLEMS:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nSTATIC VALIDATION: PASS")

    if args.live:
        from scripts._bootstrap import live_validation

        ok, detail = asyncio.run(live_validation(cfg))
        print(f"LIVE VALIDATION: {'PASS' if ok else 'UNAVAILABLE'} - {detail}")
        return 0 if ok else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
