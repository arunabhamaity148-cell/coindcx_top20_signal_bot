"""Limit-fill simulation (FINAL_DELIVERABLE §Y, master prompt §21).

CRITICAL RULE: do NOT assume every LIMIT order fills. A limit fills only when price
actually traded through the zone on a LATER bar than the signal bar (next-bar execution,
no look-ahead). A probability model then discounts marginal fills, and missed fills are
counted explicitly so the reported fill rate is real.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from app.core.models import Candle, Direction


class FillOutcome(str, Enum):
    FILLED = "FILLED"
    MISSED = "MISSED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class FillResult:
    outcome: FillOutcome
    bar_index: int | None = None
    fill_price: float | None = None
    probability: float = 0.0
    reason: str = ""

    @property
    def filled(self) -> bool:
        return self.outcome is FillOutcome.FILLED


@dataclass
class LimitFillModel:
    """Deterministic touch model by default; stochastic models are opt-in for sensitivity runs."""

    model: str = "touch_only"
    max_probability: float = 0.95
    latency_bars: int = 1
    seed: int = 7

    def probability(self, *, distance_atr: float) -> float:
        if self.model in ("touch_only", "deterministic_touch"):
            return 1.0
        if distance_atr <= 0:
            return self.max_probability
        if self.model == "constant":
            return self.max_probability
        if self.model != "linear_distance":
            raise ValueError(f"unsupported fill model: {self.model}")
        prob = self.max_probability * max(0.0, 1.0 - distance_atr)
        return max(0.05, min(self.max_probability, prob))

    def simulate(
        self,
        *,
        direction: Direction,
        entry: float,
        zone_low: float,
        zone_high: float,
        invalidation: float,
        atr: float,
        bars: Sequence[Candle],
        start_index: int,
        expiry_index: int,
        rng: random.Random | None = None,
    ) -> FillResult:
        """Walk bars strictly after the signal bar (next-bar execution only)."""
        rng = rng or random.Random(self.seed)
        for index in range(start_index + self.latency_bars, min(expiry_index, len(bars))):
            bar = bars[index]
            if direction is Direction.LONG:
                # A LONG limit at `entry` fills only if the actual traded low reached
                # the entry price. Merely entering the wider entry zone is not a fill.
                # If the same candle crossed invalidation first, resolve pessimistically.
                if bar.low <= invalidation:
                    return FillResult(
                        FillOutcome.INVALIDATED,
                        index,
                        None,
                        0.0,
                        "invalidation traded before/equal to the entry level",
                    )
                touched = bar.low <= entry
            else:
                # A SHORT limit fills only if the actual traded high reached the entry.
                if bar.high >= invalidation:
                    return FillResult(
                        FillOutcome.INVALIDATED,
                        index,
                        None,
                        0.0,
                        "invalidation traded before/equal to the entry level",
                    )
                touched = bar.high >= entry
            if not touched:
                continue
            distance = 0.0
            # Queue-position/slippage uncertainty is measured beyond the requested entry,
            # not beyond the entire zone.
            if direction is Direction.LONG and bar.low < entry:
                distance = (entry - bar.low) / atr if atr > 0 else 0.0
            if direction is Direction.SHORT and bar.high > entry:
                distance = (bar.high - entry) / atr if atr > 0 else 0.0
            prob = self.probability(distance_atr=distance)
            if rng.random() <= prob:
                fill_price = entry
                return FillResult(
                    FillOutcome.FILLED, index, fill_price, prob, "limit touched and filled"
                )
            return FillResult(
                FillOutcome.MISSED,
                index,
                None,
                prob,
                "limit touched but the fill model declined (queue position / partial)",
            )
        return FillResult(
            FillOutcome.EXPIRED,
            None,
            None,
            0.0,
            "price never entered the zone before expiry - signal expires, never chases",
        )


def bars_until(*, expiry_min: int, bar_minutes: float) -> int:
    return max(1, int(expiry_min / bar_minutes))


__all__ = ["FillOutcome", "FillResult", "LimitFillModel", "bars_until"]
