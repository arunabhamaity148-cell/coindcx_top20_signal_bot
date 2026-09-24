"""DANGER / emergency advisory logic (FINAL_DELIVERABLE §R).

The bot re-evaluates every active signal continuously and can issue ADVISORY alerts.
It can NEVER close anything: the instruction is always `CLOSE / REDUCE / EXIT MANUALLY`
followed by `NO AUTO-CLOSE. MANUAL ACTION REQUIRED.`

Triggers: thesis invalidation · opposite HIGH/CRITICAL news · BTC regime reversal ·
abnormal Binance/CoinDCX divergence · extreme volatility spike · liquidity disappearance ·
structure break · liquidation-cascade risk · feed integrity failure · entry window expiry
with price outside the zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.config import AppConfig
from app.core.models import (
    BtcRegime,
    CascadeRisk,
    Direction,
    DivergenceClass,
    FeedState,
    MarketSnapshot,
    NewsState,
    SignalState,
)
from app.core.timeutils import now_ms
from app.signals.models import DangerAlert, Signal


class DangerLevel(str, Enum):
    NONE = "NONE"
    WATCH = "WATCH"  # 🟡
    DANGER = "DANGER"  # 🟠
    EMERGENCY = "EMERGENCY"  # 🚨


DANGER_REASONS = {
    "THESIS_INVALIDATION": "thesis invalidation breached",
    "OPPOSITE_NEWS": "opposite HIGH/CRITICAL news",
    "BTC_REVERSAL": "BTC regime reversal against the position",
    "DIVERGENCE": "abnormal Binance/CoinDCX divergence",
    "VOL_SPIKE": "extreme volatility spike",
    "LIQUIDITY": "liquidity disappearance",
    "STRUCTURE_BREAK": "structure break",
    "CASCADE": "liquidation-cascade risk",
    "FEED_FAILURE": "feed integrity failure",
    "EXPIRY_OUTSIDE_ZONE": "entry window expired with price outside the zone",
}


@dataclass
class DangerAssessment:
    level: DangerLevel
    reasons: tuple[str, ...] = ()
    alert: DangerAlert | None = None
    new_state: SignalState | None = None


@dataclass
class DangerMonitor:
    cfg: AppConfig

    def assess(
        self,
        *,
        signal: Signal,
        snap: MarketSnapshot,
        price: float | None = None,
        realized_vol: float | None = None,
        extreme_vol: float = 1.50,
        opposite_news: bool = False,
        structure_broken: bool = False,
        reference_ms: int | None = None,
    ) -> DangerAssessment:
        reference = reference_ms or now_ms()
        price = price if price is not None else snap.last_price
        reasons: list[str] = []
        level = DangerLevel.NONE

        # 1. thesis invalidation
        if signal.direction is Direction.LONG and price < signal.invalidation:
            reasons.append(DANGER_REASONS["THESIS_INVALIDATION"])
            level = DangerLevel.EMERGENCY
        if signal.direction is Direction.SHORT and price > signal.invalidation:
            reasons.append(DANGER_REASONS["THESIS_INVALIDATION"])
            level = DangerLevel.EMERGENCY

        # 2. stop-loss breach
        if signal.direction is Direction.LONG and price <= signal.stop_loss:
            reasons.append("stop level breached")
            level = DangerLevel.EMERGENCY
        if signal.direction is Direction.SHORT and price >= signal.stop_loss:
            reasons.append("stop level breached")
            level = DangerLevel.EMERGENCY

        # 3. opposite HIGH/CRITICAL news
        if opposite_news or snap.news_state is NewsState.BLOCK:
            reasons.append(DANGER_REASONS["OPPOSITE_NEWS"])
            level = max(level, DangerLevel.DANGER, key=_order)

        # 4. BTC regime reversal
        conflict = snap.btc_conflict or (
            (signal.direction is Direction.LONG and snap.btc_regime is BtcRegime.RISK_OFF_STRONG)
            or (signal.direction is Direction.SHORT and snap.btc_regime is BtcRegime.RISK_ON_STRONG)
        )
        if conflict:
            reasons.append(DANGER_REASONS["BTC_REVERSAL"])
            level = max(level, DangerLevel.DANGER, key=_order)

        # 5. abnormal divergence
        if snap.basis is not None:
            z = snap.basis.z
            if snap.basis.classification is DivergenceClass.EXTREME or (
                z is not None and abs(z) >= 3.0
            ):
                reasons.append(
                    f"{DANGER_REASONS['DIVERGENCE']} (z {z:+.2f})"
                    if z is not None
                    else DANGER_REASONS["DIVERGENCE"]
                )
                level = max(level, DangerLevel.DANGER, key=_order)

        # 6. volatility spike
        if realized_vol is not None and realized_vol > extreme_vol:
            reasons.append(f"{DANGER_REASONS['VOL_SPIKE']} (realized vol {realized_vol:.2f})")
            level = max(level, DangerLevel.DANGER, key=_order)

        # 7. liquidity disappearance
        from app.config import AppConfig as _AppConfig  # noqa: F401 - explicit import for clarity

        min_depth = float(self.cfg.veto.liquidity.get("min_depth_usd_within_50bps", 150_000.0))
        if snap.liquidity.depth_usd_min < min_depth / 2:
            reasons.append(
                f"{DANGER_REASONS['LIQUIDITY']} (depth {snap.liquidity.depth_usd_min:,.0f} USD)"
            )
            level = max(level, DangerLevel.DANGER, key=_order)

        # 8. cascade risk
        if snap.derivatives.cascade_risk is CascadeRisk.HIGH:
            reasons.append(DANGER_REASONS["CASCADE"])
            level = max(level, DangerLevel.WATCH, key=_order)

        # 9. structure break
        if structure_broken:
            reasons.append(DANGER_REASONS["STRUCTURE_BREAK"])
            level = max(level, DangerLevel.WATCH, key=_order)

        # 10. feed integrity failure
        degraded = {
            name: h.state.value
            for name, h in snap.feed_health.items()
            if h.state is not FeedState.HEALTHY and h.state is not FeedState.DEGRADED
        }
        if degraded:
            reasons.append(f"{DANGER_REASONS['FEED_FAILURE']} ({degraded})")
            level = max(level, DangerLevel.DANGER, key=_order)

        # 11. expiry with price outside the zone
        if reference >= signal.expiry_ms and not (
            signal.entry_zone_low <= price <= signal.entry_zone_high
        ):
            reasons.append(DANGER_REASONS["EXPIRY_OUTSIDE_ZONE"])
            level = max(level, DangerLevel.WATCH, key=_order)

        alert = None
        new_state = None
        if level is not DangerLevel.NONE:
            alert = DangerAlert(
                symbol=signal.symbol,
                signal_id=signal.signal_id,
                reasons=tuple(reasons),
                price=price,
                invalidation=signal.invalidation,
                news_note="" if snap.news_state is NewsState.CLEAR else snap.news_state.value,
                divergence_z=snap.basis.z if snap.basis else None,
                issued_ms=reference,
            )
            if level is DangerLevel.EMERGENCY:
                new_state = SignalState.INVALIDATED
            elif level is DangerLevel.DANGER:
                new_state = SignalState.DANGER
        return DangerAssessment(
            level=level, reasons=tuple(reasons), alert=alert, new_state=new_state
        )

    def should_alert(self, previous: DangerLevel, current: DangerLevel) -> bool:
        """Rate-limit: only escalate notifications, never repeat the same level."""
        return _order(current) > _order(previous)


def _order(level: DangerLevel) -> int:
    return {
        DangerLevel.NONE: 0,
        DangerLevel.WATCH: 1,
        DangerLevel.DANGER: 2,
        DangerLevel.EMERGENCY: 3,
    }[level]


def manual_instruction() -> str:
    return "CLOSE / REDUCE / EXIT MANUALLY"


__all__ = [
    "DANGER_REASONS",
    "DangerAssessment",
    "DangerLevel",
    "DangerMonitor",
    "manual_instruction",
]
