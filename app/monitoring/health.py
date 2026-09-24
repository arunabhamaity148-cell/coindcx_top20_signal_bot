"""Health surface (FINAL_DELIVERABLE §AC dashboard data).

Surfaces: bot status · Binance connection · CoinDCX connection · news feed status ·
Telegram status · data latency · WS health · current BTC regime · active/expired signals ·
danger alerts · veto counts by guard · strategy performance · signal quality mix ·
false-signal rate · limit fill rate · average R · drawdown · 10-second WHY budget adherence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger
from app.core.models import FeedState
from app.core.timeutils import now_ms, to_iso

log = get_logger(__name__)


@dataclass
class HealthReport:
    status: str
    checks: Mapping[str, Mapping[str, Any]]
    blockers: tuple[str, ...] = ()
    generated_ms: int = field(default_factory=now_ms)

    @property
    def ok(self) -> bool:
        return not self.blockers and self.status in ("HEALTHY", "DRY_RUN")

    def render(self) -> str:
        lines = [f"BOT STATUS: {self.status} ({to_iso(self.generated_ms)})"]
        for name, detail in self.checks.items():
            state = detail.get("state", "?")
            extra = ", ".join(f"{k}={v}" for k, v in detail.items() if k != "state")
            lines.append(f"  {name}: {state}{(' — ' + extra) if extra else ''}")
        for blocker in self.blockers:
            lines.append(f"  BLOCKER: {blocker}")
        return "\n".join(lines)


def startup_health(
    *,
    cfg,
    binance,
    coindcx,
    news,
    telegram,
    repository,
    validation_report=None,
    clock_skew_ms: int = 0,
) -> HealthReport:
    """Startup safety check (master prompt §30). Any blocker means DO NOT signal."""
    checks: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []

    # 1 configuration + 2 top-20 validation + 9 safety invariants
    from app.safety import safety_report

    report = safety_report(cfg)
    checks["config"] = {
        "state": "OK",
        "mode": cfg.system.mode,
        "fail_closed": cfg.system.fail_closed,
        "pairs": len(cfg.pairs.pairs),
    }
    if not report["signal_only"]:
        blockers.append(f"signal-only invariants violated: {report}")

    # 3/4 venue validation
    if validation_report is None:
        blockers.append("CoinDCX/Binance pair validation has not run")
        checks["instrument_validation"] = {"state": "MISSING"}
    else:
        checks["instrument_validation"] = {
            "state": "OK" if validation_report.valid else "FAILED",
            "valid_pairs": len(validation_report.valid),
            "rejected": len(validation_report.rejected),
        }
        if not validation_report.valid:
            blockers.append("no pair passed CoinDCX/Binance instrument validation")

    # 5 feed health
    feed_health = {}
    feed_health.update(binance.health() if binance else {})
    feed_health.update(coindcx.health() if coindcx else {})
    if news is not None:
        feed_health["news"] = news.health()
    checks["feeds"] = {
        name: {"state": h.state.value, "age_ms": h.age_ms} for name, h in feed_health.items()
    }
    unhealthy = [n for n, h in feed_health.items() if h.state is not FeedState.HEALTHY]
    if unhealthy:
        blockers.append(f"feeds not healthy at startup: {unhealthy}")

    # 6 news health
    if news is None:
        checks["news"] = {"state": "DISABLED"}
    else:
        checks["news"] = {"state": news.health().state.value, "detail": news.health().detail}

    # 7 database health
    db_health = repository.health() if repository else {"state": "MISSING"}
    checks["database"] = db_health
    if db_health.get("state") != "HEALTHY":
        blockers.append("journal database is not writable")

    # 8 telegram health (dry-run is acceptable)
    tg = {
        "state": "DRY_RUN"
        if cfg.telegram.dry_run
        else ("CONFIGURED" if cfg.telegram.configured else "UNCONFIGURED")
    }
    if not cfg.telegram.dry_run and not cfg.telegram.configured:
        blockers.append("telegram dry_run is false but no TELEGRAM_BOT_TOKEN/CHAT_ID is configured")
    checks["telegram"] = tg

    # clock
    checks["clock"] = {
        "state": "OK" if abs(clock_skew_ms) <= cfg.normalization.max_clock_drift_ms else "DRIFT",
        "drift_ms": clock_skew_ms,
    }
    if abs(clock_skew_ms) > cfg.normalization.max_clock_drift_ms:
        blockers.append(f"system clock drift {clock_skew_ms} ms exceeds the budget")

    status = "HEALTHY" if not blockers else "BLOCKED"
    if cfg.telegram.dry_run and status == "HEALTHY":
        status = "DRY_RUN"
    return HealthReport(status=status, checks=checks, blockers=tuple(blockers))


__all__ = ["HealthReport", "startup_health"]
