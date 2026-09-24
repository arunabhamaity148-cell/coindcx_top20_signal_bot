"""Hard risk gates plus persistent *advisory* capital accounting.

The bot is signal-only. Risk accounting therefore has two sources:
1. lifecycle events that free an open-signal slot; and
2. operator-recorded realised outcomes, because the bot cannot observe private fills/PnL.

Sizing is calculated from stop distance (cash-at-stop risk), not from notional alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.config import AppConfig
from app.core.models import Grade, StrategyCandidate
from app.core.timeutils import now_ms


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""
    expiry_min: int = 45
    cooldown_min: int = 60
    position_multiplier: float = 1.0
    details: Mapping[str, float] = field(default_factory=dict)


@dataclass
class RiskState:
    open_signals: dict[str, int] = field(default_factory=dict)
    open_groups: dict[str, str] = field(default_factory=dict)
    group_counts: dict[str, int] = field(default_factory=dict)
    daily_signal_count: dict[str, int] = field(default_factory=dict)
    daily_realised_r: dict[str, float] = field(default_factory=dict)
    last_signal_ms: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def today(reference_ms: int | None = None) -> str:
        if reference_ms is None:
            return datetime.now(UTC).date().isoformat()
        return datetime.fromtimestamp(reference_ms / 1000.0, tz=UTC).date().isoformat()


@dataclass
class RiskEngine:
    cfg: AppConfig
    state: RiskState = field(default_factory=RiskState)
    state_path: Path | None = None

    def __post_init__(self) -> None:
        if self.state_path is not None:
            self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        assert self.state_path is not None
        try:
            if not self.state_path.exists():
                return
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.state = RiskState(
                open_signals={str(k): int(v) for k, v in raw.get("open_signals", {}).items()},
                open_groups={str(k): str(v) for k, v in raw.get("open_groups", {}).items()},
                group_counts={str(k): int(v) for k, v in raw.get("group_counts", {}).items()},
                daily_signal_count={str(k): int(v) for k, v in raw.get("daily_signal_count", {}).items()},
                daily_realised_r={str(k): float(v) for k, v in raw.get("daily_realised_r", {}).items()},
                last_signal_ms={str(k): int(v) for k, v in raw.get("last_signal_ms", {}).items()},
            )
        except (OSError, ValueError, TypeError) as exc:
            # Persisted risk state corruption is a fail-closed condition.
            raise RuntimeError(f"risk state cannot be loaded safely: {exc}") from exc

    def _persist(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "open_signals": self.state.open_signals,
            "open_groups": self.state.open_groups,
            "group_counts": self.state.group_counts,
            "daily_signal_count": self.state.daily_signal_count,
            "daily_realised_r": self.state.daily_realised_r,
            "last_signal_ms": self.state.last_signal_ms,
            "written_ms": now_ms(),
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

    # ------------------------------------------------------------------ checks
    def check(
        self,
        *,
        symbol: str,
        group: str,
        grade: Grade,
        rr_tp2: float,
        now_ms: int,
        requested_expiry_min: int | None = None,
    ) -> RiskDecision:
        if rr_tp2 < self.cfg.risk.min_rr_tp2:
            return RiskDecision(False, f"R:R {rr_tp2:.2f} below minimum {self.cfg.risk.min_rr_tp2:.2f}")
        max_expiry = self._expiry_for(grade)
        if requested_expiry_min is None:
            expiry = max_expiry
        else:
            expiry = int(requested_expiry_min)
            if expiry <= 0:
                return RiskDecision(False, "strategy expiry must be positive")
            if expiry > max_expiry:
                return RiskDecision(False, f"strategy expiry {expiry}m exceeds risk maximum {max_expiry}m for {grade.value}")
        if len(self.state.open_signals) >= self.cfg.risk.max_concurrent_signals:
            return RiskDecision(False, f"max concurrent signals ({self.cfg.risk.max_concurrent_signals}) reached")
        if symbol in self.state.open_signals:
            return RiskDecision(False, f"a signal is already live for {symbol} (duplicate/anti-chase)")
        day = RiskState.today(now_ms)
        if self.state.daily_signal_count.get(day, 0) >= self.cfg.risk.max_daily_signals:
            return RiskDecision(False, f"daily signal cap ({self.cfg.risk.max_daily_signals}) reached")
        if self.state.daily_realised_r.get(day, 0.0) <= -abs(self.cfg.risk.max_daily_loss_r):
            return RiskDecision(False, f"daily loss cap ({self.cfg.risk.max_daily_loss_r}R) reached from recorded outcomes")
        cooldown = self.cfg.risk.cooldown_min_per_symbol
        last = self.state.last_signal_ms.get(symbol)
        if last is not None and (now_ms - last) < cooldown * 60_000:
            remaining = cooldown - (now_ms - last) / 60_000
            return RiskDecision(False, f"cooldown active for {symbol} ({remaining:.0f} min remaining)")
        if group and self.state.group_counts.get(group, 0) >= self.cfg.risk.max_per_group:
            return RiskDecision(False, f"correlation group '{group}' already at {self.cfg.risk.max_per_group} concurrent signals")
        multiplier = 1.0 if grade is Grade.B else 0.75 if grade is Grade.A else 1.0
        return RiskDecision(
            True, "ok", expiry_min=expiry, cooldown_min=cooldown,
            position_multiplier=multiplier,
            details={"rr_tp2": rr_tp2, "open": float(len(self.state.open_signals))},
        )

    def register(self, *, symbol: str, group: str, now_ms: int) -> None:
        if symbol in self.state.open_signals:
            raise RuntimeError(f"cannot register duplicate live signal for {symbol}")
        self.state.open_signals[symbol] = now_ms
        if group:
            self.state.open_groups[symbol] = group
            self.state.group_counts[group] = self.state.group_counts.get(group, 0) + 1
        else:
            self.state.open_groups.pop(symbol, None)
        day = RiskState.today(now_ms)
        self.state.daily_signal_count[day] = self.state.daily_signal_count.get(day, 0) + 1
        self.state.last_signal_ms[symbol] = now_ms
        self._persist()

    def close(
        self, *, symbol: str, group: str = "", realised_r: float | None = None, now_ms: int | None = None
    ) -> None:
        existed = symbol in self.state.open_signals
        self.state.open_signals.pop(symbol, None)
        stored_group = self.state.open_groups.pop(symbol, None)
        if existed and stored_group:
            self.state.group_counts[stored_group] = max(0, self.state.group_counts.get(stored_group, 0) - 1)
            if self.state.group_counts[stored_group] == 0:
                self.state.group_counts.pop(stored_group, None)
        elif existed and group and group != stored_group:
            # A group mismatch is suspicious; never decrement an unrelated group.
            raise RuntimeError(f"risk lifecycle group mismatch for {symbol}: {stored_group!r} != {group!r}")
        if realised_r is not None:
            day = RiskState.today(now_ms)
            self.state.daily_realised_r[day] = self.state.daily_realised_r.get(day, 0.0) + float(realised_r)
        self._persist()

    def record_manual_outcome(self, *, realised_r: float, now_ms: int) -> None:
        """Record operator-observed realised R for the daily loss guard.

        This is the only honest way to enforce a realised-PnL cap in a signal-only bot that
        has no private exchange account connection.
        """
        day = RiskState.today(now_ms)
        self.state.daily_realised_r[day] = self.state.daily_realised_r.get(day, 0.0) + float(realised_r)
        self._persist()

    def record_paper_outcome(self, *, symbol: str, group: str, realised_r: float, now_ms: int) -> None:
        self.close(symbol=symbol, group=group, realised_r=realised_r, now_ms=now_ms)

    # ------------------------------------------------------------------ sizing
    def advisory_size(
        self, *, price: float, instrument, stop_distance: float, multiplier: float = 1.0
    ) -> dict[str, float | str | bool]:
        """Advisory-only cash-at-stop sizing for linear USDT contracts."""
        if price <= 0 or stop_distance <= 0:
            raise ValueError("price and stop_distance must be positive")
        from app.utils.rounding import round_qty

        sizing = self.cfg.risk.sizing
        equity = float(sizing.get("reference_equity_usdt", 1000.0))
        risk_pct = float(sizing.get("risk_per_trade_pct", 1.0)) * multiplier
        risk_budget = equity * risk_pct / 100.0
        unit_value = float(getattr(instrument, "unit_contract_value", 1.0))
        quanto = float(getattr(instrument, "quanto_multiplier", 1.0))
        if getattr(instrument, "inverse", False):
            per_contract_loss = unit_value * quanto * abs(1.0 / price - 1.0 / (price + stop_distance))
        else:
            per_contract_loss = stop_distance * unit_value * quanto
        if per_contract_loss <= 0:
            raise ValueError("unable to derive positive per-contract stop-loss risk")
        raw_qty = risk_budget / per_contract_loss
        qty = round_qty(raw_qty, instrument.quantity_increment)
        notional = qty * price * unit_value * quanto
        cash_risk = qty * per_contract_loss
        if qty < instrument.min_trade_size or notional < instrument.min_notional:
            honoured = False
        else:
            honoured = True
        return {
            "advisory_qty": qty,
            "advisory_notional_usdt": round(notional, 4),
            "risk_budget_usdt": round(risk_budget, 4),
            "cash_at_stop_risk_usdt": round(cash_risk, 4),
            "per_contract_stop_risk_usdt": round(per_contract_loss, 8),
            "stop_distance": float(stop_distance),
            "min_trade_size": instrument.min_trade_size,
            "min_notional": instrument.min_notional,
            "honours_minimums": honoured,
            "sizing_model": "stop_distance_cash_risk",
        }

    def _expiry_for(self, grade: Grade) -> int:
        mapping = {"A+": "A_plus", "A": "A", "B": "B"}
        return self.cfg.risk.expiry_for_grade(mapping.get(grade.value, "default"))

    def snapshot(self, now_ms: int) -> dict[str, object]:
        day = RiskState.today(now_ms)
        return {
            "open_signals": dict(self.state.open_signals),
            "open_groups": dict(self.state.open_groups),
            "max_concurrent": self.cfg.risk.max_concurrent_signals,
            "daily_signals": self.state.daily_signal_count.get(day, 0),
            "max_daily_signals": self.cfg.risk.max_daily_signals,
            "daily_realised_r": self.state.daily_realised_r.get(day, 0.0),
            "max_daily_loss_r": self.cfg.risk.max_daily_loss_r,
        }


def rr_tp2_for(candidate: StrategyCandidate) -> float:
    risk = abs(candidate.entry_price - candidate.stop_loss)
    if risk <= 0:
        return 0.0
    tp2 = candidate.metadata.get("tp2") if candidate.metadata else None
    if tp2 is None:
        tp2 = candidate.tp2
    return abs(float(tp2) - candidate.entry_price) / risk


__all__ = ["RiskDecision", "RiskEngine", "RiskState", "rr_tp2_for"]
