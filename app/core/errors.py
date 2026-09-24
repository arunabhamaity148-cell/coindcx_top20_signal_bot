"""Exception hierarchy.

Design rule (DESIGN_SPEC §0/§7): anything that cannot be answered from verified data
must be raised, and the caller must convert it into NO TRADE. There is no "assume"
exception in this hierarchy.
"""

from __future__ import annotations


class SignalBotError(Exception):
    """Base class for every error raised by this system."""


class FailClosedError(SignalBotError):
    """Raised when the system must refuse to emit a signal (fail-closed)."""


class ConfigError(SignalBotError):
    """Configuration is missing, malformed, or violates an invariant."""


class SafetyViolation(FailClosedError):
    """A signal-only invariant would be broken (e.g. a trading key is present)."""


class DataUnavailableError(FailClosedError):
    """A required data item does not exist / was never published by the venue."""


class StaleDataError(FailClosedError):
    """A feed exceeded its staleness budget."""


class ClockDriftError(FailClosedError):
    """Venue timestamps disagree by more than the configured drift budget."""


class NormalizationError(FailClosedError):
    """Cross-venue prices could not be made comparable."""


class SymbolMappingError(FailClosedError):
    """A pair exists on one venue but not on the other / not tradable."""


class VetoGuardError(SignalBotError):
    """A veto guard raised. The VetoEngine converts this into a BLOCK."""
