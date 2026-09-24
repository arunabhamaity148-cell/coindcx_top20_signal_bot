"""Binance USDⓈ-M Futures REST client (public endpoints only).

VERIFIED endpoints (FINAL_DELIVERABLE §I): ping, exchangeInfo, openInterest,
fundingRate, premiumIndex, klines, depth, bookTicker.

UNPROVEN endpoints are present but DISABLED by default (`allow_unproven=False`):
    /futures/data/openInterestHist
    /futures/data/takerlongshortRatio
    /futures/data/topLongShortPositionRatio

They raise DataUnavailableError until a probe test (scripts/probe_endpoints.py)
converts them to VERIFIED - the system never fabricates their data.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

import aiohttp

from app.core.errors import DataUnavailableError, FailClosedError
from app.core.timeutils import now_ms
from app.core.logging_setup import get_logger
from app.core.models import Candle, OrderBook
from app.data.binance import models as bm
from app.utils.rate_limit import RateLimitBudget, RetryPolicy

log = get_logger(__name__)

VERIFIED_PATHS = {
    "ping": "/fapi/v1/ping",
    "exchange_info": "/fapi/v1/exchangeInfo",
    "depth": "/fapi/v1/depth",
    "klines": "/fapi/v1/klines",
    "book_ticker": "/fapi/v1/ticker/bookTicker",
    "open_interest": "/fapi/v1/openInterest",
    "funding_rate": "/fapi/v1/fundingRate",
    "premium_index": "/fapi/v1/premiumIndex",
}

# Documented but NOT re-verified this build -> UNPROVEN (never silently trusted).
UNPROVEN_PATHS = {
    "open_interest_hist": "/futures/data/openInterestHist",
    "taker_long_short_ratio": "/futures/data/takerlongshortRatio",
    "top_long_short_position_ratio": "/futures/data/topLongShortPositionRatio",
    "global_long_short_account_ratio": "/futures/data/globalLongShortAccountRatio",
}

WEIGHTS = {"ping": 1, "exchange_info": 1, "depth": 5, "klines": 2, "book_ticker": 2,
           "open_interest": 1, "funding_rate": 1, "premium_index": 1}

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10.0)


class BinanceRestClient:
    def __init__(self, base_url: str, budget: RateLimitBudget, *, allow_unproven: bool = False,
                 session: aiohttp.ClientSession | None = None, timeout: float = 10.0,
                 max_retries: int = 3, retry_policy: RetryPolicy | None = None):
        self.base_url = base_url.rstrip("/")
        self.budget = budget
        self.allow_unproven = allow_unproven
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.retry = retry_policy or RetryPolicy()
        self.weight_limit_events = 0
        self.request_count = 0

    async def __aenter__(self) -> "BinanceRestClient":
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
            raise FailClosedError("Binance REST session is not started")
        return self._session

    # ------------------------------------------------------------------ plumbing
    async def _get(self, path: str, params: Mapping[str, Any] | None = None,
                   weight: int = 1, *, unproven: bool = False) -> Any:
        if unproven and not self.allow_unproven:
            raise DataUnavailableError(
                f"Binance endpoint {path} is UNPROVEN for this build; refusing to request it. "
                "Run scripts/probe_endpoints.py to verify, then set allow_unproven=True."
            )
        await self.budget.spend(weight)
        last_error: Exception | None = None
        for _ in range(max(1, self.max_retries)):
            try:
                self.request_count += 1
                async with self.session.get(f"{self.base_url}{path}", params=params or {}) as resp:
                    if resp.status == 429 or resp.status in (418, 403):
                        self.weight_limit_events += 1
                        raise FailClosedError(f"Binance rate-limit/denied response {resp.status}")
                    if resp.status >= 500:
                        raise FailClosedError(f"Binance server error {resp.status}")
                    if resp.status >= 400:
                        body = await resp.text()
                        raise DataUnavailableError(f"Binance {path} returned {resp.status}: {body[:200]}")
                    self.retry.reset()
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, FailClosedError) as exc:
                last_error = exc
                await asyncio.sleep(self.retry.next_delay())
        raise FailClosedError(f"Binance {path} failed after retries: {last_error}")

    # ------------------------------------------------------------------ verified
    async def ping(self) -> bool:
        await self._get(VERIFIED_PATHS["ping"], weight=WEIGHTS["ping"])
        return True

    async def exchange_info(self) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
        payload = await self._get(VERIFIED_PATHS["exchange_info"], weight=WEIGHTS["exchange_info"])
        return bm.parse_exchange_info(payload)

    async def klines(self, symbol: str, interval: str = "5m", limit: int = 500,
                     *, end_time_ms: int | None = None, start_time_ms: int | None = None) -> list[Candle]:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        rows = await self._get(VERIFIED_PATHS["klines"], params=params, weight=WEIGHTS["klines"])
        return bm.parse_klines(rows, reference_ms=now_ms())

    async def depth(self, symbol: str, limit: int = 100) -> OrderBook:
        payload = await self._get(VERIFIED_PATHS["depth"], params={"symbol": symbol, "limit": limit},
                                  weight=WEIGHTS["depth"])
        return bm.parse_depth(payload, symbol)

    async def book_ticker(self, symbol: str) -> OrderBook:
        payload = await self._get(VERIFIED_PATHS["book_ticker"], params={"symbol": symbol},
                                  weight=WEIGHTS["book_ticker"])
        return bm.parse_book_ticker(payload, symbol)

    async def open_interest(self, symbol: str) -> tuple[float | None, int]:
        payload = await self._get(VERIFIED_PATHS["open_interest"], params={"symbol": symbol},
                                  weight=WEIGHTS["open_interest"])
        return bm.parse_open_interest(payload)

    async def funding_rate(self, symbol: str, limit: int = 100) -> tuple[float | None, int | None, float | None]:
        payload = await self._get(VERIFIED_PATHS["funding_rate"],
                                  params={"symbol": symbol, "limit": limit},
                                  weight=WEIGHTS["funding_rate"])
        return bm.parse_funding_history(payload)

    async def funding_rate_history(self, symbol: str, limit: int = 100) -> list[dict[str, float | None]]:
        payload = await self._get(VERIFIED_PATHS["funding_rate"],
                                  params={"symbol": symbol, "limit": limit},
                                  weight=WEIGHTS["funding_rate"])
        out: list[dict[str, float | None]] = []
        for row in payload:
            rate = row.get("fundingRate")
            if rate is None:
                continue
            out.append({
                "rate": float(rate),
                "ts_ms": float(row.get("fundingTime")) if row.get("fundingTime") is not None else None,
            })
        return out

    async def premium_index(self, symbol: str) -> dict[str, float | None]:
        payload = await self._get(VERIFIED_PATHS["premium_index"], params={"symbol": symbol},
                                  weight=WEIGHTS["premium_index"])
        return bm.parse_premium_index(payload)

    # ------------------------------------------------------------------ unproven
    async def open_interest_history(self, symbol: str, period: str = "5m", limit: int = 500) -> Sequence[Mapping[str, Any]]:
        return await self._get(UNPROVEN_PATHS["open_interest_hist"],
                               params={"symbol": symbol, "period": period, "limit": limit},
                               weight=2, unproven=True)

    async def taker_long_short_ratio(self, symbol: str, period: str = "5m",
                                     limit: int = 500) -> Sequence[Mapping[str, Any]]:
        return await self._get(UNPROVEN_PATHS["taker_long_short_ratio"],
                               params={"symbol": symbol, "period": period, "limit": limit},
                               weight=2, unproven=True)

    async def top_long_short_position_ratio(self, symbol: str, period: str = "5m",
                                            limit: int = 500) -> Sequence[Mapping[str, Any]]:
        return await self._get(UNPROVEN_PATHS["top_long_short_position_ratio"],
                               params={"symbol": symbol, "period": period, "limit": limit},
                               weight=2, unproven=True)


__all__ = ["BinanceRestClient", "UNPROVEN_PATHS", "VERIFIED_PATHS", "WEIGHTS"]
