"""BTC market-wide regime filter (master prompt §15, FINAL_DELIVERABLE §N).

BTC is evaluated on structure, volatility, OI, funding, liquidation stress, basis and
impulse; if BTC invalidates the proposed direction the candidate is BLOCKED or DEGRADED
per configuration. `UNKNOWN` is fail-closed: the configured action (default BLOCK)
applies, because a filter that cannot be evaluated must not silently permit a signal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.config import AppConfig
from app.core.logging_setup import get_logger
from app.core.mathx import atr, ema_last, percentile_rank, realized_vol, slope
from app.core.models import (
    BtcRegime,
    Candle,
    CascadeRisk,
    DerivativesSnapshot,
    Direction,
    MarketSnapshot,
)

log = get_logger(__name__)


@dataclass
class BtcRegimeResult:
    regime: BtcRegime
    conflict: bool
    action: str
    reasons: tuple[str, ...] = ()


@dataclass
class BtcRegimeEngine:
    cfg: AppConfig

    def evaluate(
        self,
        *,
        candles_1h: Sequence[Candle],
        derivatives: DerivativesSnapshot | None,
        direction: Direction | None = None,
        basis_z: float | None = None,
    ) -> BtcRegimeResult:
        btc = self.cfg.btc_regime
        reasons: list[str] = []
        if len(candles_1h) < btc.structure_lookback:
            return BtcRegimeResult(
                BtcRegime.UNKNOWN,
                True,
                btc.unknown_action,
                ("insufficient BTC history for a regime classification",),
            )

        closes = [c.close for c in candles_1h]
        e_fast = ema_last(closes, btc.ema_fast)
        e_slow = ema_last(closes, btc.ema_slow)
        atr_value = atr(list(candles_1h), self.cfg.strategy.atr_period)
        if e_fast is None or e_slow is None or not atr_value:
            return BtcRegimeResult(
                BtcRegime.UNKNOWN, True, btc.unknown_action, ("BTC indicators unavailable",)
            )

        trend_slope = slope(closes[-20:])
        rv = realized_vol(closes, periods_per_year=24 * 365, window=min(24 * 30, len(closes) - 1))
        rv_series = _rolling_vol(closes)
        vol_rank = percentile_rank(rv_series, rv) if rv_series and rv is not None else None

        oi_rank = derivatives.oi_pct_rank if derivatives else None
        funding_z = derivatives.funding_z if derivatives else None
        cascade = derivatives.cascade_risk if derivatives else CascadeRisk.LOW

        bullish = e_fast > e_slow and trend_slope > 0
        bearish = e_fast < e_slow and trend_slope < 0
        strong_move = abs(closes[-1] - closes[-2]) > btc.strong_trend_atr_mult * atr_value
        stressed = cascade is CascadeRisk.HIGH or (
            vol_rank is not None and vol_rank >= btc.vol_percentile_high
        )

        if bullish and not stressed:
            regime = BtcRegime.RISK_ON_STRONG
            reasons.append(f"BTC EMA{btc.ema_fast} > EMA{btc.ema_slow} with positive slope")
        elif bearish and not stressed:
            regime = BtcRegime.RISK_OFF_STRONG
            reasons.append(f"BTC EMA{btc.ema_fast} < EMA{btc.ema_slow} with negative slope")
        elif stressed:
            regime = BtcRegime.UNKNOWN if cascade is CascadeRisk.HIGH else BtcRegime.NEUTRAL
            reasons.append(
                f"BTC volatility/cascade stress (vol rank {vol_rank}, cascade {cascade.value})"
            )
        else:
            regime = BtcRegime.NEUTRAL
            reasons.append("BTC structure mixed/range-bound")

        if atr_value:
            reasons.append(f"BTC ATR {atr_value:.2f}; 1h close {closes[-1]:.2f}")
        if strong_move:
            reasons.append("BTC impulse bar exceeds the strong-trend ATR multiple")
        if funding_z is not None:
            reasons.append(f"BTC funding z {funding_z:+.2f}")
        if oi_rank is not None:
            reasons.append(f"BTC OI percentile {oi_rank:.2f}")
        if basis_z is not None:
            reasons.append(f"BTC cross-venue basis z {basis_z:+.2f}")

        conflict = False
        if direction is not None:
            conflict = (
                (direction is Direction.LONG and regime is BtcRegime.RISK_OFF_STRONG)
                or (direction is Direction.SHORT and regime is BtcRegime.RISK_ON_STRONG)
                or regime is BtcRegime.UNKNOWN
            )
        action = (
            btc.unknown_action
            if regime is BtcRegime.UNKNOWN
            else (btc.conflict_action if conflict else "ALLOW")
        )
        return BtcRegimeResult(regime, conflict, action, tuple(reasons))

    def apply(self, result: BtcRegimeResult) -> tuple[bool, bool]:
        """Return (block, degrade) for the configured action."""
        if result.action == "BLOCK":
            return True, False
        if result.action == "DEGRADE":
            return False, True
        return False, False


def _rolling_vol(closes: Sequence[float], window: int = 24, lookback: int = 120) -> list[float]:
    rv: list[float] = []
    for end in range(window + 2, len(closes) + 1):
        value = realized_vol(closes[:end], periods_per_year=24 * 365, window=window)
        if value is not None:
            rv.append(value)
    return rv[-lookback:]


def btc_state_snapshot(snap: MarketSnapshot) -> dict[str, object]:
    """Compact BTC context used in journals and the Telegram footer."""
    return {
        "regime": snap.btc_regime.value,
        "conflict": snap.btc_conflict,
    }


__all__ = ["BtcRegimeEngine", "BtcRegimeResult", "btc_state_snapshot"]
