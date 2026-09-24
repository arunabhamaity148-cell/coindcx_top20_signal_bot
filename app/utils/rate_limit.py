"""Weight-budget guard for the Binance REST allowance.

FINAL_DELIVERABLE §I: "a weight-budget guard that refuses requests beyond 80 % of the
2400/min allowance". FAILURE MODE MATRIX §AA: "rate-limit pressure -> refuse request,
back off".
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from app.core.errors import FailClosedError


class RateLimitBudget:
    """Sliding 60-second weight window with a hard refusal ceiling."""

    def __init__(self, weight_per_min: int, budget_fraction: float = 0.80):
        if weight_per_min <= 0:
            raise ValueError("weight_per_min must be positive")
        self.weight_per_min = weight_per_min
        self.budget = int(weight_per_min * budget_fraction)
        self._events: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()
        self.refusals = 0
        self.consumed = 0

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def used(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        self._prune(now)
        return sum(w for _, w in self._events)

    @property
    def utilisation(self) -> float:
        return self.used() / self.weight_per_min if self.weight_per_min else 1.0

    def can_spend(self, weight: int) -> bool:
        return self.used() + weight <= self.budget

    async def spend(self, weight: int) -> None:
        async with self._lock:
            if not self.can_spend(weight):
                self.refusals += 1
                raise FailClosedError(
                    f"rate-limit budget exceeded: {self.used()}+{weight} > {self.budget} "
                    f"(80 % ceiling of {self.weight_per_min}/min)"
                )
            self._events.append((time.monotonic(), weight))
            self.consumed += weight

    def snapshot(self) -> dict[str, float]:
        return {
            "weight_per_min": float(self.weight_per_min),
            "budget_80pct": float(self.budget),
            "used": float(self.used()),
            "utilisation": round(self.utilisation, 4),
            "refusals": float(self.refusals),
        }


@dataclass
class RetryPolicy:
    backoff_sec: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)
    jitter: float = 0.30
    attempt: int = field(default=0)

    def next_delay(self, rng=None) -> float:
        base = self.backoff_sec[min(self.attempt, len(self.backoff_sec) - 1)]
        self.attempt += 1
        if self.jitter <= 0:
            return base
        if rng is None:
            import random

            rng = random.Random()
        return base * (1.0 + rng.uniform(-self.jitter, self.jitter))

    def reset(self) -> None:
        self.attempt = 0


__all__ = ["RateLimitBudget", "RetryPolicy"]
