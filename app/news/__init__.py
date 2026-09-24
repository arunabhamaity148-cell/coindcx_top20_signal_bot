"""Free news layer: collectors, dedupe, credibility, impact, decay, correlation, engine.

Every source used here is a free RSS/JSON feed (FINAL_DELIVERABLE §F). Sources whose
availability could not be verified on 2026-09-22 are shipped DISABLED and labelled
UNPROVEN in `config/news.yaml`; they are never silently trusted.
"""

from __future__ import annotations

from app.news.collectors import NewsCollector, SourceResult, StaticCollector, parse_gdelt, parse_rss
from app.news.correlation import NewsCorrelationEngine
from app.news.credibility import CredibilityEngine, tier_name
from app.news.decay import decay_item, decayed, live_items
from app.news.deduper import Deduper, dedupe
from app.news.engine import NewsEngine
from app.news.impact import ImpactEngine, NewsClassifier
from app.news.models import CategoryRule, NewsItem, NewsSnapshot, RawNewsItem

__all__ = [
    "CategoryRule",
    "CredibilityEngine",
    "Deduper",
    "ImpactEngine",
    "NewsClassifier",
    "NewsCollector",
    "NewsCorrelationEngine",
    "NewsEngine",
    "NewsItem",
    "NewsSnapshot",
    "RawNewsItem",
    "SourceResult",
    "StaticCollector",
    "decay_item",
    "decayed",
    "dedupe",
    "live_items",
    "parse_gdelt",
    "parse_rss",
    "tier_name",
]
