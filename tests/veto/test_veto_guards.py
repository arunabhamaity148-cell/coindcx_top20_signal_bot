"""Veto guard + VetoEngine tests (acceptance test 7, plus the fail-closed requirement).

Every guard is exercised twice where relevant: once passing, once blocking.
The last two tests assert the two structural guarantees - a guard exception is a BLOCK,
and missing data for a guard is a BLOCK (never a silent pass).
"""

from __future__ import annotations

from app.core.models import (
    Direction,
    DivergenceClass,
    FeedHealth,
    FeedState,
    NewsState,
    StrategyCandidate,
)
from app.risk import veto as guards
from app.risk.veto import GuardTier, VetoSeverity
from app.risk.veto_engine import VetoEngine
from tests.conftest import (
    BINANCE_SYMBOL,
    PAIR,
    SnapshotParts,
    flat_series,
    make_basis,
    make_snapshot,
)


def _candidate(
    direction: Direction = Direction.LONG,
    *,
    entry: float = 100.0,
    invalidation: float = 98.0,
    stop: float = 97.5,
) -> StrategyCandidate:
    return StrategyCandidate(
        strategy_id="S1",
        symbol=PAIR,
        direction=direction,
        confidence=0.7,
        entry_price=entry,
        entry_zone_low=entry - 0.5,
        entry_zone_high=entry + 0.5,
        invalidation=invalidation,
        stop_loss=stop,
        atr=2.0,
        expiry_min=45,
        reasons=("test",),
        correlation_group="MICRO_LIQUIDITY",
    )


def _snapshot(cfg, instrument, **overrides):
    fields = dict(
        candles=flat_series(price=100.0),
        binance_mid=100.0,
        basis=make_basis(binance_mid=100.0, z=0.2),
    )
    fields.update(overrides)
    return make_snapshot(SnapshotParts(**fields), instrument=instrument)


# --------------------------------------------------------------------------- G1


def test_g1_passes_on_healthy_feeds(cfg, instrument):
    snap = _snapshot(cfg, instrument)
    assert guards.g1_data_integrity(snap, cfg.veto.data_integrity).severity is VetoSeverity.PASS


def test_g1_blocks_on_stale_feed(cfg, instrument):
    feeds = {
        "binance_rest": FeedHealth("binance_rest", FeedState.STALE, 0, 9000),
        "binance_ws": FeedHealth("binance_ws", FeedState.HEALTHY, 0, 100),
        "coindcx_rest": FeedHealth("coindcx_rest", FeedState.HEALTHY, 0, 500),
        "news": FeedHealth("news", FeedState.HEALTHY, 0, 1000),
    }
    result = guards.g1_data_integrity(
        _snapshot(cfg, instrument, feeds=feeds), cfg.veto.data_integrity
    )
    assert result.blocked and result.tier is GuardTier.HARD_BLOCK


def test_g1_blocks_on_clock_drift(cfg, instrument):
    snap = _snapshot(cfg, instrument)
    drifted = type(snap)(**{**snap.__dict__, "clock_drift_ms": 4000})
    result = guards.g1_data_integrity(drifted, cfg.veto.data_integrity)
    assert result.blocked
    assert "clock drift" in result.reason


def test_g1_blocks_on_missing_required_feed(cfg, instrument):
    feeds = {
        "binance_rest": FeedHealth("binance_rest", FeedState.HEALTHY, 0, 10),
        "binance_ws": FeedHealth("binance_ws", FeedState.HEALTHY, 0, 10),
        "coindcx_rest": FeedHealth("coindcx_rest", FeedState.HEALTHY, 0, 10),
    }
    result = guards.g1_data_integrity(
        _snapshot(cfg, instrument, feeds=feeds), cfg.veto.data_integrity, required_feeds=("news",)
    )
    assert result.blocked


# --------------------------------------------------------------------------- G2


def test_g2_passes_within_bands(cfg, instrument):
    assert (
        guards.g2_cross_exchange_divergence(
            _snapshot(cfg, instrument), cfg.veto.divergence
        ).severity
        is VetoSeverity.PASS
    )


def test_g2_blocks_at_extreme_divergence(cfg, instrument):
    basis = make_basis(binance_mid=100.0, coindcx_mid=100.4, z=3.6)
    result = guards.g2_cross_exchange_divergence(
        _snapshot(cfg, instrument, basis=basis), cfg.veto.divergence
    )
    assert result.blocked and result.reason.startswith("divergence EXTREME")


def test_g2_blocks_when_basis_is_unavailable(cfg, instrument):
    result = guards.g2_cross_exchange_divergence(
        _snapshot(cfg, instrument, basis=None), cfg.veto.divergence
    )
    assert result.blocked


def test_g2_blocks_on_insufficient_history(cfg, instrument):
    basis = make_basis(
        binance_mid=100.0, z=0.1, observations=10, classification=DivergenceClass.ABNORMAL
    )
    result = guards.g2_cross_exchange_divergence(
        _snapshot(cfg, instrument, basis=basis), cfg.veto.divergence
    )
    assert result.blocked and "insufficient basis history" in result.reason


# --------------------------------------------------------------------------- G3


def test_g3_blocks_on_wide_spread(cfg, instrument):
    from app.core.models import LiquiditySnapshot

    liq = LiquiditySnapshot(
        spread_bps=40.0,
        depth_bid_usd=900_000.0,
        depth_ask_usd=900_000.0,
        imbalance=0.0,
        mid_jump_bps=0.0,
        expected_slippage_bps=20.0,
    )
    result = guards.g3_liquidity_slippage(
        _snapshot(cfg, instrument, liquidity=liq), cfg.veto.liquidity
    )
    assert result.blocked and "spread" in result.reason


def test_g3_blocks_on_thin_depth(cfg, instrument):
    from app.core.models import LiquiditySnapshot

    liq = LiquiditySnapshot(
        spread_bps=1.0,
        depth_bid_usd=1_000.0,
        depth_ask_usd=1_000.0,
        imbalance=0.0,
        mid_jump_bps=0.0,
        expected_slippage_bps=3.0,
    )
    result = guards.g3_liquidity_slippage(
        _snapshot(cfg, instrument, liquidity=liq), cfg.veto.liquidity
    )
    assert result.blocked and "depth" in result.reason


def test_g3_blocks_on_extreme_imbalance(cfg, instrument):
    from app.core.models import LiquiditySnapshot

    liq = LiquiditySnapshot(
        spread_bps=1.0,
        depth_bid_usd=900_000.0,
        depth_ask_usd=900_000.0,
        imbalance=0.95,
        mid_jump_bps=0.0,
        expected_slippage_bps=3.0,
    )
    result = guards.g3_liquidity_slippage(
        _snapshot(cfg, instrument, liquidity=liq), cfg.veto.liquidity
    )
    assert result.blocked and "imbalance" in result.reason


# --------------------------------------------------------------------------- G4


def test_g4_blocks_on_news_block_state(cfg, instrument):
    snap = _snapshot(cfg, instrument, news_state=NewsState.BLOCK)
    result = guards.g4_news_shock(snap, cfg.veto.news_shock, blocking_headline="CFTC halts trading")
    assert result.blocked and "BLOCK" in result.reason


def test_g4_blocks_during_blackout(cfg, instrument):
    result = guards.g4_news_shock(
        _snapshot(cfg, instrument), cfg.veto.news_shock, blackout_active=True
    )
    assert result.blocked


def test_g4_blocks_when_the_news_feed_is_unhealthy(cfg, instrument):
    feeds = {
        "binance_rest": FeedHealth("binance_rest", FeedState.HEALTHY, 0, 10),
        "binance_ws": FeedHealth("binance_ws", FeedState.HEALTHY, 0, 10),
        "coindcx_rest": FeedHealth("coindcx_rest", FeedState.HEALTHY, 0, 10),
        "news": FeedHealth("news", FeedState.STALE, 0, 900_000, "healthy_sources=0"),
    }
    result = guards.g4_news_shock(_snapshot(cfg, instrument, feeds=feeds), cfg.veto.news_shock)
    assert result.blocked


# --------------------------------------------------------------------------- G5


def test_g5_blocks_on_extreme_two_sided_crowding(cfg, instrument):
    from app.core.models import DerivativesSnapshot, OIQuadrant

    deriv = DerivativesSnapshot(
        symbol=BINANCE_SYMBOL,
        ts_ms=0,
        source="BINANCE",
        funding_z=2.8,
        oi_pct_rank=0.99,
        quadrant=OIQuadrant.PRICE_UP_OI_UP,
    )
    result = guards.g5_crowding(_snapshot(cfg, instrument, derivatives=deriv), cfg.veto.crowding)
    assert result.blocked and result.tier is GuardTier.HARD_BLOCK


def test_g5_blocks_when_crowding_inputs_are_missing(cfg, instrument):
    from app.core.models import DerivativesSnapshot

    deriv = DerivativesSnapshot(symbol=BINANCE_SYMBOL, ts_ms=0, source="BINANCE")
    result = guards.g5_crowding(_snapshot(cfg, instrument, derivatives=deriv), cfg.veto.crowding)
    assert result.blocked and "unavailable" in result.reason


def test_g5_passes_when_only_one_condition_holds(cfg, instrument):
    from app.core.models import DerivativesSnapshot

    deriv = DerivativesSnapshot(
        symbol=BINANCE_SYMBOL, ts_ms=0, source="BINANCE", funding_z=2.8, oi_pct_rank=0.50
    )
    result = guards.g5_crowding(_snapshot(cfg, instrument, derivatives=deriv), cfg.veto.crowding)
    assert result.severity is VetoSeverity.PASS


# --------------------------------------------------------------------------- degrade tier


def test_degrade_guards_never_block(cfg, instrument):

    snap = _snapshot(cfg, instrument)
    g6 = guards.g6_structure_invalidation(
        snap, cfg.veto.guard("structure_invalidation"), _candidate(invalidation=200.0)
    )
    g7 = guards.g7_extreme_volatility(
        snap, cfg.veto.guard("extreme_volatility"), realized_vol=3.0, atr_pct_rank=0.999
    )
    conflicted = type(snap)(**{**snap.__dict__, "btc_conflict": True})
    g8 = guards.g8_btc_regime_conflict(
        conflicted, cfg.veto.guard("btc_regime_conflict"), _candidate()
    )
    g11 = guards.g11_duplicate_anti_chase(
        snap, cfg.veto.guard("duplicate_anti_chase"), _candidate(), live_signals=(PAIR,)
    )
    g12 = guards.g12_spread_expansion(
        snap, cfg.veto.guard("spread_expansion"), spread_limit_bps=0.1
    )
    for result in (g6, g7, g8, g11, g12):
        assert result.degraded and not result.blocked
        assert result.tier is GuardTier.DEGRADE


def test_g10_blocks_mid_jump(cfg, instrument):
    snap = _snapshot(cfg, instrument)
    jumped = type(snap)(**{**snap.__dict__, "book_mid_jump_bps": 45.0})
    result = guards.g10_orderbook_instability(jumped, cfg.veto.guard("orderbook_instability"))
    assert result.degraded


# --------------------------------------------------------------------------- engine


def test_engine_blocks_when_any_hard_guard_blocks(cfg, instrument):
    basis = make_basis(binance_mid=100.0, coindcx_mid=100.4, z=3.9)
    outcome = VetoEngine(cfg).run(_snapshot(cfg, instrument, basis=basis), candidate=_candidate())
    assert outcome.blocked
    assert not outcome.passed
    assert any(r.guard == "G2" for r in outcome.hard_blocks)


def test_engine_passes_and_reports_degrade_penalty(cfg, instrument):
    outcome = VetoEngine(cfg).run(
        _snapshot(cfg, instrument), candidate=_candidate(), price=100.0, live_signals=()
    )
    assert outcome.passed
    assert outcome.confidence_penalty in (0.0, cfg.veto.degrade_penalty)


def test_guard_exception_becomes_a_block(cfg, instrument, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated guard failure")

    monkeypatch.setattr(guards, "g5_crowding", boom)
    outcome = VetoEngine(cfg).run(_snapshot(cfg, instrument), candidate=_candidate())
    assert outcome.blocked, "a guard exception must be a BLOCK (fail-closed)"
    assert any("G5" == r.guard for r in outcome.hard_blocks)
    assert outcome.errors


def test_veto_rows_are_journallable(cfg, instrument):
    basis = make_basis(binance_mid=100.0, coindcx_mid=100.4, z=3.9)
    outcome = VetoEngine(cfg).run(_snapshot(cfg, instrument, basis=basis), candidate=_candidate())
    rows = outcome.rows(PAIR, ts_ms=123)
    assert rows and rows[0]["guard"] == "G2" and rows[0]["severity"] == "BLOCK"
    assert rows[0]["evidence"]


def test_hard_block_cannot_be_disabled_by_a_strategy_score(cfg, instrument):
    engine = VetoEngine(cfg)
    assert engine.hard_block_enabled is True
    assert cfg.veto.override_allowed is False
    high_confidence = _candidate()
    object.__setattr__(high_confidence, "confidence", 1.0)
    basis = make_basis(binance_mid=100.0, coindcx_mid=100.5, z=4.0)
    outcome = engine.run(_snapshot(cfg, instrument, basis=basis), candidate=high_confidence)
    assert outcome.blocked
