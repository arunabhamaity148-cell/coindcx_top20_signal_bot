"""Backwards-compatible re-export of the canonical news models.

The news data structures used to live here; they now live in `app.news.models` (single
source of truth - no duplicated definitions). This module is kept so imports that predate
the move keep working, and it re-exports the real objects rather than redefining them.
"""

from __future__ import annotations

from app.news.models import (
    CategoryRule,
    NewsItem,
    NewsSnapshot,
    RawNewsItem,
    headline_hash,
    jaccard,
    normalize_headline,
    simhash,
    token_set,
)

__all__ = [
    "CategoryRule",
    "NewsItem",
    "NewsSnapshot",
    "RawNewsItem",
    "headline_hash",
    "jaccard",
    "normalize_headline",
    "simhash",
    "token_set",
]
