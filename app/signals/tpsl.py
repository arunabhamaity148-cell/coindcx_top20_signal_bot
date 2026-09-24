"""TP / SL construction (FINAL_DELIVERABLE §P, §Q).

  risk = entry - SL (long) / SL - entry (short), FLOORED at 0.25 * ATR so a too-tight
  stop cannot manufacture absurd R multiples.

  TP1 = 1.0R (structural: first opposing liquidity / prior micro-swing)
  TP2 = 2.0R (the R:R filter is applied here, min_rr_tp2 = 1.8)
  TP3 = 3.0R
  TP4 = 5.0R (runner)

SL is the level where the thesis is objectively WRONG, not a pain threshold:
`SL = structure_invalidation -/+ (0.5 * ATR)`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.core.errors import FailClosedError
from app.core.models import Direction, StrategyCandidate
from app.utils.rounding import round_price


@dataclass(frozen=True)
class TpSlPlan:
    entry: float
    stop_loss: float
    risk: float
    tps: tuple[float, float, float, float]
    rr_tp2: float
    invalidation: float
    atr_floor_applied: bool = False


def build_tp_sl(
    *,
    direction: Direction,
    entry: float,
    invalidation: float,
    atr: float,
    tick: float,
    sl_atr_buffer: float,
    risk_floor_atr_mult: float,
    r_multiples: Sequence[float] = (1.0, 2.0, 3.0, 5.0),
    sl_override: float | None = None,
) -> TpSlPlan:
    """Deterministic, tick-snapped, monotonic TP/SL ladder. Raises on an impossible ladder."""
    if entry <= 0 or atr <= 0:
        raise FailClosedError("entry and ATR must be positive to build TP/SL")
    if tick <= 0:
        raise FailClosedError("tick size must be positive (CoinDCX instrument metadata)")

    if sl_override is not None:
        stop = sl_override
    elif direction is Direction.LONG:
        stop = invalidation - sl_atr_buffer * atr
    else:
        stop = invalidation + sl_atr_buffer * atr

    entry_s = round_price(entry, tick)
    stop_s = round_price(stop, tick)
    risk = abs(entry_s - stop_s)
    floor = risk_floor_atr_mult * atr
    applied = False
    if risk < floor:
        applied = True
        risk = floor
        stop_s = round_price(
            entry_s - risk if direction is Direction.LONG else entry_s + risk, tick
        )
        risk = abs(entry_s - stop_s)
    if risk <= 0:
        raise FailClosedError("computed risk is non-positive")

    sign = 1.0 if direction is Direction.LONG else -1.0
    r1, r2, r3, r4 = (list(r_multiples) + [1.0, 2.0, 3.0, 5.0])[:4]
    snapped = [round_price(entry_s + sign * risk * r, tick) for r in (r1, r2, r3, r4)]

    if direction is Direction.LONG and not (stop_s < entry_s < min(snapped)):
        raise FailClosedError(
            f"impossible LONG ladder: SL {stop_s} entry {entry_s} TP1 {snapped[0]}"
        )
    if direction is Direction.SHORT and not (stop_s > entry_s > max(snapped)):
        raise FailClosedError(
            f"impossible SHORT ladder: SL {stop_s} entry {entry_s} TP1 {snapped[0]}"
        )

    rr = abs(snapped[1] - entry_s) / abs(entry_s - stop_s)
    return TpSlPlan(
        entry=entry_s,
        stop_loss=stop_s,
        risk=abs(entry_s - stop_s),
        tps=(snapped[0], snapped[1], snapped[2], snapped[3]),
        rr_tp2=rr,
        invalidation=round_price(invalidation, tick),
        atr_floor_applied=applied,
    )


def plan_from_candidate(
    candidate: StrategyCandidate,
    *,
    tick: float,
) -> TpSlPlan:
    """Preserve the strategy-selected level plan exactly; validate, never overwrite it.

    Strategy-specific entries, invalidations, SL and TP ladders are part of the strategy
    thesis. The signal engine must not rebuild them from generic ATR rules after consensus.
    Any malformed or off-tick candidate is rejected fail-closed.
    """
    if tick <= 0:
        raise FailClosedError("tick size must be positive")
    values = (
        candidate.entry_price, candidate.entry_zone_low, candidate.entry_zone_high,
        candidate.stop_loss, candidate.invalidation, candidate.tp1, candidate.tp2,
        candidate.tp3, candidate.tp4,
    )
    if any(v <= 0 for v in values):
        raise FailClosedError("candidate contains non-positive entry/zone/SL/TP values")

    entry = float(candidate.entry_price)
    stop = float(candidate.stop_loss)
    tps = tuple(float(v) for v in (candidate.tp1, candidate.tp2, candidate.tp3, candidate.tp4))
    if abs(entry / tick - round(entry / tick)) > 1e-6:
        raise FailClosedError("candidate entry is not tick-aligned")
    if any(abs(v / tick - round(v / tick)) > 1e-6 for v in (stop, *tps)):
        raise FailClosedError("candidate SL/TP is not tick-aligned")
    zone_low = float(candidate.entry_zone_low)
    zone_high = float(candidate.entry_zone_high)
    invalidation = float(candidate.invalidation)
    if zone_low > zone_high or not (zone_low <= entry <= zone_high):
        raise FailClosedError("candidate entry is outside its declared entry zone")
    if candidate.direction is Direction.LONG and not (stop <= invalidation < entry):
        raise FailClosedError("LONG geometry requires SL <= invalidation < entry")
    if candidate.direction is Direction.SHORT and not (stop >= invalidation > entry):
        raise FailClosedError("SHORT geometry requires SL >= invalidation > entry")

    if candidate.direction is Direction.LONG:
        if not (stop < entry < tps[0] < tps[1] < tps[2] < tps[3]):
            raise FailClosedError("candidate LONG ladder is invalid")
    else:
        if not (stop > entry > tps[0] > tps[1] > tps[2] > tps[3]):
            raise FailClosedError("candidate SHORT ladder is invalid")

    risk = abs(entry - stop)
    rr = abs(tps[1] - entry) / risk if risk > 0 else 0.0
    if rr <= 0:
        raise FailClosedError("candidate R:R is non-positive")
    return TpSlPlan(
        entry=entry,
        stop_loss=stop,
        risk=risk,
        tps=tps,
        rr_tp2=rr,
        invalidation=float(candidate.invalidation),
    )


def apply_tp1_clamp(
    plan: TpSlPlan, *, direction: Direction, clamp_level: float, tick: float
) -> TpSlPlan:
    """S1 clamps TP1 to entry_high + 1*ATR; S2 anchors it to the compression boundary."""
    tps = list(plan.tps)
    if direction is Direction.LONG:
        tps[0] = min(tps[0], round_price(clamp_level, tick))
    else:
        tps[0] = max(tps[0], round_price(clamp_level, tick))
    if direction is Direction.LONG and tps[0] <= plan.entry:
        raise FailClosedError("TP1 clamp produced a non-positive R for a LONG")
    if direction is Direction.SHORT and tps[0] >= plan.entry:
        raise FailClosedError("TP1 clamp produced a non-positive R for a SHORT")
    return TpSlPlan(
        entry=plan.entry,
        stop_loss=plan.stop_loss,
        risk=plan.risk,
        tps=(tps[0], tps[1], tps[2], tps[3]),
        rr_tp2=plan.rr_tp2,
        invalidation=plan.invalidation,
        atr_floor_applied=plan.atr_floor_applied,
    )


def management_plan() -> list[str]:
    """Manual management guidance - NEVER automated (spec §P)."""
    return [
        "TP1 -> close 40% and move SL to breakeven",
        "TP2 -> close 30% and trail under the prior 5m swing",
        "TP3 -> close 20%; 10% runner trails 15m structure",
        "NO AUTO-CLOSE. All adjustments are manual.",
    ]


__all__ = ["TpSlPlan", "apply_tp1_clamp", "build_tp_sl", "management_plan", "plan_from_candidate"]
