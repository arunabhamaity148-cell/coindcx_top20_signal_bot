"""Walk-forward validation + acceptance gates (FINAL_DELIVERABLE §Y, §Z).

  6 anchored folds · 1 % embargo between train and test · regime buckets
  COMPRESSION / RANGE / TREND_UP / TREND_DOWN / HIGH_VOL / POST_EVENT · untouched final
  holdout that is NEVER used for tuning.

Acceptance gates (`acceptance`) - a strategy is REJECTED unless ALL pass out-of-sample:
  >= 100 trades · PF >= 1.25 · avg_R >= 0.05 · max_dd_R <= 15R · fill rate >= 0.35 ·
  no regime worse than -0.15R.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.backtest.metrics import Metrics
from app.core.logging_setup import get_logger

log = get_logger(__name__)

REGIME_BUCKETS = ("COMPRESSION", "RANGE", "TREND_UP", "TREND_DOWN", "HIGH_VOL", "POST_EVENT")


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    embargo_bars: int

    def describe(self) -> dict[str, int]:
        return {
            "fold": self.index,
            "train": self.train_end - self.train_start,
            "test": self.test_end - self.test_start,
            "embargo": self.embargo_bars,
        }


@dataclass
class GateResult:
    name: str
    passed: bool
    observed: float | None
    threshold: float
    detail: str = ""

    def render(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        observed = "n/a" if self.observed is None else f"{self.observed:.4f}"
        return f"[{status}] {self.name}: observed {observed} vs required {self.threshold}"


@dataclass
class AcceptanceReport:
    gates: tuple[GateResult, ...]
    metrics: Metrics | None = None

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def render(self) -> str:
        lines = ["Acceptance gates (out-of-sample)"]
        lines.extend(g.render() for g in self.gates)
        lines.append(f"OVERALL: {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(g.render() for g in self.gates if not g.passed)


def anchored_folds(
    total_bars: int, *, folds: int = 6, embargo_pct: float = 0.01, min_train: int = 500,
    holdout_pct: float = 0.20,
) -> list[Fold]:
    """Anchored folds that reserve the final holdout entirely outside model selection."""
    if total_bars < min_train + folds * 10:
        return []
    holdout_pct = min(0.50, max(0.05, float(holdout_pct)))
    holdout_start = int(total_bars * (1.0 - holdout_pct))
    if holdout_start <= min_train + folds * 10:
        return []
    test_size = max(50, (holdout_start - min_train) // folds)
    embargo = max(1, int(total_bars * embargo_pct))
    out: list[Fold] = []
    train_end = min_train
    for index in range(folds):
        test_start = train_end + embargo
        test_end = min(holdout_start, test_start + test_size)
        if test_end - test_start < 20:
            break
        out.append(
            Fold(
                index=index + 1,
                train_start=0,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                embargo_bars=embargo,
            )
        )
        train_end = test_end
    return out


def acceptance(
    metrics: Metrics,
    *,
    min_trades: int = 100,
    min_pf: float = 1.25,
    min_avg_r: float = 0.05,
    max_dd_r: float = 15.0,
    min_fill_rate: float = 0.35,
    worst_regime_floor: float = -0.15,
) -> AcceptanceReport:
    """The documented gates. Any failure means the run is NOT production-ready."""
    gates = [
        GateResult(
            "minimum OOS trades",
            metrics.trades >= min_trades,
            float(metrics.trades),
            float(min_trades),
            f"{metrics.trades} closed trades",
        ),
        GateResult("profit factor", metrics.profit_factor >= min_pf, metrics.profit_factor, min_pf),
        GateResult("average R", metrics.avg_r >= min_avg_r, metrics.avg_r, min_avg_r),
        GateResult(
            "max drawdown (R)",
            metrics.max_dd_r <= max_dd_r,
            metrics.max_dd_r,
            max_dd_r,
            "lower is better",
        ),
        GateResult(
            "fill rate", metrics.fill_rate >= min_fill_rate, metrics.fill_rate, min_fill_rate
        ),
    ]
    worst_regime = min(metrics.per_regime.values()) if metrics.per_regime else None
    gates.append(
        GateResult(
            "no regime worse than floor",
            worst_regime is None or worst_regime >= worst_regime_floor,
            worst_regime,
            worst_regime_floor,
            "regimes with no trades are not counted",
        )
    )
    return AcceptanceReport(gates=tuple(gates), metrics=metrics)


@dataclass
class WalkForwardResult:
    folds: Sequence[Fold]
    per_fold: Sequence[Metrics]
    oos_metrics: Metrics | None
    report: AcceptanceReport
    holdout_metrics: Metrics | None = None
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.report.passed

    def render(self) -> str:
        lines = ["Walk-forward result"]
        for fold, metrics in zip(self.folds, self.per_fold):
            lines.append(
                f"fold {fold.index}: trades={metrics.trades} avg_r={metrics.avg_r:.4f} "
                f"pf={metrics.profit_factor:.3f} dd={metrics.max_dd_r:.2f}"
            )
        lines.append(self.report.render())
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)


def run_walk_forward(
    *,
    total_bars: int,
    runner: Callable[..., Metrics | None],
    folds: int = 6,
    embargo_pct: float = 0.01,
    holdout_runner: Callable[[], Metrics | None] | None = None,
    min_train: int = 500,
) -> WalkForwardResult:
    """Drive a caller-supplied backtest runner over the anchored folds.

    `runner(train_start, test_end)` must return out-of-sample Metrics for the fold; if it
    returns None the fold is recorded as NOT RUN rather than fabricated.
    """
    fold_list = anchored_folds(
        total_bars, folds=folds, embargo_pct=embargo_pct, min_train=min_train
    )
    if not fold_list:
        empty = Metrics()
        return WalkForwardResult(
            folds=(),
            per_fold=(),
            oos_metrics=None,
            report=acceptance(empty),
            notes=(
                f"NOT RUN: only {total_bars} bars available; "
                f"walk-forward needs >= {min_train + folds * 10}",
            ),
        )
    per_fold: list[Metrics] = []
    aggregated: list[Metrics] = []
    for fold in fold_list:
        metrics = None
        try:
            # Preferred contract exposes the true OOS boundary explicitly. Two-argument
            # runners remain supported for deterministic unit-test fixtures. Inspect the
            # callable before invocation so an internal TypeError is never mistaken for an
            # arity mismatch.
            import inspect

            positional = [
                p for p in inspect.signature(runner).parameters.values()
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            accepts_varargs = any(
                p.kind is inspect.Parameter.VAR_POSITIONAL
                for p in inspect.signature(runner).parameters.values()
            )
            if accepts_varargs or len(positional) >= 3:
                metrics = runner(fold.train_end, fold.test_start, fold.test_end)
            elif len(positional) >= 2:
                metrics = runner(fold.train_end, fold.test_end)
            else:
                raise TypeError("walk-forward runner must accept 2 or 3 positional arguments")
        except Exception as exc:
            log.error("walk-forward fold %s failed: %s", fold.index, exc)
        if metrics is None:
            continue
        per_fold.append(metrics)
        aggregated.append(metrics)
    combined = _combine(aggregated) if aggregated else Metrics()
    report = acceptance(combined)
    if len(aggregated) != len(fold_list):
        report.gates = report.gates + (
            GateResult(
                "walk-forward fold completeness",
                False,
                float(len(aggregated)),
                float(len(fold_list)),
                "every configured fold must execute successfully",
            ),
        )
    holdout = None
    if holdout_runner is not None:
        try:
            holdout = holdout_runner()
        except Exception as exc:
            log.error("holdout run failed: %s", exc)
    notes = ()
    if holdout is not None:
        notes = ("the final holdout is reported but was NEVER used for parameter selection",)
    return WalkForwardResult(
        folds=tuple(fold_list),
        per_fold=tuple(per_fold),
        oos_metrics=combined,
        report=report,
        holdout_metrics=holdout,
        notes=notes,
    )


def _combine(metrics_list: Sequence[Metrics]) -> Metrics:
    combined = Metrics()
    combined.trades = sum(m.trades for m in metrics_list)
    combined.entry_attempts = sum(m.entry_attempts for m in metrics_list)
    combined.fills = sum(m.fills for m in metrics_list)
    combined.wins = sum(m.wins for m in metrics_list)
    combined.losses = sum(m.losses for m in metrics_list)
    combined.gross_r = sum(m.gross_r for m in metrics_list)
    combined.gross_profit_r = sum(m.gross_profit_r for m in metrics_list)
    combined.gross_loss_r = sum(m.gross_loss_r for m in metrics_list)
    combined.net_r = sum(m.net_r for m in metrics_list)
    combined.avg_r = combined.net_r / combined.fills if combined.fills else 0.0
    combined.expectancy = combined.avg_r
    if combined.gross_loss_r > 0:
        combined.profit_factor = combined.gross_profit_r / combined.gross_loss_r
    elif combined.gross_profit_r > 0:
        combined.profit_factor = float("inf")
    else:
        combined.profit_factor = 0.0
    # Compute drawdown over the concatenated OOS equity path, not merely the worst
    # per-fold drawdown. This catches a drawdown that begins in one fold and continues
    # across the next fold boundary.
    combined_curve: list[float] = []
    offset = 0.0
    for m in metrics_list:
        curve = list(m.equity_curve)
        if not curve:
            continue
        combined_curve.extend(offset + x for x in curve)
        offset += curve[-1]
    combined.equity_curve = tuple(combined_curve)
    peak = 0.0
    combined.max_dd_r = 0.0
    for equity in combined_curve:
        peak = max(peak, equity)
        combined.max_dd_r = max(combined.max_dd_r, peak - equity)
    combined.max_consecutive_losses = max(
        (m.max_consecutive_losses for m in metrics_list), default=0
    )
    combined.fill_rate = (combined.fills / combined.entry_attempts) if combined.entry_attempts else 0.0
    regimes: dict[str, list[float]] = {}
    for m in metrics_list:
        for regime, value in m.per_regime.items():
            regimes.setdefault(regime, []).append(value)
    combined.per_regime = {k: sum(v) / len(v) for k, v in regimes.items()}
    return combined


__all__ = [
    "AcceptanceReport",
    "Fold",
    "GateResult",
    "REGIME_BUCKETS",
    "WalkForwardResult",
    "acceptance",
    "anchored_folds",
    "run_walk_forward",
]
