"""Journal repository: SQLite + rotating JSONL mirrors.

Reconstruction contract: every field needed to rebuild a signal is persisted, including
the Binance and CoinDCX snapshots, spread, basis, news state, strategy votes, veto states
and the full payload. Nothing that would let a signal be silently rewritten is stored
without its evidence.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging_setup import get_logger, redact
from app.core.timeutils import now_ms
from app.database.models import DDL, JSONL_FILES, SCHEMA_VERSION

log = get_logger(__name__)


@dataclass
class JournalRepository:
    sqlite_path: str = "logs/signal_journal.sqlite"
    jsonl_dir: str = "logs"
    jsonl_mirror: bool = True
    _conn: sqlite3.Connection | None = None
    _jsonl_handles: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        path = Path(self.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(DDL)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()
        if self.jsonl_mirror:
            Path(self.jsonl_dir).mkdir(parents=True, exist_ok=True)
        log.info("journal opened at %s (schema v%s)", path, SCHEMA_VERSION)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None
        for handle in self._jsonl_handles.values():
            try:
                handle.close()
            except Exception:
                pass
        self._jsonl_handles.clear()

    def health(self) -> dict[str, Any]:
        try:
            conn = self.connect()
            conn.execute("SELECT 1").fetchone()
            return {"state": "HEALTHY", "path": self.sqlite_path, "schema_version": SCHEMA_VERSION}
        except Exception as exc:
            return {"state": "UNHEALTHY", "error": str(exc), "path": self.sqlite_path}

    # ------------------------------------------------------------------ writes
    def _mirror(self, table: str, row: Mapping[str, Any]) -> None:
        if not self.jsonl_mirror:
            return
        handle = self._jsonl_handles.get(table)
        if handle is None:
            filename = JSONL_FILES.get(table, f"{table}.jsonl")
            handle = open(Path(self.jsonl_dir) / filename, "a", encoding="utf-8")  # noqa: SIM115
            self._jsonl_handles[table] = handle
        handle.write(json.dumps(redact(dict(row)), default=str) + "\n")
        handle.flush()

    def _insert(self, table: str, row: Mapping[str, Any]) -> None:
        conn = self.connect()
        columns = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
                tuple(_sql_value(v) for v in row.values()),
            )
            conn.commit()
            self.counts[table] = self.counts.get(table, 0) + 1
        except sqlite3.Error as exc:  # pragma: no cover - storage failure must not crash
            log.error("journal insert into %s failed: %s", table, exc)
        self._mirror(table, row)

    def record_signal(self, row: Mapping[str, Any]) -> None:
        payload = dict(row)
        payload["payload"] = json.dumps(redact(dict(row)), default=str)
        for key in ("strategy_votes", "reason"):
            if key in payload and not isinstance(payload[key], str):
                payload[key] = json.dumps(payload[key], default=str)
        self._insert("signals", payload)

    def record_veto(self, row: Mapping[str, Any]) -> None:
        payload = dict(row)
        if "evidence" in payload and not isinstance(payload["evidence"], str):
            payload["evidence"] = json.dumps(redact(payload["evidence"]), default=str)
        self._insert("vetoes", payload)

    def record_vetoes(self, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            self.record_veto(row)

    def record_news(self, row: Mapping[str, Any]) -> None:
        self._insert("news", row)

    def record_news_items(self, items: Sequence[Any]) -> None:
        for item in items:
            try:
                self.record_news(item.to_row())
            except Exception as exc:
                log.warning("news journal row failed: %s", exc)

    def record_error(
        self, component: str, error: str, context: Mapping[str, Any] | None = None
    ) -> None:
        self._insert(
            "errors",
            {
                "ts": now_ms(),
                "component": component,
                "error": str(error)[:2000],
                "context": json.dumps(redact(dict(context or {})), default=str),
            },
        )

    def record_performance(self, row: Mapping[str, Any]) -> None:
        self._insert("performance", row)

    def record_feed_health(self, feeds: Mapping[str, Any], ts_ms: int | None = None) -> None:
        ts = ts_ms or now_ms()
        for name, health in feeds.items():
            self._insert(
                "feed_health",
                {
                    "ts": ts,
                    "feed": name,
                    "state": getattr(health, "state", health),
                    "age_ms": getattr(health, "age_ms", None),
                    "detail": str(getattr(health, "detail", ""))[:500],
                },
            )

    # ------------------------------------------------------------------ reads
    def fetch_signals(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute("SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def fetch_signal(self, signal_id: str) -> dict[str, Any] | None:
        conn = self.connect()
        row = conn.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        try:
            record["reconstruction"] = json.loads(record.get("payload") or "{}")
        except json.JSONDecodeError:
            record["reconstruction"] = {}
        return record

    def fetch_vetoes(self, limit: int = 200) -> list[dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute("SELECT * FROM vetoes ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def veto_counts(self) -> dict[str, int]:
        conn = self.connect()
        rows = conn.execute("SELECT guard, COUNT(*) AS n FROM vetoes GROUP BY guard").fetchall()
        return {str(r["guard"]): int(r["n"]) for r in rows}

    def reconstruct_signal(self, signal_id: str) -> dict[str, Any] | None:
        """Rebuild a signal exactly as emitted, including veto rows and news context at the time."""
        record = self.fetch_signal(signal_id)
        if record is None:
            return None
        conn = self.connect()
        vetoes = conn.execute(
            "SELECT * FROM vetoes WHERE symbol = ? AND ts BETWEEN ? AND ?",
            (record["symbol"], record["ts"] - 300_000, record["ts"] + 60_000),
        ).fetchall()
        perf = conn.execute(
            "SELECT * FROM performance WHERE signal_id = ?", (signal_id,)
        ).fetchall()
        return {
            "signal": record,
            "vetoes": [dict(r) for r in vetoes],
            "performance": [dict(r) for r in perf],
        }


def _sql_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(redact(value), default=str)
    if isinstance(value, bool):
        return int(value)
    return value


__all__ = ["JournalRepository"]
