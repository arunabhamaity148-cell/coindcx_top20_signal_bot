"""Tick / step snapping.

FINAL_DELIVERABLE §O: the price printed in a Telegram signal must be literally placeable
on CoinDCX, i.e. snapped to that contract's `price_increment`. Quantity must respect
`quantity_increment` and `min_notional`.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal


def _decimals(increment: float) -> int:
    if increment <= 0:
        raise ValueError("increment must be positive")
    text = f"{Decimal(str(increment)):f}"
    return len(text.split(".")[1]) if "." in text else 0


def round_price(price: float, increment: float) -> float:
    """Snap a price to the venue tick (half-up, venue-friendly)."""
    if increment <= 0:
        return float(price)
    quant = Decimal(1).scaleb(-_decimals(increment))
    value = (Decimal(str(price)) / Decimal(str(increment))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return float((value * Decimal(str(increment))).quantize(quant, rounding=ROUND_HALF_UP))


def floor_price(price: float, increment: float) -> float:
    if increment <= 0:
        return float(price)
    quant = Decimal(1).scaleb(-_decimals(increment))
    value = (Decimal(str(price)) / Decimal(str(increment))).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    )
    return float((value * Decimal(str(increment))).quantize(quant, rounding=ROUND_DOWN))


def ceil_price(price: float, increment: float) -> float:
    return (
        floor_price(price, increment) + increment
        if floor_price(price, increment) < price
        else floor_price(price, increment)
    )


def round_qty(qty: float, increment: float) -> float:
    """Round quantity DOWN: never send more size than the operator intended."""
    if increment <= 0:
        return float(qty)
    quant = Decimal(1).scaleb(-_decimals(increment))
    value = (Decimal(str(qty)) / Decimal(str(increment))).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    )
    return float((value * Decimal(str(increment))).quantize(quant, rounding=ROUND_DOWN))


def qty_for_notional(
    notional: float, price: float, increment: float, min_trade_size: float, min_notional: float
) -> float:
    """Advisory size that satisfies quantity_increment / min_trade_size / min_notional."""
    if price <= 0:
        return 0.0
    raw = notional / price
    qty = round_qty(raw, increment)
    qty = max(qty, round_qty(min_trade_size, increment))
    if qty * price < min_notional:
        needed = (
            math.ceil((min_notional / price) / increment) * increment
            if increment > 0
            else min_notional / price
        )
        qty = round_qty(needed + increment, increment) if increment > 0 else needed
    return qty


def in_zone(price: float, low: float, high: float) -> bool:
    return low <= price <= high
