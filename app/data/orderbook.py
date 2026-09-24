"""Orderbook engine: spread, depth, imbalance, stability, slippage estimate.

Feeds veto guard G3 (liquidity/slippage) and G10 (orderbook instability) and the
S5 entry logic (limit at the executable side of the book).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Deque
from collections import deque

from app.core.models import BookLevel, LiquiditySnapshot, OrderBook


def depth_usd(book: OrderBook, side: str, band_bps: float, mid: float | None = None) -> float:
    mid = mid if mid is not None else book.mid
    if mid is None:
        return 0.0
    band = mid * band_bps / 1e4
    levels = book.bids if side == "bid" else book.asks
    total = 0.0
    for level in levels:
        if abs(level.price - mid) > band:
            break
        total += level.price * level.qty
    return total


def walk_book(book: OrderBook, side: str, notional_usd: float) -> float:
    """Average execution price for `notional_usd` swept through one side of the book."""
    levels = book.bids if side == "bid" else book.asks
    remaining = notional_usd
    filled = 0.0
    cost = 0.0
    for level in levels:
        available = level.price * level.qty
        take = min(remaining, available)
        if take <= 0:
            break
        cost += take
        filled += take / level.price
        remaining -= take
        if remaining <= 0:
            break
    if filled <= 0:
        return 0.0
    return cost / filled


def book_imbalance(book: OrderBook, band_bps: float, mid: float | None = None) -> float:
    mid = mid if mid is not None else book.mid
    if mid is None:
        return 0.0
    band = mid * band_bps / 1e4
    bid_total = 0.0
    ask_total = 0.0
    for level in book.bids:
        if mid - level.price > band:
            break
        bid_total += level.price * level.qty
    for level in book.asks:
        if level.price - mid > band:
            break
        ask_total += level.price * level.qty
    if bid_total + ask_total <= 0:
        return 0.0
    return (bid_total - ask_total) / (bid_total + ask_total)


def estimate_slippage_bps(book: OrderBook, side: str, notional_usd: float,
                          fallback_bps: float = 3.0) -> float:
    mid = book.mid
    if mid is None or notional_usd <= 0:
        return fallback_bps
    avg = walk_book(book, side, notional_usd)
    if avg <= 0:
        return fallback_bps
    ref = book.best_ask if side == "ask" else book.best_bid
    if not ref:
        return fallback_bps
    return abs(avg - ref) / ref * 1e4


@dataclass
class BookTracker:
    """Tracks mid jumps to detect orderbook instability (G10: mid jump > 20 bps)."""

    window: int = 60
    mid_history: Deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=60))

    def __post_init__(self) -> None:
        if self.mid_history.maxlen != self.window:
            self.mid_history = deque(self.mid_history, maxlen=self.window)

    def push(self, ts_ms: int, mid: float | None) -> None:
        if mid is None or mid <= 0:
            return
        self.mid_history.append((int(ts_ms), float(mid)))

    def mid_jump_bps(self, mid: float | None, window_ms: int = 10_000) -> float:
        if mid is None or not self.mid_history:
            return 0.0
        cutoff = self.mid_history[-1][0] - window_ms
        recent = [(ts, value) for ts, value in self.mid_history if ts >= cutoff]
        if len(recent) < 2:
            return 0.0
        values = [value for _, value in recent]
        worst = 0.0
        for i in range(1, len(values)):
            if values[i - 1] > 0:
                worst = max(worst, abs(values[i] - values[i - 1]) / values[i - 1] * 1e4)
        return worst


@dataclass
class OrderBookEngine:
    depth_band_bps: float = 50.0
    notional_for_slippage: float = 2_000.0
    fallback_slippage_bps: float = 3.0
    trackers: dict[str, BookTracker] = field(default_factory=dict)

    def tracker(self, symbol: str) -> BookTracker:
        if symbol not in self.trackers:
            self.trackers[symbol] = BookTracker()
        return self.trackers[symbol]

    def observe(self, book: OrderBook) -> None:
        self.tracker(book.symbol).push(book.ts_ms, book.mid)

    def liquidity(self, book: OrderBook, *, side_for_slippage: str = "ask") -> LiquiditySnapshot:
        mid = book.mid
        bid_depth = depth_usd(book, "bid", self.depth_band_bps, mid)
        ask_depth = depth_usd(book, "ask", self.depth_band_bps, mid)
        spread = book.spread_bps or 0.0
        slip = estimate_slippage_bps(book, side_for_slippage, self.notional_for_slippage,
                                    self.fallback_slippage_bps)
        jump = self.tracker(book.symbol).mid_jump_bps(mid)
        return LiquiditySnapshot(
            spread_bps=spread,
            depth_bid_usd=bid_depth,
            depth_ask_usd=ask_depth,
            imbalance=book_imbalance(book, self.depth_band_bps, mid),
            mid_jump_bps=jump,
            expected_slippage_bps=max(slip, spread / 2.0),
        )

    def slippage_for_notional(self, book: OrderBook, side: str, notional_usd: float) -> float:
        return estimate_slippage_bps(book, side, notional_usd, self.fallback_slippage_bps)


__all__ = [
    "BookTracker", "OrderBookEngine", "book_imbalance", "depth_usd",
    "estimate_slippage_bps", "walk_book",
]
