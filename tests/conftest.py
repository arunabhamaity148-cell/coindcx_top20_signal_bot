"""Shared test fixtures.

All fixtures are deterministic: no network calls, no live exchange or Telegram access.
Snapshots are built in-memory so the tests exercise the REAL strategy/veto/consensus/
signal code paths against controlled inputs.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
# Ensure no trading credential is ever present during tests.
for _name in (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "COINDCX_API_KEY",
    "COINDCX_API_SECRET",
    "BINANCE_FUTURES_API_KEY",
    "BINANCE_FUTURES_API_SECRET",
):
    os.environ.pop(_name, None)

from app.config import AppConfig, load_config
from app.core.models import (
    BasisSnapshot,
    BookLevel,
    BtcRegime,
    Candle,
    Crowding,
    DerivativesSnapshot,
    DivergenceClass,
    FeedHealth,
    FeedState,
    InstrumentSpec,
    LiquiditySnapshot,
    MarketSnapshot,
    NewsState,
    OIQuadrant,
    OrderBook,
)

TICK = 0.01
BINANCE_SYMBOL = "BTCUSDT"
PAIR = "B-BTC_USDT"
TS_MS = 1_700_000_000_000


@pytest.fixture(scope="session")
def cfg() -> AppConfig:
    return load_config(ROOT / "config")


@pytest.fixture
def instrument() -> InstrumentSpec:
    """Mirrors the VERIFIED CoinDCX /instrument fields (2026-09-22)."""
    return InstrumentSpec(
        pair=PAIR,
        binance_symbol=BINANCE_SYMBOL,
        price_increment=TICK,
        quantity_increment=1.0,
        min_trade_size=1.0,
        min_notional=6.0,
        maker_fee_pct=0.0236,
        taker_fee_pct=0.059,
        funding_frequency=4,
    )


def make_candles(
    closes,
    *,
    spread: float = 0.4,
    taker_ratio: float | None = 0.5,
    start_ms: int = TS_MS,
    bar_ms: int = 60_000,
) -> list[Candle]:
    out: list[Candle] = []
    for index, close in enumerate(closes):
        open_price = closes[index - 1] if index else close
        high = max(open_price, close) + spread / 2
        low = min(open_price, close) - spread / 2
        volume = 10_000.0
        out.append(
            Candle(
                open_time_ms=start_ms + index * bar_ms,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                taker_buy_quote=None if taker_ratio is None else volume * taker_ratio,
            )
        )
    return out


def book(
    price: float, *, venue: str, levels: int = 25, depth_usd: float = 500_000.0, ts_ms: int = TS_MS
) -> OrderBook:
    qty = max(0.5, depth_usd / (levels * price))
    return OrderBook(
        venue=venue,
        symbol=PAIR if venue == "COINDCX" else BINANCE_SYMBOL,
        ts_ms=ts_ms,
        bids=[BookLevel(price * (1 - 0.00005 * (i + 1)), qty) for i in range(levels)],
        asks=[BookLevel(price * (1 + 0.00005 * (i + 1)), qty) for i in range(levels)],
    )


def healthy_feeds(ts_ms: int = TS_MS) -> dict[str, FeedHealth]:
    return {
        "binance_rest": FeedHealth("binance_rest", FeedState.HEALTHY, ts_ms, 50),
        "binance_ws": FeedHealth("binance_ws", FeedState.HEALTHY, ts_ms, 40),
        "coindcx_rest": FeedHealth("coindcx_rest", FeedState.HEALTHY, ts_ms, 800),
        "news": FeedHealth("news", FeedState.HEALTHY, ts_ms, 30_000, "healthy_sources=4"),
    }


def make_basis(
    *,
    binance_mid: float,
    coindcx_mid: float | None = None,
    z: float | None = 0.2,
    observations: int = 200,
    net: float | None = None,
    classification: DivergenceClass | None = None,
) -> BasisSnapshot:
    coindcx_mid = binance_mid if coindcx_mid is None else coindcx_mid
    basis_bps = (coindcx_mid - binance_mid) / binance_mid * 1e4
    net_basis = basis_bps - 2.6 if net is None else net
    if classification is None:
        if observations < 60 or z is None:
            classification = DivergenceClass.ABNORMAL
        elif abs(z) >= 3.0:
            classification = DivergenceClass.EXTREME
        elif abs(z) >= 2.0:
            classification = DivergenceClass.ABNORMAL
        elif abs(z) >= 1.0:
            classification = DivergenceClass.ELEVATED
        else:
            classification = DivergenceClass.NORMAL
    return BasisSnapshot(
        binance_usd_mid=binance_mid,
        coindcx_usd_mid=coindcx_mid,
        basis_bps=basis_bps,
        net_basis_bps=net_basis,
        z=z,
        percentile=0.5,
        vol_adjusted=None,
        effective_cost_bps=2.6,
        observations=observations,
        drift_ms=10,
        classification=classification,
        raw_coindcx_mid=coindcx_mid,
        quote="USDT",
        # Positive S5 fixtures explicitly provide observed convergence evidence;
        # production code never assumes this field when it is missing.
        convergence_probability=0.80,
        expected_capture_bps=max(0.0, abs(basis_bps) - 4.6) * 0.80,
    )


@dataclass
class SnapshotParts:
    """Container so tests can override individual snapshot fields cleanly."""

    candles: dict
    binance_mid: float
    coindcx_mid: float | None = None
    derivatives: DerivativesSnapshot | None = None
    liquidity: LiquiditySnapshot | None = None
    basis: BasisSnapshot | None = None
    feeds: dict | None = None
    news_state: NewsState = NewsState.CLEAR
    btc_regime: BtcRegime = BtcRegime.NEUTRAL
    btc_conflict: bool = False
    book_mid_jump_bps: float = 1.0
    clock_drift_ms: int = 10


def make_snapshot(
    parts: SnapshotParts, *, instrument: InstrumentSpec, symbol: str = PAIR, ts_ms: int = TS_MS
) -> MarketSnapshot:
    coindcx_mid = parts.coindcx_mid if parts.coindcx_mid is not None else parts.binance_mid
    deriv = parts.derivatives or DerivativesSnapshot(
        symbol=BINANCE_SYMBOL,
        ts_ms=ts_ms,
        source="BINANCE",
        mark_price=parts.binance_mid,
        funding_rate=0.0001,
        funding_z=0.4,
        open_interest=40_000.0,
        oi_chg_pct=0.2,
        oi_pct_rank=0.5,
        taker_buy_sell_ratio=0.5,
        price_chg_pct=0.1,
        quadrant=OIQuadrant.PRICE_UP_OI_UP,
        crowding=Crowding.NEUTRAL,
    )
    liq = parts.liquidity or LiquiditySnapshot(
        spread_bps=1.5,
        depth_bid_usd=800_000.0,
        depth_ask_usd=780_000.0,
        imbalance=0.02,
        mid_jump_bps=1.0,
        expected_slippage_bps=2.0,
    )
    return MarketSnapshot(
        symbol=symbol,
        ts_ms=ts_ms,
        binance_book=book(parts.binance_mid, venue="BINANCE", ts_ms=ts_ms),
        coindcx_book=book(coindcx_mid, venue="COINDCX", ts_ms=ts_ms),
        candles=parts.candles,
        derivatives=deriv,
        liquidity=liq,
        basis=parts.basis,
        instrument=instrument,
        feed_health=parts.feeds or healthy_feeds(ts_ms),
        clock_drift_ms=parts.clock_drift_ms,
        news_state=parts.news_state,
        btc_regime=parts.btc_regime,
        btc_conflict=parts.btc_conflict,
        book_mid_jump_bps=parts.book_mid_jump_bps,
    )


def candle(
    *,
    open: float,
    high: float,
    low: float,
    close: float,
    taker_ratio: float | None = None,
    volume: float = 10_000.0,
    open_time_ms: int = TS_MS,
) -> Candle:
    """An explicit OHLC candle - tests must never guess the geometry."""
    return Candle(
        open_time_ms=open_time_ms,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        taker_buy_quote=None if taker_ratio is None else volume * taker_ratio,
    )


def flat_series(
    price: float = 100.0, count: int = 200, *, spread: float = 0.4, taker_ratio: float | None = 0.5
) -> dict:
    closes = [price] * count
    return {
        tf: make_candles(closes, spread=spread, taker_ratio=taker_ratio)
        for tf in ("1m", "5m", "15m", "1h", "4h")
    }
