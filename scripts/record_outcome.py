#!/usr/bin/env python3
"""Record an operator-observed realised R outcome for the signal-only risk guard.

Usage:
  python scripts/record_outcome.py --r -1.0
  python scripts/record_outcome.py --symbol B-BTC_USDT --group MICRO_LIQUIDITY --r 1.5

This records only operator-observed P/L. It never connects to an exchange account and
cannot place, modify or close orders.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.risk.risk_engine import RiskEngine  # noqa: E402
from app.core.timeutils import now_ms  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="record operator-observed realised R")
    parser.add_argument("--r", required=True, type=float, dest="realised_r", help="realised result in R")
    parser.add_argument("--symbol", default="", help="optional pair; used when recording a paper outcome")
    parser.add_argument("--group", default="", help="optional correlation group")
    args = parser.parse_args()

    cfg = load_config(ROOT / "config")
    engine = RiskEngine(cfg, state_path=ROOT / cfg.database.jsonl_path / "risk_state.json")
    if args.symbol:
        engine.record_paper_outcome(symbol=args.symbol, group=args.group, realised_r=args.realised_r, now_ms=now_ms())
    else:
        engine.record_manual_outcome(realised_r=args.realised_r, now_ms=now_ms())
    print(engine.snapshot(now_ms()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
