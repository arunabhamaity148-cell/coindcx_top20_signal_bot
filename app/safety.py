"""Signal-only safety assertions and static audit.

This module is the enforcement point for the non-negotiable invariant:

    THE PROCESS NEVER HOLDS AN EXCHANGE TRADING KEY AND CANNOT TRADE.

It provides:
  * `assert_signal_only(cfg)`   - runtime boot gate (env + config level)
  * `scan_forbidden_capabilities(root)` - static source scan used by tests/CI and by
    `scripts/validate_config.py` so that a future edit cannot quietly add trading code.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import SafetyViolation

# Names that would indicate an order-placement / account-mutation capability.
FORBIDDEN_PATTERNS: tuple[str, ...] = (
    r"\bplace_order\s*\(",
    r"\bcreate_order\s*\(",
    r"\bnew_order\s*\(",
    r"\bcancel_order\s*\(",
    r"\bmodify_order\s*\(",
    r"\bcreate_order_list\s*\(",
    r"\bclose_position\s*\(",
    r"\bclose_all_positions\s*\(",
    r"\bset_leverage\s*\(",
    r"\bchange_leverage\s*\(",
    r"\bwithdraw\s*\(",
    r"\btransfer_funds\s*\(",
    r"\bplace_futures_order\b",
    r"\bfutures_create_order\b",
    r"\bcreateMarketOrder\b",
    r"\bcreateLimitOrder\b",
    # private/signed REST paths on either venue
    r"/exchange/v1/orders/create",
    r"/exchange/v1/derivatives/futures/orders/create",
    r"/fapi/v1/order",
    r"/fapi/v1/batchOrders",
    r"/fapi/v1/leverage",
    r"/fapi/v1/allOpenOrders",
)

# Files allowed to contain the words above because they DEFINE the guard.
_ALLOWLISTED_FILES = {
    "app/safety.py",
    "scripts/validate_config.py",
    "tests/safety/test_no_trading_capability.py",
    "docs/SECURITY.md",
    "docs/TROUBLESHOOTING.md",
    "README.md",
}

_TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh", ".md", ".env.example"}
_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "venv",
    ".venv",
    "node_modules",
    "dist",
    "build",
}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    pattern: str
    text: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return (
            f"{self.path}:{self.line}: forbidden capability '{self.pattern}' -> {self.text.strip()}"
        )


def _iter_text_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in _TEXT_SUFFIXES or path.name.startswith(".env"):
            yield path


def scan_forbidden_capabilities(
    root: Path | str, *, extra_patterns: Sequence[str] = ()
) -> list[Violation]:
    """Static scan for trading capability. Returns every violation found (never raises)."""
    root = Path(root)
    patterns = [re.compile(p) for p in (*FORBIDDEN_PATTERNS, *extra_patterns)]
    violations: list[Violation] = []
    for path in _iter_text_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in _ALLOWLISTED_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - defensive
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # A comment that merely *names* a forbidden capability is documentation.
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            for pattern in patterns:
                if pattern.search(line):
                    violations.append(Violation(rel, lineno, pattern.pattern, line))
    return violations


def assert_no_trading_credentials(env: Mapping[str, str], forbidden: Sequence[str]) -> None:
    present = [name for name in forbidden if env.get(name)]
    if present:
        raise SafetyViolation(
            "exchange trading credentials detected in the environment: "
            f"{sorted(present)} - SIGNAL ENGINE MUST NOT START"
        )


def assert_signal_only(
    cfg,
    *,
    env: Mapping[str, str] | None = None,
    root: Path | str | None = None,
    scan_source: bool = True,
) -> None:
    """Boot gate. Raises SafetyViolation on ANY violation (fail-closed)."""
    import os

    env = dict(os.environ if env is None else env)

    if cfg.system.mode != "signal_only":
        raise SafetyViolation(f"system.mode is '{cfg.system.mode}', must be 'signal_only'")
    if not cfg.system.fail_closed:
        raise SafetyViolation("system.fail_closed must be true")
    if cfg.veto.override_allowed:
        raise SafetyViolation("veto.override_allowed must be false")
    if not cfg.system.forbidden_capabilities:
        raise SafetyViolation("system.forbidden_capabilities must not be empty")

    assert_no_trading_credentials(env, cfg.system.forbidden_credential_env)

    if scan_source:
        from app.config import repo_root

        root = Path(root) if root else repo_root()
        violations = scan_forbidden_capabilities(root)
        if violations:
            detail = "; ".join(str(v) for v in violations[:5])
            raise SafetyViolation(f"order-capable code detected in the source tree: {detail}")


def safety_report(
    cfg, *, env: Mapping[str, str] | None = None, root: Path | str | None = None
) -> dict[str, object]:  # pragma: no cover - reporting
    import os

    env = dict(os.environ if env is None else env)
    from app.config import repo_root

    root = Path(root) if root else repo_root()
    violations = scan_forbidden_capabilities(root)
    creds = [name for name in cfg.system.forbidden_credential_env if env.get(name)]
    return {
        "mode": cfg.system.mode,
        "fail_closed": cfg.system.fail_closed,
        "veto_override_allowed": cfg.veto.override_allowed,
        "trading_credentials_present": creds,
        "forbidden_code_violations": [str(v) for v in violations],
        "signal_only": not violations and not creds and cfg.system.mode == "signal_only",
    }


__all__ = [
    "FORBIDDEN_PATTERNS",
    "Violation",
    "assert_no_trading_credentials",
    "assert_signal_only",
    "safety_report",
    "scan_forbidden_capabilities",
]
