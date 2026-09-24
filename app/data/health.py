"""Feed health gate (G1 data integrity).

DATA QUALITY / FAIL-CLOSED MATRIX (DESIGN_SPEC §7): Binance stale, CoinDCX stale,
clock drift > 1500 ms, symbol mapping failure, divergence z >= 3, missing orderbook,
corrupted candle, news feed down (< min_sources_healthy), WS disconnect, rate-limit
budget > 80 %, system clock fault -> each maps to NO TRADE plus a logged veto row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from app.core.logging_setup import get_logger
from app.core.models import FeedHealth, FeedState
from app.core.timeutils import now_ms

log = get_logger(__name__)

REQUIRED_FEEDS = ("binance_rest", "binance_ws", "coindcx_rest", "news")


@dataclass
class FeedHealthRegistry:
    staleness: Mapping[str, int] = field(default_factory=dict)
    min_sources_healthy: int = 2
    feeds: dict[str, FeedHealth] = field(default_factory=dict)
    clock_drift_ms: int = 0
    max_clock_drift_ms: int = 1500
    rate_limit_utilisation: float = 0.0
    rate_limit_ceiling: float = 0.80

    def update(self, health: Mapping[str, FeedHealth]) -> None:
        self.feeds.update(health)

    def healthy_names(self) -> list[str]:
        return [name for name, h in self.feeds.items() if h.state is FeedState.HEALTHY]

    def unhealthy(self) -> dict[str, str]:
        return {name: h.state.value for name, h in self.feeds.items() if h.state is not FeedState.HEALTHY}

    @property
    def healthy_count(self) -> int:
        return len(self.healthy_names())

    def required_ok(self, required: Iterable[str] = REQUIRED_FEEDS) -> tuple[bool, list[str]]:
        missing: list[str] = []
        for name in required:
            health = self.feeds.get(name)
            if health is None or health.state is not FeedState.HEALTHY:
                missing.append(name)
        return (not missing), missing

    def gate(self, required: Iterable[str] = REQUIRED_FEEDS) -> tuple[bool, list[str]]:
        """Return (ok, reasons). Any reason means NO TRADE."""
        reasons: list[str] = []
        if self.healthy_count < self.min_sources_healthy:
            reasons.append(f"healthy feeds {self.healthy_count} < min_sources_healthy {self.min_sources_healthy}")
        ok, missing = self.required_ok(required)
        if not ok:
            reasons.append(f"unhealthy required feeds: {missing}")
        if abs(self.clock_drift_ms) > self.max_clock_drift_ms:
            reasons.append(f"clock drift {self.clock_drift_ms} ms > {self.max_clock_drift_ms} ms")
        if self.rate_limit_utilisation > self.rate_limit_ceiling:
            reasons.append(f"rate-limit utilisation {self.rate_limit_utilisation:.2f} > {self.rate_limit_ceiling:.2f}")
        if reasons:
            log.warning("feed health gate FAILED (fail-closed): %s", "; ".join(reasons))
        return (not reasons), reasons

    def snapshot(self) -> dict[str, object]:
        return {
            "feeds": {name: {"state": h.state.value, "age_ms": h.age_ms, "detail": h.detail}
                      for name, h in self.feeds.items()},
            "healthy_count": self.healthy_count,
            "min_sources_healthy": self.min_sources_healthy,
            "clock_drift_ms": self.clock_drift_ms,
            "rate_limit_utilisation": self.rate_limit_utilisation,
            "gate_ok": self.gate()[0],
            "gate_reasons": self.gate()[1],
        }


@dataclass
class StaleDetector:
    """Per-feed staleness budget checker with DEGRADED/STALE/DISCONNECTED grading."""

    budgets: Mapping[str, int]
    _last_seen: dict[str, int] = field(default_factory=dict)

    def observe(self, name: str, ts_ms: int | None) -> FeedHealth:
        reference = now_ms()
        if ts_ms is None:
            return FeedHealth(name, FeedState.DISCONNECTED, None, None)
        self._last_seen[name] = int(ts_ms)
        age = reference - int(ts_ms)
        budget = int(self.budgets.get(name, 3000))
        if age > budget * 3:
            state = FeedState.DISCONNECTED
        elif age > budget:
            state = FeedState.STALE
        elif age > budget / 2:
            state = FeedState.DEGRADED
        else:
            state = FeedState.HEALTHY
        return FeedHealth(name, state, int(ts_ms), age)

    def detect(self, observations: Mapping[str, int | None]) -> dict[str, FeedHealth]:
        return {name: self.observe(name, ts) for name, ts in observations.items()}


__all__ = ["FeedHealthRegistry", "REQUIRED_FEEDS", "StaleDetector"]
