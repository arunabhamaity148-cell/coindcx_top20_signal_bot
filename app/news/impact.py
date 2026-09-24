"""Impact scoring + mandatory time decay (FINAL_DELIVERABLE §H).

impact = max(category_weight) * credibility * (0.5 + 0.5*novelty)
         * (1.15 if market_wide else 1.0) * liquidity_sensitivity   -> clip [0,1]

severity:  CRITICAL if impact >= 0.80 AND tier <= 2 AND corroborating >= 1
           HIGH     if impact >= 0.60 AND tier <= 2
           MEDIUM   if impact >= 0.35
           LOW      otherwise

decay(t) = impact * 0.5 ^ (age_min / half_life_min)
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.core.mathx import clamp
from app.news.credibility import CredibilityEngine
from app.news.models import CategoryRule, NewsItem, RawNewsItem

DEFAULT_RULES: tuple[CategoryRule, ...] = (
    CategoryRule(
        "BLACK_SWAN",
        (r"black swan", r"existential", r"halts? trading globally", r"emergency shutdown"),
        "NEGATIVE",
        True,
        1.35,
    ),
    CategoryRule(
        "HACK",
        (
            r"\bhack(ed|er|s)?\b",
            r"\bexploit(ed)?\b",
            r"stolen funds",
            r"\bbridge attack\b",
            r"security breach",
        ),
        "NEGATIVE",
        False,
        1.25,
        ("BTC", "ETH"),
    ),
    CategoryRule(
        "EXPLOIT",
        (r"\bexploit\b", r"vulnerabilit", r"drained", r"rug ?pull", r"private key compromis"),
        "NEGATIVE",
        False,
        1.20,
    ),
    CategoryRule(
        "FOMC",
        (r"\bfomc\b", r"federal open market committee", r"rate decision", r"policy meeting"),
        "NEUTRAL",
        True,
        1.30,
    ),
    CategoryRule(
        "FED",
        (
            r"\bfed\b",
            r"federal reserve",
            r"\bpowell\b",
            r"\bwarsh\b",
            r"rate (hike|cut|decision)",
            r"monetary policy",
            r"\bbasis points?\b",
        ),
        "NEUTRAL",
        True,
        1.30,
    ),
    CategoryRule(
        "STABLECOIN",
        (r"stablecoin", r"\busdt\b", r"\busdc\b", r"\btether\b", r"depeg", r"reserve attestation"),
        "NEUTRAL",
        True,
        1.20,
    ),
    CategoryRule(
        "ETF",
        (
            r"\betf\b",
            r"exchange[- ]traded fund",
            r"spot bitcoin fund",
            r"etf (inflow|outflow|flows)",
        ),
        "POSITIVE",
        True,
        1.20,
    ),
    CategoryRule(
        "SEC",
        (
            r"\bsec\b",
            r"securities and exchange commission",
            r"\bgensler\b",
            r"enforcement action",
            r"innovation exemption",
        ),
        "NEUTRAL",
        True,
        1.20,
    ),
    CategoryRule(
        "CFTC",
        (r"\bcftc\b", r"commodity futures trading commission", r"innovation task force"),
        "NEUTRAL",
        True,
        1.15,
    ),
    CategoryRule(
        "REGULATION",
        (
            r"regulat",
            r"\bbill\b",
            r"\bact\b",
            r"legislation",
            r"\bsenate\b",
            r"\bcongress\b",
            r"clarity act",
            r"compliance rules?",
            r"licen[cs]e",
        ),
        "NEUTRAL",
        True,
        1.15,
    ),
    CategoryRule(
        "GEOPOLITICAL",
        (
            r"geopolit",
            r"\bwar\b",
            r"sanctions?",
            r"\bhormuz\b",
            r"\biran\b",
            r"tariffs?",
            r"strait of",
        ),
        "NEUTRAL",
        True,
        1.15,
    ),
    CategoryRule(
        "EXCHANGE",
        (
            r"\bbinance\b",
            r"\bcoinbase\b",
            r"\bcoindcx\b",
            r"\bkraken\b",
            r"\bokx\b",
            r"exchange (outage|halt|listing|maintenance)",
            r"suspends? withdrawals",
        ),
        "NEUTRAL",
        False,
        1.10,
    ),
    CategoryRule(
        "MACRO",
        (
            r"\bcpi\b",
            r"inflation",
            r"\bgdp\b",
            r"treasury yields?",
            r"\bnfp\b",
            r"payrolls",
            r"jobless claims",
        ),
        "NEUTRAL",
        True,
        1.20,
    ),
    CategoryRule(
        "DELISTING",
        (r"delist", r"removes? trading", r"trading (pairs? )?terminat"),
        "NEGATIVE",
        False,
        1.15,
    ),
    CategoryRule(
        "LISTING",
        (r"will list", r"\blisting\b", r"perpetual contract launch", r"adds? support for"),
        "POSITIVE",
        False,
        1.05,
    ),
    CategoryRule(
        "LIQUIDATION",
        (r"liquidat", r"forced selling", r"cascade", r"margin call"),
        "NEGATIVE",
        True,
        1.10,
    ),
    CategoryRule(
        "INSTITUTIONAL",
        (
            r"institutional",
            r"\bhedge fund\b",
            r"\betf issuer\b",
            r"custody (deal|partnership)",
            r"treasury (company|reserve)",
        ),
        "POSITIVE",
        False,
        1.05,
    ),
    CategoryRule(
        "WHALE",
        (r"\bwhale\b", r"moves? \d+[,.]?\d*\s?(btc|eth)", r"large transfer", r"cold wallet"),
        "NEUTRAL",
        False,
        1.00,
    ),
    CategoryRule(
        "TOKEN_UNLOCK",
        (r"\bunlock\b", r"vesting (schedule|cliff)", r"token release"),
        "NEGATIVE",
        False,
        1.00,
    ),
    CategoryRule(
        "MINING",
        (r"\bminers?\b", r"hashrate", r"hash rate", r"difficulty adjust"),
        "NEUTRAL",
        False,
        0.95,
    ),
    CategoryRule(
        "MAJOR_CORPORATE_EVENT",
        (r"\bacqui(re|sition)\b", r"\bmerger\b", r"stake in", r"partnership with"),
        "POSITIVE",
        False,
        1.05,
    ),
    CategoryRule(
        "RISK_ON",
        (r"risk[- ]on", r"rally", r"record high", r"recovery", r"rebound"),
        "POSITIVE",
        True,
        1.10,
    ),
    CategoryRule(
        "RISK_OFF",
        (r"risk[- ]off", r"sell[- ]?off", r"plunge", r"crash", r"slump", r"tumbles"),
        "NEGATIVE",
        True,
        1.15,
    ),
)

ASSET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("BTC", r"\bbitcoin\b|\bbtc\b"),
    ("ETH", r"\bethereum\b|\beth\b"),
    ("SOL", r"\bsolana\b|\bsol\b"),
    ("XRP", r"\bxrp\b|\bripple\b"),
    ("BNB", r"\bbnb\b|\bbinance coin\b"),
    ("DOGE", r"\bdogecoin\b"),
    ("ADA", r"\bcardano\b"),
    ("AVAX", r"\bavalanche\b"),
    ("LINK", r"\bchainlink\b"),
    ("LTC", r"\blitecoin\b"),
    ("DOT", r"\bpolkadot\b"),
    ("TRX", r"\btron\b"),
    ("SUI", r"\bsui\b"),
    ("APT", r"\baptos\b"),
    ("ARB", r"\barbitrum\b"),
    ("OP", r"\boptimism\b"),
    ("TON", r"\btoncoin\b"),
    ("NEAR", r"\bnear protocol\b"),
    ("ATOM", r"\bcosmos\b"),
    ("BCH", r"\bbitcoin cash\b"),
    ("ZEC", r"\bzcash\b|\bzec\b"),
    ("USDC", r"\busdc\b"),
    ("USDT", r"\busdt\b|\btether\b"),
)

ENTITY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("SEC", r"\bsec\b|securities and exchange commission"),
    ("CFTC", r"\bcftc\b|commodity futures trading commission"),
    ("FED", r"\bfed\b|federal reserve|\bfomc\b"),
    ("Binance", r"\bbinance\b"),
    ("CoinDCX", r"\bcoindcx\b"),
    ("Coinbase", r"\bcoinbase\b"),
    ("Treasury", r"\btreasury\b"),
    ("WhiteHouse", r"white house"),
    ("BlackRock", r"\bblackrock\b"),
    ("Tether", r"\btether\b"),
    ("Circle", r"\bcircle\b"),
)


@dataclass
class NewsClassifier:
    rules: Sequence[CategoryRule] = field(default_factory=lambda: DEFAULT_RULES)
    categories: Sequence[str] = ()

    def classify(self, item: RawNewsItem, cfg) -> NewsItem:
        text = f"{item.headline} {item.summary}".lower()
        matched = [
            rule
            for rule in self.rules
            if any(re.search(pattern, text) for pattern in rule.patterns)
        ]
        if not matched:
            matched = [CategoryRule("GENERAL", (), "NEUTRAL", False, 1.0)]
        if self.categories:
            known = set(self.categories)
            matched = [r for r in matched if r.category in known] or matched
        matched.sort(key=lambda r: cfg.weight(r.category), reverse=True)
        categories = [r.category for r in matched]
        assets = _extract_assets(text)
        if not assets:
            for rule in matched:
                assets.extend(rule.assets)
        return NewsItem(
            hash=item.hash,
            ts_ms=item.published_ms or item.fetched_ms,
            source=item.source_id,
            tier=item.tier,
            headline=item.headline,
            url=item.url,
            categories=categories,
            entities=_extract_entities(text),
            affected_assets=sorted(set(assets)),
            half_life_min=cfg.half_life(categories[0]),
            market_wide=any(r.market_wide for r in matched),
            direction=_resolve_direction(matched),
            sources=[item.source_id],
        )


def _resolve_direction(rules: Iterable[CategoryRule]) -> str:
    score = 0
    for rule in rules:
        if rule.direction == "POSITIVE":
            score += 1
        elif rule.direction == "NEGATIVE":
            score -= 1
    if score > 0:
        return "BULLISH"
    if score < 0:
        return "BEARISH"
    return "NEUTRAL"


def _extract_assets(text: str) -> list[str]:
    return [asset for asset, pattern in ASSET_PATTERNS if re.search(pattern, text)]


def _extract_entities(text: str) -> list[str]:
    return [entity for entity, pattern in ENTITY_PATTERNS if re.search(pattern, text)]


@dataclass
class ImpactEngine:
    cfg: object
    credibility: CredibilityEngine
    novelty_lookback_hours: int = 72
    severity_thresholds: dict[str, float] = field(
        default_factory=lambda: {"critical": 0.80, "high": 0.60, "medium": 0.35}
    )
    market_wide_multiplier: float = 1.15

    @classmethod
    def from_config(cls, cfg, credibility: CredibilityEngine) -> ImpactEngine:
        thresholds = {k: float(v) for k, v in (cfg.severity_thresholds or {}).items()}
        return cls(
            cfg=cfg,
            credibility=credibility,
            novelty_lookback_hours=int(cfg.novelty_lookback_hours),
            severity_thresholds=thresholds or {"critical": 0.80, "high": 0.60, "medium": 0.35},
            market_wide_multiplier=float(cfg.market_wide_multiplier),
        )

    def novelty(self, item: NewsItem, recent: Sequence[NewsItem]) -> float:
        from app.news.models import jaccard

        max_similarity = 0.0
        for other in recent:
            if other.hash == item.hash:
                continue
            max_similarity = max(max_similarity, jaccard(item.headline, other.headline))
        return clamp(1.0 - max_similarity, 0.0, 1.0)

    def score(
        self,
        item: NewsItem,
        *,
        recent: Sequence[NewsItem] = (),
        corroborating: int = 0,
        liquidity_sensitivity: float = 1.0,
        tier: int | None = None,
    ) -> NewsItem:
        tier = item.tier if tier is None else tier
        category_weight = max(
            (self.cfg.weight(c) for c in item.categories), default=self.cfg.weight("GENERAL")
        )
        credibility = self.credibility.score(tier, independent_sources=max(corroborating + 1, 1))
        novelty = self.novelty(item, recent)
        market_mult = self.market_wide_multiplier if item.market_wide else 1.0
        raw = (
            category_weight
            * credibility
            * (0.5 + 0.5 * novelty)
            * market_mult
            * liquidity_sensitivity
        )
        impact = clamp(raw, 0.0, 1.0)

        item.credibility = credibility
        item.novelty = novelty
        item.impact = impact
        item.corroborating = corroborating
        item.severity = self.severity(impact, tier=tier, corroborating=corroborating)
        item.confidence = clamp(
            impact * (0.6 + 0.4 * min(1.0, (corroborating + 1) / 3.0)), 0.0, 1.0
        )
        item.decayed_impact = impact
        return item

    def severity(self, impact: float, *, tier: int, corroborating: int) -> str:
        critical = self.severity_thresholds.get("critical", 0.80)
        high = self.severity_thresholds.get("high", 0.60)
        medium = self.severity_thresholds.get("medium", 0.35)
        can_raise = self.credibility.can_raise_high(tier, corroborating)
        if impact >= critical and can_raise:
            return "CRITICAL"
        if impact >= high and can_raise:
            return "HIGH"
        if impact >= medium:
            return "MEDIUM"
        return "LOW"

    def decay(self, item: NewsItem, *, now_ms: int) -> float:
        from app.news.decay import decay_item

        return decay_item(
            item, now_ms=now_ms, default_half_life=self.cfg.half_life(item.primary_category)
        )

    def decay_all(self, items: Iterable[NewsItem], *, now_ms: int) -> list[NewsItem]:
        out = list(items)
        for item in out:
            self.decay(item, now_ms=now_ms)
        return out

    def dominant(self, items: Sequence[NewsItem]) -> NewsItem | None:
        scored = [
            i for i in items if i.decayed_impact > float(self.cfg.state.get("decay_floor", 0.05))
        ]
        if not scored:
            return None
        return max(scored, key=lambda i: i.decayed_impact)


__all__ = ["ASSET_PATTERNS", "DEFAULT_RULES", "ENTITY_PATTERNS", "ImpactEngine", "NewsClassifier"]
