"""News correlation engine.

Maps a news item to the configured tradeable universe: which assets it touches, whether
it is market-wide, and whether it argues AGAINST a proposed direction. This is what lets
veto guard G4 block a signal whose direction the news contradicts, and what populates the
`affected_assets` column of the news journal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.core.models import Direction
from app.news.models import NewsItem

# Base-asset symbol -> CoinDCX pair, injected from the config at construction time.
ASSET_ALIASES: Mapping[str, str] = {
    "BTC": "B-BTC_USDT",
    "ETH": "B-ETH_USDT",
    "SOL": "B-SOL_USDT",
    "BNB": "B-BNB_USDT",
    "XRP": "B-XRP_USDT",
    "DOGE": "B-DOGE_USDT",
    "ADA": "B-ADA_USDT",
    "AVAX": "B-AVAX_USDT",
    "LINK": "B-LINK_USDT",
    "LTC": "B-LTC_USDT",
    "DOT": "B-DOT_USDT",
    "TRX": "B-TRX_USDT",
    "SUI": "B-SUI_USDT",
    "APT": "B-APT_USDT",
    "ARB": "B-ARB_USDT",
    "OP": "B-OP_USDT",
    "TON": "B-TON_USDT",
    "NEAR": "B-NEAR_USDT",
    "ATOM": "B-ATOM_USDT",
    "BCH": "B-BCH_USDT",
}

# Directional stance of an item's `direction` field from the perspective of a LONG.
_DIRECTION_SIGN = {"BULLISH": 1, "BEARISH": -1, "NEUTRAL": 0}


@dataclass
class NewsCorrelationEngine:
    """Correlates news items with the configured tradeable universe and directions."""

    pairs: Mapping[str, str] = field(default_factory=lambda: dict(ASSET_ALIASES))
    market_wide_assets: Sequence[str] = field(default_factory=lambda: ("BTC", "ETH"))

    @classmethod
    def from_symbol_map(cls, symbol_map) -> "NewsCorrelationEngine":
        pairs: dict[str, str] = {}
        for item in symbol_map.all():
            pairs[item.base.upper()] = item.coindcx
        return cls(pairs=pairs, market_wide_assets=tuple(sorted(pairs)))

    def pair_for(self, asset: str) -> str | None:
        return self.pairs.get(asset.upper())

    def affected_pairs(self, item: NewsItem) -> list[str]:
        out: list[str] = []
        for asset in item.affected_assets:
            pair = self.pair_for(asset)
            if pair and pair not in out:
                out.append(pair)
        if item.market_wide:
            for asset in self.market_wide_assets:
                pair = self.pair_for(asset)
                if pair and pair not in out:
                    out.append(pair)
        return out

    def applies_to(self, item: NewsItem, pair: str) -> bool:
        if item.market_wide:
            return True
        return pair in self.affected_pairs(item)

    def direction_conflict(self, item: NewsItem, direction: Direction) -> bool:
        """True when the item actively argues against the proposed direction."""
        sign = _DIRECTION_SIGN.get(item.direction.upper(), 0)
        if sign == 0:
            return False
        return (direction is Direction.LONG and sign < 0) or (
            direction is Direction.SHORT and sign > 0
        )

    def opposing(
        self,
        items: Sequence[NewsItem],
        *,
        pair: str,
        direction: Direction,
        min_impact: float = 0.35,
    ) -> list[NewsItem]:
        return [
            i
            for i in items
            if self.applies_to(i, pair)
            and i.decayed_impact >= min_impact
            and self.direction_conflict(i, direction)
        ]

    def summarise(self, items: Sequence[NewsItem], *, pair: str) -> dict[str, object]:
        relevant = [i for i in items if self.applies_to(i, pair)]
        return {
            "pair": pair,
            "relevant": len(relevant),
            "max_impact": max((i.decayed_impact for i in relevant), default=0.0),
            "categories": sorted({c for i in relevant for c in i.categories}),
        }


__all__ = ["ASSET_ALIASES", "NewsCorrelationEngine"]
