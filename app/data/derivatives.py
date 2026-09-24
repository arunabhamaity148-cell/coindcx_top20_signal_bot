"""Binance-sourced derivatives intelligence with timestamp-aware freshness.

The engine deliberately fails closed when required derivatives observations are stale or
insufficient. OI change is measured over an explicit time horizon (not simply the previous
scan), and percentile/z-score values require a configured minimum history so a restart cannot
manufacture a confident rank from a tiny sample.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Sequence

from app.core.mathx import mean, percentile_rank, stdev
from app.core.models import CascadeRisk, Candle, Crowding, DerivativesSnapshot, OIQuadrant
from app.core.timeutils import now_ms
from app.data.binance.models import classify_quadrant


@dataclass
class RollingSeries:
    maxlen: int = 500
    values: Deque[float] = field(default_factory=deque)
    timestamps_ms: Deque[int | None] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.values = deque(self.values, maxlen=self.maxlen)
        self.timestamps_ms = deque(self.timestamps_ms, maxlen=self.maxlen)
        while len(self.timestamps_ms) < len(self.values):
            self.timestamps_ms.append(None)

    def push(self, value: float | None, ts_ms: int | None = None) -> None:
        if value is None:
            return
        self.values.append(float(value))
        self.timestamps_ms.append(int(ts_ms) if ts_ms is not None else None)

    def clear(self) -> None:
        self.values.clear()
        self.timestamps_ms.clear()

    def __len__(self) -> int:
        return len(self.values)

    def list(self) -> list[float]:
        return list(self.values)

    def latest_ts(self) -> int | None:
        return self.timestamps_ms[-1] if self.timestamps_ms else None

    def value_at_or_before(self, target_ms: int, max_gap_ms: int | None = None) -> tuple[float, int] | None:
        for value, ts in reversed(tuple(zip(self.values, self.timestamps_ms, strict=True))):
            if ts is None or ts > target_ms:
                continue
            if max_gap_ms is not None and target_ms - ts > max_gap_ms:
                return None
            return value, ts
        return None

    def age_ms(self, reference_ms: int | None = None) -> int | None:
        ts = self.latest_ts()
        if ts is None:
            return None
        return max(0, (reference_ms or now_ms()) - ts)

    def z(self, value: float | None = None, *, min_obs: int = 3) -> float | None:
        series = self.list()
        if len(series) < min_obs:
            return None
        if value is None:
            value = series[-1]
        sd = stdev(series)
        if sd == 0:
            return 0.0
        return (value - mean(series)) / sd

    def percentile(self, value: float | None = None, *, min_obs: int = 60) -> float | None:
        if len(self.values) < min_obs:
            return None
        return percentile_rank(self.list(), value)


def taker_buy_ratio(candles: Sequence[Candle], bars: int = 1) -> float | None:
    window = [c for c in list(candles)[-bars:] if c.is_closed]
    if not window or any(c.taker_buy_quote is None for c in window):
        return None
    num = sum(float(c.taker_buy_quote) for c in window)
    den = sum(c.volume for c in window)
    if den <= 0:
        return None
    return num / den


@dataclass
class DerivativesEngine:
    funding_history_len: int = 100
    oi_history_len: int = 500
    crowding_elevated_z: float = 1.5
    crowding_extreme_z: float = 2.5
    cascade_oi_rank: float = 0.95
    cascade_funding_z: float = 2.0
    min_history_obs: int = 60
    oi_horizon_ms: int = 5 * 60_000
    oi_max_age_ms: int = 15 * 60_000
    funding_max_age_ms: int = 45 * 60_000
    funding_series: dict[str, RollingSeries] = field(default_factory=dict)
    oi_series: dict[str, RollingSeries] = field(default_factory=dict)

    def _funding(self, symbol: str) -> RollingSeries:
        return self.funding_series.setdefault(
            symbol, RollingSeries(maxlen=self.funding_history_len)
        )

    def _oi(self, symbol: str) -> RollingSeries:
        return self.oi_series.setdefault(symbol, RollingSeries(maxlen=self.oi_history_len))

    def observe_funding(self, symbol: str, rates: Sequence[Any] | None) -> None:
        if not rates:
            return
        series = self._funding(symbol)
        series.clear()
        for raw in rates:
            if isinstance(raw, dict):
                rate = raw.get("rate", raw.get("fundingRate"))
                ts = raw.get("ts_ms", raw.get("fundingTime"))
            elif isinstance(raw, (tuple, list)) and len(raw) >= 2:
                rate, ts = raw[0], raw[1]
            else:
                rate, ts = raw, None
            try:
                series.push(float(rate), int(float(ts)) if ts is not None else None)
            except (TypeError, ValueError):
                continue

    def observe_oi(self, symbol: str, oi: float | None, ts_ms: int | None = None) -> None:
        self._oi(symbol).push(oi, ts_ms)

    def build(
        self,
        *,
        symbol: str,
        candles: Sequence[Candle],
        funding_rate: float | None = None,
        mark_price: float | None = None,
        index_price: float | None = None,
        open_interest: float | None = None,
        oi_reference: float | None = None,
        price_reference: float | None = None,
        oi_ts_ms: int | None = None,
        funding_ts_ms: int | None = None,
        mark_ts_ms: int | None = None,
        index_ts_ms: int | None = None,
        ts_ms: int | None = None,
    ) -> DerivativesSnapshot:
        reference = int(ts_ms or now_ms())
        oi_series = self._oi(symbol)
        if open_interest is not None:
            oi_series.push(open_interest, oi_ts_ms or reference)

        oi_age = None if oi_ts_ms is None else max(0, reference - oi_ts_ms)
        oi_chg_pct: float | None = None
        # Backward-compatible offline/test path: an explicitly supplied reference is a
        # measured comparison and does not need a separate timestamp. Live calculations
        # still require a timestamp for freshness gating.
        if open_interest is not None and oi_reference not in (None, 0):
            if oi_ts_ms is None or (oi_age is not None and oi_age <= self.oi_max_age_ms):
                oi_chg_pct = (open_interest - oi_reference) / oi_reference * 100.0
        elif open_interest is not None and oi_age is not None and oi_age <= self.oi_max_age_ms:
            target = (oi_ts_ms or reference) - self.oi_horizon_ms
            historical = oi_series.value_at_or_before(target, self.oi_horizon_ms * 2)
            if historical is not None and historical[0] != 0:
                oi_chg_pct = (open_interest - historical[0]) / historical[0] * 100.0

        closed_candles = [c for c in candles if c.is_closed]
        price_chg_pct: float | None = None
        if len(closed_candles) >= 2 and closed_candles[-2].close:
            price_chg_pct = (
                (closed_candles[-1].close - closed_candles[-2].close)
                / closed_candles[-2].close
                * 100.0
            )
        elif price_reference not in (None, 0) and mark_price is not None:
            price_chg_pct = (mark_price - price_reference) / price_reference * 100.0

        funding_series = self._funding(symbol)
        funding_fresh = (
            funding_rate is not None
            and funding_ts_ms is not None
            and 0 <= reference - funding_ts_ms <= self.funding_max_age_ms
        )
        funding_z = funding_series.z(funding_rate, min_obs=min(10, self.min_history_obs)) if funding_fresh else None
        oi_rank = oi_series.percentile(open_interest, min_obs=self.min_history_obs)
        quadrant = classify_quadrant(price_chg_pct, oi_chg_pct)

        crowding = Crowding.NEUTRAL
        if funding_z is not None:
            if abs(funding_z) >= self.crowding_extreme_z:
                crowding = Crowding.EXTREME
            elif abs(funding_z) >= self.crowding_elevated_z:
                crowding = Crowding.ELEVATED

        cascade = CascadeRisk.LOW
        if oi_rank is not None and funding_z is not None:
            if oi_rank >= self.cascade_oi_rank and abs(funding_z) >= self.cascade_funding_z:
                cascade = CascadeRisk.HIGH
            elif oi_rank >= 0.80 and abs(funding_z) >= 1.0:
                cascade = CascadeRisk.MODERATE

        observed_ts = [x for x in (oi_ts_ms, funding_ts_ms, mark_ts_ms, index_ts_ms) if x is not None]
        oi_fresh = oi_age is not None and oi_age <= self.oi_max_age_ms
        return DerivativesSnapshot(
            symbol=symbol,
            ts_ms=max(observed_ts, default=reference),
            source="BINANCE",
            mark_price=mark_price,
            index_price=index_price,
            funding_rate=funding_rate if funding_fresh else None,
            funding_z=funding_z,
            open_interest=open_interest if oi_fresh else None,
            oi_chg_pct=oi_chg_pct,
            oi_pct_rank=oi_rank,
            taker_buy_sell_ratio=taker_buy_ratio(closed_candles, bars=5),
            price_chg_pct=price_chg_pct,
            quadrant=quadrant,
            crowding=crowding,
            cascade_risk=cascade,
            oi_ts_ms=oi_ts_ms,
            funding_ts_ms=funding_ts_ms,
            mark_ts_ms=mark_ts_ms,
            index_ts_ms=index_ts_ms,
        )

    def describe(self, snap: DerivativesSnapshot) -> list[str]:
        out: list[str] = []
        if snap.oi_chg_pct is not None:
            out.append(f"OI {snap.oi_chg_pct:+.2f}% ({snap.quadrant.value})")
        if snap.funding_z is not None:
            out.append(f"funding z {snap.funding_z:+.2f} ({snap.crowding.value} crowding)")
        if snap.oi_pct_rank is not None:
            out.append(f"OI percentile {snap.oi_pct_rank:.2f}")
        if snap.taker_buy_sell_ratio is not None:
            out.append(f"taker buy ratio {snap.taker_buy_sell_ratio:.2f}")
        return out


__all__ = ["DerivativesEngine", "RollingSeries", "taker_buy_ratio"]
