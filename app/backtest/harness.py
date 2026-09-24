"""Backtest harness: builds synthetic-but-deterministic snapshots for offline runs.

IMPORTANT HONESTY NOTE: this harness runs the REAL strategy/veto/consensus/signal
pipeline against a synthetic price generator. It is a plumbing/robustness harness, not
evidence of profitability. Real acceptance evidence requires real historical klines
(scripts/run_backtest.py --csv ...) and is reported as NOT RUN until that data exists.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.config import AppConfig
from app.core.models import (
    BasisSnapshot,
    BookLevel,
    BtcRegime,
    Candle,
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
from app.core.timeutils import now_ms


def synthetic_candles(
    count: int = 1500, *, seed: int = 3, start: float = 86_000.0, bar_ms: int = 5 * 60 * 1000
) -> list[Candle]:
    rng = random.Random(seed)
    out: list[Candle] = []
    price = start
    for i in range(count):
        drift = math.sin(i / 70.0) * 0.0008
        shock = rng.gauss(0, 0.0022)
        price = max(100.0, price * (1 + drift + shock))
        high = price * (1 + abs(rng.gauss(0, 0.0012)))
        low = price * (1 - abs(rng.gauss(0, 0.0012)))
        vol = abs(rng.gauss(60_000, 15_000))
        out.append(
            Candle(
                open_time_ms=i * bar_ms,
                open=price * (1 - drift / 2),
                high=high,
                low=low,
                close=price,
                volume=vol,
                taker_buy_quote=vol * min(0.9, max(0.1, 0.5 + rng.gauss(0, 0.06))),
            )
        )
    return out


def book_from_price(
    price: float,
    *,
    venue: str,
    symbol: str,
    ts_ms: int,
    levels: int = 40,
    depth_usd: float = 900_000.0,
) -> OrderBook:
    qty = max(0.5, depth_usd / (levels * price))
    bids = [BookLevel(price * (1 - 0.00005 * (i + 1)), qty) for i in range(levels)]
    asks = [BookLevel(price * (1 + 0.00005 * (i + 1)), qty) for i in range(levels)]
    return OrderBook(venue=venue, symbol=symbol, ts_ms=ts_ms, bids=bids, asks=asks)


def aggregate_candles(
    candles: Sequence[Candle], timeframe_minutes: int, *, source_bar_minutes: float = 1.0
) -> list[Candle]:
    """Aggregate a closed base series into fully completed OHLCV buckets only."""
    if timeframe_minutes <= 0:
        raise ValueError("timeframe_minutes must be positive")
    bucket_ms = timeframe_minutes * 60_000
    source_ms = max(1, int(round(float(source_bar_minutes) * 60_000)))
    ordered = sorted((c for c in candles if c.is_closed), key=lambda c: c.open_time_ms)
    out: list[Candle] = []
    current_bucket = None
    acc: list[Candle] = []
    for candle in ordered:
        bucket = (candle.open_time_ms // bucket_ms) * bucket_ms
        if current_bucket is None:
            current_bucket = bucket
        if bucket != current_bucket:
            expected_end = current_bucket + bucket_ms
            actual_end = (acc[-1].close_time_ms or (acc[-1].open_time_ms + source_ms - 1)) if acc else 0
            # A bucket is only valid when the final source candle closes at/after its bucket end.
            if acc and actual_end >= expected_end - 1:
                out.append(_aggregate_bucket(current_bucket, acc, expected_end - 1))
            acc = []
            current_bucket = bucket
        acc.append(candle)
    if acc and current_bucket is not None:
        expected_end = current_bucket + bucket_ms
        actual_end = acc[-1].close_time_ms or (acc[-1].open_time_ms + source_ms - 1)
        if actual_end >= expected_end - 1:
            out.append(_aggregate_bucket(current_bucket, acc, expected_end - 1))
    return out


def _aggregate_bucket(bucket_ms: int, candles: Sequence[Candle], close_time_ms: int) -> Candle:
    first, last = candles[0], candles[-1]
    taker = None
    if all(c.taker_buy_quote is not None for c in candles):
        taker = sum(float(c.taker_buy_quote) for c in candles)
    return Candle(
        open_time_ms=bucket_ms,
        open=first.open,
        high=max(c.high for c in candles),
        low=min(c.low for c in candles),
        close=last.close,
        volume=sum(c.volume for c in candles),
        taker_buy_quote=taker,
        close_time_ms=close_time_ms,
        is_closed=True,
    )


def _prefix_until(candles: Sequence[Candle], ts_ms: int) -> list[Candle]:
    return [c for c in candles if (c.close_time_ms or c.open_time_ms) <= ts_ms]


@dataclass
class SyntheticSnapshotFactory:
    """Produces an immutable `MarketSnapshot` for bar `index` - and ONLY bars <= index.

    This slicing is what prevents look-ahead bias in the harness: the snapshot can never
    observe a future candle even if the caller passes the full series.
    """

    cfg: AppConfig
    series: Sequence[Candle]
    instrument: InstrumentSpec
    symbol: str = "B-BTC_USDT"
    binance_symbol: str = "BTCUSDT"
    bar_minutes: float = 1.0
    ts_ms: int = field(default_factory=now_ms)

    def __call__(self, symbol: str, index: int) -> MarketSnapshot | None:
        if index < 60 or index >= len(self.series):
            return None
        history = [c for c in self.series[: index + 1] if c.is_closed]
        if not history:
            return None
        last = history[-1]
        ts = last.close_time_ms or (last.open_time_ms + int(self.bar_minutes * 60_000) - 1)
        binance_mid = last.close
        basis_bps = 0.0
        if len(history) % 37 == 0:
            basis_bps = 6.0  # occasional rich basis so S5 exercises
        coindcx_mid = binance_mid * (1 + basis_bps / 1e4)
        coindcx_book = book_from_price(coindcx_mid, venue="COINDCX", symbol=self.symbol, ts_ms=ts)
        binance_book = book_from_price(
            binance_mid, venue="BINANCE", symbol=self.binance_symbol, ts_ms=ts
        )
        # The snapshot sees only completed higher-TF buckets that existed at the current bar.
        tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
        candles: dict[str, Sequence[Candle]] = {}
        for tf, minutes in tf_minutes.items():
            if minutes < self.bar_minutes:
                candles[tf] = ()  # unavailable resolution; no synthetic downsampling is invented
            elif minutes == self.bar_minutes:
                candles[tf] = tuple(history)
            else:
                candles[tf] = tuple(
                aggregate_candles(
                    self.series[: index + 1], minutes, source_bar_minutes=self.bar_minutes
                )
            )
        oi = 40_000 + (index % 50) * 120
        oi_ref = 40_000 + ((index - 5) % 50) * 120
        funding = 0.0001 * math.sin(index / 11.0)
        funding_z = 2.6 * math.sin(index / 11.0)
        derivatives = DerivativesSnapshot(
            symbol=self.binance_symbol,
            ts_ms=ts,
            source="BINANCE",
            mark_price=binance_mid,
            index_price=binance_mid,
            funding_rate=funding,
            funding_z=funding_z,
            open_interest=oi,
            oi_chg_pct=(oi - oi_ref) / oi_ref * 100.0,
            oi_pct_rank=min(0.999, max(0.0, (index % 100) / 100.0)),
            taker_buy_sell_ratio=last.taker_buy_ratio,
            price_chg_pct=(history[-1].close - history[-2].close) / history[-2].close * 100.0,
            quadrant=OIQuadrant.PRICE_UP_OI_UP,
            crowding=__import__("app.core.models", fromlist=["Crowding"]).Crowding.NEUTRAL,
            oi_ts_ms=ts,
            funding_ts_ms=ts,
            mark_ts_ms=ts,
            index_ts_ms=ts,
        )
        liquidity = LiquiditySnapshot(
            spread_bps=1.4,
            depth_bid_usd=800_000,
            depth_ask_usd=780_000,
            imbalance=0.02,
            mid_jump_bps=1.0,
            expected_slippage_bps=2.0,
        )
        z = 2.4 if basis_bps else 0.2
        recent_basis = []
        # Deterministic basis path used only by the plumbing harness; real backtests should
        # supply measured venue basis observations.
        for j in range(max(60, index - 300), index + 1):
            recent_basis.append(6.0 if j % 37 == 0 else 0.0)
        converging = sum(1 for a, b in zip(recent_basis, recent_basis[1:]) if abs(b) < abs(a))
        conv_prob = converging / max(1, len(recent_basis) - 1)
        expected_capture = max(0.0, abs(basis_bps) - 2.6 - 2.0) * conv_prob
        basis = BasisSnapshot(
            binance_usd_mid=binance_mid,
            coindcx_usd_mid=coindcx_mid,
            basis_bps=basis_bps,
            net_basis_bps=basis_bps - 2.6,
            z=z,
            percentile=0.98,
            vol_adjusted=1.1,
            effective_cost_bps=2.6,
            observations=200,
            drift_ms=12,
            classification=DivergenceClass.ABNORMAL if abs(z) >= 2 else DivergenceClass.NORMAL,
            raw_coindcx_mid=coindcx_mid,
            quote="USDT",
            convergence_probability=conv_prob,
            expected_capture_bps=expected_capture,
        )
        feed_health = {
            "binance_rest": FeedHealth("binance_rest", FeedState.HEALTHY, ts, 50),
            "binance_ws": FeedHealth("binance_ws", FeedState.HEALTHY, ts, 40),
            "coindcx_rest": FeedHealth("coindcx_rest", FeedState.HEALTHY, ts, 900),
            "news": FeedHealth("news", FeedState.HEALTHY, ts, 30_000, "healthy_sources=4"),
        }
        return MarketSnapshot(
            symbol=symbol,
            ts_ms=ts,
            binance_book=binance_book,
            coindcx_book=coindcx_book,
            candles=candles,
            derivatives=derivatives,
            liquidity=liquidity,
            basis=basis,
            instrument=self.instrument,
            feed_health=feed_health,
            clock_drift_ms=12,
            news_state=NewsState.CLEAR,
            btc_regime=BtcRegime.NEUTRAL,
            btc_conflict=False,
            book_mid_jump_bps=1.0,
        )


@dataclass
class MarketDataFeed:
    """Deterministic offline feed (used by the paper/soak harness in tests)."""

    series: Sequence[Candle]
    index: int = 0

    def next_bar(self) -> Candle | None:
        if self.index >= len(self.series):
            return None
        bar = self.series[self.index]
        self.index += 1
        return bar


__all__ = ["MarketDataFeed", "SyntheticSnapshotFactory", "aggregate_candles", "book_from_price", "synthetic_candles"]
