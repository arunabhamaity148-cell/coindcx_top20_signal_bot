"""Monitoring journals facade (re-exported for the documented module path)."""

from __future__ import annotations

from app.database.repository import JournalRepository

# FINAL_DELIVERABLE §U/§X refers to `monitoring/journals.py`; the implementation lives in
# the database layer, and this alias keeps the documented import path valid.
Journals = JournalRepository

__all__ = ["JournalRepository", "Journals"]
