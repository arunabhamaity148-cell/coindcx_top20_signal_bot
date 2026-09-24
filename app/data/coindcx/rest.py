"""CoinDCX Futures public REST client.

Current CoinDCX public documentation also exposes futures market-data sockets; this build
keeps the bounded REST poller as its deterministic fallback/data path. The code never
requests private trading endpoints and never assumes undocumented derivatives fields.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping

import aiohttp

from app.core.errors import DataUnavailableError, FailClosedError
from app.core.logging_setup import get_logger
from app.core.models import Candle, InstrumentSpec, OrderBook
from app.core.timeutils import now_ms
from app.data.coindcx import models as cm
from app.utils.rate_limit import RetryPolicy

log = get_logger(__name__)

VERIFIED_PATHS = {
    "active_instruments": "/exchange/v1/derivatives/futures/data/active_instruments",
    "instrument": "/exchange/v1/derivatives/futures/data/instrument",
}
# Current CoinDCX public Futures market-data endpoints. These are deliberately split
# from the API base because orderbook/candlestick feeds are served from
# public.coindcx.com, while instrument metadata remains on api.coindcx.com.
MARKET_DATA_PATHS = {
    "orderbook": "/market_data/v3/orderbook/{pair}-futures/{depth}",
    "trade_history": "/exchange/v1/derivatives/futures/data/trades",
    "candles": "/market_data/candlesticks",
}
# UNPROVEN: no public OI / funding / liquidation exists for CoinDCX Futures.
UNPROVEN_PATHS = {
    "open_interest": "/exchange/v1/derivatives/futures/data/open_interest",
    "funding_rate": "/exchange/v1/derivatives/futures/data/funding_rate",
}


class CoinDCXRestClient:
    def __init__(self, base_url: str, *, market_data_base: str | None = None,
                 session: aiohttp.ClientSession | None = None, timeout: float = 10.0,
                 max_retries: int = 3, poll_sec: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.market_data_base = (market_data_base or "https://public.coindcx.com").rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.retry = RetryPolicy()
        self.poll_sec = poll_sec
        self.last_error: str = ""

    async def __aenter__(self) -> "CoinDCXRestClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise FailClosedError("CoinDCX REST session is not started")
        return self._session

    async def _get(self, path: str, params: Mapping[str, Any] | None = None, *,
                   unproven: bool = False, base_url: str | None = None) -> Any:
        if unproven:
            raise DataUnavailableError(
                f"CoinDCX endpoint {path} is UNPROVEN (not publicly documented); this system will not "
                "request it and will never fabricate its values. Derivatives intelligence is sourced "
                "from Binance instead."
            )
        last_error: Exception | None = None
        for _ in range(max(1, self.max_retries)):
            try:
                async with self.session.get(f"{(base_url or self.base_url).rstrip('/')}{path}",
                                             params=params or {}) as resp:
                    if resp.status == 429:
                        raise FailClosedError("CoinDCX rate limited (429)")
                    if resp.status >= 500:
                        raise FailClosedError(f"CoinDCX server error {resp.status}")
                    if resp.status >= 400:
                        body = await resp.text()
                        raise DataUnavailableError(f"CoinDCX {path} returned {resp.status}: {body[:200]}")
                    self.retry.reset()
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, FailClosedError) as exc:
                last_error = exc
                self.last_error = str(exc)
                await asyncio.sleep(self.retry.next_delay())
        raise FailClosedError(f"CoinDCX {path} failed after retries: {last_error}")

    # ------------------------------------------------------------------ verified
    async def active_instruments(self) -> set[str]:
        return cm.parse_active_instruments(await self._get(VERIFIED_PATHS["active_instruments"],
                                                            params={"margin_currency_short_name[]": "USDT"}))

    async def instrument(self, pair: str, *, binance_symbol: str,
                         defaults: Mapping[str, Any] | None = None) -> InstrumentSpec:
        payload = await self._get(VERIFIED_PATHS["instrument"],
                                  params={"pair": pair, "margin_currency_short_name": "USDT"})
        return cm.parse_instrument(payload, pair=pair, binance_symbol=binance_symbol, defaults=defaults)

    # ------------------------------------------------------------------ market data
    async def orderbook(self, pair: str, depth: int = 50) -> OrderBook:
        path = MARKET_DATA_PATHS["orderbook"].format(pair=pair, depth=int(depth))
        payload = await self._get(path, base_url=self.market_data_base)
        return cm.parse_orderbook(payload, pair)

    async def candles(self, pair: str, resolution: str = "5", from_ms: int | None = None,
                      to_ms: int | None = None) -> list[Candle]:
        params: dict[str, Any] = {"pair": pair, "resolution": resolution, "pcode": "f"}
        if from_ms is not None:
            params["from"] = int(from_ms / 1000)
        if to_ms is not None:
            params["to"] = int(to_ms / 1000)
        return cm.parse_candles(await self._get(MARKET_DATA_PATHS["candles"], params=params,
                                                 base_url=self.market_data_base), pair)

    async def trade_history(self, pair: str, limit: int = 100) -> list[Mapping[str, Any]]:
        payload = await self._get(MARKET_DATA_PATHS["trade_history"], params={"pair": pair, "limit": limit})
        return list(payload) if isinstance(payload, list) else []

    # ------------------------------------------------------------------ unproven
    async def open_interest(self, pair: str) -> None:
        return await self._get(UNPROVEN_PATHS["open_interest"], params={"pair": pair}, unproven=True)

    async def funding_rate(self, pair: str) -> None:
        return await self._get(UNPROVEN_PATHS["funding_rate"], params={"pair": pair}, unproven=True)


@dataclass
class CoinDCXPoller:
    """Adaptive REST polling loop - the shipped substitute for the UNPROVEN WebSocket.

    FAILURE MODE MATRIX: "CoinDCX poll timeout -> staleness > 3x interval => STALE =>
    NO TRADE; DEGRADE if between thresholds".
    """

    client: CoinDCXRestClient
    pairs: tuple[str, ...]
    poll_sec: float = 2.0
    books: dict[str, OrderBook] = field(default_factory=dict)
    last_ts: dict[str, int] = field(default_factory=dict)
    consecutive_failures: dict[str, int] = field(default_factory=dict)
    _task: asyncio.Task | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    max_concurrency: int = 5
    stats: dict[str, int] = field(default_factory=lambda: {"polls": 0, "errors": 0, "timeouts": 0})

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="coindcx-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            await asyncio.sleep(self.poll_sec)

    async def poll_once(self) -> None:
        semaphore = asyncio.Semaphore(max(1, int(self.max_concurrency)))

        async def _poll(pair: str) -> None:
            async with semaphore:
                try:
                    book = await asyncio.wait_for(
                        self.client.orderbook(pair), timeout=max(5.0, self.poll_sec * 3)
                    )
                    if book is not None and book.is_valid:
                        self.books[pair] = book
                        # Prefer local receipt for last_ts so age_ms measures observation freshness.
                        self.last_ts[pair] = int(book.received_ts_ms or book.ts_ms)
                        self.consecutive_failures[pair] = 0
                    self.stats["polls"] += 1
                except asyncio.TimeoutError:
                    self.stats["timeouts"] += 1
                    self.consecutive_failures[pair] = self.consecutive_failures.get(pair, 0) + 1
                    log.warning("coindcx poll timeout for %s", pair)
                except Exception as exc:  # noqa: BLE001 - poll errors must not kill the bot
                    self.stats["errors"] += 1
                    self.consecutive_failures[pair] = self.consecutive_failures.get(pair, 0) + 1
                    log.warning("coindcx poll error for %s: %s", pair, exc)

        await asyncio.gather(*(_poll(pair) for pair in self.pairs))

    def book(self, pair: str) -> OrderBook | None:
        return self.books.get(pair)

    def age_ms(self, pair: str) -> int | None:
        """Freshness age relative to local receipt (not exchange event time)."""
        book = self.books.get(pair)
        if book is not None and book.received_ts_ms is not None:
            return now_ms() - int(book.received_ts_ms)
        ts = self.last_ts.get(pair)
        return now_ms() - ts if ts else None

    def stale(self, pair: str, staleness_ms: int) -> bool:
        age = self.age_ms(pair)
        return age is None or age > staleness_ms


__all__ = ["CoinDCXPoller", "CoinDCXRestClient", "MARKET_DATA_PATHS", "UNPROVEN_PATHS", "VERIFIED_PATHS"]
