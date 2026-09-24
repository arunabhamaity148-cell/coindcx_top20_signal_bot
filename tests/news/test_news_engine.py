"""News engine tests (acceptance tests 4 and 5) + the credibility ceiling rule.

The headline requirement: a SINGLE UNVERIFIED RUMOR can never produce HIGH or CRITICAL.
"""

from __future__ import annotations

import time

import pytest

from app.core.models import Direction, NewsState
from app.news.collectors import StaticCollector, parse_gdelt, parse_rss
from app.news.correlation import NewsCorrelationEngine
from app.news.credibility import CredibilityEngine
from app.news.decay import decayed
from app.news.deduper import Deduper, dedupe
from app.news.engine import NewsEngine
from app.news.impact import ImpactEngine, NewsClassifier
from app.news.models import RawNewsItem, jaccard, normalize_headline

NOW = int(time.time() * 1000) - 60_000  # live clock: items are 'published' 1 min ago


def _raw(headline: str, *, source="cftc", tier=1, age_min=1.0) -> RawNewsItem:
    return RawNewsItem(
        source_id=source,
        tier=tier,
        headline=headline,
        url="https://example.invalid/x",
        published_ms=NOW - int(age_min * 60_000),
        fetched_ms=NOW,
    )


# --------------------------------------------------------------------------- credibility


def test_credibility_hierarchy_and_corroboration_bonus(cfg):
    engine = CredibilityEngine.from_config(cfg.news)
    assert engine.score(1, 1) == pytest.approx(1.00)
    assert engine.score(2, 1) == pytest.approx(0.80)
    assert engine.score(4, 1) == pytest.approx(0.30)
    assert engine.score(4, 3) == pytest.approx(0.30 + 0.25)


def test_single_unverified_rumor_can_never_be_high_or_critical(cfg):
    engine = CredibilityEngine.from_config(cfg.news)
    assert engine.can_raise_high(tier=4, corroborating=0) is False
    assert engine.can_raise_high(tier=2, corroborating=0) is False
    assert engine.can_raise_high(tier=1, corroborating=0) is False
    assert engine.can_raise_high(tier=2, corroborating=1) is True
    assert engine.can_raise_high(tier=1, corroborating=1) is True


def test_social_only_news_is_capped_below_critical(cfg):
    classifier = NewsClassifier()
    impact = ImpactEngine.from_config(cfg.news, CredibilityEngine.from_config(cfg.news))
    item = classifier.classify(
        _raw("Binance hacked: funds stolen", source="telegram", tier=4), cfg.news
    )
    impact.score(item, recent=[], corroborating=0)
    assert item.severity in ("LOW", "MEDIUM")
    assert item.severity not in ("HIGH", "CRITICAL")


def test_official_source_with_corroboration_can_reach_critical(cfg):
    classifier = NewsClassifier()
    impact = ImpactEngine.from_config(cfg.news, CredibilityEngine.from_config(cfg.news))
    item = classifier.classify(
        _raw("SEC issues emergency measure after exchange hack; funds stolen"), cfg.news
    )
    impact.score(item, recent=[], corroborating=1)
    assert item.impact >= 0.6
    assert item.severity in ("HIGH", "CRITICAL")


# --------------------------------------------------------------------------- impact / decay


def test_impact_formula_and_clamp(cfg):
    classifier = NewsClassifier()
    impact = ImpactEngine.from_config(cfg.news, CredibilityEngine.from_config(cfg.news))
    item = classifier.classify(_raw("Federal Reserve signals surprise rate hike"), cfg.news)
    impact.score(item, recent=[], corroborating=1, liquidity_sensitivity=1.1)
    assert 0.0 <= item.impact <= 1.0
    assert item.novelty == pytest.approx(1.0)


def test_time_decay_is_mandatory():
    assert decayed(1.0, age_min=0, half_life_min=60) == pytest.approx(1.0)
    assert decayed(1.0, age_min=60, half_life_min=60) == pytest.approx(0.5)
    assert decayed(1.0, age_min=240, half_life_min=60) == pytest.approx(0.0625)
    assert decayed(1.0, age_min=10, half_life_min=0) == 0.0


def test_stale_news_cannot_steer_a_new_signal(cfg):
    classifier = NewsClassifier()
    impact = ImpactEngine.from_config(cfg.news, CredibilityEngine.from_config(cfg.news))
    item = classifier.classify(
        _raw("SEC issues emergency measure after a major hack", age_min=2000), cfg.news
    )
    impact.score(item, recent=[], corroborating=1)
    decayed_impact = impact.decay(item, now_ms=NOW)
    assert decayed_impact < 0.05


# --------------------------------------------------------------------------- dedupe


def test_normalize_and_jaccard():
    assert normalize_headline("The BTC Rally!") == "btc rally"
    assert jaccard("bitcoin rally continues", "bitcoin rally continues today") > 0.6


def test_deduper_collapses_near_duplicates():
    deduper = Deduper(similarity_threshold=0.8)
    items = [
        _raw("Bitcoin ETF inflows hit a record in September"),
        _raw("Bitcoin ETF inflows hit record in September", source="coindesk", tier=2),
        _raw("Solana network upgrade ships", source="coindesk", tier=2),
    ]
    fresh = deduper.filter_new(items)
    assert len(fresh) == 2
    assert dedupe(items, threshold=0.8)[0].source_id == "cftc"


# --------------------------------------------------------------------------- collectors


def test_rss_parser_handles_a_minimal_feed():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>CFTC Innovation Task Force announces forum</title>
      <link>https://www.cftc.gov/x</link>
      <pubDate>Mon, 21 Sep 2026 12:00:00 GMT</pubDate></item>
    </channel></rss>"""
    items = parse_rss(xml, source_id="cftc", tier=1)
    assert len(items) == 1
    assert items[0].headline.startswith("CFTC Innovation")
    assert items[0].published_ms is not None


def test_gdelt_parser_reads_articles():
    payload = (
        '{"articles":[{"title":"Bitcoin holds near $86,000","url":"https://x.invalid/1",'
        '"seendate":"20260922T113500Z","domain":"coindesk.com"}]}'
    )
    items = parse_gdelt(payload, source_id="gdelt", tier=2)
    assert items and items[0].headline.endswith("$86,000")


# --------------------------------------------------------------------------- engine


@pytest.mark.asyncio
async def test_engine_state_is_clear_with_healthy_sources(cfg):
    collector = StaticCollector([
        _raw("Solana network upgrade ships", source="cftc"),
        _raw("Bitcoin ETF flow update", source="sec"),
        _raw("Federal Reserve policy update", source="fed"),
        _raw("Crypto market update", source="coindesk", tier=2),
    ])
    engine = NewsEngine(cfg.news, collector)
    snapshot = await engine.run_once()
    assert snapshot.state is NewsState.CLEAR
    assert len(snapshot.healthy_sources) >= cfg.news.min_sources_healthy
    assert engine.health().state.value == "HEALTHY"


@pytest.mark.asyncio
async def test_engine_blocks_when_sources_drop_below_the_minimum(cfg):
    collector = StaticCollector([_raw("Solana network upgrade ships")], healthy=("cftc",))
    engine = NewsEngine(cfg.news, collector)
    snapshot = await engine.run_once()
    assert snapshot.state is NewsState.BLOCK
    assert engine.health().state.value == "STALE"


@pytest.mark.asyncio
async def test_critical_official_news_blocks_and_sets_blackout(cfg):
    collector = StaticCollector(
        [
            _raw(
                "SEC and CFTC announce an emergency halt after a major exchange hack: funds stolen"
            ),
            _raw(
                "SEC and CFTC announce emergency halt after major exchange hack — funds stolen",
                source="coindesk",
                tier=2,
            ),
        ]
    )
    engine = NewsEngine(cfg.news, collector)
    snapshot = await engine.run_once()
    assert snapshot.state is NewsState.BLOCK
    assert snapshot.blocking
    assert engine.is_blackout(snapshot) is True


@pytest.mark.asyncio
async def test_engine_survives_a_failing_collector(cfg):
    class Broken:
        async def collect(self):
            raise RuntimeError("feed exploded")

    engine = NewsEngine(cfg.news, Broken())
    snapshot = await engine.run_once()
    assert snapshot.state is NewsState.BLOCK  # outage => fail-closed
    assert engine.last_error


def test_correlation_engine_maps_assets_to_pairs_and_conflicts():
    from app.news.models import NewsItem

    correlation = NewsCorrelationEngine()
    assert correlation.pair_for("BTC") == "B-BTC_USDT"
    news_item = NewsItem(
        hash="x",
        ts_ms=NOW,
        source="coindesk",
        tier=2,
        headline="Bitcoin sell-off",
        categories=["RISK_OFF"],
        affected_assets=["BTC"],
        direction="BEARISH",
        market_wide=True,
        decayed_impact=0.7,
        impact=0.7,
    )
    assert correlation.applies_to(news_item, "B-ETH_USDT") is True
    assert correlation.direction_conflict(news_item, Direction.LONG) is True
    assert correlation.direction_conflict(news_item, Direction.SHORT) is False
    assert correlation.opposing([news_item], pair="B-BTC_USDT", direction=Direction.LONG)
