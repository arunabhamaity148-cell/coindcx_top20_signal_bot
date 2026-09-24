"""Limit-signal expiry and anti-chase logic (FINAL_DELIVERABLE §O, §AA).

HARD RULES:
  * every signal is LIMIT ENTRY ONLY; the price printed is tick-snapped to CoinDCX
    `price_increment` so it is literally placeable;
  * NO FILL BEYOND THE ZONE. If price runs away the SIGNAL EXPIRES - there is no code
    path that converts a limit signal into a market-order recommendation;
  * entry validity is bounded by the grade-derived expiry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.core.models import Direction, SignalState


class EntryVerdict(str, Enum):
    FILLABLE = "FILLABLE"
    WAITING = "WAITING"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class EntryCheck:
    verdict: EntryVerdict
    reason: str
    must_expire: bool = False

    @property
    def fillable(self) -> bool:
        return self.verdict is EntryVerdict.FILLABLE


def evaluate_entry(
    *,
    direction: Direction,
    price: float,
    zone_low: float,
    zone_high: float,
    invalidation: float,
    created_ms: int,
    expiry_ms: int,
    now_ms: int,
    atr: float,
) -> EntryCheck:
    if now_ms >= expiry_ms:
        return EntryCheck(
            EntryVerdict.EXPIRED,
            f"entry window closed at {expiry_ms} (limit signal never chases)",
            must_expire=True,
        )
    if direction is Direction.LONG and price < invalidation:
        return EntryCheck(
            EntryVerdict.INVALIDATED,
            f"price {price:.4f} below invalidation {invalidation:.4f}",
            must_expire=True,
        )
    if direction is Direction.SHORT and price > invalidation:
        return EntryCheck(
            EntryVerdict.INVALIDATED,
            f"price {price:.4f} above invalidation {invalidation:.4f}",
            must_expire=True,
        )
    if zone_low <= price <= zone_high:
        return EntryCheck(
            EntryVerdict.FILLABLE,
            f"price {price:.4f} inside zone [{zone_low:.4f}, {zone_high:.4f}]",
        )
    # price is outside the zone: either it ran away or it has not arrived yet
    ran_away = (direction is Direction.LONG and price > zone_high) or (
        direction is Direction.SHORT and price < zone_low
    )
    if ran_away:
        return EntryCheck(
            EntryVerdict.WAITING,
            f"price {price:.4f} ran beyond the zone; no fill beyond zone, "
            "expiry will retire the signal",
        )
    return EntryCheck(EntryVerdict.WAITING, f"price {price:.4f} has not entered the zone yet")


def expiry_timestamp(created_ms: int, expiry_min: int) -> int:
    return created_ms + int(expiry_min * 60_000)


def next_state(current: SignalState, check: EntryCheck) -> SignalState:
    if check.verdict is EntryVerdict.EXPIRED and check.must_expire:
        return SignalState.EXPIRED
    if check.verdict is EntryVerdict.INVALIDATED and check.must_expire:
        return SignalState.INVALIDATED
    if current is SignalState.PENDING and check.fillable:
        return SignalState.ACTIVE
    return current


def remaining_minutes(expiry_ms: int, now_ms: int) -> int:
    return max(0, int((expiry_ms - now_ms) / 60_000))


__all__ = [
    "EntryCheck",
    "EntryVerdict",
    "evaluate_entry",
    "expiry_timestamp",
    "next_state",
    "remaining_minutes",
]
