#!/usr/bin/env python3
"""Event-driven backtest over REAL historical candles supplied by the operator.

    python scripts/run_backtest.py --csv data/BTCUSDT_5m.csv --symbol B-BTC_USDT

The CSV must contain: open_time_ms,open,high,low,close,volume (a Binance kline export).
Without --csv the script prints NOT RUN - it never invents results, never downloads data,
and never reports a profitability figure it did not compute from real bars.

Exit codes: 0 = backtest completed and acceptance gates PASSED · 2 = completed but gates
FAILED · 3 = NOT RUN (no dataset).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.logging_setup import setup_logging  # noqa: E402


def load_csv(path: Path) -> list:
    from app.core.models import Candle

    rows: list[Candle] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"open_time_ms", "open", "high", "low", "close", "volume"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"{path}: missing column(s) {sorted(missing)}")
        for row in reader:
            rows.append(
                Candle(
                    open_time_ms=int(float(row["open_time_ms"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    taker_buy_quote=(
                        float(row["taker_buy_quote"]) if row.get("taker_buy_quote") else None
                    ),
                )
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="event-driven backtest")
    parser.add_argument("--csv", help="historical OHLCV csv (required to actually run)")
    parser.add_argument("--symbol", default="B-BTC_USDT")
    parser.add_argument("--bar-minutes", type=float, default=1.0,
                        help="source candle resolution; 1m is required for the full S1-S5 set")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--json-out", help="write the metrics dict to this path")
    args = parser.parse_args()
    setup_logging(level="WARNING")

    if not args.csv:
        print("BACKTEST: NOT RUN")
        print("  reason: no historical dataset supplied (--csv).")
        print("  The engine never downloads data and never fabricates results.")
        print("  Export Binance USDs-M klines to CSV, or use synthetic_candles() in tests.")
        return 3

    path = Path(args.csv)
    if not path.exists():
        print(f"BACKTEST: NOT RUN - {path} does not exist")
        return 3

    from app.backtest.engine import BacktestEngine, regime_label
    from app.backtest.harness import SyntheticSnapshotFactory
    from app.backtest.walk_forward import acceptance
    from app.config import load_config
    from scripts._bootstrap import build_signal_engine, instrument_for

    cfg = load_config(ROOT / "config")
    if args.bar_minutes <= 0:
        raise SystemExit("--bar-minutes must be positive")
    required_1m = {"S1", "S5"} & set(cfg.strategy.enabled)
    if required_1m and abs(args.bar_minutes - 1.0) > 1e-9:
        raise SystemExit("full strategy validation requires --bar-minutes 1 because S1/S5 use 1m triggers")
    bars = load_csv(path)
    instrument = instrument_for(cfg, args.symbol)
    engine, _ = build_signal_engine(cfg)
    factory = SyntheticSnapshotFactory(
        cfg=cfg, series=bars, instrument=instrument, bar_minutes=args.bar_minutes
    )
    backtester = BacktestEngine(
        cfg=cfg, signal_engine=engine, snapshot_factory=factory, seed=args.seed,
        warmup_bars=int(cfg.backtest.warmup_bars),
        cost_model=__import__("app.backtest.costs", fromlist=["CostModel"]).CostModel(
            maker_fee_pct=cfg.backtest.maker_fee_pct,
            taker_fee_pct=cfg.backtest.taker_fee_pct,
            slippage_bps=cfg.backtest.slippage_bps,
            spread_cost_bps=cfg.backtest.spread_cost_bps,
            latency_ms=cfg.backtest.latency_ms,
            use_taker_for_stop=cfg.backtest.use_taker_for_stop,
        ),
        fill_model=__import__("app.backtest.fills", fromlist=["LimitFillModel"]).LimitFillModel(
            model=cfg.backtest.limit_fill_probability_model,
            max_probability=cfg.backtest.limit_fill_prob_max,
            latency_bars=1 if cfg.backtest.next_bar_fills_only else 0,
            seed=args.seed,
        ),
    )

    metrics, trades = backtester.run(
        symbol=args.symbol, bars=bars, regime=regime_label(bars), bar_minutes=args.bar_minutes
    )
    report = acceptance(metrics)
    print(f"BACKTEST {args.symbol} - {len(bars)} bars")
    print(json.dumps(metrics.as_dict(), indent=2, default=str))
    print(report.render())
    print(f"  trades simulated: {len(trades)}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {"metrics": metrics.as_dict(), "gates": report.render()}, indent=2, default=str
            ),
            encoding="utf-8",
        )
        print(f"  metrics written to {args.json_out}")
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
