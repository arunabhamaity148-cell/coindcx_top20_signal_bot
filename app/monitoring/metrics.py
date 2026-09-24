"""Metrics counters + the 10-second WHY latency budget monitor (FINAL_DELIVERABLE §AC)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from app.core.timeutils import now_ms


@dataclass
class Counter:
    name: str
    value: int = 0

    def inc(self, amount: int = 1) -> None:
        self.value += amount


@dataclass
class LatencyBudget:
    """Tracks whether a stage stayed inside its budget (used for the 10 s WHY rule)."""

    name: str
    budget_ms: int
    samples: deque[int] = field(default_factory=lambda: deque(maxlen=500))
    breaches: int = 0

    def record(self, elapsed_ms: int) -> bool:
        self.samples.append(elapsed_ms)
        if elapsed_ms > self.budget_ms:
            self.breaches += 1
            return False
        return True

    @property
    def max_ms(self) -> int | None:
        return max(self.samples) if self.samples else None

    @property
    def p95_ms(self) -> int | None:
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        return ordered[int(0.95 * (len(ordered) - 1))]

    @property
    def within_budget(self) -> bool:
        return self.breaches == 0

    def snapshot(self) -> dict[str, int | bool | None]:
        return {
            "name": self.name,
            "budget_ms": self.budget_ms,
            "count": len(self.samples),
            "max_ms": self.max_ms,
            "p95_ms": self.p95_ms,
            "breaches": self.breaches,
            "within_budget": self.within_budget,
        }


@dataclass
class Metrics:
    counters: dict[str, Counter] = field(default_factory=dict)
    latencies: dict[str, LatencyBudget] = field(default_factory=dict)
    started_ms: int = field(default_factory=now_ms)

    def counter(self, name: str) -> Counter:
        if name not in self.counters:
            self.counters[name] = Counter(name=name)
        return self.counters[name]

    def inc(self, name: str, amount: int = 1) -> None:
        self.counter(name).inc(amount)

    def latency(self, name: str, budget_ms: int) -> LatencyBudget:
        if name not in self.latencies:
            self.latencies[name] = LatencyBudget(name=name, budget_ms=budget_ms)
        return self.latencies[name]

    @property
    def uptime_sec(self) -> int:
        return int((now_ms() - self.started_ms) / 1000)

    def snapshot(self) -> dict[str, object]:
        return {
            "uptime_sec": self.uptime_sec,
            "counters": {name: counter.value for name, counter in self.counters.items()},
            "latencies": {name: budget.snapshot() for name, budget in self.latencies.items()},
        }

    def render(self) -> str:
        lines = [f"uptime: {self.uptime_sec}s"]
        for name, counter in sorted(self.counters.items()):
            lines.append(f"  {name}: {counter.value}")
        for name, budget in sorted(self.latencies.items()):
            snapshot = budget.snapshot()
            lines.append(
                f"  {name} latency: max={snapshot['max_ms']}ms p95={snapshot['p95_ms']}ms "
                f"budget={snapshot['budget_ms']}ms breaches={snapshot['breaches']}"
            )
        return "\n".join(lines)


@dataclass
class VetoCounter:
    by_guard: dict[str, int] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)

    def record(self, guard: str, severity: str) -> None:
        self.by_guard[guard] = self.by_guard.get(guard, 0) + 1
        self.by_severity[severity] = self.by_severity.get(severity, 0) + 1

    def snapshot(self) -> dict[str, object]:
        total = sum(self.by_guard.values()) or 1
        return {
            "by_guard": dict(self.by_guard),
            "by_severity": dict(self.by_severity),
            "share": {g: round(n / total, 4) for g, n in self.by_guard.items()},
        }


__all__ = ["Counter", "LatencyBudget", "Metrics", "VetoCounter"]
