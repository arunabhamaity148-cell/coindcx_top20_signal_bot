"""Mandatory time decay for news impact (FINAL_DELIVERABLE §H).

    decay(t) = impact * 0.5 ** (age_min / half_life_min)

Both the news engine and the DANGER monitor use this, so it lives in its own module. An
item whose decayed impact falls below the configured floor stops steering signals - a
week-old headline can never justify a new entry.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence

from app.core.mathx import clamp
from app.news.models import NewsItem


def decayed(impact: float, *, age_min: float, half_life_min: float) -> float:
    """Exponential half-life decay, clipped to [0, 1]."""
    if half_life_min <= 0:
        return 0.0
    return clamp(impact * (0.5 ** (max(0.0, age_min) / half_life_min)), 0.0, 1.0)


def decay_item(item: NewsItem, *, now_ms: int, default_half_life: float = 60.0) -> float:
    """Recompute `item.decayed_impact` in place and return it."""
    age_min = max(0.0, (now_ms - item.ts_ms) / 60_000.0)
    half_life = item.half_life_min or default_half_life
    item.decayed_impact = decayed(item.impact, age_min=age_min, half_life_min=half_life)
    return item.decayed_impact


def live_items(
    items: Iterable[NewsItem], *, now_ms: int | None = None, floor: float = 0.05
) -> list[NewsItem]:
    reference = int(time.time() * 1000) if now_ms is None else now_ms
    return [item for item in items if decay_item(item, now_ms=reference) > floor]


def most_recent(items: Sequence[NewsItem]) -> NewsItem | None:
    return max(items, key=lambda item: item.ts_ms) if items else None


__all__ = ["decay_item", "decayed", "live_items", "most_recent"]
