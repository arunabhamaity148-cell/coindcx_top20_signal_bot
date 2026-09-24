"""Structured logging with secret redaction.

Security checklist (FINAL_DELIVERABLE §AE): the Telegram token and any exchange key
must never reach a log line. Redaction is applied by a logging filter, so it holds even
if a caller passes a raw payload.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

REDACT_KEYS = (
    "token",
    "api_key",
    "apikey",
    "secret",
    "signature",
    "passphrase",
    "authorization",
    "bot_token",
)
_SECRET = "***REDACTED***"
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_SECRET if any(s in str(k).lower() for s in REDACT_KEYS) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _TELEGRAM_TOKEN_RE.sub(_SECRET, value)
    return value


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TELEGRAM_TOKEN_RE.sub(_SECRET, record.msg)
        if isinstance(record.args, tuple):
            # logging expects a tuple for printf-style multi-argument messages;
            # redact each argument without collapsing it into a list.
            record.args = tuple(redact(v) for v in record.args)
        elif record.args:
            record.args = redact(record.args)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("component", "symbol", "signal_id", "guard"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str)


def setup_logging(level: str | None = None, json_output: bool = True) -> None:
    level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(_RedactingFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
