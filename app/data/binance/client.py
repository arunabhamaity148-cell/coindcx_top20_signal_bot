"""Binance data facade.

Production correction: `now_ms` is explicitly imported because the client uses it
throughout lifecycle, websocket, REST, candle-health and feed-health paths.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import FailClosedError
from app.core.logging_setup import get_logger
from app.core.models import Candle, FeedHealth, FeedState, OrderBook

# IMPORTANT: this import must exist in the runtime copy of client.py.
from app.core.timeutils import age_ms, now_ms

from app.data.binance import models as bm
from app.data.binance.rest import BinanceRestClient
from app.data.binance.websocket import BinanceWsCollector, build_default_streams
from app.utils.rate_limit import RateLimitBudget

log = get_logger(__name__)

DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")
_ONE_MINUTE_MS = 60_000
_AGGREGATION_MS = {
    "5m": 5 * _ONE_MINUTE_MS,
    "15m": 15 * _ONE_MINUTE_MS,
    "1h": 60 * _ONE_MINUTE_MS,
    "4h": 4 * 60 * _ONE_MINUTE_MS,
}


@dataclass
class BinanceDataClient:
    exchange_cfg: Any
    staleness_ms: int = 3000
    timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES
    allow_unproven: bool = False

    rest: BinanceRestClient = field(init=False)
    ws: BinanceWsCollector | None = field(default=None, init=False)
    _books: dict[str, OrderBook] = field(default_factory=dict, init=False)
    _book_resync_required: set[str] = field(default_factory=set, init=False)
    _klines_cache: dict[tuple[str, str], list[Candle]] = field(default_factory=dict, init=False)
    _symbol_meta: dict[str, dict[str, float]] = field(default_factory=dict, init=False)
    _last_rest_ts: int | None = field(default=None, init=False)
    _last_ws_ts: int | None = field(default=None, init=False)
    _last_error: str = field(default="", init=False)

    def __post_init__(self) -> None:
        budget = RateLimitBudget(
            weight_per_min=int(self.exchange_cfg.rate_limit_weight_per_min),
            budget_fraction=float(self.exchange_cfg.weight_budget_fraction),
        )
        self.rest = BinanceRestClient(
            self.exchange_cfg.rest_base,
            budget,
            allow_unproven=self.allow_unproven,
        )

    async def start(self, symbols: list[str]) -> None:
        await self.rest.start()
        try:
            info, _limits = await self.rest.exchange_info()
            self._symbol_meta = info
            self._last_rest_ts = now_ms()
            missing = [symbol for symbol in symbols if symbol not in info]
            if missing:
                raise FailClosedError(
                    f"Binance symbols missing from exchangeInfo: {', '.join(missing[:10])}"
                )
            log.info("binance exchangeInfo loaded: %d symbols", len(info))
        except Exception as exc:
            self._last_error = str(exc)
            log.error("binance exchangeInfo failed: %s", exc)

    async def start_ws(self, symbols: list[str]) -> None:
        if not symbols:
            raise FailClosedError("Binance WS requires at least one validated symbol")
        streams = build_default_streams(symbols)
        self.ws = BinanceWsCollector(
            streams=streams,
            stale_ms=self.staleness_ms,
            ping_interval_sec=int(self.exchange_cfg.ping_interval_sec),
            backoff_sec=tuple(self.exchange_cfg.reconnect_backoff_sec),
            jitter=float(self.exchange_cfg.jitter),
            on_message=self._on_ws_message,
            on_gap=self._on_ws_gap,
        )
        await self.ws.start()

    async def stop(self) -> None:
        if self.ws is not None:
            await self.ws.stop()
        await self.rest.close()

    def _on_ws_message(self, stream: str, data: Any) -> None:
        payload = data if isinstance(data, dict) else {}
        event_ts = payload.get("E") or payload.get("T") or now_ms()
        try:
            self._last_ws_ts = int(event_ts)
        except (TypeError, ValueError):
            self._last_ws_ts = now_ms()

        symbol = str(payload.get("s") or "").upper()
        if not symbol and "@" in stream:
            symbol = stream.split("@", 1)[0].upper()
        if not symbol:
            return

        try:
            if "b" in payload and "a" in payload:
                book = bm.parse_depth(
                    {
                        "bids": payload.get("b", []),
                        "asks": payload.get("a", []),
                        "E": payload.get("E") or payload.get("T") or now_ms(),
                    },
                    symbol,
                )
                if book.is_valid:
                    self._books[symbol] = book
                return

            k = payload.get("k")
            if isinstance(k, dict):
                # Fail closed: Binance must explicitly mark the candle closed.
                if k.get("x") is not True:
                    return
                close_time = k.get("T")
                if close_time is None:
                    raise ValueError("closed Binance kline missing close time")
                candle = bm.parse_kline(
                    [
                        int(k["t"]),
                        k["o"],
                        k["h"],
                        k["l"],
                        k["c"],
                        0,
                        int(close_time),
                        k.get("q") or 0,
                        0,
                        k.get("v") or 0,
                        k.get("Q"),
                        0,
                    ],
                    reference_ms=int(close_time),
                )
                if not candle.is_closed:
                    raise ValueError("Binance WS reported a non-closed kline as closed")
                self._upsert_ws_candle(symbol, candle)
        except (TypeError, ValueError, KeyError) as exc:
            self._last_error = str(exc)
            log.warning("binance ws state ingest failed for %s: %s", symbol, exc)

    def _on_ws_gap(self, stream: str) -> None:
        symbol = stream.split("@", 1)[0].upper()
        if symbol:
            self._books.pop(symbol, None)
            self._book_resync_required.add(symbol)
            self._last_error = (
                f"websocket sequence gap for {symbol}; REST book resync required"
            )
            log.warning(self._last_error)

    def _upsert_ws_candle(self, symbol: str, candle: Candle) -> None:
        if not candle.is_closed:
            return

        base_key = (symbol, "1m")
        series = self._klines_cache.setdefault(base_key, [])

        if series and series[-1].open_time_ms == candle.open_time_ms:
            series[-1] = candle
        elif not series or series[-1].open_time_ms < candle.open_time_ms:
            series.append(candle)
        else:
            replaced = False
            for idx, existing in enumerate(series):
                if existing.open_time_ms == candle.open_time_ms:
                    series[idx] = candle
                    replaced = True
                    break
            if not replaced:
                series.append(candle)
                series.sort(key=lambda item: item.open_time_ms)

        if len(series) > 2000:
            del series[:-2000]

        for timeframe, bucket_ms in _AGGREGATION_MS.items():
            self._publish_completed_bucket(
                symbol, timeframe, bucket_ms, candle.open_time_ms
            )

    def _publish_completed_bucket(
        self,
        symbol: str,
        timeframe: str,
        bucket_ms: int,
        changed_open_time_ms: int,
    ) -> None:
        bucket = (changed_open_time_ms // bucket_ms) * bucket_ms
        expected = bucket_ms // _ONE_MINUTE_MS
        base = self._klines_cache.get((symbol, "1m"), [])

        members = [
            c
            for c in base
            if bucket <= c.open_time_ms < bucket + bucket_ms and c.is_closed
        ]
        if len(members) != expected:
            return

        members.sort(key=lambda c: c.open_time_ms)
        expected_times = [bucket + i * _ONE_MINUTE_MS for i in range(expected)]
        if [c.open_time_ms for c in members] != expected_times:
            return

        first, last = members[0], members[-1]
        taker_values = [c.taker_buy_quote for c in members]
        taker_buy_quote = (
            sum(float(value) for value in taker_values)
            if all(value is not None for value in taker_values)
            else None
        )

        aggregate = Candle(
            open_time_ms=bucket,
            open=first.open,
            high=max(c.high for c in members),
            low=min(c.low for c in members),
            close=last.close,
            volume=sum(c.volume for c in members),
            taker_buy_quote=taker_buy_quote,
            close_time_ms=last.close_time_ms,
            is_closed=True,
        )

        out = self._klines_cache.setdefault((symbol, timeframe), [])
        replaced = False
        for idx, existing in enumerate(out):
            if existing.open_time_ms == bucket:
                out[idx] = aggregate
                replaced = True
                break

        if not replaced:
            out.append(aggregate)
            out.sort(key=lambda item: item.open_time_ms)

        if len(out) > 500:
            del out[:-500]

    async def klines(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        candles = await self.rest.klines(symbol, timeframe, limit)
        self._klines_cache[(symbol, timeframe)] = list(candles)
        self._last_rest_ts = now_ms()
        return list(candles)

    async def warm_cache(
        self,
        symbols: list[str],
        *,
        limit: int = 300,
        max_concurrency: int | None = None,
    ) -> None:
        semaphore = asyncio.Semaphore(
            max(
                1,
                int(
                    max_concurrency
                    or getattr(self.exchange_cfg, "poll_concurrency", 5)
                ),
            )
        )

        async def load(symbol: str, timeframe: str) -> None:
            async with semaphore:
                try:
                    await self.klines(symbol, timeframe, limit)
                except Exception as exc:
                    self._last_error = str(exc)
                    log.warning(
                        "binance klines %s %s failed: %s",
                        symbol,
                        timeframe,
                        exc,
                    )

        await asyncio.gather(
            *(load(symbol, timeframe) for symbol in symbols for timeframe in self.timeframes)
        )

    async def orderbook(self, symbol: str, limit: int = 100) -> OrderBook | None:
        reference = now_ms()
        cached = self.cached_book(symbol, reference_ms=reference)
        if cached is not None:
            return cached

        try:
            book = await self.rest.depth(symbol, limit)
            if not book.is_valid:
                raise ValueError(f"invalid Binance order book for {symbol}")
            self._books[symbol] = book
            self._book_resync_required.discard(symbol)
            self._last_rest_ts = now_ms()
            return book
        except Exception as exc:
            self._last_error = str(exc)
            log.warning("binance depth %s failed: %s", symbol, exc)
            return None

    def cached_book(
        self,
        symbol: str,
        *,
        reference_ms: int | None = None,
    ) -> OrderBook | None:
        if symbol in self._book_resync_required:
            return None

        book = self._books.get(symbol)
        if book is None:
            return None

        if reference_ms is None:
            return book

        if reference_ms < book.ts_ms or reference_ms - book.ts_ms > self.staleness_ms:
            return None

        return book

    def cached_klines(
        self,
        symbol: str,
        timeframe: str,
        *,
        closed_only: bool = True,
    ) -> list[Candle]:
        candles = list(self._klines_cache.get((symbol, timeframe), []))
        if closed_only:
            candles = [c for c in candles if c.is_closed]
        return candles

    def candle_health(
        self,
        symbol: str,
        timeframe: str,
        *,
        reference_ms: int | None = None,
    ) -> bool:
        candles = self.cached_klines(symbol, timeframe, closed_only=True)
        if not candles:
            return False

        last = candles[-1]
        ref = reference_ms if reference_ms is not None else now_ms()

        tf_ms = {
            "1m": 60_000,
            "5m": 5 * 60_000,
            "15m": 15 * 60_000,
            "1h": 60 * 60_000,
            "4h": 4 * 60 * 60_000,
        }.get(timeframe)

        if tf_ms is None:
            return False

        close_ts = last.close_time_ms or (last.open_time_ms + tf_ms - 1)
        age = ref - close_ts
        return 0 <= age <= max(self.staleness_ms, tf_ms)

    def symbol_meta(self, symbol: str) -> dict[str, float] | None:
        return self._symbol_meta.get(symbol)

    async def derivatives_context(self, symbol: str) -> dict[str, float | None]:
        out: dict[str, Any] = {}

        try:
            out.update(await self.rest.premium_index(symbol))
        except Exception as exc:
            out["premium_index_error"] = None
            self._last_error = str(exc)

        try:
            oi, oi_ts = await self.rest.open_interest(symbol)
            out["open_interest"] = oi
            out["open_interest_ts"] = float(oi_ts) if oi_ts else None
        except Exception as exc:
            out["open_interest"] = None
            out["open_interest_ts"] = None
            self._last_error = str(exc)

        try:
            funding_history = await self.rest.funding_rate_history(symbol, limit=100)
            out["funding_history"] = funding_history

            ts_values = [
                int(float(row["ts_ms"]))
                for row in funding_history
                if row.get("ts_ms") is not None
            ]
            out["funding_history_latest_ts_ms"] = max(ts_values) if ts_values else None
            out["funding_ts_ms"] = out.get("mark_ts_ms")
        except Exception as exc:
            out["funding_history"] = None
            out["funding_history_latest_ts_ms"] = None
            out["funding_ts_ms"] = out.get("mark_ts_ms")
            self._last_error = str(exc)

        return out

    def health(self) -> dict[str, FeedHealth]:
        reference = now_ms()

        rest_age = age_ms(self._last_rest_ts, reference)
        rest_state = FeedState.HEALTHY

        if self._last_rest_ts is None:
            rest_state = FeedState.DISCONNECTED
        elif rest_age is not None and rest_age > self.staleness_ms:
            rest_state = FeedState.STALE

        ws_state = FeedState.HEALTHY
        ws_age = age_ms(self._last_ws_ts, reference)

        if self.ws is None:
            ws_state = FeedState.STALE
        elif not self.ws.stream_state(reference):
            ws_state = FeedState.STALE

        return {
            "binance_rest": FeedHealth(
                "binance_rest",
                rest_state,
                self._last_rest_ts,
                rest_age,
                self._last_error,
            ),
            "binance_ws": FeedHealth(
                "binance_ws",
                ws_state,
                self._last_ws_ts,
                ws_age,
            ),
        }

    @property
    def available(self) -> bool:
        return bool(self._symbol_meta)

    def require_metadata(self, symbol: str) -> dict[str, float]:
        meta = self.symbol_meta(symbol)

        if not meta:
            raise FailClosedError(
                f"no Binance contract metadata for {symbol} - NO TRADE"
            )

        if meta.get("status_trading", 0.0) != 1.0:
            raise FailClosedError(
                f"Binance symbol {symbol} is not TRADING - NO TRADE"
            )

        return meta


__all__ = ["BinanceDataClient", "DEFAULT_TIMEFRAMES"]
