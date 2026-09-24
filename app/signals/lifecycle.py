"""Signal lifecycle: PENDING -> ACTIVE -> EXPIRED / INVALIDATED / DANGER.

Rules enforced here:
  * a limit signal expires when the window closes; it NEVER converts to a market order;
  * anti-chase: no fill beyond the zone;
  * DANGER alerts are advisory only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from app.core.logging_setup import get_logger
from app.core.models import MarketSnapshot, SignalState
from app.core.timeutils import now_ms
from app.signals.danger import DangerAssessment, DangerLevel, DangerMonitor
from app.signals.expiry import EntryCheck, EntryVerdict, evaluate_entry, next_state
from app.signals.models import DangerAlert, Signal

log = get_logger(__name__)


@dataclass
class LifecycleUpdate:
    signal_id: str
    state: SignalState
    entry: EntryCheck | None = None
    danger: DangerAssessment | None = None

    @property
    def alert(self) -> DangerAlert | None:
        return self.danger.alert if self.danger else None


@dataclass
class SignalLifecycle:
    danger_monitor: DangerMonitor
    signals: dict[str, Signal] = field(default_factory=dict)
    danger_levels: dict[str, DangerLevel] = field(default_factory=dict)
    expired: list[str] = field(default_factory=list)
    on_state_change: Callable[[Signal, SignalState], None] | None = None

    def add(self, signal: Signal) -> None:
        self.signals[signal.signal_id] = signal
        signal.state = SignalState.PENDING
        self.danger_levels[signal.signal_id] = DangerLevel.NONE

    def remove(self, signal_id: str) -> None:
        self.signals.pop(signal_id, None)

    def active_symbols(self) -> tuple[str, ...]:
        return tuple(
            {
                s.symbol
                for s in self.signals.values()
                if s.state in (SignalState.PENDING, SignalState.ACTIVE, SignalState.DANGER)
            }
        )

    def live_signals(self) -> tuple[Signal, ...]:
        return tuple(
            s
            for s in self.signals.values()
            if s.state in (SignalState.PENDING, SignalState.ACTIVE, SignalState.DANGER)
        )

    def update(
        self,
        *,
        snap: MarketSnapshot,
        price: float,
        realized_vol: float | None = None,
        opposite_news: bool = False,
        reference_ms: int | None = None,
    ) -> list[LifecycleUpdate]:
        reference = reference_ms or now_ms()
        updates: list[LifecycleUpdate] = []
        for signal in list(self.signals.values()):
            if signal.symbol != snap.symbol:
                continue
            check = evaluate_entry(
                direction=signal.direction,
                price=price,
                zone_low=signal.entry_zone_low,
                zone_high=signal.entry_zone_high,
                invalidation=signal.invalidation,
                created_ms=signal.created_ms,
                expiry_ms=signal.expiry_ms,
                now_ms=reference,
                atr=signal.atr,
            )
            previous_state = signal.state
            signal.state = next_state(signal.state, check)
            if check.verdict is EntryVerdict.WAITING and reference >= signal.expiry_ms:
                # price never entered the zone in time -> EXPIRE, never chase
                signal.state = SignalState.EXPIRED
                check = EntryCheck(
                    EntryVerdict.EXPIRED,
                    "entry window closed; limit signal never chases",
                    must_expire=True,
                )

            assessment = self.danger_monitor.assess(
                signal=signal,
                snap=snap,
                price=price,
                realized_vol=realized_vol,
                opposite_news=opposite_news,
                reference_ms=reference,
            )
            if assessment.new_state is not None:
                if (
                    assessment.new_state is SignalState.INVALIDATED
                    and signal.state is not SignalState.ACTIVE
                ):
                    # PENDING signals are simply retired with the window
                    assessment = DangerAssessment(
                        level=assessment.level,
                        reasons=assessment.reasons,
                        alert=assessment.alert,
                        new_state=SignalState.EXPIRED,
                    )
                signal.state = assessment.new_state
            if signal.state is SignalState.EXPIRED:
                self.expired.append(signal.signal_id)
                self.signals.pop(signal.signal_id, None)
            if signal.state is not previous_state and self.on_state_change is not None:
                try:
                    self.on_state_change(signal, signal.state)
                except Exception as exc:
                    log.warning("lifecycle state-change callback failed: %s", exc)
            previous_level = self.danger_levels.get(signal.signal_id, DangerLevel.NONE)
            if assessment.level is not DangerLevel.NONE and self.danger_monitor.should_alert(
                previous_level, assessment.level
            ):
                self.danger_levels[signal.signal_id] = assessment.level
            updates.append(
                LifecycleUpdate(
                    signal_id=signal.signal_id, state=signal.state, entry=check, danger=assessment
                )
            )
        return updates

    def pending_danger_alerts(self, updates: Iterable[LifecycleUpdate]) -> list[DangerAlert]:
        return [u.danger.alert for u in updates if u.danger and u.danger.alert is not None]


__all__ = ["LifecycleUpdate", "SignalLifecycle"]
