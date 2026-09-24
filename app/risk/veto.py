"""Veto guard definitions + implementations (FINAL_DELIVERABLE §E, DESIGN_SPEC §4).

HARD BLOCK (non-overridable, `override_allowed: false` hard-coded):
  G1 Data Integrity            critical feed STALE/DISCONNECTED, healthy < min_sources,
                               stale > 3000 ms, clock drift > 1500 ms
  G2 Cross-Exchange Divergence |z| >= 3.0 or classification EXTREME
  G3 Liquidity / Slippage      spread > 12 bps, depth within +/-50 bps < $150k,
                               |imbalance| > 0.85
  G4 News Shock                news state BLOCK (CRITICAL), or 120 s blackout
  G5 Crowding                  |funding z| >= 2.5 AND OI pct rank >= 0.97

DEGRADE tier (confidence -0.15, never block outright): G6 Structure Invalidation,
G7 Extreme Volatility, G8 BTC Regime Conflict, G10 Orderbook Instability,
G11 Duplicate / Anti-Chase, G12 Spread Expansion.

Design principle (verified in code): every guard runs inside try/except and an
exception becomes a BLOCK - never a silent pass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.models import (
    Direction,
    DivergenceClass,
    FeedState,
    MarketSnapshot,
    NewsState,
    StrategyCandidate,
    VetoSeverity,
)

GUARD_NAMES = {
    "G1": "Data Integrity",
    "G2": "Cross-Exchange Divergence",
    "G3": "Liquidity / Slippage",
    "G4": "News Shock",
    "G5": "Crowding",
    "G6": "Structure Invalidation",
    "G7": "Extreme Volatility",
    "G8": "BTC Regime Conflict",
    "G10": "Orderbook Instability",
    "G11": "Duplicate / Anti-Chase",
    "G12": "Spread Expansion",
}

HARD_BLOCK_GUARDS = ("G1", "G2", "G3", "G4", "G5")
DEGRADE_GUARDS = ("G6", "G7", "G8", "G10", "G11", "G12")


class GuardTier(str, Enum):
    HARD_BLOCK = "HARD_BLOCK"
    DEGRADE = "DEGRADE"


@dataclass
class VetoResult:
    guard: str
    severity: VetoSeverity
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    tier: GuardTier = GuardTier.HARD_BLOCK

    @property
    def blocked(self) -> bool:
        return self.severity is VetoSeverity.BLOCK

    @property
    def degraded(self) -> bool:
        return self.severity is VetoSeverity.DEGRADE

    def row(self, symbol: str, ts_ms: int) -> dict[str, Any]:
        return {
            "ts": ts_ms,
            "symbol": symbol,
            "guard": self.guard,
            "severity": self.severity.value,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


def passed(guard: str, tier: GuardTier = GuardTier.HARD_BLOCK) -> VetoResult:
    return VetoResult(guard=guard, severity=VetoSeverity.PASS, reason="pass", tier=tier)


def blocked(guard: str, reason: str, **evidence: Any) -> VetoResult:
    tier = GuardTier.HARD_BLOCK if guard in HARD_BLOCK_GUARDS else GuardTier.DEGRADE
    return VetoResult(
        guard=guard, severity=VetoSeverity.BLOCK, reason=reason, evidence=evidence, tier=tier
    )


def degraded(guard: str, reason: str, **evidence: Any) -> VetoResult:
    return VetoResult(
        guard=guard,
        severity=VetoSeverity.DEGRADE,
        reason=reason,
        evidence=evidence,
        tier=GuardTier.DEGRADE,
    )


# --------------------------------------------------------------------------- guards


def g1_data_integrity(
    snap: MarketSnapshot,
    cfg: Mapping[str, Any],
    *,
    required_feeds: Sequence[str] = ("binance_rest", "binance_ws", "coindcx_rest"),
) -> VetoResult:
    guard = "G1"
    max_stale = int(cfg.get("max_tick_staleness_ms", 3000))
    min_healthy = int(cfg.get("min_sources_healthy", 2))
    max_drift = int(cfg.get("max_clock_drift_ms", 1500))

    unhealthy = {
        name: h.state.value
        for name, h in snap.feed_health.items()
        if h.state not in (FeedState.HEALTHY,)
    }
    if unhealthy:
        return blocked(guard, f"unhealthy feeds: {unhealthy}", unhealthy=unhealthy)
    healthy_count = len([h for h in snap.feed_health.values() if h.state is FeedState.HEALTHY])
    if healthy_count < min_healthy:
        return blocked(
            guard,
            f"healthy feeds {healthy_count} < min_sources_healthy {min_healthy}",
            healthy=healthy_count,
        )
    for name in required_feeds:
        health = snap.feed_health.get(name)
        if health is None:
            return blocked(guard, f"required feed '{name}' missing from the health registry")
        if health.age_ms is not None and health.age_ms > max_stale:
            return blocked(
                guard, f"{name} stale {health.age_ms} ms > {max_stale} ms", age_ms=health.age_ms
            )
    if abs(snap.clock_drift_ms) > max_drift:
        return blocked(
            guard,
            f"clock drift {snap.clock_drift_ms} ms > {max_drift} ms",
            drift_ms=snap.clock_drift_ms,
        )
    if cfg.get("require_orderbook", True):
        if not snap.binance_book.is_valid or not snap.coindcx_book.is_valid:
            return blocked(guard, "orderbook missing, crossed or empty")
    if cfg.get("require_instrument_metadata", True):
        if snap.instrument.price_increment <= 0:
            return blocked(guard, "CoinDCX instrument metadata invalid (tick size <= 0)")
    return passed(guard)


def g2_cross_exchange_divergence(snap: MarketSnapshot, cfg: Mapping[str, Any]) -> VetoResult:
    guard = "G2"
    block_z = float(cfg.get("block_z", 3.0))
    min_obs = int(cfg.get("require_min_history_obs", 60))
    basis = snap.basis
    if basis is None:
        return blocked(guard, "basis snapshot unavailable (normalization failed)")
    if basis.classification is DivergenceClass.EXTREME:
        return blocked(
            guard,
            f"divergence EXTREME (|z| >= {block_z})",
            z=basis.z,
            basis_bps=basis.basis_bps,
            observations=basis.observations,
        )
    if basis.observations < min_obs:
        return blocked(
            guard,
            f"insufficient basis history ({basis.observations} < {min_obs}) - fail-closed",
            observations=basis.observations,
        )
    if basis.z is not None and abs(basis.z) >= block_z:
        return blocked(guard, f"|z| {basis.z:+.2f} >= {block_z}", z=basis.z)
    return passed(guard)


def g3_liquidity_slippage(snap: MarketSnapshot, cfg: Mapping[str, Any]) -> VetoResult:
    guard = "G3"
    max_spread = float(cfg.get("max_spread_bps", 12.0))
    min_depth = float(cfg.get("min_depth_usd_within_50bps", 150_000.0))
    max_imb = float(cfg.get("max_book_imbalance", 0.85))
    liq = snap.liquidity
    if liq.spread_bps > max_spread:
        return blocked(
            guard,
            f"spread {liq.spread_bps:.2f} bps > {max_spread:.2f} bps",
            spread_bps=liq.spread_bps,
        )
    if liq.depth_usd_min < min_depth:
        return blocked(
            guard,
            f"depth {liq.depth_usd_min:,.0f} USD < {min_depth:,.0f} USD",
            depth_usd=liq.depth_usd_min,
        )
    if abs(liq.imbalance) > max_imb:
        return blocked(
            guard,
            f"|book imbalance| {abs(liq.imbalance):.2f} > {max_imb:.2f}",
            imbalance=liq.imbalance,
        )
    return passed(guard)


def g4_news_shock(
    snap: MarketSnapshot,
    cfg: Mapping[str, Any],
    *,
    blackout_active: bool = False,
    blocking_headline: str = "",
) -> VetoResult:
    guard = "G4"
    block_sev = set(cfg.get("block_severities", ["CRITICAL"]))
    min_sources = int(cfg.get("require_min_healthy_sources", 2))

    news_health = snap.feed_health.get("news")
    if news_health is not None and news_health.state is not FeedState.HEALTHY:
        return blocked(
            guard,
            f"news feed unhealthy ({news_health.state.value}): {news_health.detail}",
            news_state=news_health.state.value,
        )
    if snap.news_state is NewsState.BLOCK:
        return blocked(
            guard,
            f"news state BLOCK ({sorted(block_sev)}): {blocking_headline[:120]}",
            headline=blocking_headline,
        )
    if blackout_active:
        return blocked(guard, "CRITICAL news blackout window is active", headline=blocking_headline)
    if snap.feed_health.get("news") is None:
        return blocked(guard, f"news health unavailable (require >= {min_sources} healthy sources)")
    return passed(guard)


def g5_crowding(snap: MarketSnapshot, cfg: Mapping[str, Any]) -> VetoResult:
    guard = "G5"
    max_funding_z = float(cfg.get("max_funding_z", 2.5))
    max_oi_rank = float(cfg.get("max_oi_pct_rank", 0.97))
    require_both = bool(cfg.get("require_both_conditions", True))
    fz = snap.derivatives.funding_z
    oi_rank = snap.derivatives.oi_pct_rank
    if fz is None or oi_rank is None:
        # Missing derivative inputs cannot be used as an excuse to pass a crowding test.
        return blocked(guard, "crowding inputs unavailable (funding_z and/or oi_pct_rank missing)")
    funding_extreme = abs(fz) >= max_funding_z
    oi_extreme = oi_rank >= max_oi_rank
    condition = (
        (funding_extreme and oi_extreme) if require_both else (funding_extreme or oi_extreme)
    )
    if condition:
        return blocked(
            guard,
            f"crowded book: |funding z| {abs(fz):.2f} >= {max_funding_z} and "
            f"OI rank {oi_rank:.3f} >= {max_oi_rank}",
            funding_z=fz,
            oi_pct_rank=oi_rank,
        )
    return passed(guard)


# --------------------------------------------------------------------------- degrade tier


def g6_structure_invalidation(
    snap: MarketSnapshot, cfg: Mapping[str, Any], candidate: StrategyCandidate
) -> VetoResult:
    guard = "G6"
    if not cfg.get("enabled", True):
        return passed(guard, GuardTier.DEGRADE)
    price = snap.coindcx_book.mid or snap.binance_book.mid
    if price is None:
        return degraded(guard, "structure check skipped: no mid price")
    if candidate.direction is Direction.LONG and price < candidate.invalidation:
        return degraded(
            guard,
            f"structure invalidation breached: price {price:.4f} < invalidation "
            f"{candidate.invalidation:.4f}",
        )
    if candidate.direction is Direction.SHORT and price > candidate.invalidation:
        return degraded(
            guard,
            f"structure invalidation breached: price {price:.4f} > invalidation "
            f"{candidate.invalidation:.4f}",
        )
    return passed(guard, GuardTier.DEGRADE)


def g7_extreme_volatility(
    snap: MarketSnapshot,
    cfg: Mapping[str, Any],
    *,
    realized_vol: float | None,
    atr_pct_rank: float | None,
) -> VetoResult:
    guard = "G7"
    if not cfg.get("enabled", True):
        return passed(guard, GuardTier.DEGRADE)
    vol_max = float(cfg.get("realized_vol_max", 1.50))
    rank_max = float(cfg.get("atr_pct_rank_max", 0.995))
    reasons: list[str] = []
    if realized_vol is not None and realized_vol > vol_max:
        reasons.append(f"realized vol {realized_vol:.2f} > {vol_max}")
    if atr_pct_rank is not None and atr_pct_rank > rank_max:
        reasons.append(f"ATR percentile {atr_pct_rank:.3f} > {rank_max}")
    if reasons:
        return degraded(
            guard, "; ".join(reasons), realized_vol=realized_vol, atr_pct_rank=atr_pct_rank
        )
    return passed(guard, GuardTier.DEGRADE)


def g8_btc_regime_conflict(
    snap: MarketSnapshot, cfg: Mapping[str, Any], candidate: StrategyCandidate
) -> VetoResult:
    guard = "G8"
    if not cfg.get("enabled", True):
        return passed(guard, GuardTier.DEGRADE)
    if snap.btc_conflict:
        return degraded(
            guard,
            f"BTC regime conflict: BTC regime is {snap.btc_regime.value} "
            f"against a {candidate.direction.value} candidate",
        )
    return passed(guard, GuardTier.DEGRADE)


def g10_orderbook_instability(snap: MarketSnapshot, cfg: Mapping[str, Any]) -> VetoResult:
    guard = "G10"
    if not cfg.get("enabled", True):
        return passed(guard, GuardTier.DEGRADE)
    max_jump = float(cfg.get("mid_jump_bps_max", 20.0))
    jump = snap.book_mid_jump_bps
    if jump > max_jump:
        return degraded(
            guard, f"mid jumped {jump:.1f} bps in-window > {max_jump:.1f} bps", mid_jump_bps=jump
        )
    return passed(guard, GuardTier.DEGRADE)


def g11_duplicate_anti_chase(
    snap: MarketSnapshot,
    cfg: Mapping[str, Any],
    candidate: StrategyCandidate,
    *,
    live_signals: Sequence[str] = (),
    price: float | None = None,
) -> VetoResult:
    guard = "G11"
    if not cfg.get("enabled", True):
        return passed(guard, GuardTier.DEGRADE)
    if cfg.get("block_if_live_signal", True) and snap.symbol in set(live_signals):
        return degraded(guard, f"a live signal already exists for {snap.symbol}")
    if cfg.get("block_if_price_outside_zone", True) and price is not None:
        if not (candidate.entry_zone_low <= price <= candidate.entry_zone_high):
            return degraded(
                guard,
                f"price {price:.4f} outside entry zone "
                f"[{candidate.entry_zone_low:.4f}, {candidate.entry_zone_high:.4f}] - "
                "signal expires, never chases",
            )
    return passed(guard, GuardTier.DEGRADE)


def g12_spread_expansion(
    snap: MarketSnapshot, cfg: Mapping[str, Any], *, spread_limit_bps: float
) -> VetoResult:
    guard = "G12"
    if not cfg.get("enabled", True):
        return passed(guard, GuardTier.DEGRADE)
    multiple = float(cfg.get("spread_multiplier", 1.5))
    limit = spread_limit_bps * multiple
    if snap.liquidity.spread_bps > limit:
        return degraded(
            guard,
            f"spread {snap.liquidity.spread_bps:.2f} bps > {multiple:.1f}x limit ({limit:.2f} bps)",
            spread_bps=snap.liquidity.spread_bps,
        )
    return passed(guard, GuardTier.DEGRADE)


__all__ = [
    "DEGRADE_GUARDS",
    "GUARD_NAMES",
    "GuardTier",
    "HARD_BLOCK_GUARDS",
    "VetoResult",
    "blocked",
    "degraded",
    "g1_data_integrity",
    "g10_orderbook_instability",
    "g11_duplicate_anti_chase",
    "g12_spread_expansion",
    "g2_cross_exchange_divergence",
    "g3_liquidity_slippage",
    "g4_news_shock",
    "g5_crowding",
    "g6_structure_invalidation",
    "g7_extreme_volatility",
    "g8_btc_regime_conflict",
    "passed",
]
