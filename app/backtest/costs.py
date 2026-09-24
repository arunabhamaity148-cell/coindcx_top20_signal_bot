"""Cost model for backtests (FINAL_DELIVERABLE §Y).

VERIFIED CoinDCX fees (from `/derivatives/futures/data/instrument`, 2026-09-22):
    maker 0.0236 %   taker 0.059 %
Plus spread, an estimated slippage of 3 bps and 250 ms latency by default.
Stops are assumed to execute as TAKER fills (worst case).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    maker_fee_pct: float = 0.0236
    taker_fee_pct: float = 0.059
    slippage_bps: float = 3.0
    spread_cost_bps: float = 2.0
    latency_ms: int = 250
    use_taker_for_stop: bool = True

    @property
    def maker_fee(self) -> float:
        return self.maker_fee_pct / 100.0

    @property
    def taker_fee(self) -> float:
        return self.taker_fee_pct / 100.0

    def round_trip_fee(self, *, entry_is_limit: bool = True, exit_is_stop: bool = False) -> float:
        entry_fee = self.maker_fee if entry_is_limit else self.taker_fee
        exit_fee = self.taker_fee if (exit_is_stop and self.use_taker_for_stop) else self.maker_fee
        return entry_fee + exit_fee

    def total_round_trip_frac(
        self,
        *,
        entry_is_limit: bool = True,
        exit_is_stop: bool = False,
        spread_bps: float | None = None,
    ) -> float:
        """Fraction of notional consumed by fees + spread + slippage on a round trip."""
        fees = self.round_trip_fee(entry_is_limit=entry_is_limit, exit_is_stop=exit_is_stop)
        spread = (self.spread_cost_bps if spread_bps is None else spread_bps) / 1e4
        slip = self.slippage_bps / 1e4
        return fees + spread + slip

    def apply_entry(self, price: float, *, side: str) -> float:
        """Passive LIMIT entry is filled at the requested price; fee is applied separately."""
        return float(price)

    def apply_exit(self, price: float, *, side: str, is_stop: bool = False) -> float:
        """Apply execution drag consistent with the order type.

        TP exits are modeled as passive LIMIT exits, so no additional spread/slippage is
        assumed beyond the explicit maker fee. Stop exits are modeled as taker fills with
        adverse slippage plus spread.
        """
        extra = (self.slippage_bps + self.spread_cost_bps) if is_stop else 0.0
        slip = price * extra / 1e4
        return price - slip if side == "sell" else price + slip

    def describe(self) -> dict[str, float]:
        return {
            "maker_fee_pct": self.maker_fee_pct,
            "taker_fee_pct": self.taker_fee_pct,
            "slippage_bps": self.slippage_bps,
            "spread_cost_bps": self.spread_cost_bps,
            "latency_ms": float(self.latency_ms),
            "round_trip_taker_pct": (self.taker_fee_pct * 2),
        }


__all__ = ["CostModel"]
