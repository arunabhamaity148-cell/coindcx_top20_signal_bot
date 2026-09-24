"""Tests: metrics, acceptance gates, walk-forward structure, and the soak harness.

These cover the backtest/walk-forward/soak acceptance tests 16, 17, 18, 20 - and, crucially,
they assert that an un-run validation is reported NOT RUN, never as a pass.
"""

from __future__ import annotations

import pytest

from app.backtest.engine import regime_label
from app.backtest.harness import synthetic_candles
from app.backtest.metrics import Metrics, TradeResult, compute_metrics
from app.backtest.walk_forward import (
    AcceptanceReport,
    acceptance,
    anchored_folds,
    run_walk_forward,
)
from app.core.errors import FailClosedError
from app.monitoring.soak import (
    MIN_SOAK_HOURS,
    SoakHarness,
    SoakReport,
    SoakSample,
    rss_mb,
    soak_gate,
)


def _trade(net_r: float, *, filled: bool = True, outcome: str = "TP2", regime: str = "RANGE"):
    return TradeResult(
        signal_id="T",
        symbol="B-BTC_USDT",
        strategy="S1",
        regime=regime,
        direction="LONG",
        filled=filled,
        outcome=outcome,
        gross_r=net_r + 0.1,
        net_r=net_r,
        mfe_r=net_r + 0.05,
        mae_r=-0.2,
        bars_held=3,
    )


# --------------------------------------------------------------------------- metrics


def test_metrics_are_computed_from_real_trades_only():
    trades = [_trade(2.0), _trade(-1.0), _trade(1.0), _trade(-1.0), _trade(3.0)]
    metrics = compute_metrics(trades, signals_considered=40, vetoed=30, expiries=2, misses=4)
    assert metrics.trades == 5
    assert metrics.fills == 5
    assert metrics.wins == 3 and metrics.losses == 2
    assert metrics.profit_factor == pytest.approx((2 + 1 + 3) / 2.0)
    assert metrics.avg_r == pytest.approx((2 - 1 + 1 - 1 + 3) / 5)
    assert metrics.fill_rate == 1.0
    assert metrics.veto_rate == pytest.approx(30 / 40)


def test_unfilled_orders_do_not_count_as_trades():
    trades = [_trade(0.0, filled=False, outcome="EXPIRED"), _trade(2.0)]
    metrics = compute_metrics(trades, signals_considered=10)
    assert metrics.trades == 1
    assert metrics.fill_rate == pytest.approx(0.5)
    assert metrics.expiries == 1


def test_drawdown_tracks_the_equity_curve():
    trades = [_trade(-1.0), _trade(-1.0), _trade(-1.0), _trade(0.5)]
    metrics = compute_metrics(trades, signals_considered=10)
    assert metrics.max_dd_r >= 3.0


def test_empty_metrics_are_zero_not_invented():
    metrics = compute_metrics([], signals_considered=0)
    assert metrics.trades == 0 and metrics.avg_r == 0.0 and metrics.profit_factor == 0.0


# --------------------------------------------------------------------------- gates


def test_acceptance_gates_fail_on_an_empty_run():
    report = acceptance(Metrics())
    assert isinstance(report, AcceptanceReport)
    assert not report.passed
    rendered = report.render()
    assert "OVERALL: FAIL" in rendered
    assert any("minimum OOS trades" in failure for failure in report.failures)


def test_acceptance_gates_pass_on_a_genuinely_strong_run():
    # interleaved so the equity curve stays inside the 15R drawdown gate:
    # a strong run is *not* a run with a 40R peak-to-trough collapse.
    trades = [_trade(-1.0 if i % 5 == 4 else 2.0) for i in range(100)]
    metrics = compute_metrics(trades, signals_considered=150, vetoed=20)
    report = acceptance(metrics, min_trades=100)
    assert report.metrics is not None
    assert report.passed, report.render()


def test_deep_drawdown_blocks_production_readiness():
    trades = [_trade(2.0)] + [_trade(-1.0) for _ in range(30)]
    metrics = compute_metrics(trades, signals_considered=100, vetoed=10)
    report = acceptance(metrics, min_trades=10)
    gate = next(g for g in report.gates if g.name.startswith("max drawdown"))
    assert gate.passed is False
    assert not report.passed


def test_regime_floor_gate_reports_the_worst_regime():
    trades = [_trade(2.0, regime="RANGE") for _ in range(20)]
    trades += [_trade(-0.9, regime="HIGH_VOL") for _ in range(20)]
    metrics = compute_metrics(trades, signals_considered=60, vetoed=5)
    report = acceptance(metrics, min_trades=10)
    gate = next(g for g in report.gates if g.name.startswith("no regime"))
    assert gate.passed is False
    assert metrics.per_regime["HIGH_VOL"] < -0.15


# --------------------------------------------------------------------------- walk-forward


def test_anchored_folds_are_ordered_and_embargoed():
    folds = anchored_folds(5000, folds=6, embargo_pct=0.01, min_train=500)
    assert len(folds) == 6
    for fold in folds:
        assert fold.train_end <= fold.test_start
        assert fold.test_start - fold.train_end >= 1
    assert folds[0].test_end <= folds[1].test_end


def test_walk_forward_reports_not_run_when_the_history_is_too_short():
    result = run_walk_forward(total_bars=300, runner=lambda a, b: None)
    assert result.oos_metrics is None
    assert result.per_fold == ()
    assert any("NOT RUN" in note for note in result.notes)


def test_walk_forward_without_a_runner_never_fabricates_metrics():
    result = run_walk_forward(total_bars=5000, runner=lambda a, b: None)
    assert result.per_fold == ()
    assert result.oos_metrics is not None and result.oos_metrics.trades == 0
    assert not result.passed


def test_walk_forward_aggregates_fold_metrics_and_keeps_the_holdout_separate():
    def runner(train_end: int, test_end: int) -> Metrics | None:
        trades = [_trade(2.0) for _ in range(30)] + [_trade(-1.0) for _ in range(10)]
        return compute_metrics(trades, signals_considered=80, vetoed=10)

    holdout = compute_metrics([_trade(1.5) for _ in range(40)], signals_considered=60, vetoed=5)
    result = run_walk_forward(total_bars=6000, runner=runner, holdout_runner=lambda: holdout)
    assert len(result.per_fold) >= 6
    assert result.oos_metrics is not None and result.oos_metrics.trades >= 6 * 40
    assert result.holdout_metrics is not None
    assert any("never used for parameter selection" in note.lower() for note in result.notes)
    assert result.report.gates


def test_failing_fold_is_reported_never_invented():
    def runner(train_end: int, test_end: int):
        raise RuntimeError("fold exploded")

    result = run_walk_forward(total_bars=5000, runner=runner)
    assert result.per_fold == ()
    assert not result.passed


def test_synthetic_harness_is_deterministic_for_a_seed():
    first = synthetic_candles(300, seed=7)
    second = synthetic_candles(300, seed=7)
    assert [c.close for c in first] == [c.close for c in second]
    assert [c.close for c in synthetic_candles(300, seed=8)] != [c.close for c in first]


def test_regime_label_is_descriptive_only():
    candles = synthetic_candles(400, seed=3)
    assert regime_label(candles) in {
        "UNKNOWN",
        "COMPRESSION",
        "RANGE",
        "TREND_UP",
        "TREND_DOWN",
        "HIGH_VOL",
    }
    assert regime_label(candles[:10]) == "UNKNOWN"


def test_backtest_engine_refuses_insufficient_history():
    from app.backtest.engine import BacktestEngine

    engine = BacktestEngine(
        cfg=None, signal_engine=None, snapshot_factory=lambda s, i: None, warmup_bars=200
    )
    with pytest.raises(FailClosedError):
        engine.run(symbol="B-BTC_USDT", bars=synthetic_candles(50, seed=1))


# --------------------------------------------------------------------------- soak


@pytest.mark.asyncio
async def test_soak_harness_records_telemetry_and_never_raises():
    def cycle(index: int) -> SoakSample:
        if index == 3:
            raise RuntimeError("simulated feed hiccup")
        return SoakSample(
            index=index,
            ts_ms=index,
            signals=1 if index % 4 == 0 else 0,
            no_trades=1,
            veto_blocks=1 if index % 3 == 0 else 0,
            stale_feeds=1 if index == 7 else 0,
            latency_ms=120 + index,
        )

    harness = SoakHarness(cycle=cycle, cycles=20)
    report = await harness.run()
    assert isinstance(report, SoakReport)
    assert report.cycles == 19  # the failed cycle is recorded, not counted
    assert len(report.exceptions) == 1
    assert report.stale_feed_events == 1
    assert report.signals == 5 and report.veto_blocks >= 1
    assert report.max_signal_latency_ms > 100
    assert report.veto_rate > 0
    assert "paper observation only" in " ".join(report.notes)


@pytest.mark.asyncio
async def test_short_soak_is_pending_not_passed():
    harness = SoakHarness(cycle=lambda i: SoakSample(index=i, ts_ms=i, latency_ms=50), cycles=30)
    report = await harness.run()
    gate = soak_gate(report)
    assert gate.passed is False
    assert gate.status == "PENDING"
    assert report.duration_hours < MIN_SOAK_HOURS
    assert "STATUS: PENDING" in report.render()
    assert "72 h" in report.render() or "72" in report.render()


def test_a_full_length_run_with_no_errors_would_pass_the_gate():
    synthetic = SoakReport(
        cycles=1000, started_ms=0, finished_ms=int(MIN_SOAK_HOURS * 3_600_000), signals=10
    )
    gate = soak_gate(synthetic)
    assert gate.passed is True and gate.status == "PASSED"


def test_soak_gate_fails_on_recorded_exceptions():
    synthetic = SoakReport(
        cycles=1000,
        started_ms=0,
        finished_ms=int((MIN_SOAK_HOURS + 1) * 3_600_000),
        exceptions=("boom",),
    )
    assert soak_gate(synthetic).status == "FAILED"


def test_restart_recovery_is_recorded():
    harness = SoakHarness(cycle=lambda i: SoakSample(index=i, ts_ms=i), cycles=1)
    harness.simulate_restart()
    report = harness.report(started_ms=0, peak_rss_mb=rss_mb())
    assert report.restart_recoveries == 1
