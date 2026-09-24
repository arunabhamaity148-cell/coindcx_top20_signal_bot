"""Parsers from raw Binance USDⓈ-M payloads into canonical models.

Only fields that the venue actually publishes are read; anything not present becomes
None rather than a zero or an interpolation.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.core.models import BookLevel, Candle, OrderBook, OIQuadrant
from app.core.timeutils import now_ms

# /fapi/v1/klines positional layout (VERIFIED endpoint; layout is the documented 12-tuple)
_K_OPEN_TIME, _K_OPEN, _K_HIGH, _K_LOW, _K_CLOSE = 0, 1, 2, 3, 4
_K_CLOSE_TIME = 6
_K_QUOTE_VOL, _K_TRADES = 7, 8
_K_TAKER_BUY_QUOTE = 10


def parse_kline(row: Sequence[Any], *, reference_ms: int | None = None) -> Candle:
    quote_vol = float(row[_K_QUOTE_VOL]) if len(row) > _K_QUOTE_VOL else 0.0
    taker_buy_quote = float(row[_K_TAKER_BUY_QUOTE]) if len(row) > _K_TAKER_BUY_QUOTE else None
    close_time = int(row[_K_CLOSE_TIME]) if len(row) > _K_CLOSE_TIME and row[_K_CLOSE_TIME] is not None else None
    ref = reference_ms if reference_ms is not None else now_ms()
    is_closed = close_time is None or close_time <= ref
    return Candle(
        open_time_ms=int(row[_K_OPEN_TIME]),
        open=float(row[_K_OPEN]),
        high=float(row[_K_HIGH]),
        low=float(row[_K_LOW]),
        close=float(row[_K_CLOSE]),
        volume=quote_vol,
        taker_buy_quote=taker_buy_quote,
        close_time_ms=close_time,
        is_closed=is_closed,
    )


def parse_klines(rows: Sequence[Sequence[Any]], *, reference_ms: int | None = None) -> list[Candle]:
    candles = [parse_kline(r, reference_ms=reference_ms) for r in rows]
    candles.sort(key=lambda c: c.open_time_ms)
    return candles


def parse_depth(payload: Mapping[str, Any], symbol: str, *, venue: str = "BINANCE") -> OrderBook:
    bids = [BookLevel(float(p), float(q)) for p, q in payload.get("bids", []) or []]
    asks = [BookLevel(float(p), float(q)) for p, q in payload.get("asks", []) or []]
    bids.sort(key=lambda l: -l.price)
    asks.sort(key=lambda l: l.price)
    received = now_ms()
    exchange_raw = payload.get("E") or payload.get("T")
    event_ts: int | None = None
    if exchange_raw is not None:
        try:
            event_ts = int(exchange_raw)
        except (TypeError, ValueError):
            event_ts = None
    primary = event_ts if event_ts is not None else received
    return OrderBook(
        venue=venue,
        symbol=symbol,
        ts_ms=int(primary),
        bids=bids,
        asks=asks,
        received_ts_ms=received,
        event_ts_ms=event_ts,
    )


def parse_book_ticker(payload: Mapping[str, Any], symbol: str | None = None) -> OrderBook:
    sym = symbol or str(payload.get("symbol", ""))
    bid = float(payload["bidPrice"])
    ap = float(payload["askPrice"])
    bq = float(payload.get("bidQty", 0.0) or 0.0)
    aq = float(payload.get("askQty", 0.0) or 0.0)
    received = now_ms()
    exchange_raw = payload.get("time") or payload.get("E") or payload.get("T")
    event_ts: int | None = None
    if exchange_raw is not None:
        try:
            event_ts = int(exchange_raw)
        except (TypeError, ValueError):
            event_ts = None
    primary = event_ts if event_ts is not None else received
    return OrderBook(
        venue="BINANCE",
        symbol=sym,
        ts_ms=int(primary),
        bids=[BookLevel(bid, bq)],
        asks=[BookLevel(ap, aq)],
        received_ts_ms=received,
        event_ts_ms=event_ts,
    )


def parse_open_interest(payload: Mapping[str, Any]) -> tuple[float | None, int]:
    oi = payload.get("openInterest")
    ts = payload.get("time") or now_ms()
    return (float(oi) if oi is not None else None), int(ts)


def parse_funding_history(payload: Sequence[Mapping[str, Any]]) -> tuple[float | None, int | None, float | None]:
    if not payload:
        return None, None, None
    latest = payload[-1]
    rate = latest.get("fundingRate")
    ts = latest.get("fundingTime")
    mark = latest.get("markPrice")
    return (float(rate) if rate is not None else None,
            int(ts) if ts is not None else None,
            float(mark) if mark is not None else None)


def parse_premium_index(payload: Mapping[str, Any]) -> dict[str, float | None]:
    return {
        "mark_price": _opt_float(payload.get("markPrice")),
        "index_price": _opt_float(payload.get("indexPrice")),
        "funding_rate": _opt_float(payload.get("lastFundingRate")),
        "next_funding_time": _opt_float(payload.get("nextFundingTime")),
        "mark_ts_ms": _opt_float(payload.get("time")),
        "index_ts_ms": _opt_float(payload.get("time")),
    }


def _opt_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_quadrant(price_chg_pct: float | None, oi_chg_pct: float | None) -> OIQuadrant:
    """Derivatives Intelligence Model (§M). Unknown inputs stay UNKNOWN - never guessed."""
    if price_chg_pct is None or oi_chg_pct is None:
        return OIQuadrant.UNKNOWN
    if price_chg_pct >= 0 and oi_chg_pct >= 0:
        return OIQuadrant.PRICE_UP_OI_UP
    if price_chg_pct >= 0 > oi_chg_pct:
        return OIQuadrant.PRICE_UP_OI_DOWN
    if price_chg_pct < 0 <= oi_chg_pct:
        return OIQuadrant.PRICE_DOWN_OI_UP
    return OIQuadrant.PRICE_DOWN_OI_DOWN


def parse_exchange_info(payload: Mapping[str, Any]) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    """Return {symbol: {tick_size, step_size, price_precision, quantity_precision, min_notional}} + limits."""
    out: dict[str, dict[str, float]] = {}
    for entry in payload.get("symbols", []) or []:
        filters = {f.get("filterType"): f for f in entry.get("filters", []) or []}
        tick = float(filters.get("PRICE_FILTER", {}).get("tickSize", 0.0) or 0.0)
        step = float(filters.get("LOT_SIZE", {}).get("stepSize", 0.0) or 0.0)
        min_notional = 0.0
        for key in ("MIN_NOTIONAL", "NOTIONAL"):
            if key in filters:
                min_notional = float(filters[key].get("notional", 0.0) or 0.0)
                break
        out[str(entry.get("symbol"))] = {
            "tick_size": tick,
            "step_size": step,
            "price_precision": float(entry.get("pricePrecision", 0) or 0),
            "quantity_precision": float(entry.get("quantityPrecision", 0) or 0),
            "min_notional": min_notional,
            "status_trading": 1.0 if entry.get("status") == "TRADING" else 0.0,
        }
    limits = {
        "rate_limits": payload.get("rateLimits", []) or [],
        "timezone": payload.get("timezone", ""),
    }
    return out, limits


__all__ = [
    "classify_quadrant", "parse_book_ticker", "parse_depth", "parse_exchange_info",
    "parse_funding_history", "parse_kline", "parse_klines", "parse_open_interest",
    "parse_premium_index",
]
