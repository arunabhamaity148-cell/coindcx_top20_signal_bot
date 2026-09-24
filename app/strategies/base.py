"""Strategy base class + shared, deterministic construction helpers.

Every strategy is a pure function of `MarketSnapshot` -> `StrategyCandidate | None`.
No strategy may call an exchange, mutate state, or bypass the VetoEngine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from app.config import AppConfig, StrategyParams
from app.core.mathx import atr, atr_series, percentile_rank
from app.core.models import Candle, Direction, MarketSnapshot, StrategyCandidate
from app.utils.rounding import floor_price, round_price


@dataclass(frozen=True)
class LevelPlan:
    entry_price: float
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    invalidation: float
    risk: float
    tp1: float
    tp2: float
    tp3: float
    tp4: float
    rr_tp2: float


class StrategyError(Exception):
    """Raised inside a strategy. The pipeline treats it as NO TRADE for that engine."""


class Strategy(ABC):
    id: str = "S?"
    name: str = "strategy"

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        params = cfg.strategy.params(self.id)
        if params is None:
            raise StrategyError(f"strategy {self.id} has no configuration block")
        self.params: StrategyParams = params

    # ------------------------------------------------------------------ helpers
    @property
    def atr_period(self) -> int:
        return self.cfg.strategy.atr_period

    @property
    def min_rr_tp2(self) -> float:
        return self.cfg.strategy.min_rr_tp2

    @property
    def tp_multiples(self) -> tuple[float, ...]:
        return self.cfg.strategy.tp_r_multiples

    @property
    def risk_floor_atr_mult(self) -> float:
        return self.cfg.strategy.risk_floor_atr_mult

    def _p(self, key: str, default: float) -> float:
        value = self.params.extra.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def atr_of(self, candles: Sequence[Candle]) -> float | None:
        return atr(list(candles), self.atr_period)

    def atr_percentile(self, candles: Sequence[Candle]) -> float | None:
        series = atr_series(list(candles), self.atr_period)
        lookback = self.cfg.strategy.atr_percentile_lookback
        if not series:
            return None
        window = series[-lookback:] if len(series) > lookback else series
        return percentile_rank(window, window[-1])

    def enough(self, candles: Sequence[Candle], minimum: int | None = None) -> bool:
        return len(candles) >= (minimum or self.cfg.strategy.min_candles)

    def derivatives_ready(self, snap: MarketSnapshot, *, funding: bool = False) -> bool:
        """Require timestamped derivatives data for strategies that consume it.

        S1/S2/S4 require fresh OI; S3 additionally requires fresh funding. S5 does not
        depend on derivatives and therefore does not call this helper.
        """
        ref = snap.ts_ms
        d = snap.derivatives
        live_snapshot = "binance_oi" in snap.feed_health or "binance_mark" in snap.feed_health

        oi_ts = d.oi_ts_ms
        # Backward-compatible offline fixtures may omit component timestamps.  They are
        # accepted only when the snapshot does not advertise the live derivative feed keys.
        if oi_ts is None and not live_snapshot:
            oi_ts = d.ts_ms
        if oi_ts is None or ref < oi_ts or ref - oi_ts > 15 * 60_000:
            return False

        if funding:
            funding_ts = d.funding_ts_ms
            if funding_ts is None and not live_snapshot:
                funding_ts = d.ts_ms
            if funding_ts is None or ref < funding_ts or ref - funding_ts > 45 * 60_000:
                return False
        return True

    def build_levels(
        self,
        *,
        direction: Direction,
        cand: Candle,
        atr_value: float,
        entry_price: float,
        invalidation: float,
        sl_buffer_atr: float,
        tick: float,
        tp1_clamp_high: float | None = None,
        tp1_clamp_low: float | None = None,
        risk_override: float | None = None,
        stop_override: float | None = None,
    ) -> LevelPlan:
        """Risk-floor enforcement + monotonic, tick-snapped TP ladder.

        risk = |entry - SL|, floored at `risk_floor_atr_mult * ATR` so a too-tight stop
        cannot manufacture absurd R multiples (spec §P).
        """
        if direction is Direction.LONG:
            stop_loss = (float(stop_override) if stop_override is not None
                         else invalidation - sl_buffer_atr * atr_value)
            risk = entry_price - stop_loss
        else:
            stop_loss = (float(stop_override) if stop_override is not None
                         else invalidation + sl_buffer_atr * atr_value)
            risk = stop_loss - entry_price

        floor = self.risk_floor_atr_mult * atr_value
        if risk_override is not None and risk_override > 0 and stop_override is None:
            risk = risk_override
            stop_loss = entry_price - risk if direction is Direction.LONG else entry_price + risk
        elif risk < floor:
            risk = floor
            stop_loss = entry_price - risk if direction is Direction.LONG else entry_price + risk

        if risk <= 0:
            raise StrategyError("computed risk is non-positive")

        r1, r2, r3, r4 = self.tp_multiples
        sign = 1.0 if direction is Direction.LONG else -1.0
        tps = [entry_price + sign * risk * r for r in (r1, r2, r3, r4)]
        if tp1_clamp_high is not None and direction is Direction.LONG:
            tps[0] = min(tps[0], tp1_clamp_high)
        if tp1_clamp_low is not None and direction is Direction.SHORT:
            tps[0] = max(tps[0], tp1_clamp_low)

        snapped = [round_price(t, tick) for t in tps]
        entry_snapped = round_price(entry_price, tick)
        stop_snapped = round_price(stop_loss, tick)

        if direction is Direction.LONG:
            if not (stop_snapped < entry_snapped < min(snapped)):
                raise StrategyError("impossible LONG ladder (SL < entry < TP1 violated)")
        elif not (stop_snapped > entry_snapped > max(snapped)):
            raise StrategyError("impossible SHORT ladder (SL > entry > TP1 violated)")

        rr = abs(snapped[1] - entry_snapped) / abs(entry_snapped - stop_snapped)
        half_zone = 0.25 * atr_value * sign
        zone_low = round_price(min(entry_snapped, entry_snapped - half_zone), tick)
        zone_high = round_price(max(entry_snapped, entry_snapped - half_zone), tick)
        inval = round_price(invalidation, tick)
        return LevelPlan(
            entry_price=entry_snapped,
            entry_zone_low=floor_price(zone_low, tick),
            entry_zone_high=zone_high,
            stop_loss=stop_snapped,
            invalidation=inval,
            risk=abs(entry_snapped - stop_snapped),
            tp1=snapped[0],
            tp2=snapped[1],
            tp3=snapped[2],
            tp4=snapped[3],
            rr_tp2=rr,
        )

    def finalize(
        self,
        *,
        snap: MarketSnapshot,
        direction: Direction,
        plan: LevelPlan,
        confidence: float,
        reasons: Sequence[str],
        metadata: dict[str, float] | None = None,
    ) -> StrategyCandidate | None:
        if plan.rr_tp2 < self.min_rr_tp2:
            return None
        if not self.enough(snap.series("5m")):
            return None
        return StrategyCandidate(
            strategy_id=self.id,
            symbol=snap.symbol,
            direction=direction,
            confidence=max(0.0, min(1.0, confidence)),
            entry_price=plan.entry_price,
            entry_zone_low=plan.entry_zone_low,
            entry_zone_high=plan.entry_zone_high,
            invalidation=plan.invalidation,
            stop_loss=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            tp3=plan.tp3,
            tp4=plan.tp4,
            rr_tp2=plan.rr_tp2,
            atr=0.0
            if not snap.series(str(self.params.extra.get("trigger_tf", "5m")))
            else (atr(list(snap.series(str(self.params.extra.get("trigger_tf", "5m")))), self.atr_period) or 0.0),
            expiry_min=self.params.expiry_min,
            reasons=tuple(reasons),
            correlation_group=self.params.correlation_group,
            metadata=metadata or {},
            atr_timeframe=str(self.params.extra.get("trigger_tf", "5m")),
            evidence_channels=tuple(self.params.extra.get("evidence_channels", ())),
        )

    @abstractmethod
    def analyze(self, snap: MarketSnapshot) -> StrategyCandidate | None:
        """Return a candidate or None. Raising is equivalent to None (pipeline catches)."""


def clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, value))


__all__ = ["LevelPlan", "Strategy", "StrategyError", "clamp_confidence"]
