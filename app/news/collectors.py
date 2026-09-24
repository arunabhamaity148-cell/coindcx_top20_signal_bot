"""RSS / JSON collectors for the verified free news sources.

VERIFIED (crawled 2026-09-22): CFTC RSS, SEC press releases RSS, Federal Reserve
`press_all.xml`, CoinDesk RSS, GDELT DOC API.
UNPROVEN sources (Binance announcements, CoinDCX blog, The Block, Decrypt) are
PRESENT in config but DISABLED - `collect()` skips them and reports them as
`unverified` so nothing is silently trusted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import aiohttp

from app.core.logging_setup import get_logger
from app.core.timeutils import now_ms, parse_iso8601_ms
from app.news.models import RawNewsItem

log = get_logger(__name__)


@dataclass
class SourceResult:
    source_id: str
    tier: int
    ok: bool
    items: tuple[RawNewsItem, ...] = ()
    error: str = ""
    verified: bool = True
    latency_ms: int = 0
    transport_ok: bool = False
    parser_ok: bool = False
    content_fresh_count: int = 0
    newest_item_age_ms: int | None = None

    @property
    def content_healthy(self) -> bool:
        """Content-aware source health; transport success alone is insufficient.

        An empty feed is valid only when transport + parser succeeded.  A non-empty
        feed must contain at least one item within the configured lookback window.
        """
        if not self.transport_ok or not self.parser_ok or not self.ok:
            return False
        return self.content_fresh_count > 0


@dataclass
class NewsCollector:
    cfg: Any
    session: aiohttp.ClientSession | None = None
    timeout_sec: float = 15.0

    async def __aenter__(self) -> NewsCollector:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
                headers={"User-Agent": self.cfg.user_agent},
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def collect(self) -> tuple[SourceResult, ...]:
        sources = list(self.cfg.enabled_sources)
        tasks = [self._fetch(source) for source in sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[SourceResult] = []
        for source, result in zip(sources, results):
            if isinstance(result, Exception):  # pragma: no cover - defensive
                out.append(
                    SourceResult(
                        source.id, source.tier, False, error=str(result), verified=source.verified
                    )
                )
            else:
                out.append(result)  # type: ignore[arg-type]
        unverified = [s.id for s in self.cfg.sources if not s.verified]
        if unverified:
            log.info("news sources disabled as UNPROVEN: %s", unverified)
        return tuple(out)

    async def _fetch(self, source: Any) -> SourceResult:
        started = now_ms()
        if not source.verified:
            return SourceResult(
                source.id,
                source.tier,
                False,
                error="UNPROVEN source - disabled, not harvested",
                verified=False,
            )
        try:
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
                    headers={"User-Agent": self.cfg.user_agent},
                )
            if source.type == "rss":
                items = await self._fetch_rss(source)
            elif source.type == "json":
                items = await self._fetch_json(source)
            else:
                return SourceResult(
                    source.id, source.tier, False, error=f"unknown source type {source.type}"
                )
            fetched_at = now_ms()
            lookback_ms = int(self.cfg.lookback_min) * 60_000
            fresh_count = sum(
                1 for item in items
                if item.published_ms is None or 0 <= fetched_at - item.published_ms <= lookback_ms
            )
            ages = [item.age_ms for item in items if item.published_ms is not None]
            return SourceResult(
                source.id,
                source.tier,
                True,
                items=tuple(items),
                latency_ms=fetched_at - started,
                verified=True,
                transport_ok=True,
                parser_ok=True,
                content_fresh_count=fresh_count,
                newest_item_age_ms=min(ages) if ages else None,
            )
        except Exception as exc:
            log.warning("news source %s failed: %s", source.id, exc)
            return SourceResult(
                source.id, source.tier, False, error=str(exc), latency_ms=now_ms() - started
            )

    async def _get(self, url: str, params: Mapping[str, Any] | None = None) -> str:
        assert self.session is not None
        async with self.session.get(url, params=params or {}) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status} for {url}")
            return await resp.text()

    async def _fetch_rss(self, source: Any) -> list[RawNewsItem]:
        text = await self._get(source.url)
        return parse_rss(
            text, source_id=source.id, tier=source.tier, limit=self.cfg.max_items_per_source
        )

    async def _fetch_json(self, source: Any) -> list[RawNewsItem]:
        if source.id != "gdelt":
            raise RuntimeError(f"no JSON parser for source {source.id}")
        params = {
            "query": source.query,
            "mode": source.mode or "ArtList",
            "format": source.format or "json",
            "maxrecords": source.maxrecords,
        }
        text = await self._get(source.url, params=params)
        return parse_gdelt(
            text, source_id=source.id, tier=source.tier, limit=self.cfg.max_items_per_source
        )


# --------------------------------------------------------------------------- parsers


def parse_rss(text: str, *, source_id: str, tier: int, limit: int = 60) -> list[RawNewsItem]:
    """Minimal, dependency-tolerant RSS/Atom parser (feedparser is used when present)."""
    items: list[RawNewsItem] = []
    try:  # prefer feedparser when installed
        import feedparser  # type: ignore

        parsed = feedparser.parse(text)
        for entry in parsed.entries[:limit]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            published = entry.get("published") or entry.get("updated") or ""
            ts = None
            if published:
                try:
                    ts = parse_iso8601_ms(str(published))
                except Exception:
                    try:
                        from email.utils import parsedate_to_datetime

                        ts = int(parsedate_to_datetime(str(published)).timestamp() * 1000)
                    except Exception:
                        ts = None
            items.append(
                RawNewsItem(
                    source_id=source_id,
                    tier=tier,
                    headline=title,
                    url=str(entry.get("link") or ""),
                    published_ms=ts,
                    summary=str(entry.get("summary") or "")[:400],
                    fetched_ms=now_ms(),
                )
            )
        if items:
            return items
    except ImportError:
        pass
    except Exception as exc:
        log.debug("feedparser failed for %s (%s); using stdlib parser", source_id, exc)

    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RuntimeError(f"RSS XML parse failed for {source_id}: {exc}") from exc
    entries = root.iter("item")
    atom = False
    if not list(root.iter("item")):
        entries = root.iter("{http://www.w3.org/2005/Atom}entry")
        atom = True
    for entry in entries:
        if len(items) >= limit:
            break
        title = _text(entry, "title", atom)
        if not title:
            continue
        link = _text(entry, "link", atom)
        if not link:
            href = entry.find("{http://www.w3.org/2005/Atom}link")
            link = href.get("href", "") if href is not None else ""
        published = (
            _text(entry, "pubDate", atom)
            or _text(entry, "published", atom)
            or _text(entry, "updated", atom)
        )
        ts: int | None = None
        if published:
            try:
                from email.utils import parsedate_to_datetime

                ts = int(parsedate_to_datetime(published).timestamp() * 1000)
            except Exception:
                try:
                    ts = parse_iso8601_ms(published)
                except Exception:
                    ts = None
        items.append(
            RawNewsItem(
                source_id=source_id,
                tier=tier,
                headline=title,
                url=link,
                published_ms=ts,
                summary=_text(entry, "description", atom)[:400],
                fetched_ms=now_ms(),
            )
        )
    return items


def _text(entry: Any, tag: str, atom: bool) -> str:
    node = entry.find(tag) if not atom else entry.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
    if node is None:
        node = entry.find(tag)
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def parse_gdelt(text: str, *, source_id: str, tier: int, limit: int = 60) -> list[RawNewsItem]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GDELT payload was not JSON: {exc}") from exc
    articles: Sequence[Mapping[str, Any]] = payload.get("articles", []) or []
    items: list[RawNewsItem] = []
    for article in articles[:limit]:
        title = str(article.get("title") or "").strip()
        if not title:
            continue
        ts: int | None = None
        seen = article.get("seendate")
        if seen:
            try:
                from datetime import datetime

                ts = int(
                    datetime.strptime(str(seen), "%Y%m%dT%H%M%SZ").replace(tzinfo=None).timestamp()
                    * 1000
                )
            except Exception:
                ts = None
        items.append(
            RawNewsItem(
                source_id=source_id,
                tier=tier,
                headline=title,
                url=str(article.get("url") or ""),
                published_ms=ts,
                summary=str(article.get("domain") or ""),
                fetched_ms=now_ms(),
            )
        )
    return items


class StaticCollector:
    """Deterministic collector used by tests and the soak/paper harness."""

    def __init__(
        self,
        items: Iterable[RawNewsItem],
        *,
        healthy: Iterable[str] = ("cftc", "sec", "fed", "coindesk"),
    ):
        self._items = tuple(items)
        self._healthy = tuple(healthy)

    async def collect(self) -> tuple[SourceResult, ...]:
        grouped: dict[str, list[RawNewsItem]] = {}
        for item in self._items:
            grouped.setdefault(item.source_id, []).append(item)
        return tuple(
            SourceResult(
                source,
                1 if source in ("cftc", "sec", "fed") else 2,
                True,
                tuple(grouped.get(source, ())),
                transport_ok=True,
                parser_ok=True,
                content_fresh_count=len(grouped.get(source, ())),
            )
            for source in self._healthy
        ) + tuple(
            SourceResult(
                source, 1, True, tuple(items), transport_ok=True, parser_ok=True,
                content_fresh_count=len(items),
            )
            for source, items in grouped.items()
            if source not in self._healthy
        )


__all__ = ["NewsCollector", "SourceResult", "StaticCollector", "parse_gdelt", "parse_rss"]
