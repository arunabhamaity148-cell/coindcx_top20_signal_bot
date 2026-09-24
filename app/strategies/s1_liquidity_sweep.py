"""S1 - Liquidity Sweep & Reclaim (LSR).

Spec (FINAL_DELIVERABLE §D):
  Regime: RANGE / CHOP / POST_EVENT · 5m structure, 1m trigger
  Setup: price sweeps a prior 3-bar swing extreme, then closes back inside
  Confirmation: taker-buy ratio > 0.55 (long) / < 0.45 (short); oi_chg_pct <= +0.5 %
                (the sweep was a stop-run, not a real build)
  Entry: LONG at ref_low + 0.15*ATR; SHORT at ref_high - 0.15*ATR
  Invalidation: the swept extreme itself (last.low long / last.high short)
  SL: invalidation -/+ 0.5*ATR
  TP1..TP4: 1R/2R/3R/5R; TP1 clamped to entry_high + 1*ATR
  Expiry 45 min · cooldown 60 min
"""

from __future__ import annotations

from app.core.mathx import stdev
from app.core.models import Candle, Direction, MarketSnapshot, StrategyCandidate
from app.strategies.base import Strategy, clamp_confidence


class LiquiditySweepReclaim(Strategy):
    id = "S1"
    name = "Liquidity Sweep & Reclaim"

    def analyze(self, snap: MarketSnapshot) -> StrategyCandidate | None:
        if not self.derivatives_ready(snap):
            return None
        swing_lookback = int(self._p("swing_lookback", 3))
        trigger = list(snap.series(self.params.extra.get("trigger_tf", "1m")))
        if not trigger:
            trigger = list(snap.series("5m"))
        if (
            not self.enough(trigger, self.cfg.strategy.min_candles)
            or len(trigger) < swing_lookback + 2
        ):
            return None
        atr_value = self.atr_of(trigger)
        if not atr_value or atr_value <= 0:
            return None

        last = trigger[-1]
        prior = trigger[-(swing_lookback + 1) : -1]
        if len(prior) < swing_lookback:
            return None
        ref_high = max(c.high for c in prior)
        ref_low = min(c.low for c in prior)
        ratio = last.taker_buy_ratio
        oi_chg = snap.derivatives.oi_chg_pct
        max_oi = self._p("max_oi_chg_pct", 0.5)
        tick = snap.instrument.price_increment
        long_ratio = self._p("taker_buy_ratio_long", 0.55)
        short_ratio = self._p("taker_sell_ratio_short", 0.45)

        # ---- LONG: sweep below `ref_low`, close back inside
        if last.low < ref_low and last.close > ref_low:
            if ratio is None or ratio <= long_ratio:
                return None
            if oi_chg is not None and oi_chg > max_oi:
                return None  # a real OI build means breakout, not a stop-run
            entry = ref_low + self._p("entry_offset_atr", 0.15) * atr_value
            plan = self.build_levels(
                direction=Direction.LONG,
                cand=last,
                atr_value=atr_value,
                entry_price=entry,
                invalidation=last.low,
                sl_buffer_atr=self._p("sl_buffer_atr", 0.5),
                tick=tick,
                tp1_clamp_high=entry + 0.25 * atr_value + self._p("tp1_clamp_atr", 1.0) * atr_value,
            )
            reasons = [
                f"swept prior {swing_lookback}-bar low {ref_low:.4f} and reclaimed "
                f"(close {last.close:.4f})",
                f"taker buy ratio {ratio:.2f} > {long_ratio:.2f}",
                (
                    f"OI {oi_chg:+.2f}% <= {max_oi:.2f}% (stop-run, not a build)"
                    if oi_chg is not None
                    else "OI change unavailable on this snapshot"
                ),
            ]
            return self.finalize(
                snap=snap,
                direction=Direction.LONG,
                plan=plan,
                confidence=self._confidence(ratio, oi_chg, trigger),
                reasons=reasons,
                metadata={"ref_low": ref_low, "taker_ratio": ratio, "tp2": plan.tp2},
            )

        # ---- SHORT: sweep above `ref_high`, close back inside
        if last.high > ref_high and last.close < ref_high:
            if ratio is None or ratio >= short_ratio:
                return None
            if oi_chg is not None and oi_chg > max_oi:
                return None
            entry = ref_high - self._p("entry_offset_atr", 0.15) * atr_value
            plan = self.build_levels(
                direction=Direction.SHORT,
                cand=last,
                atr_value=atr_value,
                entry_price=entry,
                invalidation=last.high,
                sl_buffer_atr=self._p("sl_buffer_atr", 0.5),
                tick=tick,
                tp1_clamp_low=entry - 0.25 * atr_value - self._p("tp1_clamp_atr", 1.0) * atr_value,
            )
            reasons = [
                f"swept prior {swing_lookback}-bar high {ref_high:.4f} and rejected "
                f"(close {last.close:.4f})",
                f"taker buy ratio {ratio:.2f} < {short_ratio:.2f}",
                (
                    f"OI {oi_chg:+.2f}% <= {max_oi:.2f}% (stop-run, not a build)"
                    if oi_chg is not None
                    else "OI change unavailable on this snapshot"
                ),
            ]
            return self.finalize(
                snap=snap,
                direction=Direction.SHORT,
                plan=plan,
                confidence=self._confidence(1 - ratio, oi_chg, trigger),
                reasons=reasons,
                metadata={"ref_high": ref_high, "taker_ratio": ratio, "tp2": plan.tp2},
            )
        return None

    def _confidence(self, flow: float, oi_chg: float | None, candles: list[Candle]) -> float:
        base = self.params.base_confidence + 0.10
        base += min(0.12, max(0.0, (flow - 0.5) * 0.5))
        if len(candles) >= 30:
            vol = stdev([c.close for c in candles[-30:]])
            last = candles[-1].close
            if last and vol / last > 0.01:
                base += 0.04
        if oi_chg is not None and abs(oi_chg) <= self._p("max_oi_chg_pct", 0.5):
            base += 0.03
        return clamp_confidence(base)


__all__ = ["LiquiditySweepReclaim"]
