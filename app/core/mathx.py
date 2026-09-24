"""Deterministic numeric helpers.

All functions are pure and side-effect free so the strategy layer stays unit-testable
and the backtester cannot leak look-ahead state through them.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from app.core.models import Candle


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def zscore(series: Sequence[float], value: float | None = None) -> float | None:
    """Population-sample z-score of `value` (default: last element) against `series`."""
    if value is None:
        if not series:
            return None
        value = series[-1]
    if len(series) < 2:
        return None
    sd = stdev(series)
    if sd == 0:
        return 0.0
    return (value - mean(series)) / sd


def percentile_rank(series: Sequence[float], value: float | None = None) -> float | None:
    """Fraction of observations <= `value` (0..1). None when history is empty."""
    if not series:
        return None
    if value is None:
        value = series[-1]
    return sum(1 for v in series if v <= value) / len(series)


def ema(series: Sequence[float], period: int) -> list[float]:
    if period <= 1 or not series:
        return list(series)
    k = 2.0 / (period + 1.0)
    out = [series[0]]
    for value in series[1:]:
        out.append(value * k + out[-1] * (1.0 - k))
    return out


def ema_last(series: Sequence[float], period: int) -> float | None:
    return ema(series, period)[-1] if series else None


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    out: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
            continue
        prev_close = candles[i - 1].close
        out.append(max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close)))
    return out


def atr_series(candles: Sequence[Candle], period: int = 14) -> list[float]:
    """Wilder ATR, seeded with the first `period` true ranges."""
    trs = true_ranges(candles)
    if len(trs) < period:
        return []
    out = [mean(trs[:period])]
    for tr in trs[period:]:
        out.append((out[-1] * (period - 1) + tr) / period)
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> float | None:
    series = atr_series(candles, period)
    return series[-1] if series else None


def realized_vol(closes: Sequence[float], periods_per_year: float, window: int) -> float | None:
    """Annualised realised volatility from log returns of the last `window` closes."""
    if len(closes) < 3:
        return None
    window = min(window, len(closes) - 1)
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(len(closes) - window, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    if len(rets) < 2:
        return None
    return stdev(rets) * math.sqrt(periods_per_year)


def swing_extrema(candles: Sequence[Candle], lookback: int) -> tuple[float | None, float | None]:
    window = list(candles)[-lookback:]
    if not window:
        return None, None
    return max(c.high for c in window), min(c.low for c in window)


def rolling_range(candles: Sequence[Candle], lookback: int) -> tuple[float, float] | None:
    window = list(candles)[-lookback:]
    if not window:
        return None
    return max(c.high for c in window), min(c.low for c in window)


def pct_change(previous: float | None, current: float | None) -> float | None:
    if previous in (None, 0) or current is None:
        return None
    return (current - previous) / previous * 100.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default


def rolling_window(series: Sequence[float], size: int) -> list[float]:
    return list(series)[-size:] if size > 0 else []


def slope(series: Sequence[float]) -> float:
    """Normalised least-squares slope over an index axis (per-bar % of level)."""
    n = len(series)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = mean(series)
    denom = sum((i - x_mean) ** 2 for i in range(n))
    if denom == 0 or y_mean == 0:
        return 0.0
    num = sum((i - x_mean) * (series[i] - y_mean) for i in range(n))
    return (num / denom) / abs(y_mean)


def dedupe_ordered(values: Iterable[float]) -> list[float]:
    """Remove consecutive duplicates (used for monotonic TP-level validation)."""
    out: list[float] = []
    for value in values:
        if not out or out[-1] != value:
            out.append(value)
    return out
