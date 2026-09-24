"""Clock helpers.

The cross-venue drift budget (1500 ms) is enforced here so the rule exists in exactly
one place; callers convert the raised error into NO TRADE.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from app.core.errors import ClockDriftError


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def hhmm_utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC).strftime("%H:%M")


def age_ms(ts_ms: int | None, reference_ms: int | None = None) -> int | None:
    if ts_ms is None:
        return None
    reference_ms = reference_ms if reference_ms is not None else now_ms()
    return reference_ms - ts_ms


def drift_ms(*timestamps: int) -> int:
    """Max pairwise absolute difference between venue timestamps."""
    values = [ts for ts in timestamps if ts is not None]
    if len(values) < 2:
        return 0
    return max(values) - min(values)


def assert_drift_within_budget(drift: int, budget_ms: int) -> int:
    """Raise (fail-closed) when venues disagree about 'now' by more than the budget."""
    if abs(drift) > budget_ms:
        raise ClockDriftError(f"clock drift {drift} ms exceeds budget {budget_ms} ms")
    return drift


def parse_iso8601_ms(value: str) -> int:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)
