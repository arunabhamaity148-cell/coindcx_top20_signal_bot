"""VetoEngine: runs every guard, fail-closed, non-overridable.

Contract (FINAL_DELIVERABLE §E, DESIGN_SPEC §0.2):
  * every hard-block guard is a HARD BLOCK; no strategy score may override it;
  * ANY guard exception becomes a BLOCK;
  * missing data required by a guard becomes a BLOCK.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.config import AppConfig
from app.core.logging_setup import get_logger
from app.core.models import MarketSnapshot, StrategyCandidate, VetoSeverity
from app.core.timeutils import now_ms
from app.risk import veto as guards
from app.risk.veto import GuardTier, VetoResult

log = get_logger(__name__)


@dataclass
class VetoOutcome:
    blocked: bool
    degraded: bool
    hard_blocks: tuple[VetoResult, ...] = ()
    degrade: tuple[VetoResult, ...] = ()
    confidence_penalty: float = 0.0
    results: tuple[VetoResult, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.blocked

    def summary(self) -> str:
        if self.blocked:
            return "BLOCK: " + "; ".join(f"{r.guard} {r.reason}" for r in self.hard_blocks)
        if self.degraded:
            return "DEGRADE: " + "; ".join(f"{r.guard} {r.reason}" for r in self.degrade)
        return "PASS"

    def rows(self, symbol: str, ts_ms: int | None = None) -> list[dict[str, Any]]:
        ts = ts_ms or now_ms()
        return [r.row(symbol, ts) for r in self.results if r.severity is not VetoSeverity.PASS]


@dataclass
class VetoEngine:
    cfg: AppConfig
    results: list[VetoResult] = field(default_factory=list)

    # ------------------------------------------------------------------ API
    @property
    def hard_block_enabled(self) -> bool:
        return bool(self.cfg.veto.hard_block) and not bool(self.cfg.veto.override_allowed)

    def run(
        self,
        snap: MarketSnapshot,
        *,
        candidate: StrategyCandidate | None = None,
        realized_vol: float | None = None,
        atr_pct_rank: float | None = None,
        blackout_active: bool = False,
        blocking_headline: str = "",
        live_signals: Sequence[str] = (),
        price: float | None = None,
    ) -> VetoOutcome:
        """Execute every guard. A guard that raises is converted into a BLOCK."""
        results: list[VetoResult] = []
        errors: list[str] = []

        def _safe(guard_id: str, fn, *args, **kwargs) -> VetoResult:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                errors.append(f"{guard_id}: {exc}")
                log.error("veto guard %s raised: %s -> BLOCK (fail-closed)", guard_id, exc)
                return guards.blocked(guard_id, f"guard raised ({exc}) - blocked (fail-closed)")

        results.append(_safe("G1", guards.g1_data_integrity, snap, self.cfg.veto.data_integrity))
        results.append(
            _safe("G2", guards.g2_cross_exchange_divergence, snap, self.cfg.veto.divergence)
        )
        results.append(_safe("G3", guards.g3_liquidity_slippage, snap, self.cfg.veto.liquidity))
        results.append(
            _safe(
                "G4",
                guards.g4_news_shock,
                snap,
                self.cfg.veto.news_shock,
                blackout_active=blackout_active,
                blocking_headline=blocking_headline,
            )
        )
        results.append(_safe("G5", guards.g5_crowding, snap, self.cfg.veto.crowding))

        degrade_cfg = self.cfg.veto.degrade_tier
        if candidate is not None:
            results.append(
                _safe(
                    "G6",
                    guards.g6_structure_invalidation,
                    snap,
                    degrade_cfg.get("structure_invalidation", {}),
                    candidate,
                )
            )
            results.append(
                _safe(
                    "G11",
                    guards.g11_duplicate_anti_chase,
                    snap,
                    degrade_cfg.get("duplicate_anti_chase", {}),
                    candidate,
                    live_signals=live_signals,
                    price=price,
                )
            )
        results.append(
            _safe(
                "G7",
                guards.g7_extreme_volatility,
                snap,
                degrade_cfg.get("extreme_volatility", {}),
                realized_vol=realized_vol,
                atr_pct_rank=atr_pct_rank,
            )
        )
        if candidate is not None:
            results.append(
                _safe(
                    "G8",
                    guards.g8_btc_regime_conflict,
                    snap,
                    degrade_cfg.get("btc_regime_conflict", {}),
                    candidate,
                )
            )
        results.append(
            _safe(
                "G10",
                guards.g10_orderbook_instability,
                snap,
                degrade_cfg.get("orderbook_instability", {}),
            )
        )
        results.append(
            _safe(
                "G12",
                guards.g12_spread_expansion,
                snap,
                degrade_cfg.get("spread_expansion", {}),
                spread_limit_bps=float(self.cfg.veto.liquidity.get("max_spread_bps", 12.0)),
            )
        )

        self.results = results
        hard = tuple(r for r in results if r.tier is GuardTier.HARD_BLOCK and r.blocked)
        deg = tuple(r for r in results if r.degraded)
        # Spec §8/A: spread expansion escalates to BLOCK when configured to.
        if self.cfg.veto.escalation.get("spread_expansion_to_block", True):
            escalated = tuple(r for r in deg if r.guard == "G12")
            if escalated and not hard:
                hard = escalated
                deg = tuple(r for r in deg if r.guard != "G12")

        penalty = 0.0
        if deg:
            penalty = self.cfg.veto.degrade_penalty
        if hard and not self.hard_block_enabled:  # pragma: no cover - config invariant
            log.error("hard_block disabled in config; treating result as BLOCK anyway")

        return VetoOutcome(
            blocked=bool(hard),
            degraded=bool(deg),
            hard_blocks=hard,
            degrade=deg,
            confidence_penalty=penalty,
            results=tuple(results),
            errors=tuple(errors),
        )

    def status_text(self, outcome: VetoOutcome) -> str:
        if outcome.blocked:
            return "BLOCK"
        if outcome.degraded:
            return "DEGRADE"
        return "PASS"


__all__ = ["VetoEngine", "VetoOutcome"]
