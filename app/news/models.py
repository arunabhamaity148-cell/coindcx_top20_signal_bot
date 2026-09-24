"""Canonical news models (FINAL_DELIVERABLE §G §H).

`RawNewsItem` is what a collector yields; `NewsItem` is the enriched, scored record that
is journalled and queried by veto guard G4 and the Telegram `News:` line. `NewsSnapshot`
is the per-cycle aggregate whose `state` drives the fail-closed news gate.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.models import NewsState

# --------------------------------------------------------------------------- text utils

_PUNCT = re.compile(r"[^a-z0-9\s$%]")
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "at",
        "by",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "as",
        "it",
        "its",
        "this",
        "that",
        "from",
        "after",
        "over",
        "into",
        "s",
        "new",
        "says",
        "said",
        "amid",
        "has",
        "have",
        "will",
    }
)


def normalize_headline(text: str) -> str:
    """Lowercase, strip punctuation, drop stopwords - the dedupe/corroboration key."""
    cleaned = _PUNCT.sub(" ", (text or "").lower())
    tokens = [token for token in cleaned.split() if token and token not in _STOPWORDS]
    return " ".join(tokens)


def token_set(text: str) -> set[str]:
    return set(normalize_headline(text).split())


def jaccard(left: str, right: str) -> float:
    a, b = token_set(left), token_set(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def simhash(text: str, bits: int = 64) -> int:
    """Deterministic 64-bit simhash of the normalized headline."""
    tokens = normalize_headline(text).split()
    if not tokens:
        return 0
    vector = [0] * bits
    for token in tokens:
        digest = int(hashlib.blake2b(token.encode("utf-8"), digest_size=8).hexdigest(), 16)
        for index in range(bits):
            vector[index] += 1 if (digest >> index) & 1 else -1
    out = 0
    for index, value in enumerate(vector):
        if value > 0:
            out |= 1 << index
    return out


def headline_hash(text: str, when_ms: int | None = None, *, bucket_ms: int = 3_600_000) -> str:
    """Stable identity for a headline, bucketed to the hour so a story re-fetched on the
    next poll collapses onto the same row instead of inflating the journal."""
    payload = normalize_headline(text)
    if when_ms:
        payload += f"|{when_ms // bucket_ms}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------- raw item


@dataclass(slots=True)
class RawNewsItem:
    """Exactly what a collector produces - no interpretation applied."""

    source_id: str
    tier: int
    headline: str
    url: str = ""
    summary: str = ""
    published_ms: int | None = None
    fetched_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    language: str = "en"

    @property
    def hash(self) -> str:
        return headline_hash(self.headline, self.published_ms)

    @property
    def age_ms(self) -> int:
        return max(0, self.fetched_ms - (self.published_ms or self.fetched_ms))


# --------------------------------------------------------------------------- taxonomy


@dataclass(frozen=True, slots=True)
class CategoryRule:
    """One taxonomy entry: regex patterns, directional stance, market-wide flag, weight."""

    category: str
    patterns: tuple[str, ...] = ()
    direction: str = "NEUTRAL"  # POSITIVE | NEGATIVE | NEUTRAL
    market_wide: bool = False
    weight: float = 1.0
    assets: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        return any(re.search(pattern, text) for pattern in self.patterns)


# --------------------------------------------------------------------------- scored item


@dataclass(slots=True)
class NewsItem:
    """Enriched + scored news record (one row of the `news` journal table)."""

    hash: str
    ts_ms: int
    source: str
    tier: int
    headline: str
    url: str = ""
    summary: str = ""
    categories: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    affected_assets: list[str] = field(default_factory=list)
    credibility: float = 0.0
    novelty: float = 1.0
    relevance: float = 1.0
    direction: str = "NEUTRAL"
    confidence: float = 0.0
    corroborating: int = 0
    sources: list[str] = field(default_factory=list)
    impact: float = 0.0
    decayed_impact: float = 0.0
    severity: str = "LOW"
    half_life_min: float = 60.0
    market_wide: bool = False
    liquidity_sensitivity: float = 1.0

    # ---- derived -----------------------------------------------------------
    def age_minutes(self, now_ms: int | None = None) -> float:
        reference = int(time.time() * 1000) if now_ms is None else now_ms
        return max(0.0, (reference - self.ts_ms) / 60_000.0)

    @property
    def age_ms(self) -> int:
        return max(0, int(time.time() * 1000) - self.ts_ms)

    @property
    def expiry_ms(self) -> int:
        """Moment the item stops being able to influence a signal (3 half-lives)."""
        return self.ts_ms + int(self.half_life_min * 3 * 60_000)

    def is_expired(self, now_ms: int | None = None) -> bool:
        reference = int(time.time() * 1000) if now_ms is None else now_ms
        return reference > self.expiry_ms

    @property
    def primary_category(self) -> str:
        """Highest-weight category assigned by the classifier (`GENERAL` when none)."""
        return self.categories[0] if self.categories else "GENERAL"

    def to_row(self) -> dict[str, Any]:
        """Flat row for the `news` journal table (json columns stay in `payload`)."""
        return {
            "hash": self.hash,
            "ts": self.ts_ms,
            "source": self.source,
            "tier": self.tier,
            "headline": self.headline,
            "url": self.url,
            "category": self.categories[0] if self.categories else "GENERAL",
            "categories": ",".join(self.categories),
            "entities": ",".join(self.entities),
            "affected_assets": ",".join(self.affected_assets),
            "credibility": self.credibility,
            "novelty": self.novelty,
            "relevance": self.relevance,
            "direction": self.direction,
            "confidence": self.confidence,
            "corroboration": self.corroborating,
            "impact": self.impact,
            "decayed_impact": self.decayed_impact,
            "severity": self.severity,
            "half_life_min": self.half_life_min,
            "market_wide": int(self.market_wide),
            "expiry": self.expiry_ms,
            "payload": {
                "summary": self.summary,
                "sources": self.sources,
                "liquidity_sensitivity": self.liquidity_sensitivity,
            },
        }

    @classmethod
    def from_raw(cls, raw: RawNewsItem, **overrides: Any) -> NewsItem:
        payload: dict[str, Any] = dict(
            hash=raw.hash,
            ts_ms=raw.published_ms or raw.fetched_ms,
            source=raw.source_id,
            tier=raw.tier,
            headline=raw.headline,
            url=raw.url,
            summary=raw.summary,
            sources=[raw.source_id],
        )
        payload.update(overrides)
        return cls(**payload)


# --------------------------------------------------------------------------- snapshot


@dataclass(slots=True)
class NewsSnapshot:
    """Aggregate of one news cycle; `state` is the gate that veto guard G4 reads."""

    ts_ms: int
    state: NewsState = NewsState.CLEAR
    items: tuple[NewsItem, ...] = ()
    sources_ok: tuple[str, ...] = ()
    sources_failed: tuple[str, ...] = ()
    sources_unverified: tuple[str, ...] = ()
    blocking_items: tuple[NewsItem, ...] = ()
    degraded_items: tuple[NewsItem, ...] = ()
    max_impact: float = 0.0
    reason: str = ""
    blackout_after_sec: int = 120

    @property
    def healthy_sources(self) -> tuple[str, ...]:
        return self.sources_ok

    @property
    def unhealthy_sources(self) -> tuple[str, ...]:
        return self.sources_failed

    @property
    def source_count(self) -> int:
        return len(self.sources_ok)

    @property
    def blocking(self) -> bool:
        """True only when the whole market/feed is blocked. Pair-specific items are routed by G4."""
        return self.state is NewsState.BLOCK

    @property
    def blocking_headline(self) -> str | None:
        return self.blocking_items[0].headline if self.blocking_items else None

    @property
    def blackout_until_ms(self) -> int | None:
        """Blackout ends `blackout_after_sec` after the newest blocking release."""
        if not self.blocking_items:
            return None
        return max(i.ts_ms for i in self.blocking_items) + self.blackout_after_sec * 1000

    def for_pair(self, pair: str, *, correlation: Any = None) -> tuple[NewsItem, ...]:
        if correlation is None:
            return self.items
        return tuple(item for item in self.items if correlation.applies_to(item, pair))

    def to_row(self) -> dict[str, Any]:
        return {
            "ts": self.ts_ms,
            "state": self.state.value,
            "impact": self.max_impact,
            "reason": self.reason,
            "payload": {
                "sources_ok": list(self.sources_ok),
                "sources_failed": list(self.sources_failed),
                "sources_unverified": list(self.sources_unverified),
                "blocking": [item.hash for item in self.blocking_items],
            },
        }


def flatten(items: Iterable[Sequence[NewsItem] | NewsItem]) -> list[NewsItem]:
    out: list[NewsItem] = []
    for entry in items:
        if isinstance(entry, NewsItem):
            out.append(entry)
        else:
            out.extend(entry)
    return out
