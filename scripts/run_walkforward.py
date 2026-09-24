#!/usr/bin/env python3
"""Walk-forward validation (6 anchored folds, 1 % embargo, untouched final holdout).

    python scripts/run_walkforward.py --csv data/BTCUSDT_5m.csv --symbol B-BTC_USDT

Without a real dataset the script prints NOT RUN. Metrics are only ever reported from bars
that were actually simulated; the final holdout is never used for parameter selection.

Exit codes: 0 = all acceptance gates PASSED · 2 = gates FAILED · 3 = NOT RUN.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="walk-forward validation")
    parser.add_argument("--csv", help="historical OHLCV csv (required to actually run)")
    parser.add_argument("--symbol", default="B-BTC_USDT")
    parser.add_argument("--folds", type=int, default=6)
    parser.add_argument("--bar-minutes", type=float, default=1.0,
                        help="source candle resolution; 1m is required for the full S1-S5 set")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--json-out")
    args = parser.parse_args()
    setup_logging(level="WARNING")

    if not args.csv or not Path(args.csv).exists():
        print("WALK-FORWARD: NOT RUN")
        print(f"  reason: {'no --csv supplied' if not args.csv else 'csv not found'}")
        print("  No walk-forward, OOS or holdout metric is reported without real bars.")
        return 3

    from app.backtest.engine import BacktestEngine, regime_label
    from app.backtest.harness import SyntheticSnapshotFactory
    from app.backtest.walk_forward import anchored_folds, run_walk_forward
    from app.config import load_config
    from scripts._bootstrap import build_signal_engine, instrument_for

    sys.path.insert(0, str(ROOT / "scripts"))
    from run_backtest import load_csv

    cfg = load_config(ROOT / "config")
    if args.bar_minutes <= 0:
        raise SystemExit("--bar-minutes must be positive")
    required_1m = {"S1", "S5"} & set(cfg.strategy.enabled)
    if required_1m and abs(args.bar_minutes - 1.0) > 1e-9:
        raise SystemExit("full strategy validation requires --bar-minutes 1 because S1/S5 use 1m triggers")
    bars = load_csv(Path(args.csv))
    instrument = instrument_for(cfg, args.symbol)
    folds = anchored_folds(
        len(bars), folds=args.folds, embargo_pct=float(cfg.backtest.embargo_pct),
        min_train=500
    )
    if len(folds) != args.folds:
        print(f"WALK-FORWARD: NOT RUN - could construct only {len(folds)} of {args.folds} folds")
        return 3

    def _backtester_for(engine, factory, seed):
        from app.backtest.costs import CostModel
        from app.backtest.fills import LimitFillModel
        return BacktestEngine(
            cfg=cfg, signal_engine=engine, snapshot_factory=factory, seed=seed,
            warmup_bars=int(cfg.backtest.warmup_bars),
            cost_model=CostModel(
                maker_fee_pct=cfg.backtest.maker_fee_pct, taker_fee_pct=cfg.backtest.taker_fee_pct,
                slippage_bps=cfg.backtest.slippage_bps, spread_cost_bps=cfg.backtest.spread_cost_bps,
                latency_ms=cfg.backtest.latency_ms, use_taker_for_stop=cfg.backtest.use_taker_for_stop,
            ),
            fill_model=LimitFillModel(
                model=cfg.backtest.limit_fill_probability_model,
                max_probability=cfg.backtest.limit_fill_prob_max,
                latency_bars=1 if cfg.backtest.next_bar_fills_only else 0,
                seed=seed,
            ),
        )

    def runner(train_end: int, test_start: int, test_end: int):
        engine, _ = build_signal_engine(cfg)
        factory = SyntheticSnapshotFactory(
            cfg=cfg, series=bars, instrument=instrument, bar_minutes=args.bar_minutes
        )
        backtester = _backtester_for(engine, factory, args.seed + test_start)
        metrics, _trades = backtester.run(
            symbol=args.symbol, bars=bars, regime=regime_label(bars[test_start:test_end]),
            bar_minutes=args.bar_minutes, start_index=test_start, end_index=test_end
        )
        return metrics

    holdout_start = folds[-1].test_end

    def holdout_runner():
        engine, _ = build_signal_engine(cfg)
        factory = SyntheticSnapshotFactory(
            cfg=cfg, series=bars, instrument=instrument, bar_minutes=args.bar_minutes
        )
        backtester = _backtester_for(engine, factory, args.seed + 10_000)
        metrics, _trades = backtester.run(
            symbol=args.symbol, bars=bars, regime="HOLDOUT", bar_minutes=args.bar_minutes,
            start_index=holdout_start, end_index=len(bars)
        )
        return metrics

    result = run_walk_forward(
        total_bars=len(bars), runner=runner, folds=args.folds,
        embargo_pct=float(cfg.backtest.embargo_pct), holdout_runner=holdout_runner
    )
    print(result.render())
    if result.holdout_metrics is not None:
        print("\nUNTOUCHED FINAL HOLDOUT (reported only, never tuned on):")
        print(json.dumps(result.holdout_metrics.as_dict(), indent=2, default=str))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "oos": result.oos_metrics.as_dict() if result.oos_metrics else None,
                    "holdout": result.holdout_metrics.as_dict() if result.holdout_metrics else None,
                    "gates": [
                        {
                            "name": g.name,
                            "passed": g.passed,
                            "observed": g.observed,
                            "threshold": g.threshold,
                        }
                        for g in result.report.gates
                    ],
                    "notes": list(result.notes),
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"written: {args.json_out}")
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
