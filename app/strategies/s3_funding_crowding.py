"""S3 - Funding / Crowding Exhaustion Reversal (FCR).

Spec (FINAL_DELIVERABLE §D):
  Regime: any; strongest after event-driven cascades · 1h context, 5m trigger
  Setup: extreme funding (|z| > 2.0) + crowded OI, and structure failing to extend
  Entry: crowded longs fail at prior swing highs (short) / crowded shorts fail at
         lows (long); candle colour flip + structure non-extension
  Limit entry: 0.2*ATR beyond the last close toward the mean
  Invalidation: prior swing extreme · SL: extreme -/+ 0.6*ATR
  TP1 mechanically 1R (squeezes are fast) · Expiry 90 min

This engine FADES the aftermath; it never forecasts a cascade (spec §D, §M).
"""

from __future__ import annotations

from app.core.mathx import ema_last
from app.core.models import Direction, MarketSnapshot, StrategyCandidate
from app.strategies.base import Strategy, clamp_confidence


class FundingCrowdingExhaustion(Strategy):
    id = "S3"
    name = "Funding / Crowding Exhaustion Reversal"

    def analyze(self, snap: MarketSnapshot) -> StrategyCandidate | None:
        if not self.derivatives_ready(snap, funding=True):
            return None
        deriv = snap.derivatives
        fz = deriv.funding_z
        if fz is None or abs(fz) < self._p("min_abs_funding_z", 2.0):
            return None
        oi_rank = deriv.oi_pct_rank
        if oi_rank is None or oi_rank < self._p("min_oi_pct_rank", 0.85):
            return None

        context_tf = self.params.extra.get("context_tf", "1h")
        context = list(snap.series(context_tf))
        if not self.enough(context, 60):
            return None
        context_closes = [c.close for c in context]
        ctx_fast = ema_last(context_closes, int(self._p("context_ema_fast", 21)))
        ctx_slow = ema_last(context_closes, int(self._p("context_ema_slow", 55)))
        if ctx_fast is None or ctx_slow is None:
            return None

        trigger = list(snap.series(self.params.extra.get("trigger_tf", "5m")))
        if not self.enough(trigger, self.cfg.strategy.min_candles):
            return None
        atr_value = self.atr_of(trigger)
        if not atr_value or atr_value <= 0:
            return None
        last, prev = trigger[-1], trigger[-2]
        swing_high = max(c.high for c in trigger[-20:-1])
        swing_low = min(c.low for c in trigger[-20:-1])
        offset = self._p("entry_offset_atr", 0.2) * atr_value
        sl_buf = self._p("sl_buffer_atr", 0.6)
        tick = snap.instrument.price_increment

        # Crowded LONGS (positive funding) failing at swing highs -> SHORT (fade)
        if fz > 0 and (ctx_fast <= ctx_slow or context[-1].close < ctx_fast) and last.high >= swing_high * (1 - 0.0005) and last.close < prev.close:
            entry = last.close + offset  # limit sell above the market, toward the mean
            plan = self.build_levels(
                direction=Direction.SHORT,
                cand=last,
                atr_value=atr_value,
                entry_price=entry,
                invalidation=swing_high,
                sl_buffer_atr=sl_buf,
                tick=tick,
            )
            reasons = [
                f"funding z {fz:+.2f} (|z| > {self._p('min_abs_funding_z', 2.0):.1f}) - crowded longs",
                (
                    f"OI percentile {oi_rank:.2f} (crowded book)"
                    if oi_rank is not None
                    else "OI percentile unavailable"
                ),
                f"1h context EMA{int(self._p('context_ema_fast', 21))} {ctx_fast:.4f} vs EMA{int(self._p('context_ema_slow', 55))} {ctx_slow:.4f}; context not bullish against short",
                f"rejection at swing high {swing_high:.4f}; candle closed lower",
            ]
            return self.finalize(
                snap=snap,
                direction=Direction.SHORT,
                plan=plan,
                confidence=clamp_confidence(
                    self.params.base_confidence + min(0.18, (abs(fz) - 2.0) * 0.06)
                ),
                reasons=reasons,
                metadata={"funding_z": fz, "swing_high": swing_high},
            )

        # Crowded SHORTS (negative funding) failing at swing lows -> LONG (fade)
        if fz < 0 and (ctx_fast >= ctx_slow or context[-1].close > ctx_fast) and last.low <= swing_low * (1 + 0.0005) and last.close > prev.close:
            entry = last.close - offset  # limit buy below the market, toward the mean
            plan = self.build_levels(
                direction=Direction.LONG,
                cand=last,
                atr_value=atr_value,
                entry_price=entry,
                invalidation=swing_low,
                sl_buffer_atr=sl_buf,
                tick=tick,
            )
            reasons = [
                f"funding z {fz:+.2f} (|z| > {self._p('min_abs_funding_z', 2.0):.1f}) - crowded shorts",
                (
                    f"OI percentile {oi_rank:.2f} (crowded book)"
                    if oi_rank is not None
                    else "OI percentile unavailable"
                ),
                f"1h context EMA{int(self._p('context_ema_fast', 21))} {ctx_fast:.4f} vs EMA{int(self._p('context_ema_slow', 55))} {ctx_slow:.4f}; context not bearish against long",
                f"rejection at swing low {swing_low:.4f}; candle closed higher",
            ]
            return self.finalize(
                snap=snap,
                direction=Direction.LONG,
                plan=plan,
                confidence=clamp_confidence(
                    self.params.base_confidence + min(0.18, (abs(fz) - 2.0) * 0.06)
                ),
                reasons=reasons,
                metadata={"funding_z": fz, "swing_low": swing_low},
            )

        if deriv.cascade_risk.value == "HIGH":
            return None  # cascade context is a veto input, never an entry trigger
        return None


__all__ = ["FundingCrowdingExhaustion"]
