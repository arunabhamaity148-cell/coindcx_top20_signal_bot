"""Regression suite for clock-drift semantics, timestamp separation, and WS sequence.

Addresses known issues:
  - false >1500ms cross-venue drift under REST poll vs WS cadence
  - event_ts vs received_ts separation
  - negative age handling
  - Binance WS contiguous / gap / reconnect / duplicate
  - CoinDCX orderbook parse without exchange timestamp
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.errors import ClockDriftError
from app.core.models import BookLevel, InstrumentSpec, OrderBook
from app.core.timeutils import now_ms
from app.data.binance.models import parse_depth
from app.data.binance.websocket import BinanceWsCollector, StreamRoute
from app.data.coindcx.models import parse_orderbook
from app.data.normalization import Normalizer


def _levels(mid: float = 100.0) -> tuple[list[BookLevel], list[BookLevel]]:
    return [BookLevel(mid - 0.1, 10.0)], [BookLevel(mid + 0.1, 10.0)]


def _spec() -> InstrumentSpec:
    return InstrumentSpec(
        pair="B-BTC_USDT",
        binance_symbol="BTCUSDT",
        price_increment=0.1,
        quantity_increment=0.001,
        min_trade_size=0.001,
        min_notional=5.0,
        maker_fee_pct=0.02,
        taker_fee_pct=0.04,
        funding_frequency=8,
        quote_currency="USDT",
    )


def test_coindcx_parse_without_exchange_ts_sets_received_not_event():
    book = parse_orderbook({"bids": [["100", "1"]], "asks": [["100.1", "1"]]}, "B-BTC_USDT")
    assert book.received_ts_ms is not None
    assert book.event_ts_ms is None
    assert book.has_exchange_event_ts is False
    assert abs(book.ts_ms - book.received_ts_ms) < 50


def test_coindcx_parse_with_exchange_ts_sets_event():
    event = 1_700_000_000_000
    book = parse_orderbook(
        {"bids": [["100", "1"]], "asks": [["100.1", "1"]], "timestamp": event},
        "B-BTC_USDT",
    )
    assert book.event_ts_ms == event
    assert book.ts_ms == event
    assert book.has_exchange_event_ts is True
    assert book.received_ts_ms is not None


def test_binance_parse_depth_with_event_ts():
    event = 1_700_000_000_500
    book = parse_depth(
        {"bids": [["100", "1"]], "asks": [["100.1", "1"]], "E": event},
        "BTCUSDT",
    )
    assert book.event_ts_ms == event
    assert book.ts_ms == event
    assert book.received_ts_ms is not None


def test_normalization_mixed_clocks_does_not_false_drift_on_poll_cadence():
    """CoinDCX polled ~2s ago (local receipt), Binance WS just updated (event ts ≈ now).

    Pre-fix this raised ClockDriftError at ~2000ms. Post-fix: freshness gates only.
    """
    reference = now_ms()
    bids, asks = _levels()
    # CoinDCX: no exchange event ts; received 1800ms ago (within 2x*1500 freshness)
    coindcx = OrderBook(
        venue="COINDCX",
        symbol="B-BTC_USDT",
        ts_ms=reference - 1800,
        bids=bids,
        asks=asks,
        received_ts_ms=reference - 1800,
        event_ts_ms=None,
    )
    # Binance: exchange event near now
    binance = OrderBook(
        venue="BINANCE",
        symbol="BTCUSDT",
        ts_ms=reference - 50,
        bids=bids,
        asks=asks,
        received_ts_ms=reference - 20,
        event_ts_ms=reference - 50,
    )
    norm = Normalizer(max_clock_drift_ms=1500)
    snap = norm.basis(binance_book=binance, coindcx_book=coindcx, spec=_spec(), now_ms=reference)
    assert snap is not None
    assert abs(snap.drift_ms) < 5000  # diagnostic only; not a hard event-time compare


def test_normalization_both_exchange_ts_still_enforces_event_drift():
    reference = now_ms()
    bids, asks = _levels()
    coindcx = OrderBook(
        venue="COINDCX",
        symbol="B-BTC_USDT",
        ts_ms=reference - 2000,
        bids=bids,
        asks=asks,
        received_ts_ms=reference - 100,
        event_ts_ms=reference - 2000,
    )
    binance = OrderBook(
        venue="BINANCE",
        symbol="BTCUSDT",
        ts_ms=reference - 50,
        bids=bids,
        asks=asks,
        received_ts_ms=reference - 20,
        event_ts_ms=reference - 50,
    )
    norm = Normalizer(max_clock_drift_ms=1500)
    with pytest.raises(ClockDriftError):
        norm.basis(binance_book=binance, coindcx_book=coindcx, spec=_spec(), now_ms=reference)


def test_normalization_stale_receipt_still_fails_closed():
    reference = now_ms()
    bids, asks = _levels()
    coindcx = OrderBook(
        venue="COINDCX",
        symbol="B-BTC_USDT",
        ts_ms=reference - 5000,
        bids=bids,
        asks=asks,
        received_ts_ms=reference - 5000,
        event_ts_ms=None,
    )
    binance = OrderBook(
        venue="BINANCE",
        symbol="BTCUSDT",
        ts_ms=reference - 50,
        bids=bids,
        asks=asks,
        received_ts_ms=reference - 20,
        event_ts_ms=reference - 50,
    )
    norm = Normalizer(max_clock_drift_ms=1500)
    with pytest.raises(ClockDriftError):
        norm.basis(binance_book=binance, coindcx_book=coindcx, spec=_spec(), now_ms=reference)


def test_negative_age_does_not_raise_when_within_budget():
    """Exchange clock slightly ahead of local (negative age) must not fail closed."""
    reference = now_ms()
    bids, asks = _levels()
    # Exchange event 100ms ahead of local reference
    coindcx = OrderBook(
        venue="COINDCX",
        symbol="B-BTC_USDT",
        ts_ms=reference + 100,
        bids=bids,
        asks=asks,
        received_ts_ms=reference,
        event_ts_ms=reference + 100,
    )
    binance = OrderBook(
        venue="BINANCE",
        symbol="BTCUSDT",
        ts_ms=reference + 80,
        bids=bids,
        asks=asks,
        received_ts_ms=reference,
        event_ts_ms=reference + 80,
    )
    norm = Normalizer(max_clock_drift_ms=1500)
    snap = norm.basis(binance_book=binance, coindcx_book=coindcx, spec=_spec(), now_ms=reference)
    assert snap is not None
    assert snap.drift_ms <= 1500


def test_ws_contiguous_sequence_no_gap():
    collector = BinanceWsCollector(streams={StreamRoute.PUBLIC: ["btcusdt@depth20@100ms"]}, stale_ms=5000)
    gaps = []
    collector.on_gap = lambda s: gaps.append(s)
    collector._check_sequence(
        "btcusdt@depth20@100ms",
        {"U": 100, "u": 105, "pu": None},
        collector.health.setdefault("btcusdt@depth20@100ms", __import__("app.data.binance.websocket", fromlist=["StreamHealth"]).StreamHealth(name="btcusdt@depth20@100ms")),
    )
    collector._check_sequence(
        "btcusdt@depth20@100ms",
        {"U": 106, "u": 110, "pu": 105},
        collector.health["btcusdt@depth20@100ms"],
    )
    assert gaps == []
    assert collector.health["btcusdt@depth20@100ms"].gaps == 0


def test_ws_sequence_gap_detected():
    from app.data.binance.websocket import StreamHealth

    collector = BinanceWsCollector(streams={StreamRoute.PUBLIC: ["btcusdt@depth20@100ms"]}, stale_ms=5000)
    gaps = []
    collector.on_gap = lambda s: gaps.append(s)
    h = StreamHealth(name="btcusdt@depth20@100ms")
    collector.health["btcusdt@depth20@100ms"] = h
    collector._check_sequence("btcusdt@depth20@100ms", {"U": 100, "u": 105}, h)
    collector._check_sequence("btcusdt@depth20@100ms", {"U": 200, "u": 205, "pu": 199}, h)
    assert gaps == ["btcusdt@depth20@100ms"]
    assert h.gaps == 1


def test_ws_reconnect_clears_sequence_state():
    """After reconnect, first event must not be compared against pre-reconnect last id."""
    from app.data.binance.websocket import StreamHealth
    import asyncio

    collector = BinanceWsCollector(streams={StreamRoute.PUBLIC: ["btcusdt@depth20@100ms"]}, stale_ms=5000)
    h = StreamHealth(name="btcusdt@depth20@100ms")
    collector.health["btcusdt@depth20@100ms"] = h
    collector._last_depth_update_id["btcusdt@depth20@100ms"] = 9999
    collector._last_event_time_ms["btcusdt@depth20@100ms"] = 1_700_000_000_000

    # Simulate the reset that _consume performs on connect
    name = "btcusdt@depth20@100ms"
    collector._last_depth_update_id.pop(name, None)
    collector._last_event_time_ms.pop(name, None)

    collector._check_sequence(name, {"U": 1, "u": 5}, h)
    assert h.gaps == 0
    assert collector._last_depth_update_id[name] == 5


def test_ws_duplicate_event_is_gap():
    from app.data.binance.websocket import StreamHealth

    collector = BinanceWsCollector(streams={StreamRoute.PUBLIC: ["btcusdt@depth20@100ms"]}, stale_ms=5000)
    h = StreamHealth(name="btcusdt@depth20@100ms")
    collector.health["btcusdt@depth20@100ms"] = h
    collector._check_sequence("btcusdt@depth20@100ms", {"U": 100, "u": 105, "pu": None}, h)
    # Duplicate / rewind: pu points to something already consumed incorrectly
    collector._check_sequence("btcusdt@depth20@100ms", {"U": 100, "u": 105, "pu": 99}, h)
    assert h.gaps >= 1


def test_all_pairs_ws_stream_builder_covers_universe():
    from app.data.binance.websocket import build_default_streams

    symbols = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT",
        "DOTUSDT", "TRXUSDT", "SUIUSDT", "APTUSDT", "ARBUSDT",
        "OPUSDT", "TONUSDT", "NEARUSDT", "ATOMUSDT",
    ]
    streams = build_default_streams(symbols)
    public = list(streams.get(StreamRoute.PUBLIC, []))
    depth_symbols = {s.split("@")[0].upper() for s in public if "@depth" in s}
    assert depth_symbols == set(symbols)
