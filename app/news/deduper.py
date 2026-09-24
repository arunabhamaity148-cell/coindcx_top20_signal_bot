"""News deduplication.

FINAL_DELIVERABLE §F/§G: "≥2 independent sources via dedupe cluster" raises credibility.
Clustering is lexical (token Jaccard over normalized headlines) with a configurable
threshold; a hash bucket handles exact repeats cheaply.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.news.models import NewsItem, RawNewsItem, jaccard, normalize_headline


@dataclass
class Deduper:
    similarity_threshold: float = 0.82
    window_min: int = 720
    seen_hashes: set[str] = field(default_factory=set)
    clusters: list[list[NewsItem]] = field(default_factory=list)

    def is_duplicate(self, item: RawNewsItem) -> bool:
        if item.hash in self.seen_hashes:
            return True
        for cluster in self.clusters[-50:]:
            for existing in cluster:
                if (
                    jaccard(
                        normalize_headline(item.headline), normalize_headline(existing.headline)
                    )
                    >= self.similarity_threshold
                ):
                    return True
        return False

    def register(self, item: NewsItem) -> None:
        self.seen_hashes.add(item.hash)
        for cluster in self.clusters[-50:]:
            if any(
                jaccard(normalize_headline(item.headline), normalize_headline(existing.headline))
                >= self.similarity_threshold
                for existing in cluster
            ):
                cluster.append(item)
                return
        self.clusters.append([item])

    def cluster_for(self, headline: str) -> list[NewsItem]:
        for cluster in self.clusters:
            if (
                cluster
                and jaccard(normalize_headline(headline), normalize_headline(cluster[0].headline))
                >= self.similarity_threshold
            ):
                return cluster
        return []

    def independent_source_count(self, headline: str, tier_lookup) -> int:
        cluster = self.cluster_for(headline)
        tiers = {tier_lookup(item.source) for item in cluster} if cluster else set()
        return len({t for t in tiers if t is not None}) or len(cluster)

    def filter_new(self, items: Sequence[RawNewsItem]) -> list[RawNewsItem]:
        out: list[RawNewsItem] = []
        staged: set[str] = set()
        for item in items:
            if item.hash in self.seen_hashes or item.hash in staged:
                continue
            if any(
                jaccard(normalize_headline(item.headline), normalize_headline(prev.headline))
                >= self.similarity_threshold
                for prev in out
            ):
                continue
            staged.add(item.hash)
            out.append(item)
        return out


def dedupe(items: Iterable[RawNewsItem], *, threshold: float = 0.82) -> list[RawNewsItem]:
    deduper = Deduper(similarity_threshold=threshold)
    return deduper.filter_new(list(items))


__all__ = ["Deduper", "dedupe"]
