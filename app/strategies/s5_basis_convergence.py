"""S5 - Cross-Venue Basis Convergence (XBC).

Spec (FINAL_DELIVERABLE §D):
  Regime: any EXCEPT extreme vol / news-block / degraded feeds
  Setup: quote-normalized CoinDCX-vs-Binance basis |z| in [2.0, 3.0) with healthy feeds
         and a CLEAR news state
  Limit entry: at the CoinDCX bid (rich) / ask (cheap), tick-snapped
  Invalidation: +/-1 % adverse move · SL: +/-1 % from reference
  TPs: convergence target = Binance reference mid, R-scaled · Expiry 20 min
  HARD RULE: |z| >= 3.0 is a VETO, never an opportunity
  Fee reality: valid ONLY if net edge after CoinDCX costs > 0
"""

from __future__ import annotations

from app.core.models import Direction, MarketSnapshot, NewsState, StrategyCandidate
from app.strategies.base import Strategy, clamp_confidence


class CrossVenueBasisConvergence(Strategy):
    id = "S5"
    name = "Cross-Venue Basis Convergence"

    def analyze(self, snap: MarketSnapshot) -> StrategyCandidate | None:
        basis = snap.basis
        if basis is None:
            return None
        if snap.news_state is not NewsState.CLEAR:
            return None
        z = basis.z
        min_z = self._p("min_abs_z", 2.0)
        max_z = self._p("max_abs_z", 3.0)
        if z is None or abs(z) < min_z or abs(z) >= max_z:
            return None
        # A single-venue convergence signal is NOT risk-free arbitrage. Require the
        # expected convergence distance to exceed verified costs plus an explicit
        # latency/adverse-selection buffer.
        min_buffer = self._p("latency_adverse_selection_bps", 2.0)
        expected_edge_bps = abs(basis.binance_usd_mid - basis.coindcx_usd_mid) / basis.binance_usd_mid * 1e4
        if expected_edge_bps <= basis.effective_cost_bps + min_buffer:
            return None
        min_probability = self._p("min_convergence_probability", 0.55)
        convergence_probability = basis.convergence_probability
        if convergence_probability is None or convergence_probability < min_probability:
            return None
        expected_capture = basis.expected_capture_bps
        min_expected_capture = self._p("minimum_expected_net_capture_bps", 1.0)
        if expected_capture is None or expected_capture < min_expected_capture:
            return None
        if self.params.extra.get("require_net_edge_positive", True) and basis.net_basis_bps <= 0:
            return None

        trigger = list(snap.series(self.params.extra.get("trigger_tf", "1m"))) or list(
            snap.series("5m")
        )
        if len(trigger) < 30:
            return None
        atr_value = self.atr_of(trigger)
        if not atr_value or atr_value <= 0:
            return None
        tick = snap.instrument.price_increment

        binance_mid = basis.binance_usd_mid
        coindcx_book = snap.coindcx_book
        sl_pct = self._p("sl_pct", 1.0)

        if basis.basis_bps > 0:  # CoinDCX rich -> sell CoinDCX, converge down
            entry = coindcx_book.best_bid
            if entry is None:
                return None
            direction = Direction.SHORT
            invalidation = entry * (1 + sl_pct / 100.0)
        else:  # CoinDCX cheap -> buy CoinDCX, converge up
            entry = coindcx_book.best_ask
            if entry is None:
                return None
            direction = Direction.LONG
            invalidation = entry * (1 - sl_pct / 100.0)

        risk = abs(entry - invalidation)
        reason_floor = 0.25 * atr_value
        if risk < reason_floor:
            risk = reason_floor
            invalidation = entry - risk if direction is Direction.LONG else entry + risk

        # TPs: convergence target = Binance reference mid, R-scaled toward it
        target = binance_mid
        distance = abs(target - entry)
        if distance <= 0:
            return None
        r_multiples = self.tp_multiples
        tps = []
        for multiple in r_multiples:
            scaled = entry + (1.0 if target > entry else -1.0) * min(distance, risk * multiple)
            tps.append(scaled)
        if direction is Direction.LONG and not (invalidation < entry < target):
            return None
        if direction is Direction.SHORT and not (invalidation > entry > target):
            return None

        plan = _convergence_plan(
            entry=entry,
            invalidation=invalidation,
            stop=invalidation,
            tps=tps,
            tick=tick,
            atr_value=atr_value,
        )
        if plan is None or plan.rr_tp2 < self.min_rr_tp2:
            return None

        reasons = [
            f"basis z {z:+.2f} within [{min_z:.1f}, {max_z:.1f}) - convergence, not a veto band",
            f"basis {basis.basis_bps:+.2f} bps vs effective cost {basis.effective_cost_bps:.2f} bps "
            f"(net {basis.net_basis_bps:+.2f} bps)",
            f"empirical 1-step convergence probability {convergence_probability:.0%}; "
            f"expected net capture {expected_capture:.2f} bps",
            f"target = Binance reference mid {target:.4f}",
        ]
        candidate = self.finalize(
            snap=snap,
            direction=direction,
            plan=plan,
            confidence=clamp_confidence(
                self.params.base_confidence + 0.06 + min(0.12, (abs(z) - min_z) * 0.08)
            ),
            reasons=reasons,
            metadata={
                "basis_z": z,
                "net_basis_bps": basis.net_basis_bps,
                "expected_edge_bps": expected_edge_bps,
                "convergence_probability": convergence_probability,
                "expected_capture_bps": expected_capture,
            },
        )
        return candidate


def _convergence_plan(
    *,
    entry: float,
    invalidation: float,
    stop: float,
    tps: list[float],
    tick: float,
    atr_value: float,
):
    from app.core.models import Direction as D
    from app.strategies.base import LevelPlan
    from app.utils.rounding import ceil_price, floor_price, round_price

    entry_s = round_price(entry, tick)
    stop_s = round_price(stop, tick)
    snapped = [round_price(t, tick) for t in tps]
    for i, value in enumerate(snapped):
        if value == entry_s:
            snapped[i] = round_price(entry + (tick * 2 if tps[i] > entry else -tick * 2), tick)
    _direction = D.LONG if tps[-1] > entry else D.SHORT
    risk = abs(entry_s - stop_s)
    if risk <= 0:
        return None
    rr = abs(snapped[1] - entry_s) / risk
    half = 0.25 * atr_value
    zone_low = floor_price(entry_s - half, tick)
    zone_high = ceil_price(entry_s + half, tick)
    return LevelPlan(
        entry_price=entry_s,
        entry_zone_low=zone_low,
        entry_zone_high=zone_high,
        stop_loss=stop_s,
        invalidation=round_price(invalidation, tick),
        risk=risk,
        tp1=snapped[0],
        tp2=snapped[1],
        tp3=snapped[2],
        tp4=snapped[3],
        rr_tp2=rr,
    )


__all__ = ["CrossVenueBasisConvergence"]
