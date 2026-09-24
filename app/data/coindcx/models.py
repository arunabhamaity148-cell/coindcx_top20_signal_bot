"""Parsers for CoinDCX Futures public payloads.

VERIFIED (FINAL_DELIVERABLE §J):
  * active_instruments -> FLAT ARRAY of USDT-margined symbols
  * instrument?pair=... -> contract metadata incl. price_increment, quantity_increment,
    min_trade_size, min_notional, maker_fee, taker_fee, funding_frequency
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.core.errors import NormalizationError
from app.core.models import BookLevel, Candle, InstrumentSpec, OrderBook
from app.core.timeutils import now_ms


def parse_active_instruments(payload: Any) -> set[str]:
    if not isinstance(payload, list):
        raise NormalizationError(f"active_instruments must be a flat array, got {type(payload).__name__}")
    out: set[str] = set()
    for item in payload:
        if isinstance(item, str):
            out.add(item)
        elif isinstance(item, Mapping) and "symbol" in item:
            out.add(str(item["symbol"]))
    if not out:
        raise NormalizationError("active_instruments returned no symbols")
    return out


def parse_instrument(payload: Any, *, pair: str, binance_symbol: str,
                     defaults: Mapping[str, Any] | None = None) -> InstrumentSpec:
    """Build an InstrumentSpec from a CoinDCX /instrument response.

    The endpoint may return either a mapping or a single-element list; both are handled.
    Missing mandatory precision fields raise (fail-closed) instead of defaulting.
    """
    defaults = dict(defaults or {})
    record: Mapping[str, Any]
    if isinstance(payload, list):
        if not payload:
            raise NormalizationError(f"instrument response for {pair} was empty")
        record = payload[0]
    elif isinstance(payload, Mapping):
        nested = payload.get("instrument")
        if isinstance(nested, Mapping):
            record = nested
        else:
            nested = payload.get("data")
            record = nested if isinstance(nested, Mapping) else payload
    else:
        raise NormalizationError(f"unexpected instrument payload type for {pair}: {type(payload).__name__}")

    for field_name in ("price_increment", "quantity_increment", "min_trade_size"):
        if record.get(field_name) is None:
            raise NormalizationError(f"instrument {pair} is missing mandatory field '{field_name}'")

    def _f(key: str, fallback: float) -> float:
        value = record.get(key)
        if value is None:
            return fallback
        return float(value)

    return InstrumentSpec(
        pair=str(record.get("symbol") or record.get("pair") or pair),
        binance_symbol=binance_symbol,
        price_increment=float(record["price_increment"]),
        quantity_increment=float(record["quantity_increment"]),
        min_trade_size=float(record["min_trade_size"]),
        min_notional=_f("min_notional", float(defaults.get("min_notional", 6.0))),
        maker_fee_pct=_f("maker_fee", float(defaults.get("maker_fee_pct", 0.0236))),
        taker_fee_pct=_f("taker_fee", float(defaults.get("taker_fee_pct", 0.059))),
        funding_frequency=int(_f("funding_frequency", float(defaults.get("funding_frequency", 4)))),
        quote_currency=str(record.get("quote_currency_short_name") or defaults.get("quote_currency", "USDT")),
        settle_currency=str(record.get("settle_currency_short_name") or defaults.get("settle_currency", "USDT")),
        kind=str(record.get("kind") or defaults.get("kind", "perpetual")),
        unit_contract_value=_f("unit_contract_value", 1.0),
        quanto_multiplier=_f("quanto_multiplier", _f("quanto_to_settle_multiplier", 1.0)),
        inverse=bool(record.get("inverse", record.get("is_inverse", False))),
    )


def _sort_book(levels: Sequence[Any] | Mapping[Any, Any], *, descending: bool) -> list[BookLevel]:
    parsed: list[BookLevel] = []
    if isinstance(levels, Mapping):
        levels = list(levels.items())
    for level in levels or []:
        if isinstance(level, Mapping):
            price = level.get("price", level.get("p"))
            qty = level.get("quantity", level.get("qty", level.get("q")))
        else:
            try:
                price, qty = level[0], level[1]
            except (TypeError, IndexError, ValueError):
                continue
        if price is None or qty is None:
            continue
        parsed.append(BookLevel(float(price), float(qty)))
    parsed.sort(key=lambda l: -l.price if descending else l.price)
    return parsed


def parse_orderbook(payload: Any, pair: str, *, ts_ms: int | None = None) -> OrderBook:
    """Accept the common CoinDCX shapes: {bids:[{price,quantity}], asks:[...]} or
    {bids:[[p,q]], asks:[[p,q]]}, optionally wrapped in {"data": ...}.

    Timestamp semantics:
      * Prefer venue-published event time when present -> event_ts_ms + ts_ms.
      * Otherwise fall back to local receipt clock for ts_ms so freshness remains
        measurable. Cross-venue event-time drift is only enforced when both books
        have real exchange event timestamps (see Normalizer.basis).
    """
    received = now_ms()
    if isinstance(payload, Mapping) and isinstance(payload.get("data"), Mapping):
        payload = payload["data"]
    if not isinstance(payload, Mapping):
        raise NormalizationError(f"orderbook payload for {pair} is not a mapping")
    bids_raw = payload.get("bids", payload.get("bid", [])) or []
    asks_raw = payload.get("asks", payload.get("ask", [])) or []
    exchange_raw = ts_ms or payload.get("timestamp") or payload.get("ts") or payload.get("time")
    event_ts: int | None = None
    if exchange_raw is not None:
        try:
            val = float(exchange_raw)
            event_ts = int(val * 1000) if val < 1e12 else int(val)
        except (TypeError, ValueError):
            event_ts = None
    primary = event_ts if event_ts is not None else received
    return OrderBook(
        venue="COINDCX",
        symbol=pair,
        ts_ms=int(primary),
        bids=_sort_book(bids_raw, descending=True),
        asks=_sort_book(asks_raw, descending=False),
        received_ts_ms=received,
        event_ts_ms=event_ts,
    )


def parse_candles(payload: Any, pair: str) -> list[Candle]:
    """CoinDCX candle rows are [timestamp, open, high, low, close, volume] (unix seconds)."""
    if isinstance(payload, Mapping):
        payload = payload.get("data", payload.get("candles", []))
    if not isinstance(payload, list):
        raise NormalizationError(f"candle payload for {pair} is not a list")
    out: list[Candle] = []
    for row in payload:
        if isinstance(row, Mapping):
            ts = row.get("time") or row.get("timestamp") or row.get("open_time")
            o, h, l, c = row.get("open"), row.get("high"), row.get("low"), row.get("close")
            v = row.get("volume", 0.0)
        else:
            try:
                ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
                v = row[5] if len(row) > 5 else 0.0
            except (TypeError, IndexError, ValueError):
                continue
        if None in (ts, o, h, l, c):
            continue
        ts_ms = int(float(ts) * 1000) if float(ts) < 1e12 else int(ts)
        out.append(Candle(open_time_ms=ts_ms, open=float(o), high=float(h), low=float(l),
                          close=float(c), volume=float(v or 0.0)))
    out.sort(key=lambda c: c.open_time_ms)
    return out


def parse_last_trade_price(payload: Any) -> float | None:
    if isinstance(payload, Mapping):
        for key in ("price", "last_price", "lastPrice", "close"):
            if payload.get(key) is not None:
                return float(payload[key])
    if isinstance(payload, list) and payload:
        return parse_last_trade_price(payload[-1])
    return None


__all__ = [
    "parse_active_instruments", "parse_candles", "parse_instrument",
    "parse_last_trade_price", "parse_orderbook",
]
