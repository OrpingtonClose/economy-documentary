"""CPython sys.monitoring auto-tracer + SQLite WAL store.

Zero manual instrumentation.  Every PY_START / PY_RETURN in the pipeline
is captured by CPython itself and written to SQLite via a background
thread with batched inserts.

Usage::
    from tracing.auto_trace import AutoTracer
    t = AutoTracer("/path/to/traces.db")
    t.start_run("run_123", topic="brief here")
    t.start()   # registers sys.monitoring
    # ... pipeline runs ...
    t.stop()    # unregisters, flushes

Query live::
    SELECT * FROM calls WHERE run_id = 'run_123' ORDER BY ts;
    SELECT func, COUNT(*), SUM(duration_ms) FROM calls
    WHERE run_id = 'run_123' GROUP BY func ORDER BY SUM(duration_ms) DESC;
"""

from __future__ import annotations

import sys
import threading
import time
import sqlite3
from pathlib import Path
from typing import Any, Optional


class _RingBuffer:
    """Lock-free-ish ring buffer for monitor → DB thread handoff.

    Two buffers swap: monitor writes to ``_active``, background thread
    drains ``_drain``.  Lock held only during swap (microseconds).
    """

    _CAP = 10_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: list[dict] = []
        self._drain: list[dict] = []
        self._dropped = 0

    def push(self, record: dict) -> None:
        with self._lock:
            buf = self._active
            if len(buf) < self._CAP:
                buf.append(record)
            else:
                self._dropped += 1

    def swap(self) -> tuple[list[dict], int]:
        with self._lock:
            dropped = self._dropped
            self._dropped = 0
            self._active, self._drain = self._drain, self._active
            return self._drain, dropped


class AutoTracer:
    """Auto-tracer powered by sys.monitoring (Python 3.12+).

    Stores every call/return in SQLite with WAL mode + batched inserts.
    No manual instrumentation anywhere in the pipeline.
    """

    _TOOL_ID = 2
    _FLUSH_INTERVAL = 3.0  # seconds
    _BATCH_SIZE = 500

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._run_id: str = ""
        self._start_time: float = 0.0
        self._active = False
        self._buf = _RingBuffer()
        self._worker: Optional[threading.Thread] = None
        self._shutdown = threading.Event()
        self._call_counts: dict[str, int] = {}
        self._stack: list[tuple[str, str, float]] = []

        self._init_db()

    # ------------------------------------------------------------------
    # DB
    # ------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    topic TEXT,
                    start_ts REAL,
                    end_ts REAL,
                    status TEXT
                );
                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    elapsed_ms REAL,
                    func TEXT,
                    module TEXT,
                    event TEXT,          -- 'call' | 'return'
                    duration_ms REAL,
                    parent_func TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_calls_run_ts
                    ON calls(run_id, ts);
                CREATE INDEX IF NOT EXISTS idx_calls_func
                    ON calls(run_id, func, duration_ms);
                """
            )

    def _insert_batch(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO calls (run_id, ts, elapsed_ms, func, module, event, duration_ms, parent_func)
                VALUES (:run_id, :ts, :elapsed_ms, :func, :module, :event, :duration_ms, :parent_func)
                """,
                rows,
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------
    def start_run(self, run_id: str, topic: str = "") -> None:
        self._run_id = run_id
        self._start_time = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, topic, start_ts) VALUES (?, ?, ?)",
                (run_id, topic, self._start_time),
            )
            conn.commit()

    def end_run(self, status: str = "completed") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET end_ts = ?, status = ? WHERE run_id = ?",
                (time.time(), status, self._run_id),
            )
            conn.commit()

    def start(self) -> None:
        if self._active or not hasattr(sys, "monitoring"):
            return
        sys.monitoring.use_tool_id(self._TOOL_ID, "auto-trace")
        sys.monitoring.set_events(
            self._TOOL_ID,
            sys.monitoring.events.PY_START | sys.monitoring.events.PY_RETURN,
        )
        sys.monitoring.register_callback(
            self._TOOL_ID, sys.monitoring.events.PY_START, self._on_start
        )
        sys.monitoring.register_callback(
            self._TOOL_ID, sys.monitoring.events.PY_RETURN, self._on_return
        )
        self._active = True
        self._shutdown.clear()
        self._worker = threading.Thread(target=self._flush_loop, daemon=True, name="auto-trace")
        self._worker.start()

    def stop(self) -> None:
        if not self._active or not hasattr(sys, "monitoring"):
            return
        sys.monitoring.set_events(self._TOOL_ID, 0)
        self._active = False
        self._shutdown.set()
        if self._worker:
            self._worker.join(timeout=5.0)
        # Final drain
        rows, dropped = self._buf.swap()
        if dropped:
            rows.append({
                "run_id": self._run_id,
                "ts": time.time(),
                "elapsed_ms": 0,
                "func": "__dropped__",
                "module": "",
                "event": "drop",
                "duration_ms": dropped,
                "parent_func": "",
            })
        self._insert_batch(rows)

    # ------------------------------------------------------------------
    # sys.monitoring callbacks — must be FAST
    # ------------------------------------------------------------------
    def _on_start(self, code, instruction_offset) -> Any:
        mod = code.co_filename
        if "/server/" not in mod:
            return self._on_start
        fname = code.co_name
        now = time.time()
        parent = self._stack[-1][0] if self._stack else ""
        self._stack.append((fname, mod, now))
        self._buf.push({
            "run_id": self._run_id,
            "ts": now,
            "elapsed_ms": round((now - self._start_time) * 1000, 2) if self._start_time else 0,
            "func": fname,
            "module": mod,
            "event": "call",
            "duration_ms": None,
            "parent_func": parent,
        })
        return self._on_start

    def _on_return(self, code, instruction_offset, retval) -> Any:
        if not self._stack:
            return self._on_return
        fname, mod, t0 = self._stack.pop()
        dur = (time.time() - t0) * 1000
        now = time.time()
        self._buf.push({
            "run_id": self._run_id,
            "ts": now,
            "elapsed_ms": round((now - self._start_time) * 1000, 2) if self._start_time else 0,
            "func": fname,
            "module": mod,
            "event": "return",
            "duration_ms": round(dur, 3),
            "parent_func": "",
        })
        return self._on_return

    # ------------------------------------------------------------------
    # Background flush
    # ------------------------------------------------------------------
    def _flush_loop(self) -> None:
        while not self._shutdown.is_set():
            self._shutdown.wait(self._FLUSH_INTERVAL)
            rows, dropped = self._buf.swap()
            if dropped:
                rows.append({
                    "run_id": self._run_id,
                    "ts": time.time(),
                    "elapsed_ms": 0,
                    "func": "__dropped__",
                    "module": "",
                    "event": "drop",
                    "duration_ms": dropped,
                    "parent_func": "",
                })
            self._insert_batch(rows)
