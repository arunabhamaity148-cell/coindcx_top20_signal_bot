"""News engine: collect -> parse -> dedupe -> credibility -> impact -> decay -> state.

Health contract (DESIGN_SPEC §7 / §AA): a news-layer outage, or fewer than
`min_sources_healthy` responding sources, is fail-closed - the published NewsState becomes
BLOCK and veto guard G4 refuses every signal for the cycle. The engine never raises: a
broken collector, a broken parser or a broken scorer degrades the state instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger
from app.core.models import FeedHealth, FeedState, NewsState
from app.core.timeutils import now_ms
from app.news.correlation import NewsCorrelationEngine
from app.news.credibility import CredibilityEngine
from app.news.decay import decay_item
from app.news.deduper import Deduper
from app.news.impact import ImpactEngine, NewsClassifier
from app.news.models import NewsItem, NewsSnapshot, RawNewsItem, jaccard

log = get_logger(__name__)


@dataclass
class NewsEngine:
    cfg: Any
    collector: Any

    classifier: NewsClassifier = field(default_factory=NewsClassifier)
    credibility: CredibilityEngine = field(init=False)
    impact_engine: ImpactEngine = field(init=False)
    deduper: Deduper = field(init=False)
    correlation: NewsCorrelationEngine = field(default_factory=NewsCorrelationEngine)
    items: list[NewsItem] = field(default_factory=list)
    last_run_ms: int | None = None
    last_error: str = ""
    source_results: tuple[Any, ...] = ()
    runs: int = 0

    def __post_init__(self) -> None:
        cfg = self.cfg
        self.credibility = CredibilityEngine.from_config(cfg)
        self.impact_engine = ImpactEngine.from_config(cfg, self.credibility)
        self.deduper = Deduper(
            similarity_threshold=float(cfg.dedupe.get("similarity_threshold", 0.82)),
            window_min=int(cfg.dedupe.get("window_min", 720)),
        )
        self.classifier.categories = tuple(cfg.categories)

    # ------------------------------------------------------------------ ingestion
    async def run_once(self) -> NewsSnapshot:
        started = now_ms()
        try:
            self.source_results = tuple(await self.collector.collect())
            self.last_error = ""
        except Exception as exc:
            self.last_error = str(exc)
            log.error("news collection failed: %s", exc)
            self.source_results = ()

        results = list(self.source_results)
        healthy = tuple(
            getattr(r, "source_id", "")
            for r in results
            if getattr(r, "content_healthy", getattr(r, "ok", False))
        )
        unhealthy = tuple(
            getattr(r, "source_id", "")
            for r in results
            if not getattr(r, "content_healthy", getattr(r, "ok", False))
        )
        unverified = tuple(
            getattr(r, "source_id", "") for r in results if not getattr(r, "verified", True)
        )

        raw: list[RawNewsItem] = []
        for result in results:
            raw.extend(getattr(result, "items", ()) or ())

        fresh = self.deduper.filter_new(raw)
        lookback_ms = 3_600_000 * int(self.cfg.novelty_lookback_hours)
        recent = [i for i in self.items if started - i.ts_ms < lookback_ms]
        for raw_item in fresh:
            try:
                item = self.classifier.classify(raw_item, self.cfg)
                corroborating = self._corroboration(item, raw)
                self.impact_engine.score(
                    item,
                    recent=recent,
                    corroborating=corroborating,
                    liquidity_sensitivity=1.1 if item.market_wide else 1.0,
                )
                item.sources = sorted({item.source, *self._cluster_sources(item, raw)})
                decay_item(item, now_ms=started)
                self.deduper.register(item)
                self.items.append(item)
                recent.append(item)
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("news item scoring failed (%s): %s", raw_item.headline[:60], exc)
        self.items = self.items[-400:]
        self.last_run_ms = now_ms()
        self.runs += 1
        return self.state(healthy=healthy, unhealthy=unhealthy, unverified=unverified)

    def _corroboration(self, item: NewsItem, raw: Sequence[RawNewsItem]) -> int:
        threshold = float(self.cfg.dedupe.get("similarity_threshold", 0.82))
        sources: set[str] = {item.source}
        for candidate in raw:
            if candidate.source_id == item.source:
                continue
            if jaccard(item.headline, candidate.headline) >= threshold:
                sources.add(candidate.source_id)
        return max(0, len(sources) - 1)

    def _cluster_sources(self, item: NewsItem, raw: Sequence[RawNewsItem]) -> list[str]:
        threshold = float(self.cfg.dedupe.get("similarity_threshold", 0.82))
        return [
            c.source_id
            for c in raw
            if c.source_id != item.source and jaccard(item.headline, c.headline) >= threshold
        ]

    # ------------------------------------------------------------------ state
    def state(
        self,
        *,
        healthy: Sequence[str] | None = None,
        unhealthy: Sequence[str] | None = None,
        unverified: Sequence[str] | None = None,
    ) -> NewsSnapshot:
        reference = now_ms()
        for item in self.items:
            decay_item(item, now_ms=reference)
        floor = float(self.cfg.state.get("decay_floor", 0.05))
        active = [i for i in self.items if i.decayed_impact > floor]

        block_sev = set(self.cfg.state.get("block_severities", ["CRITICAL"]))
        degrade_sev = set(self.cfg.state.get("degrade_severities", ["HIGH"]))
        blocking = [i for i in active if i.severity in block_sev]
        degraded = [i for i in active if i.severity in degrade_sev]

        if healthy is None:
            healthy = tuple(
                getattr(r, "source_id", "") for r in self.source_results if getattr(r, "content_healthy", getattr(r, "ok", False))
            )
        if unhealthy is None:
            unhealthy = tuple(
                getattr(r, "source_id", "")
                for r in self.source_results
                if not getattr(r, "content_healthy", getattr(r, "ok", False))
            )
        if unverified is None:
            unverified = tuple(
                getattr(r, "source_id", "")
                for r in self.source_results
                if not getattr(r, "verified", True)
            )

        reason = ""
        minimum = int(self.cfg.min_sources_healthy)
        if len(healthy) < minimum:
            state = NewsState.BLOCK
            reason = (
                f"news sources healthy {len(healthy)} < required {minimum}; "
                f"failed={list(unhealthy)}"
            )
        elif any(i.market_wide for i in blocking):
            state = NewsState.BLOCK
            global_block = next(i for i in blocking if i.market_wide)
            reason = f"market-wide blocking news: {global_block.severity} {global_block.headline[:80]}"
        elif degraded:
            state = NewsState.DEGRADED
            reason = f"degrading news: {degraded[0].severity} {degraded[0].headline[:80]}"
        else:
            state = NewsState.CLEAR

        return NewsSnapshot(
            ts_ms=reference,
            state=state,
            items=tuple(active),
            sources_ok=tuple(healthy),
            sources_failed=tuple(unhealthy),
            sources_unverified=tuple(unverified),
            blocking_items=tuple(blocking),
            max_impact=max((i.decayed_impact for i in active), default=0.0),
            reason=reason,
            degraded_items=tuple(degraded),
        )

    def is_blackout(self, snapshot: NewsSnapshot | None = None) -> bool:
        """True while the news layer forbids new signals.

        A BLOCK state IS a blackout: either official sources have gone quiet (feed-level
        block) or a CRITICAL item is live (event-level block). Either way the signal engine
        must not publish, so this is deliberately evaluated on state, not only on items.
        """
        snap = snapshot if snapshot is not None else self.state()
        return snap.state is NewsState.BLOCK

    def health(self) -> FeedHealth:
        if self.last_run_ms is None:
            return FeedHealth("news", FeedState.DISCONNECTED, None, None, "never polled")
        age = now_ms() - self.last_run_ms
        healthy = len([
            r for r in self.source_results
            if getattr(r, "content_healthy", getattr(r, "ok", False))
        ])
        state = FeedState.HEALTHY
        if healthy < int(self.cfg.min_sources_healthy) or age > 3 * int(self.cfg.poll_sec) * 1000:
            state = FeedState.STALE
        return FeedHealth(
            "news",
            state,
            self.last_run_ms,
            age,
            f"healthy_sources={healthy} items={len(self.items)}",
        )

    def blocking_for_pair(self, snapshot: NewsSnapshot, pair: str) -> list[NewsItem]:
        # Global outage/market-wide CRITICAL block remains global. Pair-specific CRITICAL
        # events route only to their mapped assets. This prevents one altcoin headline from
        # freezing the entire TOP-20 universe.
        if snapshot.state is NewsState.BLOCK and not snapshot.blocking_items:
            return []
        if snapshot.state is NewsState.BLOCK and any(i.market_wide for i in snapshot.blocking_items):
            return list(snapshot.blocking_items)
        return [i for i in snapshot.blocking_items if self.correlation.applies_to(i, pair)]

    async def close(self) -> None:
        close = getattr(self.collector, "close", None)
        if close is None:
            close = getattr(self.collector, "__aexit__", None)
            if close is not None:
                await close(None, None, None)
                return
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    # ------------------------------------------------------------------ loop
    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                self.last_error = str(exc)
                log.error("news loop error: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=float(self.cfg.poll_sec))
            except TimeoutError:
                continue


__all__ = ["NewsEngine"]
