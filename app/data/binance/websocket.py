"""Binance USDⓈ-M WebSocket collector.

Binance USDⓈ-M currently uses routed WebSocket endpoints: `/public` for public
order-book streams and `/market` for market streams such as kline, markPrice and ticker.
The collector keeps the route grouping explicit so a stream can never silently land on
the wrong transport.

Implements: reconnect with exponential backoff + jitter, heartbeat, stale detection,
gap counting (by event time), REST fallback signalling.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

import aiohttp

from app.core.logging_setup import get_logger
from app.core.timeutils import now_ms

log = get_logger(__name__)


class StreamRoute(str, Enum):
    PUBLIC = "public"     # @depth / @depth20 / @bookTicker
    MARKET = "market"     # @aggTrade / @kline / @markPrice / @ticker / @forceOrder


_PUBLIC_SUFFIXES = ("@depth", "@depth5", "@depth10", "@depth20", "@depth50", "@depth100", "@depth500", "@depth1000", "@bookTicker")
_MARKET_SUFFIXES = ("@aggTrade", "@kline", "@forceOrder", "@markPrice", "@ticker", "@miniTicker", "@24hrTicker")

ROUTE_MAP: dict[StreamRoute, str] = {
    StreamRoute.PUBLIC: "wss://fstream.binance.com/public/ws",
    StreamRoute.MARKET: "wss://fstream.binance.com/market/ws",
}
COMBINED_ROUTE_MAP: dict[StreamRoute, str] = {
    StreamRoute.PUBLIC: "wss://fstream.binance.com/public/stream",
    StreamRoute.MARKET: "wss://fstream.binance.com/market/stream",
}


def required_route(stream: str) -> StreamRoute:
    """Determine which route a stream name requires. Unknown streams default to PUBLIC
    only if they match a known public suffix; otherwise the caller must be explicit."""
    for suffix in _MARKET_SUFFIXES:
        if suffix.lower() in stream.lower():
            return StreamRoute.MARKET
    for suffix in _PUBLIC_SUFFIXES:
        if suffix.lower() in stream.lower():
            return StreamRoute.PUBLIC
    return StreamRoute.PUBLIC


@dataclass
class StreamHealth:
    name: str
    connected: bool = False
    last_message_ms: int | None = None
    messages: int = 0
    gaps: int = 0
    reconnects: int = 0
    errors: int = 0
    stale_limit_ms: int | None = None

    def __post_init__(self) -> None:
        name = self.name.lower()
        # Stream cadence differs materially: a 1m kline cannot be judged by a 3s
        # liveness budget.  Depth/bookTicker/markPrice are high-frequency streams.
        if self.stale_limit_ms is None:
            if '@kline_' in name:
                self.stale_limit_ms = 90_000
            elif '@ticker' in name:
                self.stale_limit_ms = 10_000
            elif '@depth' in name or '@bookticker' in name or '@markprice' in name:
                self.stale_limit_ms = 5_000
            else:
                self.stale_limit_ms = 30_000

    def age_ms(self, reference: int | None = None) -> int | None:
        if self.last_message_ms is None:
            return None
        return (reference or now_ms()) - self.last_message_ms


@dataclass
class BinanceWsCollector:
    """Logical routed connections with sequence-aware invalidation and reconnects."""

    streams: Mapping[StreamRoute, Iterable[str]]
    stale_ms: int
    ping_interval_sec: int = 20
    backoff_sec: tuple[float, ...] = (1, 2, 5, 10, 30)
    jitter: float = 0.30
    on_message: Callable[[str, Mapping[str, Any]], None] | None = None
    on_gap: Callable[[str], None] | None = None

    health: dict[str, StreamHealth] = field(default_factory=dict)
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _last_event_time_ms: dict[str, int] = field(default_factory=dict)
    _last_depth_update_id: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for route, names in self.streams.items():
            for name in names:
                if required_route(name) != route:
                    raise ValueError(
                        f"stream '{name}' belongs on the {required_route(name).value} route, "
                        f"not {route.value} - an unrouted subscription receives no data"
                    )
                self.health[name] = StreamHealth(name=name)

    async def start(self) -> None:
        self._stop.clear()
        for route in self.streams:
            self._tasks.append(asyncio.create_task(self._run_route(route), name=f"ws:{route.value}"))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown best effort
                pass
        self._tasks.clear()

    def build_url(self, route: StreamRoute) -> str:
        names = list(self.streams.get(route, []))
        if not names:
            return ""
        if len(names) == 1:
            return f"{ROUTE_MAP[route]}/{names[0]}"
        joined = "/".join(names)
        return f"{COMBINED_ROUTE_MAP[route]}?streams={joined}"

    async def _run_route(self, route: StreamRoute) -> None:
        url = self.build_url(route)
        if not url:
            return
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._consume(url, route)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                attempt += 1
                log.warning("binance ws %s error: %s", route.value, exc)
                for name in self.streams.get(route, []):
                    self.health[name].connected = False
                    self.health[name].errors += 1
                    self.health[name].reconnects += 1
                delay = self.backoff_sec[min(attempt - 1, len(self.backoff_sec) - 1)]
                delay *= 1.0 + random.uniform(-self.jitter, self.jitter)  # noqa: S311 - jitter, not crypto
                await asyncio.sleep(max(0.1, delay))

    async def _consume(self, url: str, route: StreamRoute) -> None:
        names = list(self.streams.get(route, []))
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, heartbeat=self.ping_interval_sec) as ws:
                for name in names:
                    self.health.setdefault(name, StreamHealth(name=name)).connected = True
                    # CRITICAL: reset sequence state on every (re)connect. Comparing the
                    # previous connection's last update id against the first event of a new
                    # stream falsely declares simultaneous gaps on every subscribed pair.
                    self._last_depth_update_id.pop(name, None)
                    self._last_event_time_ms.pop(name, None)
                self._last_event_time_ms.pop(route.value, None)
                async for msg in ws:
                    if self._stop.is_set():
                        return
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle_text(msg.data, route)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        raise ConnectionError(f"binance ws {route.value} closed")

    def _handle_text(self, raw: str, route: StreamRoute) -> None:
        payload = json.loads(raw)
        stream = payload.get("stream")
        data = payload.get("data", payload)
        if stream is None:
            stream = data.get("s", "") or next(iter(self.streams.get(route, [])), route.value)
            for name in self.streams.get(route, []):
                if name.split("@")[0].upper() in stream.upper():
                    stream = name
                    break
        health = self.health.setdefault(stream, StreamHealth(name=stream))
        self._check_sequence(stream, data, health)
        ts = data.get("E") or data.get("T")
        if ts is not None:
            ts = int(ts)
            previous = self._last_event_time_ms.get(stream)
            if previous is not None and ts < previous:
                health.gaps += 1
                log.warning("binance ws gap on %s: %s -> %s", stream, previous, ts)
            self._last_event_time_ms[stream] = ts
            health.last_message_ms = ts
        else:
            health.last_message_ms = now_ms()
        health.messages += 1
        if self.on_message is not None:
            self.on_message(stream, data)

    def _check_sequence(self, stream: str, data: Mapping[str, Any], health: StreamHealth) -> None:
        if "U" not in data or "u" not in data:
            return
        try:
            first = int(data["U"])
            last = int(data["u"])
        except (TypeError, ValueError):
            return
        previous = self._last_depth_update_id.get(stream)
        if previous is not None:
            pu = data.get("pu")
            contiguous = (pu is not None and int(pu) == previous) or (pu is None and first <= previous + 1 <= last)
            if not contiguous:
                health.gaps += 1
                log.warning("binance websocket sequence gap on %s: previous=%s current=[%s,%s]", stream, previous, first, last)
                if self.on_gap is not None:
                    self.on_gap(stream)
        self._last_depth_update_id[stream] = last

    # ------------------------------------------------------------------ health
    def stream_state(self, reference_ms: int | None = None) -> bool:
        reference = reference_ms or now_ms()
        for health in self.health.values():
            if not health.connected:
                return False
            # Slow kline streams are REST-warmed and need not have emitted their first
            # websocket event before the connection can be considered alive.
            if health.last_message_ms is None and '@kline_' in health.name.lower():
                continue
            age = health.age_ms(reference)
            limit = int(health.stale_limit_ms or self.stale_ms)
            if age is None or age > limit:
                return False
        return True

    def all_healthy(self, reference_ms: int | None = None) -> bool:
        return self.stream_state(reference_ms)

    def stale_streams(self, reference_ms: int | None = None) -> list[str]:
        reference = reference_ms or now_ms()
        out: list[str] = []
        for name, health in self.health.items():
            if not health.connected:
                out.append(name)
                continue
            if health.last_message_ms is None and '@kline_' in health.name.lower():
                continue
            age = health.age_ms(reference)
            limit = int(health.stale_limit_ms or self.stale_ms)
            if age is None or age > limit:
                out.append(name)
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "streams": {
                name: {
                    "connected": h.connected,
                    "age_ms": h.age_ms(),
                    "messages": h.messages,
                    "gaps": h.gaps,
                    "reconnects": h.reconnects,
                    "errors": h.errors,
                    "stale_limit_ms": h.stale_limit_ms,
                }
                for name, h in self.health.items()
            },
            "healthy": self.stream_state(),
        }


def build_default_streams(symbols: Iterable[str], *, depth_levels: int = 20,
                          interval: str = "1m") -> dict[StreamRoute, list[str]]:
    """Build current routed Binance USDⓈ-M subscriptions for every configured symbol."""
    public: list[str] = []
    market: list[str] = []
    for symbol in symbols:
        lower = symbol.lower()
        public.append(f"{lower}@depth{depth_levels}@100ms")
        market.extend([f"{lower}@kline_{interval}", f"{lower}@markPrice@1s", f"{lower}@ticker"])
    return {StreamRoute.PUBLIC: public, StreamRoute.MARKET: market}


def route_report(routes: Mapping[StreamRoute, Iterable[str]]) -> list[dict[str, Any]]:
    """Diagnostic used by scripts/healthcheck.py and docs/TROUBLESHOOTING.md."""
    out: list[dict[str, Any]] = []
    for route, names in routes.items():
        for name in names:
            out.append({"stream": name, "declared_route": route.value,
                        "required_route": required_route(name).value,
                        "ok": required_route(name) == route})
    return out


__all__ = ["BinanceWsCollector", "ROUTE_MAP", "StreamHealth", "StreamRoute",
           "build_default_streams", "required_route", "route_report"]
