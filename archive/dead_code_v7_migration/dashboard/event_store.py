"""
SQLite-backed event store for pipeline dashboard persistence.

Ported from MiroThinker. Uses WAL mode for concurrent read/write access.
Stores runs, events, and snapshots for post-mortem analysis.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)

_DB_DIR = os.environ.get(
    "DASHBOARD_DB_DIR",
    os.path.join(os.path.expanduser("~"), ".documentary-pipeline"),
)
_DB_PATH = os.path.join(_DB_DIR, "dashboard.db")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dashboard-db")

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    topic TEXT,
    status TEXT DEFAULT 'running',
    start_time REAL,
    end_time REAL,
    metadata_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_data TEXT NOT NULL,
    timestamp REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    snapshot_data TEXT NOT NULL,
    timestamp REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_snapshots_run_id ON snapshots(run_id);
"""


def _get_conn() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode."""
    os.makedirs(_DB_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize the database schema."""
    conn = _get_conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        logger.info("Dashboard DB initialized: %s", _DB_PATH)
    finally:
        conn.close()


def insert_run(run_id: str, topic: str = "") -> None:
    """Insert a new pipeline run."""
    def _do():
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, topic, status, start_time) "
                "VALUES (?, ?, 'running', ?)",
                (run_id, topic, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    _executor.submit(_do)


def insert_event(run_id: str, event_type: str, event_data: dict) -> None:
    """Insert a pipeline event."""
    def _do():
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO events (run_id, event_type, event_data, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (run_id, event_type, json.dumps(event_data), time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    _executor.submit(_do)


def insert_snapshot(run_id: str, snapshot_data: dict) -> None:
    """Insert a pipeline snapshot."""
    def _do():
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO snapshots (run_id, snapshot_data, timestamp) "
                "VALUES (?, ?, ?)",
                (run_id, json.dumps(snapshot_data), time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    _executor.submit(_do)


def finalize_run(run_id: str, status: str = "completed", metadata: Optional[dict] = None) -> None:
    """Finalize a pipeline run."""
    def _do():
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE runs SET status = ?, end_time = ?, metadata_json = ? "
                "WHERE run_id = ?",
                (status, time.time(), json.dumps(metadata or {}), run_id),
            )
            conn.commit()
        finally:
            conn.close()

    _executor.submit(_do)


async def get_latest_snapshot(run_id: str) -> Optional[dict]:
    """Get the latest snapshot for a run (async read via thread pool)."""
    import asyncio

    def _do():
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT snapshot_data FROM snapshots "
                "WHERE run_id = ? ORDER BY timestamp DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return json.loads(row["snapshot_data"]) if row else None
        finally:
            conn.close()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _do)


def get_recent_events(run_id: str, limit: int = 50) -> list[dict]:
    """Get recent events for a run."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT event_type, event_data, timestamp FROM events "
            "WHERE run_id = ? ORDER BY timestamp DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return [
            {
                "type": row["event_type"],
                "data": json.loads(row["event_data"]),
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def get_all_runs(limit: int = 20) -> list[dict]:
    """Get all pipeline runs."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT run_id, topic, status, start_time, end_time "
            "FROM runs ORDER BY start_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_run_detail(run_id: str) -> Optional[dict]:
    """Get full detail for a specific run."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None

        events = get_recent_events(run_id, limit=1000)
        return {
            **dict(row),
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            "events": events,
        }
    finally:
        conn.close()
