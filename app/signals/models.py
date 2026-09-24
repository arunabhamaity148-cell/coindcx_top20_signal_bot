"""Signal models (FINAL_DELIVERABLE §X: signals journal schema)."""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.models import Direction, Grade, NewsState, SignalState, VetoSeverity


@dataclass
class Signal:
    signal_id: str
    symbol: str
    direction: Direction
    grade: Grade
    confidence: float
    entry_price: float
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    tp4: float
    invalidation: float
    risk: float
    rr_tp2: float
    expiry_ms: int
    created_ms: int
    strategy_votes: Mapping[str, float] = field(default_factory=dict)
    veto_status: VetoSeverity = VetoSeverity.PASS
    veto_detail: str = ""
    news_state: NewsState = NewsState.CLEAR
    news_source: str = ""
    reasons: Sequence[str] = field(default_factory=tuple)
    atr: float = 0.0
    binance_price: float = 0.0
    coindcx_price: float = 0.0
    spread_bps: float = 0.0
    basis_bps: float = 0.0
    state: SignalState = SignalState.PENDING
    advisory_qty: float = 0.0
    advisory_notional: float = 0.0
    meta: Mapping[str, Any] = field(default_factory=dict)
    latency_ms: int = 0

    @staticmethod
    def new_id(now_ms: int) -> str:
        stamp = datetime.fromtimestamp(now_ms / 1000.0, tz=UTC).strftime("%Y%m%d")
        return f"CSB-{stamp}-{secrets.token_hex(3).upper()}"

    @property
    def is_expired(self) -> bool:
        return False  # expiry is evaluated by the lifecycle against a clock

    def expired_at(self, now_ms: int) -> bool:
        return now_ms >= self.expiry_ms

    def to_row(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "ts": self.created_ms,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "grade": self.grade.value,
            "confidence": round(self.confidence, 4),
            "entry": self.entry_price,
            "entry_zone_low": self.entry_zone_low,
            "entry_zone_high": self.entry_zone_high,
            "sl": self.stop_loss,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "tp3": self.tp3,
            "tp4": self.tp4,
            "rr": round(self.rr_tp2, 3),
            "expiry": self.expiry_ms,
            "binance_price": self.binance_price,
            "coindcx_price": self.coindcx_price,
            "spread_bps": round(self.spread_bps, 3),
            "basis_bps": round(self.basis_bps, 3),
            "news_state": self.news_state.value,
            "news_source": self.news_source,
            "strategy_votes": dict(self.strategy_votes),
            "veto_status": self.veto_status.value,
            "veto_detail": self.veto_detail,
            "reason": list(self.reasons),
            "state": self.state.value,
            "latency_ms": self.latency_ms,
            "advisory_qty": self.advisory_qty,
            "advisory_notional": self.advisory_notional,
        }

    def telegram_fields(self) -> dict[str, Any]:
        return {
            "pair": self.symbol,
            "direction": self.direction.value,
            "entry": self.entry_price,
            "zone_low": self.entry_zone_low,
            "zone_high": self.entry_zone_high,
            "sl": self.stop_loss,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "tp3": self.tp3,
            "tp4": self.tp4,
            "rr": self.rr_tp2,
            "grade": self.grade.value,
            "confidence": self.confidence,
            "news_state": self.news_state.value,
            "expiry_ms": self.expiry_ms,
            "signal_id": self.signal_id,
        }


@dataclass
class DangerAlert:
    symbol: str
    signal_id: str
    reasons: Sequence[str]
    price: float
    invalidation: float
    news_note: str = ""
    divergence_z: float | None = None
    issued_ms: int = 0

    def to_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "signal_id": self.signal_id,
            "price": self.price,
            "invalidation": self.invalidation,
            "reasons": list(self.reasons),
            "news_note": self.news_note,
            "divergence_z": self.divergence_z,
            "issued_ms": self.issued_ms,
        }


@dataclass
class NoTradeRecord:
    symbol: str
    reason: str
    feed_status: Mapping[str, str] = field(default_factory=dict)
    ts_ms: int = 0

    def to_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "reason": self.reason,
            "feed_status": dict(self.feed_status),
            "ts": self.ts_ms,
        }


__all__ = ["DangerAlert", "NoTradeRecord", "Signal"]
