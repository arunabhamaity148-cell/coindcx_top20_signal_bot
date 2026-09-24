"""News credibility hierarchy (FINAL_DELIVERABLE §G).

    PRIMARY_OFFICIAL (CFTC, SEC, FED, FOMC, White House, U.S. Treasury,
                      Binance official, CoinDCX official, ETF issuers)  1.00
    REPUTABLE_SECONDARY (CoinDesk, The Block, Decrypt, Reuters, Bloomberg, GDELT) 0.80
    MULTI-SOURCE CONFIRMATION  +0.15 / +0.25
    SOCIAL DISCOVERY 0.30

ENFORCED RULE (unit-tested, not a guideline):
    can_raise_high(tier, corroborating) = (tier <= 2) and (corroborating >= 1)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.core.mathx import clamp

TIER_NAMES = {1: "PRIMARY_OFFICIAL", 2: "REPUTABLE_SECONDARY", 4: "SOCIAL_DISCOVERY"}


def tier_name(tier: int) -> str:
    return TIER_NAMES.get(tier, f"TIER_{tier}")


@dataclass
class CredibilityEngine:
    tier_scores: dict[int, float]
    multi_source_bonus: dict[int, float]
    cap: float = 1.0
    high_critical_max_tier: int = 2
    high_critical_min_corroboration: int = 1

    @classmethod
    def from_config(cls, cfg) -> CredibilityEngine:
        raw_scores = dict(cfg.credibility.get("tier_scores", {1: 1.0, 2: 0.8, 4: 0.3}))
        scores = {int(k): float(v) for k, v in raw_scores.items()}
        raw_bonus = dict(cfg.credibility.get("multi_source_bonus", {2: 0.15, 3: 0.25}))
        bonus = {int(k): float(v) for k, v in raw_bonus.items()}
        return cls(
            tier_scores=scores,
            multi_source_bonus=bonus,
            cap=float(cfg.credibility.get("corroboration_cap", 1.0)),
            high_critical_max_tier=int(cfg.credibility.get("high_critical_max_tier", 2)),
            high_critical_min_corroboration=int(
                cfg.credibility.get("high_critical_min_corroboration", 1)
            ),
        )

    def base_score(self, tier: int) -> float:
        return float(self.tier_scores.get(tier, 0.30))

    def corroboration_bonus(self, independent_sources: int) -> float:
        if independent_sources < 2:
            return 0.0
        best = 0.0
        for count, bonus in self.multi_source_bonus.items():
            if independent_sources >= count:
                best = max(best, bonus)
        return best

    def score(self, tier: int, independent_sources: int = 1) -> float:
        return clamp(
            self.base_score(tier) + self.corroboration_bonus(independent_sources), 0.0, self.cap
        )

    def can_raise_high(self, tier: int, corroborating: int) -> bool:
        """A single unverified rumor can NEVER create a HIGH or CRITICAL item."""
        return (
            tier <= self.high_critical_max_tier
            and corroborating >= self.high_critical_min_corroboration
        )

    def independent_tier_count(self, tiers: Iterable[int]) -> int:
        return len({t for t in tiers})


__all__ = ["CredibilityEngine", "TIER_NAMES", "tier_name"]
