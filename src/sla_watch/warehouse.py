"""SQLite warehouse: schema, idempotent upsert, pipeline state, checksums."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sla_watch.model import Request311

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_requests (
    unique_key              TEXT PRIMARY KEY,
    socrata_id              TEXT NOT NULL,
    source_updated_at       TEXT NOT NULL,
    row_hash                TEXT NOT NULL,
    created_date            TEXT NOT NULL,
    closed_date             TEXT,
    agency                  TEXT NOT NULL,
    agency_name             TEXT,
    complaint_type          TEXT NOT NULL,
    descriptor              TEXT,
    status                  TEXT NOT NULL,
    resolution_updated_date TEXT,
    borough                 TEXT,
    community_board         TEXT,
    incident_zip            TEXT,
    channel                 TEXT,
    latitude                REAL,
    longitude               REAL,
    stale_quarantined       INTEGER NOT NULL DEFAULT 0,
    deleted_at_source       INTEGER NOT NULL DEFAULT 0,
    first_batch_id          TEXT NOT NULL,
    last_batch_id           TEXT NOT NULL,
    ingested_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_created ON raw_requests(created_date);

CREATE TABLE IF NOT EXISTS quarantine (
    payload_hash  TEXT PRIMARY KEY,  -- same bad payload is stored once, however often replayed
    unique_key    TEXT,
    reason        TEXT NOT NULL,
    payload       TEXT NOT NULL,
    first_batch_id TEXT NOT NULL,
    last_batch_id  TEXT NOT NULL,
    times_seen     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS pipeline_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id          TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    kind              TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    rows_fetched      INTEGER NOT NULL DEFAULT 0,
    rows_inserted     INTEGER NOT NULL DEFAULT 0,
    rows_updated      INTEGER NOT NULL DEFAULT 0,
    rows_unchanged    INTEGER NOT NULL DEFAULT 0,
    rows_quarantined  INTEGER NOT NULL DEFAULT 0,
    watermark_before  TEXT,
    watermark_after   TEXT,
    status            TEXT NOT NULL DEFAULT 'running'
);
"""

_CONTENT_COLS = [
    "socrata_id",
    "source_updated_at",
    "row_hash",
    "created_date",
    "closed_date",
    "agency",
    "agency_name",
    "complaint_type",
    "descriptor",
    "status",
    "resolution_updated_date",
    "borough",
    "community_board",
    "incident_zip",
    "channel",
    "latitude",
    "longitude",
]

WATERMARK_KEY = "incremental_watermark"


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def get_watermark(conn: sqlite3.Connection) -> tuple[str, str] | None:
    row = conn.execute("SELECT value FROM pipeline_state WHERE key=?", (WATERMARK_KEY,)).fetchone()
    if row is None:
        return None
    data = json.loads(row["value"])
    return str(data["updated_at"]), str(data["id"])


def set_watermark(conn: sqlite3.Connection, updated_at: str, socrata_id: str) -> None:
    value = json.dumps({"updated_at": updated_at, "id": socrata_id})
    conn.execute(
        "INSERT INTO pipeline_state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (WATERMARK_KEY, value),
    )


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM pipeline_state WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO pipeline_state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def upsert(conn: sqlite3.Connection, rows: Iterable[Request311], batch_id: str) -> dict[str, int]:
    """Insert new keys; update only when the incoming version is newer, or the same
    version with different content. Must be called inside a transaction."""
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    now = _now()
    for r in rows:
        values: dict[str, Any] = r.model_dump(exclude={"unique_key"})
        values["row_hash"] = r.row_hash()
        existing = conn.execute(
            "SELECT source_updated_at, row_hash FROM raw_requests WHERE unique_key=?",
            (r.unique_key,),
        ).fetchone()
        if existing is None:
            cols = ["unique_key", *_CONTENT_COLS, "first_batch_id", "last_batch_id", "ingested_at"]
            params = [r.unique_key, *(values[c] for c in _CONTENT_COLS), batch_id, batch_id, now]
            conn.execute(
                f"INSERT INTO raw_requests({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                params,
            )
            counts["inserted"] += 1
            continue
        newer = r.source_updated_at > existing["source_updated_at"]
        same_version_changed = (
            r.source_updated_at == existing["source_updated_at"]
            and values["row_hash"] != existing["row_hash"]
        )
        if not (newer or same_version_changed):
            counts["unchanged"] += 1
            continue
        sets = ", ".join(f"{c}=?" for c in _CONTENT_COLS)
        conn.execute(
            f"UPDATE raw_requests SET {sets}, stale_quarantined=0, last_batch_id=?, ingested_at=? "
            "WHERE unique_key=?",
            [*(values[c] for c in _CONTENT_COLS), batch_id, now, r.unique_key],
        )
        counts["updated"] += 1
    return counts


def quarantine(conn: sqlite3.Connection, batch_id: str, raw: dict[str, Any], reason: str) -> None:
    payload = json.dumps(raw, sort_keys=True)
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    key = raw.get("unique_key")
    conn.execute(
        "INSERT INTO quarantine(payload_hash, unique_key, reason, payload, first_batch_id, "
        "last_batch_id) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(payload_hash) DO UPDATE SET "
        "last_batch_id=excluded.last_batch_id, times_seen=times_seen+1",
        (payload_hash, key, reason, payload, batch_id, batch_id),
    )
    incoming_version = str(raw.get(":updated_at", ""))
    if key:
        conn.execute(
            "UPDATE raw_requests SET stale_quarantined=1 "
            "WHERE unique_key=? AND source_updated_at < ?",
            (str(key), incoming_version),
        )


def content_checksum(conn: sqlite3.Connection) -> str:
    """Deterministic checksum of business state (ignores batch ids / ingest times)."""
    digest = hashlib.sha256()
    cur = conn.execute(
        f"SELECT unique_key, {', '.join(_CONTENT_COLS)}, stale_quarantined, deleted_at_source "
        "FROM raw_requests ORDER BY unique_key"
    )
    for row in cur:
        digest.update(json.dumps(list(row), default=str).encode())
    return digest.hexdigest()


def count(conn: sqlite3.Connection, table: str = "raw_requests") -> int:
    allowed = {"raw_requests", "quarantine", "batches"}
    if table not in allowed:
        raise ValueError(table)
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
