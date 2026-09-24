"""Journal schemas (FINAL_DELIVERABLE §X).

Five journals - signals, vetoes, news, errors, performance - such that EVERY signal is
fully reconstructable from stored rows (the §23 audit requirement).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id      TEXT PRIMARY KEY,
    ts             INTEGER NOT NULL,
    symbol         TEXT NOT NULL,
    direction      TEXT NOT NULL,
    grade          TEXT NOT NULL,
    confidence     REAL NOT NULL,
    entry          REAL NOT NULL,
    entry_zone_low REAL NOT NULL,
    entry_zone_high REAL NOT NULL,
    sl             REAL NOT NULL,
    tp1            REAL NOT NULL,
    tp2            REAL NOT NULL,
    tp3            REAL NOT NULL,
    tp4            REAL NOT NULL,
    rr             REAL NOT NULL,
    expiry         INTEGER NOT NULL,
    binance_price  REAL,
    coindcx_price  REAL,
    spread_bps     REAL,
    basis_bps      REAL,
    news_state     TEXT,
    news_source    TEXT,
    strategy_votes TEXT,
    veto_status    TEXT,
    veto_detail    TEXT,
    reason         TEXT,
    state          TEXT,
    latency_ms     INTEGER,
    advisory_qty   REAL,
    advisory_notional REAL,
    payload        TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

CREATE TABLE IF NOT EXISTS vetoes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    symbol   TEXT NOT NULL,
    guard    TEXT NOT NULL,
    severity TEXT NOT NULL,
    reason   TEXT,
    evidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_vetoes_ts ON vetoes(ts);

CREATE TABLE IF NOT EXISTS news (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hash         TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    source       TEXT,
    tier         INTEGER,
    headline     TEXT,
    link         TEXT,
    categories   TEXT,
    entities     TEXT,
    credibility  REAL,
    novelty      REAL,
    impact       REAL,
    severity     TEXT,
    direction    TEXT,
    corroborating INTEGER,
    half_life_min REAL,
    affected_assets TEXT,
    UNIQUE(hash, source)
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts);

CREATE TABLE IF NOT EXISTS errors (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    component TEXT NOT NULL,
    error     TEXT NOT NULL,
    context   TEXT
);

CREATE TABLE IF NOT EXISTS performance (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id  TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    state      TEXT NOT NULL,
    mfe_r      REAL,
    mae_r      REAL,
    realised_r REAL,
    filled     INTEGER,
    latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_perf_signal ON performance(signal_id);

CREATE TABLE IF NOT EXISTS feed_health (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    feed   TEXT NOT NULL,
    state  TEXT NOT NULL,
    age_ms INTEGER,
    detail TEXT
);
"""

JSONL_FILES = {
    "signals": "signals.jsonl",
    "vetoes": "vetoes.jsonl",
    "news": "news.jsonl",
    "errors": "errors.jsonl",
    "performance": "performance.jsonl",
}


@dataclass
class JournalRow:
    table: str
    data: Mapping[str, Any]

    def as_json(self) -> str:
        return json.dumps({"table": self.table, **dict(self.data)}, default=str)


__all__ = ["DDL", "JSONL_FILES", "JournalRow", "SCHEMA_VERSION"]
