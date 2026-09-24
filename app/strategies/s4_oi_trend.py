"""S4 - OI-Confirmed Trend Continuation (OIT).

Spec (FINAL_DELIVERABLE §D):
  Regime: TREND_UP / TREND_DOWN / RISK_ON / RISK_OFF
  Timeframes: 1h/4h trend, 15m pullback, 5m trigger
  Setup: EMA21 vs EMA55 with slope sign; pullback to EMA21 that closes back on side
  Confirmation: OI > +0.5 % AND taker flow aligned (derivatives-confirmed, not price-only)
  Limit entry: at the fast EMA; ANTI-CHASE -> skip if price is > 0.5*ATR beyond it
  Invalidation: slow EMA · SL: min(last.low, e_fast - 0.6*ATR) - 0.2*ATR
  Expiry 45 min
"""

from __future__ import annotations

from app.core.mathx import ema_last, slope
from app.core.models import Direction, MarketSnapshot, StrategyCandidate
from app.strategies.base import Strategy, clamp_confidence


class OIConfirmedTrendContinuation(Strategy):
    id = "S4"
    name = "OI-Confirmed Trend Continuation"

    def analyze(self, snap: MarketSnapshot) -> StrategyCandidate | None:
        if not self.derivatives_ready(snap):
            return None
        trend_tf = self.params.extra.get("trend_tf", "4h")
        context_tf = self.params.extra.get("context_tf", "1h")
        pullback_tf = self.params.extra.get("pullback_tf", "15m")
        trend = list(snap.series(trend_tf))
        context = list(snap.series(context_tf))
        pullback = list(snap.series(pullback_tf))
        trigger = list(snap.series(self.params.extra.get("trigger_tf", "5m")))
        pullback_lookback = int(self._p("pullback_lookback_bars", 3))
        if (
            not self.enough(trend, 60)
            or not self.enough(context, 60)
            or not self.enough(pullback, 60)
            or not self.enough(trigger, self.cfg.strategy.min_candles)
        ):
            return None

        closes = [c.close for c in trend]
        fast_period = int(self._p("ema_fast", 21))
        slow_period = int(self._p("ema_slow", 55))
        e_fast = ema_last(closes, fast_period)
        e_slow = ema_last(closes, slow_period)
        if e_fast is None or e_slow is None:
            return None
        fast_slope = slope(closes[-10:])
        slow_slope = slope(closes[-20:])
        context_closes = [c.close for c in context]
        ctx_fast = ema_last(context_closes, fast_period)
        ctx_slow = ema_last(context_closes, slow_period)
        if ctx_fast is None or ctx_slow is None:
            return None
        oi_chg = snap.derivatives.oi_chg_pct
        if oi_chg is None or oi_chg < self._p("min_oi_chg_pct", 0.5):
            return None
        buy = snap.derivatives.taker_buy_sell_ratio
        # S4 explicitly requires derivatives-confirmed flow; missing taker flow is
        # not a neutral value and therefore cannot pass the continuation filter.
        if buy is None:
            return None

        pullback_closes = [c.close for c in pullback]
        pullback_fast = ema_last(pullback_closes, fast_period)
        if pullback_fast is None:
            return None
        recent_pullback = pullback[-pullback_lookback:]
        touched_long = any(c.low <= pullback_fast for c in recent_pullback)
        touched_short = any(c.high >= pullback_fast for c in recent_pullback)
        pullback_long_ok = touched_long and pullback[-1].close >= pullback_fast
        pullback_short_ok = touched_short and pullback[-1].close <= pullback_fast

        atr_value = self.atr_of(trigger)
        if not atr_value or atr_value <= 0:
            return None
        last = trigger[-1]
        tick = snap.instrument.price_increment
        anti_chase = self._p("anti_chase_atr", 0.5) * atr_value

        # ---- LONG trend
        if (e_fast > e_slow and fast_slope > 0 and slow_slope > 0
                and ctx_fast > ctx_slow and context[-1].close >= ctx_fast
                and buy >= 0.5 and pullback_long_ok):
            if abs(last.close - e_fast) > anti_chase:
                return None  # anti-chase: price already extended beyond the fast EMA
            if last.close < e_fast:
                return None  # pullback did not close back on the trend side
            invalidation = e_slow
            stop_ref = min(last.low, e_fast - self._p("sl_stop_atr_mult", 0.6) * atr_value)
            stop = stop_ref - self._p("sl_buffer_atr", 0.2) * atr_value
            plan = self.build_levels(
                direction=Direction.LONG,
                cand=last,
                atr_value=atr_value,
                entry_price=e_fast,
                invalidation=invalidation,
                sl_buffer_atr=self._p("sl_buffer_atr", 0.2),
                tick=tick,
                stop_override=stop,
            )
            reasons = [
                f"4h EMA{fast_period} {e_fast:.4f} > EMA{slow_period} {e_slow:.4f} with positive slope",
                f"1h context EMA{fast_period} {ctx_fast:.4f} > EMA{slow_period} {ctx_slow:.4f} and price above fast EMA",
                f"15m pullback touched EMA{fast_period} {pullback_fast:.4f} and closed back above",
                f"OI {oi_chg:+.2f}% >= {self._p('min_oi_chg_pct', 0.5):.2f}% (derivatives-confirmed)",
                (
                    f"taker buy ratio {buy:.2f} aligned"
                    if buy is not None
                    else "taker flow unavailable"
                ),
            ]
            return self.finalize(
                snap=snap,
                direction=Direction.LONG,
                plan=plan,
                confidence=clamp_confidence(
                    self.params.base_confidence + 0.07 + min(0.08, oi_chg * 0.01)
                ),
                reasons=reasons,
                metadata={"ema_fast": e_fast, "ema_slow": e_slow},
            )

        # ---- SHORT trend
        if (e_fast < e_slow and fast_slope < 0 and slow_slope < 0
                and ctx_fast < ctx_slow and context[-1].close <= ctx_fast
                and buy <= 0.5 and pullback_short_ok):
            if abs(last.close - e_fast) > anti_chase:
                return None
            if last.close > e_fast:
                return None
            invalidation = e_slow
            stop_ref = max(last.high, e_fast + self._p("sl_stop_atr_mult", 0.6) * atr_value)
            stop = stop_ref + self._p("sl_buffer_atr", 0.2) * atr_value
            plan = self.build_levels(
                direction=Direction.SHORT,
                cand=last,
                atr_value=atr_value,
                entry_price=e_fast,
                invalidation=invalidation,
                sl_buffer_atr=self._p("sl_buffer_atr", 0.2),
                tick=tick,
                stop_override=stop,
            )
            reasons = [
                f"4h EMA{fast_period} {e_fast:.4f} < EMA{slow_period} {e_slow:.4f} with negative slope",
                f"1h context EMA{fast_period} {ctx_fast:.4f} < EMA{slow_period} {ctx_slow:.4f} and price below fast EMA",
                f"15m pullback touched EMA{fast_period} {pullback_fast:.4f} and closed back below",
                f"OI {oi_chg:+.2f}% >= {self._p('min_oi_chg_pct', 0.5):.2f}% (derivatives-confirmed)",
                (
                    f"taker buy ratio {buy:.2f} aligned"
                    if buy is not None
                    else "taker flow unavailable"
                ),
            ]
            return self.finalize(
                snap=snap,
                direction=Direction.SHORT,
                plan=plan,
                confidence=clamp_confidence(
                    self.params.base_confidence + 0.07 + min(0.08, oi_chg * 0.01)
                ),
                reasons=reasons,
                metadata={"ema_fast": e_fast, "ema_slow": e_slow},
            )
        return None


__all__ = ["OIConfirmedTrendContinuation"]
