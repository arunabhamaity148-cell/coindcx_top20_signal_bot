"""Strategy unit tests: S1..S5 (acceptance test 6).

Each engine is given a hand-built `MarketSnapshot` that satisfies its documented setup,
and is asserted to (a) produce a well-formed candidate and (b) refuse the obvious
near-misses. Candles are constructed explicitly (never by "one candle" shortcuts) so the
open/high/low/close geometry is unambiguous. No engine may fall back to generic RSI/MACD
behaviour.
"""

from __future__ import annotations

from app.core.mathx import ema_last
from app.core.models import (
    DerivativesSnapshot,
    Direction,
    NewsState,
    StrategyCandidate,
)
from app.strategies.registry import StrategyRegistry
from app.strategies.s1_liquidity_sweep import LiquiditySweepReclaim
from app.strategies.s2_volatility_compression import VolatilityCompressionBreakout
from app.strategies.s3_funding_crowding import FundingCrowdingExhaustion
from app.strategies.s4_oi_trend import OIConfirmedTrendContinuation
from app.strategies.s5_basis_convergence import CrossVenueBasisConvergence
from tests.conftest import (
    BINANCE_SYMBOL,
    SnapshotParts,
    candle,
    flat_series,
    make_basis,
    make_candles,
    make_snapshot,
)


def _deriv(**overrides) -> DerivativesSnapshot:
    base = dict(
        symbol=BINANCE_SYMBOL,
        ts_ms=1_700_000_000_000,
        source="BINANCE",
        mark_price=100.0,
        funding_rate=0.0001,
        funding_z=0.4,
        open_interest=40_000.0,
        oi_chg_pct=0.2,
        oi_pct_rank=0.5,
        taker_buy_sell_ratio=0.5,
        price_chg_pct=0.1,
    )
    base.update(overrides)
    return DerivativesSnapshot(**base)  # type: ignore[arg-type]


def _flat(n: int = 80, price: float = 100.0, spread: float = 1.0, ratio: float = 0.5):
    return make_candles([price] * n, spread=spread, taker_ratio=ratio)


# --------------------------------------------------------------------------- S1


def test_s1_long_fires_on_a_sweep_and_reclaim(cfg, instrument):
    prior = _flat(80, spread=1.0, ratio=0.5)  # prior lows at 99.50
    sweep = candle(
        open=100.0,
        high=100.2,
        low=99.4,
        close=99.9,
        taker_ratio=0.62,
        open_time_ms=prior[-1].open_time_ms + 60_000,
    )
    candles = [*prior, sweep]
    snap = make_snapshot(
        SnapshotParts(
            candles={"1m": candles, "5m": candles},
            binance_mid=100.0,
            derivatives=_deriv(oi_chg_pct=0.1),
            basis=make_basis(binance_mid=100.0, z=0.2),
        ),
        instrument=instrument,
    )
    candidate = LiquiditySweepReclaim(cfg).analyze(snap)
    assert isinstance(candidate, StrategyCandidate)
    assert candidate.direction is Direction.LONG
    assert candidate.stop_loss < candidate.entry_price < candidate.tp1
    assert candidate.reasons and "swept" in candidate.reasons[0]


def test_s1_short_fires_on_an_upside_sweep_rejection(cfg, instrument):
    prior = _flat(80, spread=1.0, ratio=0.5)  # prior highs at 100.50
    sweep = candle(
        open=100.0,
        high=100.6,
        low=99.8,
        close=100.1,
        taker_ratio=0.35,
        open_time_ms=prior[-1].open_time_ms + 60_000,
    )
    candles = [*prior, sweep]
    snap = make_snapshot(
        SnapshotParts(
            candles={"1m": candles, "5m": candles},
            binance_mid=100.0,
            derivatives=_deriv(oi_chg_pct=0.1),
            basis=make_basis(binance_mid=100.0),
        ),
        instrument=instrument,
    )
    candidate = LiquiditySweepReclaim(cfg).analyze(snap)
    assert candidate is not None and candidate.direction is Direction.SHORT
    assert candidate.stop_loss > candidate.entry_price > candidate.tp1


def test_s1_refuses_without_a_sweep(cfg, instrument):
    candles = _flat(80, spread=1.0, ratio=0.6)
    snap = make_snapshot(
        SnapshotParts(
            candles={"1m": candles, "5m": candles},
            binance_mid=100.0,
            derivatives=_deriv(),
            basis=make_basis(binance_mid=100.0),
        ),
        instrument=instrument,
    )
    assert LiquiditySweepReclaim(cfg).analyze(snap) is None


def test_s1_refuses_a_real_breakout_because_oi_builds(cfg, instrument):
    prior = _flat(80, spread=1.0, ratio=0.5)
    sweep = candle(
        open=100.0,
        high=100.2,
        low=99.4,
        close=99.9,
        taker_ratio=0.70,
        open_time_ms=prior[-1].open_time_ms + 60_000,
    )
    candles = [*prior, sweep]
    snap = make_snapshot(
        SnapshotParts(
            candles={"1m": candles, "5m": candles},
            binance_mid=100.0,
            derivatives=_deriv(oi_chg_pct=1.8),
            basis=make_basis(binance_mid=100.0),
        ),
        instrument=instrument,
    )
    assert LiquiditySweepReclaim(cfg).analyze(snap) is None


def test_s1_needs_min_candles(cfg, instrument):
    candles = _flat(20, spread=1.0, ratio=0.6)
    snap = make_snapshot(
        SnapshotParts(
            candles={"1m": candles, "5m": candles}, binance_mid=100.0, derivatives=_deriv()
        ),
        instrument=instrument,
    )
    assert LiquiditySweepReclaim(cfg).analyze(snap) is None


# --------------------------------------------------------------------------- S2


def _compression(min_bars: int = 200, wide: int = 140) -> list:
    """140 wide bars then (min_bars-wide) tight bars, so the tail is genuinely compressed."""
    return [
        *make_candles([90.0] * wide, spread=3.0),
        *make_candles([100.0] * (min_bars - wide), spread=0.2),
    ]


def test_s2_long_fires_on_a_compressed_breakout(cfg, instrument):
    compression = _compression()
    flat_trigger = make_candles([100.0] * 199, spread=0.2, taker_ratio=0.5)
    expansion = candle(
        open=100.05,
        high=100.95,
        low=100.0,
        close=100.9,
        taker_ratio=0.60,
        open_time_ms=flat_trigger[-1].open_time_ms + 60_000,
    )
    trigger = [*flat_trigger, expansion]
    snap = make_snapshot(
        SnapshotParts(
            candles={"15m": compression, "5m": trigger},
            binance_mid=100.9,
            derivatives=_deriv(oi_chg_pct=1.6, mark_price=100.9),
            basis=make_basis(binance_mid=100.9),
        ),
        instrument=instrument,
    )
    candidate = VolatilityCompressionBreakout(cfg).analyze(snap)
    assert isinstance(candidate, StrategyCandidate)
    assert candidate.direction is Direction.LONG
    assert any("ATR percentile" in reason for reason in candidate.reasons)
    assert any("OI" in reason for reason in candidate.reasons)


def test_s2_refuses_when_oi_does_not_confirm(cfg, instrument):
    compression = _compression()
    flat_trigger = make_candles([100.0] * 199, spread=0.2, taker_ratio=0.5)
    expansion = candle(
        open=100.05,
        high=100.95,
        low=100.0,
        close=100.9,
        taker_ratio=0.60,
        open_time_ms=flat_trigger[-1].open_time_ms + 60_000,
    )
    trigger = [*flat_trigger, expansion]
    snap = make_snapshot(
        SnapshotParts(
            candles={"15m": compression, "5m": trigger},
            binance_mid=100.9,
            derivatives=_deriv(oi_chg_pct=0.1, mark_price=100.9),
            basis=make_basis(binance_mid=100.9),
        ),
        instrument=instrument,
    )
    assert VolatilityCompressionBreakout(cfg).analyze(snap) is None


def test_s2_refuses_when_volatility_is_not_compressed(cfg, instrument):
    compression = make_candles([100.0] * 200, spread=4.0)  # uniformly wide, no compression
    flat_trigger = make_candles([100.0] * 199, spread=4.0, taker_ratio=0.5)
    expansion = candle(
        open=100.0,
        high=111.0,
        low=99.5,
        close=110.0,
        taker_ratio=0.6,
        open_time_ms=flat_trigger[-1].open_time_ms + 60_000,
    )
    trigger = [*flat_trigger, expansion]
    snap = make_snapshot(
        SnapshotParts(
            candles={"15m": compression, "5m": trigger},
            binance_mid=110.0,
            derivatives=_deriv(oi_chg_pct=2.0, mark_price=110.0),
            basis=make_basis(binance_mid=110.0),
        ),
        instrument=instrument,
    )
    assert VolatilityCompressionBreakout(cfg).analyze(snap) is None


# --------------------------------------------------------------------------- S3


def test_s3_short_fires_on_crowded_longs_failing(cfg, instrument):
    prior = _flat(79, spread=1.0, ratio=0.5)
    failure = candle(
        open=100.0,
        high=100.5,
        low=99.7,
        close=99.8,
        taker_ratio=0.40,
        open_time_ms=prior[-1].open_time_ms + 60_000,
    )
    candles = [*prior, failure]
    snap = make_snapshot(
        SnapshotParts(
            candles={"5m": candles, "1h": candles},
            binance_mid=99.8,
            derivatives=_deriv(funding_z=2.6, oi_pct_rank=0.92, mark_price=99.8),
            basis=make_basis(binance_mid=99.8),
        ),
        instrument=instrument,
    )
    candidate = FundingCrowdingExhaustion(cfg).analyze(snap)
    assert isinstance(candidate, StrategyCandidate)
    assert candidate.direction is Direction.SHORT
    assert any("funding z" in reason for reason in candidate.reasons)


def test_s3_requires_extreme_funding(cfg, instrument):
    candles = _flat(80, spread=1.0, ratio=0.5)
    snap = make_snapshot(
        SnapshotParts(
            candles={"5m": candles, "1h": candles},
            binance_mid=100.0,
            derivatives=_deriv(funding_z=0.6, oi_pct_rank=0.92),
            basis=make_basis(binance_mid=100.0),
        ),
        instrument=instrument,
    )
    assert FundingCrowdingExhaustion(cfg).analyze(snap) is None


def test_s3_requires_a_crowded_book(cfg, instrument):
    candles = _flat(80, spread=1.0, ratio=0.5)
    snap = make_snapshot(
        SnapshotParts(
            candles={"5m": candles, "1h": candles},
            binance_mid=100.0,
            derivatives=_deriv(funding_z=2.6, oi_pct_rank=0.10),
            basis=make_basis(binance_mid=100.0),
        ),
        instrument=instrument,
    )
    assert FundingCrowdingExhaustion(cfg).analyze(snap) is None


# --------------------------------------------------------------------------- S4


def _trend_series(cfg) -> tuple:
    closes = [100.0] * 60 + [100.0 + 0.1 * i for i in range(1, 41)]
    trend = make_candles(closes, spread=0.4)
    return closes, trend


def test_s4_long_fires_on_an_oi_confirmed_uptrend(cfg, instrument):
    closes, trend = _trend_series(cfg)
    # The 15m path must genuinely touch EMA21 and close back above it; the 5m trigger
    # then sits just above the 4h fast EMA and within the anti-chase band.
    e_fast = ema_last(closes, 21)
    trigger = make_candles([e_fast] * 80, spread=0.4)
    pullback_prices = [e_fast + 0.2] * 56 + [e_fast - 0.1, e_fast + 0.05, e_fast + 0.15, e_fast + 0.25]
    pullback = make_candles(pullback_prices, spread=0.2, bar_ms=15 * 60_000)
    snap = make_snapshot(
        SnapshotParts(
            candles={"4h": trend, "1h": trend, "15m": pullback, "5m": trigger},
            binance_mid=100.0,
            derivatives=_deriv(oi_chg_pct=0.9, taker_buy_sell_ratio=0.58),
            basis=make_basis(binance_mid=100.0),
        ),
        instrument=instrument,
    )
    candidate = OIConfirmedTrendContinuation(cfg).analyze(snap)
    assert isinstance(candidate, StrategyCandidate)
    assert candidate.direction is Direction.LONG
    assert any("EMA" in reason for reason in candidate.reasons)


def test_s4_refuses_when_oi_is_missing(cfg, instrument):
    closes, trend = _trend_series(cfg)
    trigger = make_candles([100.0] * 80, spread=0.4)
    snap = make_snapshot(
        SnapshotParts(
            candles={"4h": trend, "5m": trigger},
            binance_mid=100.0,
            derivatives=_deriv(oi_chg_pct=None),
            basis=make_basis(binance_mid=100.0),
        ),
        instrument=instrument,
    )
    assert OIConfirmedTrendContinuation(cfg).analyze(snap) is None


def test_s4_anti_chase_guard_skips_an_extended_price(cfg, instrument):
    closes, trend = _trend_series(cfg)
    trigger = make_candles([105.0] * 80, spread=0.4)  # far above the EMA -> no chase
    snap = make_snapshot(
        SnapshotParts(
            candles={"4h": trend, "5m": trigger},
            binance_mid=105.0,
            derivatives=_deriv(oi_chg_pct=1.2, taker_buy_sell_ratio=0.6, mark_price=105.0),
            basis=make_basis(binance_mid=105.0),
        ),
        instrument=instrument,
    )
    assert OIConfirmedTrendContinuation(cfg).analyze(snap) is None


# --------------------------------------------------------------------------- S5


def _basis_snapshot(
    instrument,
    *,
    entry: float,
    binance_mid: float,
    z: float,
    observations: int = 200,
    net: float | None = None,
):
    basis = make_basis(
        binance_mid=binance_mid, coindcx_mid=entry, z=z, observations=observations, net=net
    )
    trigger = make_candles([entry] * 60, spread=0.4)
    return make_snapshot(
        SnapshotParts(
            candles={"1m": trigger, "5m": trigger},
            binance_mid=binance_mid,
            coindcx_mid=entry,
            basis=basis,
        ),
        instrument=instrument,
    )


def test_s5_short_fires_when_coindcx_is_richer_than_binance(cfg, instrument):
    # entry 102.5 vs Binance reference 100.0 -> convergence distance ~2.5 %, 1 % stop => >= 2R
    snap = _basis_snapshot(instrument, entry=102.5, binance_mid=100.0, z=2.5)
    candidate = CrossVenueBasisConvergence(cfg).analyze(snap)
    assert isinstance(candidate, StrategyCandidate)
    assert candidate.direction is Direction.SHORT
    # the limit entry must be a real CoinDCX tick, at or below the touchable best bid
    tick = instrument.price_increment
    assert abs(candidate.entry_price / tick - round(candidate.entry_price / tick)) < 1e-6
    assert candidate.entry_price <= snap.coindcx_book.best_bid
    assert candidate.entry_price >= snap.coindcx_book.best_bid - tick


def test_s5_hard_rule_treats_z_at_or_beyond_three_as_a_veto(cfg, instrument):
    snap = _basis_snapshot(instrument, entry=102.5, binance_mid=100.0, z=3.2)
    assert CrossVenueBasisConvergence(cfg).analyze(snap) is None


def test_s5_refuses_when_net_edge_is_not_positive(cfg, instrument):
    snap = _basis_snapshot(instrument, entry=102.5, binance_mid=100.0, z=2.5, net=-1.0)
    assert CrossVenueBasisConvergence(cfg).analyze(snap) is None


def test_s5_refuses_when_news_is_not_clear(cfg, instrument):
    snap = _basis_snapshot(instrument, entry=102.5, binance_mid=100.0, z=2.5)
    blocked = type(snap)(**{**snap.__dict__, "news_state": NewsState.DEGRADED})
    assert CrossVenueBasisConvergence(cfg).analyze(blocked) is None


# --------------------------------------------------------------------------- registry


def test_registry_builds_all_five_engines_and_isolates_failures(cfg, instrument):
    registry = StrategyRegistry(cfg)
    assert set(registry.ids()) == {"S1", "S2", "S3", "S4", "S5"}
    assert registry.errors == {}

    snap = make_snapshot(
        SnapshotParts(
            candles=flat_series(price=100.0), binance_mid=100.0, basis=make_basis(binance_mid=100.0)
        ),
        instrument=instrument,
    )
    candidates, failures = registry.run(snap)
    assert isinstance(candidates, tuple)
    assert failures == {}


def test_a_raising_engine_is_reported_and_never_emits(cfg, instrument, monkeypatch):
    registry = StrategyRegistry(cfg)

    def boom(self, snap):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(LiquiditySweepReclaim, "analyze", boom)
    snap = make_snapshot(
        SnapshotParts(
            candles=flat_series(price=100.0), binance_mid=100.0, basis=make_basis(binance_mid=100.0)
        ),
        instrument=instrument,
    )
    candidates, failures = registry.run(snap)
    assert "S1" in failures
    assert all(c.strategy_id != "S1" for c in candidates)
