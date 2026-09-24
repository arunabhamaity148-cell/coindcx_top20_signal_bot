"""S2 - Volatility Compression -> Range Expansion (VCB).

Spec (FINAL_DELIVERABLE §D):
  Regime: COMPRESSION / RANGE (ATR percentile <= 0.25) · 15m compression, 5m trigger
  Setup: 60-bar range width <= 6*ATR with ATR percentile <= 0.25
  Entry condition: expansion bar > 1.2*ATR, OI >= +1.0 %, taker flow aligned
  Limit entry: RETEST at the compression boundary (hi long / lo short)
  Invalidation: 0.9*ATR back inside the range
  SL: opposite compression boundary -/+ 0.1*ATR
  Expiry 45 min
"""

from __future__ import annotations

from app.core.mathx import rolling_range
from app.core.models import Direction, MarketSnapshot, StrategyCandidate
from app.strategies.base import LevelPlan, Strategy, clamp_confidence
from app.utils.rounding import round_price


class VolatilityCompressionBreakout(Strategy):
    id = "S2"
    name = "Volatility Compression -> Range Expansion"

    def analyze(self, snap: MarketSnapshot) -> StrategyCandidate | None:
        if not self.derivatives_ready(snap):
            return None
        comp_tf = self.params.extra.get("compression_tf", "15m")
        trig_tf = self.params.extra.get("trigger_tf", "5m")
        compression = list(snap.series(comp_tf))
        trigger = list(snap.series(trig_tf))
        min_bars = int(self._p("range_bars", 60))
        if not self.enough(compression, min_bars) or not self.enough(trigger, 30):
            return None

        atr_comp = self.atr_of(compression)
        if not atr_comp or atr_comp <= 0:
            return None
        pct = self.atr_percentile(compression)
        if pct is None or pct > self._p("atr_percentile_max", 0.25):
            return None

        # the compression window is the LAST `min_bars` bars of the compression timeframe
        bounds = rolling_range(compression[-min_bars:], min_bars)
        if bounds is None:
            return None
        hi, lo = bounds
        width = hi - lo
        if width > self._p("range_width_max_atr_mult", 6.0) * atr_comp:
            return None

        atr_trig = self.atr_of(trigger)
        if not atr_trig or atr_trig <= 0:
            return None
        exp = trigger[-1]
        body = abs(exp.close - exp.open)
        oi_chg = snap.derivatives.oi_chg_pct
        min_oi = self._p("min_oi_chg_pct", 1.0)
        buy = exp.taker_buy_ratio
        tick = snap.instrument.price_increment

        if body <= self._p("expansion_atr_mult", 1.2) * atr_trig:
            return None
        if oi_chg is None or oi_chg < min_oi:
            return None
        if buy is None:
            return None

        reasons = [
            f"60-bar range width {width:.4f} <= {self._p('range_width_max_atr_mult', 6.0):.1f}xATR",
            f"ATR percentile {pct:.2f} <= {self._p('atr_percentile_max', 0.25):.2f} (compressed)",
            f"expansion bar {body / atr_trig:.2f}xATR with OI {oi_chg:+.2f}% and taker ratio {buy:.2f}",
        ]
        confidence = clamp_confidence(
            self.params.base_confidence + 0.10 + min(0.08, (oi_chg - min_oi) * 0.02)
        )

        # LONG: break above `hi` with OI confirming and taker flow aligned
        if exp.close > hi and buy >= 0.5:
            plan = self.build_levels(
                direction=Direction.LONG,
                cand=exp,
                atr_value=atr_trig,
                entry_price=hi,
                invalidation=hi - self._p("invalidation_atr_mult", 0.9) * atr_trig,
                sl_buffer_atr=0.0,
                tick=tick,
            )
            plan = _anchor_stop(
                plan,
                stop=lo + self._p("sl_boundary_buffer_atr", 0.1) * atr_trig,
                direction=Direction.LONG,
                tick=tick,
                atr_value=atr_trig,
            )
            return self.finalize(
                snap=snap,
                direction=Direction.LONG,
                plan=plan,
                confidence=confidence,
                reasons=reasons,
                metadata={"range_hi": hi, "range_lo": lo, "atr_pct": pct},
            )

        # SHORT: break below `lo`
        if exp.close < lo and buy <= 0.5:
            plan = self.build_levels(
                direction=Direction.SHORT,
                cand=exp,
                atr_value=atr_trig,
                entry_price=lo,
                invalidation=lo + self._p("invalidation_atr_mult", 0.9) * atr_trig,
                sl_buffer_atr=0.0,
                tick=tick,
            )
            plan = _anchor_stop(
                plan,
                stop=hi - self._p("sl_boundary_buffer_atr", 0.1) * atr_trig,
                direction=Direction.SHORT,
                tick=tick,
                atr_value=atr_trig,
            )
            return self.finalize(
                snap=snap,
                direction=Direction.SHORT,
                plan=plan,
                confidence=confidence,
                reasons=reasons,
                metadata={"range_hi": hi, "range_lo": lo, "atr_pct": pct},
            )
        return None


def _anchor_stop(
    plan: LevelPlan, *, stop: float, direction: Direction, tick: float, atr_value: float
) -> LevelPlan:
    """Re-anchor the stop to the opposite compression boundary (volatility-anchored SL).

    The re-anchored stop may never be TIGHTER than the level already produced by
    `build_levels`; if it is, the original (already risk-floored) stop is kept.
    """
    snapped = round_price(stop, tick)
    if direction is Direction.LONG and snapped > plan.stop_loss:
        return plan
    if direction is Direction.SHORT and snapped < plan.stop_loss:
        return plan
    risk = abs(plan.entry_price - snapped)
    floor = 0.25 * atr_value
    if risk < floor:
        snapped = round_price(
            plan.entry_price - floor if direction is Direction.LONG else plan.entry_price + floor,
            tick,
        )
        risk = abs(plan.entry_price - snapped)
    if risk <= 0:
        return plan
    rr = abs(plan.tp2 - plan.entry_price) / risk
    return LevelPlan(
        entry_price=plan.entry_price,
        entry_zone_low=plan.entry_zone_low,
        entry_zone_high=plan.entry_zone_high,
        stop_loss=snapped,
        invalidation=plan.invalidation,
        risk=risk,
        tp1=plan.tp1,
        tp2=plan.tp2,
        tp3=plan.tp3,
        tp4=plan.tp4,
        rr_tp2=rr,
    )


__all__ = ["VolatilityCompressionBreakout"]
