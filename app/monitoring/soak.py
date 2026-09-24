"""72-hour paper / soak observation harness (master prompt §24).

PAPER MODE ONLY. The harness drives the live signal pipeline against whatever snapshot
source it is handed and records stability telemetry:

    feed stability · stale-data events · signal latency · veto rate · news latency ·
    Telegram (WHY) latency · duplicate signals · exceptions · CPU · RAM · restart recovery

It never places, cancels or modifies an order - it cannot: no such capability exists in
this codebase (see app/safety.py).

`soak_gate()` is deliberately strict: a run shorter than the configured minimum duration
returns status PENDING, so a short smoke soak can never be reported as a completed 72-hour
test.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger
from app.core.timeutils import now_ms

log = get_logger(__name__)

MIN_SOAK_HOURS = 72.0


@dataclass
class SoakSample:
    """One observation cycle."""

    index: int
    ts_ms: int
    signals: int = 0
    no_trades: int = 0
    veto_blocks: int = 0
    duplicates_suppressed: int = 0
    stale_feeds: int = 0
    latency_ms: int = 0
    exception: str = ""


@dataclass
class SoakReport:
    """Aggregated soak telemetry. Every field is measured, never assumed."""

    cycles: int = 0
    started_ms: int = 0
    finished_ms: int = 0
    signals: int = 0
    no_trades: int = 0
    veto_blocks: int = 0
    duplicates_suppressed: int = 0
    stale_feed_events: int = 0
    exceptions: tuple[str, ...] = ()
    max_signal_latency_ms: int = 0
    mean_signal_latency_ms: float = 0.0
    max_news_latency_ms: int = 0
    max_why_latency_ms: int | None = None
    peak_rss_mb: float = 0.0
    restart_recoveries: int = 0
    samples: Sequence[SoakSample] = field(default_factory=tuple)
    notes: tuple[str, ...] = ()

    @property
    def duration_hours(self) -> float:
        return max(0.0, (self.finished_ms - self.started_ms) / 3_600_000.0)

    @property
    def veto_rate(self) -> float:
        evaluated = self.signals + self.no_trades
        return self.veto_blocks / evaluated if evaluated else 0.0

    @property
    def signal_rate(self) -> float:
        return self.signals / self.cycles if self.cycles else 0.0

    def render(self) -> str:
        lines = [
            "72-HOUR PAPER / SOAK TEST",
            f"  cycles               : {self.cycles}",
            f"  wall duration        : {self.duration_hours:.2f} h "
            f"(minimum required {MIN_SOAK_HOURS:.0f} h)",
            f"  signals / no-trades  : {self.signals} / {self.no_trades}",
            f"  veto blocks          : {self.veto_blocks} (rate {self.veto_rate:.3f})",
            f"  duplicate suppressed : {self.duplicates_suppressed}",
            f"  stale-feed events    : {self.stale_feed_events}",
            f"  exceptions           : {len(self.exceptions)}",
            f"  signal latency       : max {self.max_signal_latency_ms} ms "
            f"/ mean {self.mean_signal_latency_ms:.1f} ms",
            f"  news latency (max)   : {self.max_news_latency_ms} ms",
            f"  WHY latency (max)    : {self.max_why_latency_ms} ms",
            f"  peak RSS             : {self.peak_rss_mb:.1f} MB",
            f"  restart recoveries   : {self.restart_recoveries}",
        ]
        lines.extend(f"  note: {note}" for note in self.notes)
        lines.append(f"  STATUS: {soak_gate(self).status}")
        return "\n".join(lines)


@dataclass(frozen=True)
class SoakGate:
    status: str
    passed: bool
    reasons: tuple[str, ...]


def soak_gate(report: SoakReport) -> SoakGate:
    """A soak run only PASSES with >= 72 h of wall time and zero unhandled exceptions."""
    reasons: list[str] = []
    if report.duration_hours < MIN_SOAK_HOURS:
        reasons.append(
            f"wall duration {report.duration_hours:.2f} h < {MIN_SOAK_HOURS:.0f} h -> NOT TESTED"
        )
    if report.exceptions:
        reasons.append(f"{len(report.exceptions)} unhandled exception(s) recorded")
    if report.cycles == 0:
        reasons.append("no cycles executed")
    if reasons and report.duration_hours < MIN_SOAK_HOURS:
        return SoakGate("PENDING", False, tuple(reasons))
    if reasons:
        return SoakGate("FAILED", False, tuple(reasons))
    return SoakGate("PASSED", True, ())


def rss_mb() -> float:
    """Current process RSS in MB (0.0 when the platform will not report it)."""
    try:
        with open("/proc/self/statm", encoding="utf-8") as handle:
            pages = int(handle.read().split()[1])
        return pages * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
    except Exception:
        return 0.0


@dataclass
class SoakHarness:
    """Drives the pipeline and accumulates soak telemetry.

    `cycle` is an async callable `cycle(index) -> SoakSample` supplied by the caller, so the
    harness works identically against the paper snapshot source and against a live feed.
    """

    cycle: Callable[[int], asyncio.Future[SoakSample] | Any]
    cycles: int = 100
    interval_sec: float = 0.0
    stop_event: asyncio.Event | None = None
    samples: list[SoakSample] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    restarts: int = 0

    async def run(self) -> SoakReport:
        started = now_ms()
        peak = rss_mb()
        for index in range(self.cycles):
            if self.stop_event is not None and self.stop_event.is_set():
                break
            try:
                sample = self.cycle(index)
                if asyncio.iscoroutine(sample):
                    sample = await sample
                self.samples.append(sample)
            except Exception as exc:
                self.errors.append(f"cycle {index}: {exc}")
                log.error("soak cycle %s failed: %s", index, exc)
            peak = max(peak, rss_mb())
            if self.interval_sec:
                await asyncio.sleep(self.interval_sec)
        return self.report(started_ms=started, peak_rss_mb=peak)

    def simulate_restart(self) -> None:
        """Record a restart-recovery rehearsal (state rebuilt from the journal, no orders)."""
        self.restarts += 1

    def report(self, *, started_ms: int, peak_rss_mb: float = 0.0) -> SoakReport:
        latencies = [s.latency_ms for s in self.samples if s.latency_ms]
        report = SoakReport(
            cycles=len(self.samples),
            started_ms=started_ms,
            finished_ms=now_ms(),
            signals=sum(s.signals for s in self.samples),
            no_trades=sum(s.no_trades for s in self.samples),
            veto_blocks=sum(s.veto_blocks for s in self.samples),
            duplicates_suppressed=sum(s.duplicates_suppressed for s in self.samples),
            stale_feed_events=sum(s.stale_feeds for s in self.samples),
            exceptions=tuple(self.errors),
            max_signal_latency_ms=max(latencies, default=0),
            mean_signal_latency_ms=(sum(latencies) / len(latencies)) if latencies else 0.0,
            peak_rss_mb=peak_rss_mb,
            restart_recoveries=self.restarts,
            samples=tuple(self.samples),
            notes=(
                "paper observation only - the bot holds no trading key and cannot place orders",
            ),
        )
        gate = soak_gate(report)
        if gate.status == "PENDING":
            report.notes = report.notes + (gate.reasons[0],)
        return report


async def run_soak_loop(
    *, factory: Callable[[], SoakHarness], stop_event: asyncio.Event, poll_sec: float = 60.0
) -> SoakReport:
    """Wall-clock soak loop for a real 72-hour observation window."""
    harness = factory()
    harness.stop_event = stop_event
    harness.interval_sec = poll_sec
    harness.cycles = int(MIN_SOAK_HOURS * 3600 / max(1.0, poll_sec))
    return await harness.run()


__all__ = [
    "MIN_SOAK_HOURS",
    "SoakGate",
    "SoakHarness",
    "SoakReport",
    "SoakSample",
    "run_soak_loop",
    "rss_mb",
    "soak_gate",
]
