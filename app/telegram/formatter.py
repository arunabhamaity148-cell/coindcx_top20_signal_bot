"""Telegram message templates (FINAL_DELIVERABLE §S, §T).

Hard format rules: signal FIRST, reason SECOND, max 3 bullets, max 10-second budget.
Every WHY bullet must be a MEASURED observation (percentile, % change, ratio, level) -
never an adjective. The output vocabulary is fixed:
🟢 LONG · 🔴 SHORT · 🟡 WATCH · ⚪ NO TRADE · 🟠 DANGER · 🚨 EMERGENCY CLOSE
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.models import Direction
from app.core.timeutils import hhmm_utc
from app.signals.models import DangerAlert, NoTradeRecord, Signal
from app.signals.tpsl import management_plan

DIRECTION_BADGE = {Direction.LONG: "🟢 LONG", Direction.SHORT: "🔴 SHORT"}


def _fmt(value: float, tick: float | None = None) -> str:
    if value is None:
        return "-"
    if tick and tick >= 1:
        return f"{value:,.2f}"
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def format_signal(signal: Signal, *, max_chars: int = 1024) -> str:
    lines = [
        "🚨 SIGNAL",
        "",
        f"💎 PAIR: {signal.symbol}",
        "📊 MODE: SIGNAL_ONLY / LIMIT_ORDER",
        DIRECTION_BADGE.get(signal.direction, signal.direction.value),
        "",
        f"📍 LIMIT ENTRY: {_fmt(signal.entry_price)}",
        f"🎯 ENTRY ZONE: {_fmt(signal.entry_zone_low)} – {_fmt(signal.entry_zone_high)}",
        f"🛑 SL: {_fmt(signal.stop_loss)}",
        f"💰 TP1: {_fmt(signal.tp1)}",
        f"💰 TP2: {_fmt(signal.tp2)}",
        f"💰 TP3: {_fmt(signal.tp3)}",
        f"💰 TP4: {_fmt(signal.tp4)}",
        f"📊 R:R: {signal.rr_tp2:.1f}",
        f"🔥 CONFIDENCE: {signal.grade.value} ({signal.confidence:.2f})",
        f"📰 NEWS: {signal.news_state.value}",
        f"🟦 BINANCE: {_fmt(signal.binance_price)}",
        f"🟩 COINDCX: {_fmt(signal.coindcx_price)}",
        f"📐 SPREAD/BASIS: {signal.spread_bps:.1f} bps / {signal.basis_bps:+.1f} bps",
        f"⏳ EXPIRY: {hhmm_utc(signal.expiry_ms)} UTC",
        f"🆔 SIGNAL ID: {signal.signal_id}",
    ]
    if signal.veto_status.value != "PASS":
        lines.insert(-2, f"🛡️ VETO: {signal.veto_status.value}")
    text = "\n".join(lines)
    return _clip(text, max_chars)


def format_why(signal: Signal, bullets: Sequence[str], *, max_chars: int = 1024) -> str:
    lines = ["🧠 WHY THIS SIGNAL?"]
    for bullet in list(bullets)[:3]:
        lines.append(f"• {bullet}")
    return _clip("\n".join(lines), max_chars)


def format_danger(alert: DangerAlert, *, max_chars: int = 1024) -> str:
    lines = [
        "🚨 DANGER",
        "⚠️ ACTION: CLOSE / REDUCE / EXIT MANUALLY",
        f"💎 PAIR: {alert.symbol}",
        f"❌ INVALIDATION: {_fmt(alert.invalidation)}",
        f"📉 CURRENT PRICE: {_fmt(alert.price)}",
    ]
    if alert.news_note:
        lines.append(f"📰 NEWS: {alert.news_note}")
    if alert.divergence_z is not None:
        lines.append(f"📐 BINANCE/COINDCX DIVERGENCE: z = {alert.divergence_z:+.2f}")
    lines.append(f"🧠 REASON: {' + '.join(alert.reasons[:3])}")
    lines.append(f"⏱️ TIME: {hhmm_utc(alert.issued_ms)} UTC")
    lines.append("NO AUTO-CLOSE. MANUAL ACTION REQUIRED.")
    return _clip("\n".join(lines), max_chars)


def format_no_trade(record: NoTradeRecord, *, max_chars: int = 1024) -> str:
    lines = ["⚪ NO TRADE", f"💎 PAIR: {record.symbol}", f"🧠 REASON: {record.reason[:200]}"]
    if record.feed_status:
        feed = ", ".join(f"{k}={v}" for k, v in sorted(record.feed_status.items()))
        lines.append(f"🔌 FEEDS: {feed}")
    lines.append(f"⏱️ TIME: {hhmm_utc(record.ts_ms)} UTC")
    return _clip("\n".join(lines), max_chars)


def format_watch(symbol: str, note: str, *, max_chars: int = 1024) -> str:
    return _clip("\n".join(["🟡 WATCH", f"💎 PAIR: {symbol}", f"🧠 {note[:400]}"]), max_chars)


def format_emergency_close(symbol: str, reason: str, *, max_chars: int = 1024) -> str:
    return _clip(
        "\n".join(
            [
                "🚨 EMERGENCY CLOSE",
                "⚠️ ACTION: EXIT MANUALLY",
                f"💎 PAIR: {symbol}",
                f"🧠 REASON: {reason[:400]}",
                "NO AUTO-CLOSE. MANUAL ACTION REQUIRED.",
            ]
        ),
        max_chars,
    )


def format_management(signal: Signal, *, max_chars: int = 1024) -> str:
    lines = [f"📋 MANAGEMENT PLAN — {signal.symbol} {signal.signal_id}"]
    lines.extend(f"• {line}" for line in management_plan())
    return _clip("\n".join(lines), max_chars)


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n… [truncated]"


class MessageFormatter:
    """Formatter facade used by the sender/queue."""

    def __init__(self, cfg, *, instrument_lookup=None):
        self.cfg = cfg
        self.instrument_lookup = instrument_lookup

    @property
    def max_chars(self) -> int:
        return int(self.cfg.telegram.max_chars)

    def signal(self, signal: Signal) -> str:
        return format_signal(signal, max_chars=self.max_chars)

    def why(self, signal: Signal, bullets: Sequence[str]) -> str:
        return format_why(signal, bullets, max_chars=self.max_chars)

    def danger(self, alert: DangerAlert) -> str:
        return format_danger(alert, max_chars=self.max_chars)

    def no_trade(self, record: NoTradeRecord) -> str:
        return format_no_trade(record, max_chars=self.max_chars)

    def watch(self, symbol: str, note: str) -> str:
        return format_watch(symbol, note, max_chars=self.max_chars)

    def emergency(self, symbol: str, reason: str) -> str:
        return format_emergency_close(symbol, reason, max_chars=self.max_chars)

    def management(self, signal: Signal) -> str:
        return format_management(signal, max_chars=self.max_chars)

    def validate(self, text: str) -> tuple[bool, list[str]]:
        """Cheap self-check used by tests: no secrets, within the char ceiling."""
        problems: list[str] = []
        token = getattr(self.cfg.telegram, "bot_token", "") or ""
        if token and token in text:
            problems.append("bot token leaked into a message body")
        if len(text) > self.max_chars:
            problems.append(f"message exceeds {self.max_chars} chars (self-imposed ceiling)")
        return (not problems), problems


__all__ = [
    "DIRECTION_BADGE",
    "MessageFormatter",
    "format_danger",
    "format_emergency_close",
    "format_management",
    "format_no_trade",
    "format_signal",
    "format_watch",
    "format_why",
]
