"""Strategy registry: builds the enabled S1..S5 set from configuration.

A strategy that raises is recorded and skipped - it never produces a signal
("Guard exception -> BLOCK (fail closed)" applies to engines the same way).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import AppConfig
from app.core.logging_setup import get_logger
from app.core.models import MarketSnapshot, StrategyCandidate
from app.strategies.base import Strategy
from app.strategies.s1_liquidity_sweep import LiquiditySweepReclaim
from app.strategies.s2_volatility_compression import VolatilityCompressionBreakout
from app.strategies.s3_funding_crowding import FundingCrowdingExhaustion
from app.strategies.s4_oi_trend import OIConfirmedTrendContinuation
from app.strategies.s5_basis_convergence import CrossVenueBasisConvergence

log = get_logger(__name__)

BUILDERS: dict[str, type[Strategy]] = {
    "S1": LiquiditySweepReclaim,
    "S2": VolatilityCompressionBreakout,
    "S3": FundingCrowdingExhaustion,
    "S4": OIConfirmedTrendContinuation,
    "S5": CrossVenueBasisConvergence,
}


@dataclass
class StrategyRegistry:
    cfg: AppConfig
    strategies: dict[str, Strategy] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for sid in self.cfg.strategy.enabled:
            builder = BUILDERS.get(sid)
            if builder is None:
                continue
            try:
                self.strategies[sid] = builder(self.cfg)
            except Exception as exc:
                self.errors[sid] = str(exc)
                log.error("strategy %s failed to initialise: %s", sid, exc)

    def __len__(self) -> int:
        return len(self.strategies)

    def ids(self) -> tuple[str, ...]:
        return tuple(self.strategies)

    def run(self, snap: MarketSnapshot) -> tuple[tuple[StrategyCandidate, ...], dict[str, str]]:
        candidates: list[StrategyCandidate] = []
        failures: dict[str, str] = {}
        for sid, strategy in self.strategies.items():
            try:
                result = strategy.analyze(snap)
            except Exception as exc:
                failures[sid] = str(exc)
                log.warning("strategy %s raised on %s: %s", sid, snap.symbol, exc)
                continue
            if result is not None:
                candidates.append(result)
        return tuple(candidates), failures


__all__ = ["BUILDERS", "StrategyRegistry"]
