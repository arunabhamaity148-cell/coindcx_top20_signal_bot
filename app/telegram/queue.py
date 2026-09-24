"""Async Telegram delivery queue.

Requirements (master prompt §19, FINAL_DELIVERABLE §T):
  * SIGNAL first, WHY within a 10-second budget;
  * the signal is never delayed by the WHY block;
  * Telegram failure must not crash the bot;
  * duplicates are dropped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from app.core.logging_setup import get_logger
from app.core.timeutils import now_ms
from app.telegram.formatter import MessageFormatter
from app.telegram.sender import SendResult, TelegramSender

log = get_logger(__name__)


class Priority(IntEnum):
    SIGNAL = 0
    DANGER = 1
    WHY = 2
    NO_TRADE = 3
    INFO = 4


@dataclass(order=True)
class QueuedMessage:
    priority: int
    seq: int
    text: str = field(compare=False)
    dedupe_key: str | None = field(default=None, compare=False)
    created_ms: int = field(default=0, compare=False)
    sent_ms: int | None = field(default=None, compare=False)


@dataclass
class TelegramQueue:
    cfg: Any
    formatter: MessageFormatter
    sender: TelegramSender
    maxsize: int = 500
    _queue: asyncio.PriorityQueue | None = None
    _task: asyncio.Task | None = None
    _seq: int = 0
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    stats: dict[str, int] = field(
        default_factory=lambda: {"queued": 0, "sent": 0, "failed": 0, "dropped": 0, "suppressed": 0}
    )
    why_latencies_ms: list[int] = field(default_factory=list)
    _signal_events: dict[str, tuple[asyncio.Event, int | None, bool]] = field(default_factory=dict)
    _why_tasks: set[asyncio.Task] = field(default_factory=set)

    async def start(self) -> None:
        self._queue = asyncio.PriorityQueue(maxsize=self.maxsize)
        self._stop.clear()
        self._task = asyncio.create_task(self._worker(), name="telegram-queue")

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._why_tasks):
            task.cancel()
        for task in list(self._why_tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._why_tasks.clear()
        self._signal_events.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ------------------------------------------------------------------ enqueue
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def enqueue(
        self, text: str, *, priority: Priority = Priority.INFO, dedupe_key: str | None = None
    ) -> SendResult | None:
        if self._queue is None:
            # queue not started: deliver inline so nothing is silently lost
            result = await self.sender.send(text, dedupe_key=dedupe_key)
            return result
        if dedupe_key and dedupe_key in self.sender.sent_hashes:
            self.stats["suppressed"] += 1
            return
        message = QueuedMessage(
            priority=int(priority),
            seq=self._next_seq(),
            text=text,
            dedupe_key=dedupe_key,
            created_ms=now_ms(),
        )
        try:
            self._queue.put_nowait(message)
            self.stats["queued"] += 1
            return None
        except asyncio.QueueFull:
            # SIGNAL/DANGER are safety-critical operator messages. Never silently drop them
            # because the informational queue is saturated; fall back to direct delivery.
            if priority in (Priority.SIGNAL, Priority.DANGER):
                result = await self.sender.send(text, dedupe_key=dedupe_key)
                if not result.ok:
                    self.stats["failed"] += 1
                return result
            self.stats["dropped"] += 1
            log.error("telegram queue full - dropping %s message (bot continues)", priority.name)
            return SendResult(ok=False, error="telegram queue full", attempts=0)

    async def publish_signal(self, signal, why_bullets: Sequence[str]) -> None:
        """Deliver SIGNAL first, then dispatch WHY from an independent task with a hard deadline.

        The WHY timer begins when this publication is created.  The sender will refuse
        retries/throttle waits that would cross the configured deadline rather than silently
        delivering late and reporting success.
        """
        signal_id = str(signal.signal_id)
        event = asyncio.Event()
        self._signal_events[signal_id] = (event, None, False)
        result = await self.enqueue(
            self.formatter.signal(signal),
            priority=Priority.SIGNAL,
            dedupe_key=f"signal:{signal_id}",
        )
        # Inline/overflow delivery does not pass through _worker, so wake the WHY task
        # explicitly with the exact signal-delivery outcome.
        if result is not None:
            sent_ms = now_ms() if result.ok else None
            self._signal_events[signal_id] = (event, sent_ms, bool(result.ok))
            event.set()
        task = asyncio.create_task(
            self._deliver_why(signal, why_bullets, event), name=f"telegram-why:{signal_id}"
        )
        self._why_tasks.add(task)
        task.add_done_callback(self._why_tasks.discard)

    async def _deliver_why(self, signal, why_bullets: Sequence[str], event: asyncio.Event) -> None:
        signal_id = str(signal.signal_id)
        created_ms = now_ms()
        budget_ms = int(self.cfg.system.why_message_budget_sec) * 1000
        deadline_ms = created_ms + budget_ms
        try:
            try:
                await asyncio.wait_for(event.wait(), timeout=max(0.0, budget_ms / 1000.0))
            except TimeoutError:
                self.stats["failed"] += 1
                self.stats["dropped"] += 1
                log.error("WHY deadline missed because SIGNAL was not delivered in time: %s", signal_id)
                return
            _event, signal_sent_ms, signal_ok = self._signal_events.get(signal_id, (event, None, False))
            if not signal_ok or signal_sent_ms is None:
                self.stats["failed"] += 1
                log.error("WHY suppressed because SIGNAL delivery failed: %s", signal_id)
                return
            result = await self.sender.send(
                self.formatter.why(signal, why_bullets),
                dedupe_key=f"why:{signal_id}",
                deadline_ms=deadline_ms,
            )
            if result.ok:
                sent_ms = now_ms()
                self.stats["sent"] += 1
                latency = sent_ms - signal_sent_ms
                self.why_latencies_ms.append(latency)
                self.why_latencies_ms = self.why_latencies_ms[-200:]
                if latency > budget_ms:
                    self.stats["failed"] += 1
                    log.error("WHY delivered after deadline despite sender success: %s", signal_id)
            else:
                self.stats["failed"] += 1
                log.error("WHY delivery failed%s: %s", " (deadline)" if result.deadline_missed else "", result.error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats["failed"] += 1
            log.error("WHY worker error for %s: %s", signal_id, exc)
        finally:
            self._signal_events.pop(signal_id, None)

    async def publish_danger(self, alert) -> None:
        await self.enqueue(
            self.formatter.danger(alert),
            priority=Priority.DANGER,
            dedupe_key=f"danger:{alert.signal_id}:{alert.issued_ms // 60000}",
        )

    async def publish_no_trade(self, record) -> None:
        await self.enqueue(
            self.formatter.no_trade(record),
            priority=Priority.NO_TRADE,
            dedupe_key=f"notrade:{record.symbol}:{record.ts_ms // 300000}",
        )

    # ------------------------------------------------------------------ worker
    async def _worker(self) -> None:
        assert self._queue is not None
        while not self._stop.is_set():
            try:
                message: QueuedMessage = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                result: SendResult = await self.sender.send(
                    message.text, dedupe_key=message.dedupe_key
                )
                message.sent_ms = now_ms()
                if message.priority == int(Priority.SIGNAL) and message.dedupe_key:
                    signal_id = message.dedupe_key.removeprefix("signal:")
                    state = self._signal_events.get(signal_id)
                    if state is not None:
                        event, _sent_ms, _old_ok = state
                        self._signal_events[signal_id] = (event, message.sent_ms, bool(result.ok))
                        event.set()
                if result.ok:
                    self.stats["sent"] += 1
                    if message.priority == int(Priority.WHY):
                        self.why_latencies_ms.append(message.sent_ms - message.created_ms)
                        self.why_latencies_ms = self.why_latencies_ms[-200:]
                else:
                    self.stats["failed"] += 1
                    log.error("telegram delivery failed: %s", result.error)
            except Exception as exc:
                self.stats["failed"] += 1
                log.error("telegram worker error: %s", exc)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ metrics
    @property
    def why_budget_met(self) -> bool:
        """True when every measured WHY delivery stayed inside the configured budget."""
        budget_ms = int(self.cfg.system.why_message_budget_sec) * 1000
        return all(latency <= budget_ms for latency in self.why_latencies_ms)

    def max_why_latency_ms(self) -> int | None:
        return max(self.why_latencies_ms) if self.why_latencies_ms else None

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.stats,
            "queue_depth": self._queue.qsize() if self._queue else 0,
            "max_why_latency_ms": self.max_why_latency_ms(),
            "why_budget_sec": int(self.cfg.system.why_message_budget_sec),
            "why_budget_met": self.why_budget_met,
            "why_pending": len(self._why_tasks),
        }


__all__ = ["Priority", "QueuedMessage", "TelegramQueue"]
