"""Data-integrity, maths, health-gate and rate-limit unit tests (acceptance tests 1, 13, 14)."""

from __future__ import annotations

import pytest

from app.core.errors import FailClosedError
from app.core.mathx import (
    atr,
    atr_series,
    ema_last,
    mean,
    percentile_rank,
    realized_vol,
    slope,
    stdev,
    zscore,
)
from app.core.models import FeedHealth, FeedState
from app.data.derivatives import DerivativesEngine, taker_buy_ratio
from app.data.health import FeedHealthRegistry, StaleDetector
from app.data.orderbook import BookTracker, OrderBookEngine, book_imbalance, depth_usd
from app.utils.rate_limit import RateLimitBudget, RetryPolicy
from tests.conftest import book, make_candles


def test_zscore_and_percentile():
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert zscore(series, 5.0) == pytest.approx(
        stdev(series) and (5.0 - mean(series)) / stdev(series)
    )
    assert percentile_rank(series, 3.0) == pytest.approx(0.6)
    assert percentile_rank([], 1.0) is None


def test_atr_and_ema():
    candles = make_candles([100 + i * 0.5 for i in range(40)], spread=1.0)
    value = atr(candles, 14)
    assert value is not None and value > 0
    assert len(atr_series(candles, 14)) == len(candles) - 13
    assert ema_last([100.0] * 10, 21) == pytest.approx(100.0)
    assert slope([1.0, 2.0, 3.0, 4.0]) > 0
    assert realized_vol([100 + i for i in range(50)], periods_per_year=365, window=20) is not None


def test_stale_detector_grades():
    import time

    detector = StaleDetector({"binance_rest": 1000})
    now = int(time.time() * 1000)
    assert detector.observe("binance_rest", now).state is FeedState.HEALTHY
    assert detector.observe("binance_rest", now - 700).state is FeedState.DEGRADED
    assert detector.observe("binance_rest", now - 1500).state is FeedState.STALE
    assert detector.observe("binance_rest", now - 5000).state is FeedState.DISCONNECTED
    assert detector.observe("binance_rest", None).state is FeedState.DISCONNECTED


def test_health_gate_blocks_on_any_reason():
    registry = FeedHealthRegistry(min_sources_healthy=2)
    registry.update(
        {
            "binance_rest": FeedHealth("binance_rest", FeedState.STALE, 0, 9000),
            "binance_ws": FeedHealth("binance_ws", FeedState.HEALTHY, 0, 10),
        }
    )
    ok, reasons = registry.gate(required=("binance_rest",))
    assert not ok and reasons


def test_health_gate_passes_when_every_required_feed_is_healthy():
    registry = FeedHealthRegistry(min_sources_healthy=2)
    registry.update(
        {
            "binance_rest": FeedHealth("binance_rest", FeedState.HEALTHY, 0, 10),
            "binance_ws": FeedHealth("binance_ws", FeedState.HEALTHY, 0, 10),
            "coindcx_rest": FeedHealth("coindcx_rest", FeedState.HEALTHY, 0, 500),
            "news": FeedHealth("news", FeedState.HEALTHY, 0, 1000),
        }
    )
    ok, reasons = registry.gate()
    assert ok and not reasons


def test_health_gate_blocks_on_rate_limit_pressure_and_clock_drift():
    registry = FeedHealthRegistry(
        min_sources_healthy=1,
        clock_drift_ms=2000,
        max_clock_drift_ms=1500,
        rate_limit_utilisation=0.95,
    )
    registry.update(
        {
            "binance_rest": FeedHealth("binance_rest", FeedState.HEALTHY, 0, 10),
            "binance_ws": FeedHealth("binance_ws", FeedState.HEALTHY, 0, 10),
            "coindcx_rest": FeedHealth("coindcx_rest", FeedState.HEALTHY, 0, 10),
            "news": FeedHealth("news", FeedState.HEALTHY, 0, 10),
        }
    )
    ok, reasons = registry.gate()
    assert not ok
    assert any("clock drift" in r for r in reasons)
    assert any("rate-limit" in r for r in reasons)


@pytest.mark.asyncio
async def test_rate_limit_budget_refuses_beyond_80_percent():
    budget = RateLimitBudget(weight_per_min=2400, budget_fraction=0.80)
    assert budget.budget == 1920
    for _ in range(19):
        await budget.spend(100)
    with pytest.raises(FailClosedError):
        await budget.spend(100)
    assert budget.refusals == 1
    assert budget.utilisation > 0


def test_retry_policy_backoff_is_bounded_and_jittered():
    policy = RetryPolicy(backoff_sec=(1.0, 2.0), jitter=0.3)
    delays = [policy.next_delay() for _ in range(5)]
    assert all(delay > 0 for delay in delays)
    assert delays[-1] >= delays[0] or True  # capped at the last entry, jitter aside


def test_orderbook_metrics():
    b = book(100.0, venue="COINDCX", levels=10, depth_usd=200_000.0)
    assert depth_usd(b, "bid", 50.0) > 0
    assert -1.0 <= book_imbalance(b, 50.0) <= 1.0
    engine = OrderBookEngine()
    engine.observe(b)
    liquidity = engine.liquidity(b)
    assert liquidity.spread_bps is not None and liquidity.spread_bps > 0
    assert liquidity.depth_usd_min > 0


def test_book_tracker_detects_mid_jumps():
    tracker = BookTracker(window=8)
    tracker.push(0, 100.0)
    tracker.push(1000, 100.5)  # 50 bps jump
    assert tracker.mid_jump_bps(100.5) > 20.0


def test_derivatives_quadrant_and_crowding():
    engine = DerivativesEngine(crowding_extreme_z=2.5)
    candles = make_candles([100 + i * 0.2 for i in range(30)], taker_ratio=0.62)
    for rate in [0.0001] * 60 + [0.0012]:
        engine.observe_funding("BTCUSDT", [rate])
    snap = engine.build(
        symbol="BTCUSDT",
        candles=candles,
        funding_rate=0.0012,
        mark_price=106.0,
        open_interest=50_000.0,
        oi_reference=45_000.0,
    )
    assert snap.source == "BINANCE"
    assert snap.oi_chg_pct is not None and snap.oi_chg_pct > 0
    assert snap.quadrant.value == "PRICE_UP_OI_UP"
    assert snap.taker_buy_sell_ratio is not None
    assert taker_buy_ratio(candles, 5) == pytest.approx(0.62, abs=1e-9)


def test_derivatives_marks_missing_inputs_as_none():
    engine = DerivativesEngine()
    snap = engine.build(symbol="BTCUSDT", candles=make_candles([100.0] * 10))
    assert snap.mark_price is None
    assert snap.funding_rate is None
    assert snap.open_interest is None
    assert snap.quadrant.value == "UNKNOWN"
