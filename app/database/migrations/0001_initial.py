"""Baseline migration record (schema version 1).

The authoritative DDL lives in `app/database/models.py::DDL` so that a single source of
truth creates the journals. This module records the intent and is used by the migration
runner to decide whether a fresh database is required.
"""

from __future__ import annotations

VERSION = 1
DESCRIPTION = "initial journals: signals, vetoes, news, errors, performance, feed_health"


def upgrade(conn) -> None:
    from app.database.models import DDL

    conn.executescript(DDL)
    conn.commit()


__all__ = ["DESCRIPTION", "VERSION", "upgrade"]
