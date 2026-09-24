"""CoinDCX public futures WebSocket - EXPLICITLY NOT AVAILABLE.

FINAL_DELIVERABLE §J / DESIGN_SPEC §5: the CoinDCX public futures WebSocket is
UNPROVEN (not documented). This module exists so the limitation is stated in code
rather than silently ignored: the system ships `CoinDCXPoller` (REST, `poll_sec: 2`)
as the fallback, and any attempt to open a socket raises rather than guessing a URL.
"""

from __future__ import annotations

from app.core.errors import DataUnavailableError

STATUS = "DOCUMENTED_NOT_USED"
REASON = (
    "CoinDCX public futures market-data WebSocket is documented, but this release intentionally "
    "uses the bounded REST poller as its deterministic fallback/data path. No private trading socket "
    "or undocumented message contract is used."
)


class CoinDCXWebSocketUnavailable(DataUnavailableError):
    """Raised when code attempts to use an undocumented CoinDCX futures socket."""


def connect(*args: object, **kwargs: object) -> None:
    raise CoinDCXWebSocketUnavailable(REASON)


def status() -> dict[str, str]:
    return {"venue": "COINDCX", "transport": "websocket", "status": STATUS, "reason": REASON,
            "fallback": "REST adaptive polling (app.data.coindcx.rest.CoinDCXPoller)"}


__all__ = ["CoinDCXWebSocketUnavailable", "REASON", "STATUS", "connect", "status"]
