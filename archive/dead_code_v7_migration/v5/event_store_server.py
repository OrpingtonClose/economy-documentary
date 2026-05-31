"""Event Store Server — standalone HTTP service for the documentary pipeline.

The event store is the single source of truth.  It is a standalone FastAPI
server (port 8079) with an SQLite or PostgreSQL backend.  Every mutation
enters through POST /append; every read happens via GET /read_since or
GET /replay.

Design choices
--------------
* Server-based, not library: agents talk to it over HTTP.  This enforces the
  single-writer invariant at the network boundary, makes agents language-agnostic,
  and turns the store into a real infrastructure component.
* Single writer via asyncio queue: all append operations are serialised through
  one async queue, regardless of how many HTTP workers (uvicorn) are running.
* SQLite (dev) / PostgreSQL (prod): WAL mode for SQLite; connection pool for PG.
* Full event envelope: every row carries causation_id, correlation_id,
  schema_version, producer, trace_id for observability and replay.
* Idempotency: (run_id, effect_id) is UNIQUE; retries with the same effect_id
  are silently dropped and the original sequence is returned.

API (exactly GET / and POST /, per architecture rules)
-------------------------------------------------------
GET  /                     → health, stats, last_sequence per run
POST / {cmd: "append", ...} → append one event, return sequence
POST / {cmd: "read_since", run_id, sequence} → list of events after seq
POST / {cmd: "replay", run_id} → full event list for run
POST / {cmd: "read_last_n", run_id, n} → last N events for run
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, AsyncGenerator, Literal, Union
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Optional PostgreSQL support -------------------------------------------------
try:
    import asyncpg
    HAS_PG = True
except ImportError:
    HAS_PG = False

# Optional aiosqlite for SQLite dev backend -----------------------------------
try:
    import aiosqlite
    HAS_SQLITE = True
except ImportError:
    HAS_SQLITE = False

# ---------------------------------------------------------------------------
# Module-level constants (no environment variables)
# ---------------------------------------------------------------------------

EVENT_STORE_PORT: int = 8079
EVENT_STORE_DB_URL: str = "sqlite:///data/events.db"
EVENT_STORE_SCHEMA_VERSION: int = 1

logger = logging.getLogger("event_store")

# ---------------------------------------------------------------------------
# Pydantic models for request/response
# ---------------------------------------------------------------------------

class AppendRequest(BaseModel):
    cmd: Literal["append"] = "append"
    run_id: str
    effect_id: str  # UUIDv7 string — client-generated idempotency key
    kind: str
    agent: str
    payload: dict[str, Any] = Field(default_factory=dict)
    causation_id: str = ""  # UUID of the preceding effect that caused this one
    correlation_id: str = ""  # UUID grouping related effects in a transaction
    trace_id: str = ""  # W3C trace context trace-id
    producer: str = ""  # component name, e.g. "scenario_agent"
    timestamp: float = Field(default_factory=time.time)


class ReadSinceRequest(BaseModel):
    cmd: Literal["read_since"] = "read_since"
    run_id: str
    sequence: int = 0


class ReplayRequest(BaseModel):
    cmd: Literal["replay"] = "replay"
    run_id: str


class ReadLastNRequest(BaseModel):
    cmd: Literal["read_last_n"] = "read_last_n"
    run_id: str
    n: int = Field(default=20, ge=1, le=1000)


class AppendResponse(BaseModel):
    sequence: int
    inserted: bool  # False if duplicate (idempotent retry)
    effect_id: str


class EventRow(BaseModel):
    sequence: int
    run_id: str
    effect_id: str
    kind: str
    agent: str
    payload: dict[str, Any]
    causation_id: str
    correlation_id: str
    trace_id: str
    producer: str
    schema_version: int
    occurred_at: float


class HealthResponse(BaseModel):
    status: str
    backend: str
    total_events: int
    runs: int
    last_sequence_per_run: dict[str, int]


# Union of valid commands
CommandRequest = Annotated[
    Union[AppendRequest, ReadSinceRequest, ReplayRequest, ReadLastNRequest],
    Field(discriminator="cmd"),
]

# ---------------------------------------------------------------------------
# SQL schema
# ---------------------------------------------------------------------------

_SQLITE_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    sequence       INTEGER NOT NULL,
    effect_id      TEXT NOT NULL,
    kind           TEXT NOT NULL,
    agent          TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    causation_id   TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    trace_id       TEXT NOT NULL DEFAULT '',
    producer       TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL DEFAULT 1,
    occurred_at    REAL NOT NULL,
    inserted_at    REAL NOT NULL DEFAULT (unixepoch())
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_effect_id
    ON events(run_id, effect_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_seq
    ON events(run_id, sequence);

CREATE INDEX IF NOT EXISTS idx_events_kind
    ON events(kind);

CREATE INDEX IF NOT EXISTS idx_events_run_created
    ON events(run_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_events_trace
    ON events(trace_id) WHERE trace_id <> '';

CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    created_at     REAL NOT NULL DEFAULT (unixepoch()),
    last_sequence  INTEGER NOT NULL DEFAULT 0
);
"""

_PG_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id             BIGSERIAL PRIMARY KEY,
    run_id         TEXT NOT NULL,
    sequence       INTEGER NOT NULL,
    effect_id      TEXT NOT NULL,
    kind           TEXT NOT NULL,
    agent          TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    causation_id   TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    trace_id       TEXT NOT NULL DEFAULT '',
    producer       TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL DEFAULT 1,
    occurred_at    DOUBLE PRECISION NOT NULL,
    inserted_at    DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_effect_id
    ON events(run_id, effect_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_seq
    ON events(run_id, sequence);

CREATE INDEX IF NOT EXISTS idx_events_kind
    ON events(kind);

CREATE INDEX IF NOT EXISTS idx_events_run_created
    ON events(run_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_events_trace
    ON events(trace_id) WHERE trace_id <> '';

CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    created_at     DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
    last_sequence  INTEGER NOT NULL DEFAULT 0
);
"""

# ---------------------------------------------------------------------------
# Row → EventRow helper
# ---------------------------------------------------------------------------

def _row_to_event(row: aiosqlite.Row | asyncpg.Record | dict[str, Any]) -> EventRow:
    """Normalise a database row into an EventRow."""
    # sqlite3.Row supports __getitem__ but not .get(); normalise first
    if hasattr(row, "get"):
        d = row
    else:
        d = {k: row[k] for k in row.keys()}
    payload = json.loads(d["payload_json"])
    return EventRow(
        sequence=d["sequence"],
        run_id=d["run_id"],
        effect_id=d["effect_id"],
        kind=d["kind"],
        agent=d["agent"],
        payload=payload,
        causation_id=d.get("causation_id", ""),
        correlation_id=d.get("correlation_id", ""),
        trace_id=d.get("trace_id", ""),
        producer=d.get("producer", ""),
        schema_version=d.get("schema_version", 1),
        occurred_at=d["occurred_at"],
    )


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------

class EventStoreBackend:
    """Abstract interface for the event-store persistence layer."""

    async def init(self) -> None: ...
    async def close(self) -> None: ...

    async def append(
        self,
        run_id: str,
        effect_id: str,
        kind: str,
        agent: str,
        payload_json: str,
        causation_id: str,
        correlation_id: str,
        trace_id: str,
        producer: str,
        schema_version: int,
        occurred_at: float,
    ) -> tuple[int, bool]: ...

    async def read_since(self, run_id: str, sequence: int) -> list[EventRow]: ...
    async def replay(self, run_id: str) -> list[EventRow]: ...
    async def read_last_n(self, run_id: str, n: int) -> list[EventRow]: ...
    async def stats(self) -> tuple[int, int, dict[str, int]]: ...


# ---------------------------------------------------------------------------
# SQLite backend (dev / single-node)
# ---------------------------------------------------------------------------

class SQLiteBackend(EventStoreBackend):
    """SQLite backend with WAL mode.  Single writer enforced externally."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path.replace("sqlite:///", "").replace("sqlite://", "")
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        if not HAS_SQLITE:
            raise RuntimeError("aiosqlite is required for SQLite backend")
        import pathlib
        pathlib.Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SQLITE_DDL)
        await self._conn.commit()
        logger.info("SQLite backend ready: %s (WAL mode)", self.db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def append(
        self,
        run_id: str,
        effect_id: str,
        kind: str,
        agent: str,
        payload_json: str,
        causation_id: str,
        correlation_id: str,
        trace_id: str,
        producer: str,
        schema_version: int,
        occurred_at: float,
    ) -> tuple[int, bool]:
        assert self._conn is not None
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Resolve next sequence
            cur = await self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id = ?",
                (run_id,),
            )
            row = await cur.fetchone()
            next_seq = (row[0] if row else 0) + 1

            cur = await self._conn.execute(
                "INSERT OR IGNORE INTO events "
                "(run_id, sequence, effect_id, kind, agent, payload_json, "
                "causation_id, correlation_id, trace_id, producer, "
                "schema_version, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, next_seq, effect_id, kind, agent, payload_json,
                    causation_id, correlation_id, trace_id, producer,
                    schema_version, occurred_at,
                ),
            )
            if cur.rowcount == 0:
                # Duplicate — fetch original sequence
                cur = await self._conn.execute(
                    "SELECT sequence FROM events WHERE run_id = ? AND effect_id = ?",
                    (run_id, effect_id),
                )
                row = await cur.fetchone()
                seq = row[0] if row else next_seq
                await self._conn.commit()
                return seq, False

            # Update run tracking
            await self._conn.execute(
                "INSERT INTO runs (run_id, last_sequence) VALUES (?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET last_sequence=excluded.last_sequence",
                (run_id, next_seq),
            )
            await self._conn.commit()
            return next_seq, True
        except Exception:
            await self._conn.rollback()
            raise

    async def read_since(self, run_id: str, sequence: int) -> list[EventRow]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? AND sequence > ? ORDER BY sequence",
            (run_id, sequence),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_event(r) for r in rows]

    async def replay(self, run_id: str) -> list[EventRow]:
        return await self.read_since(run_id, 0)

    async def read_last_n(self, run_id: str, n: int) -> list[EventRow]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY sequence DESC LIMIT ?",
            (run_id, n),
        ) as cursor:
            rows = await cursor.fetchall()
            return list(reversed([_row_to_event(r) for r in rows]))

    async def stats(self) -> tuple[int, int, dict[str, int]]:
        assert self._conn is not None
        cur = await self._conn.execute("SELECT COUNT(*) FROM events")
        total = (await cur.fetchone())[0]
        cur = await self._conn.execute("SELECT COUNT(*) FROM runs")
        runs = (await cur.fetchone())[0]
        cur = await self._conn.execute("SELECT run_id, last_sequence FROM runs")
        seqs: dict[str, int] = {}
        async for row in cur:
            seqs[row[0]] = row[1]
        return total, runs, seqs


# ---------------------------------------------------------------------------
# PostgreSQL backend (production)
# ---------------------------------------------------------------------------

class PostgreSQLBackend(EventStoreBackend):
    """PostgreSQL backend with connection pooling."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        if not HAS_PG:
            raise RuntimeError("asyncpg is required for PostgreSQL backend")
        self._pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute(_PG_DDL)
        logger.info("PostgreSQL backend ready")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def append(
        self,
        run_id: str,
        effect_id: str,
        kind: str,
        agent: str,
        payload_json: str,
        causation_id: str,
        correlation_id: str,
        trace_id: str,
        producer: str,
        schema_version: int,
        occurred_at: float,
    ) -> tuple[int, bool]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Try insert; on conflict return existing sequence
                row = await conn.fetchrow(
                    """
                    WITH ins AS (
                        INSERT INTO events
                        (run_id, sequence, effect_id, kind, agent, payload_json,
                         causation_id, correlation_id, trace_id, producer,
                         schema_version, occurred_at)
                        VALUES (
                            $1,
                            COALESCE((SELECT MAX(sequence) FROM events WHERE run_id = $1), 0) + 1,
                            $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
                        )
                        ON CONFLICT (run_id, effect_id) DO NOTHING
                        RETURNING sequence, TRUE AS inserted
                    )
                    SELECT sequence, inserted FROM ins
                    UNION ALL
                    SELECT sequence, FALSE AS inserted FROM events
                    WHERE run_id = $1 AND effect_id = $2
                    LIMIT 1
                    """,
                    run_id, effect_id, kind, agent, payload_json,
                    causation_id, correlation_id, trace_id, producer,
                    schema_version, occurred_at,
                )
                seq = row["sequence"]
                inserted = row["inserted"]
                if inserted:
                    await conn.execute(
                        """
                        INSERT INTO runs (run_id, last_sequence) VALUES ($1, $2)
                        ON CONFLICT (run_id) DO UPDATE SET last_sequence = EXCLUDED.last_sequence
                        """,
                        run_id, seq,
                    )
                return seq, inserted

    async def read_since(self, run_id: str, sequence: int) -> list[EventRow]:
        assert self._pool is not None
        rows = await self._pool.fetch(
            "SELECT * FROM events WHERE run_id = $1 AND sequence > $2 ORDER BY sequence",
            run_id, sequence,
        )
        return [_row_to_event(r) for r in rows]

    async def replay(self, run_id: str) -> list[EventRow]:
        return await self.read_since(run_id, 0)

    async def read_last_n(self, run_id: str, n: int) -> list[EventRow]:
        assert self._pool is not None
        rows = await self._pool.fetch(
            "SELECT * FROM events WHERE run_id = $1 ORDER BY sequence DESC LIMIT $2",
            run_id, n,
        )
        return list(reversed([_row_to_event(r) for r in rows]))

    async def stats(self) -> tuple[int, int, dict[str, int]]:
        assert self._pool is not None
        total = await self._pool.fetchval("SELECT COUNT(*) FROM events")
        runs = await self._pool.fetchval("SELECT COUNT(*) FROM runs")
        seqs: dict[str, int] = {}
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                async for row in conn.cursor("SELECT run_id, last_sequence FROM runs"):
                    seqs[row["run_id"]] = row["last_sequence"]
        return total, runs, seqs


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

def _make_backend(db_url: str) -> EventStoreBackend:
    if db_url.startswith("postgres"):
        return PostgreSQLBackend(db_url)
    return SQLiteBackend(db_url)


# ---------------------------------------------------------------------------
# Single-writer queue
# ---------------------------------------------------------------------------

@dataclass
class _WriteItem:
    request: AppendRequest
    future: asyncio.Future[tuple[int, bool]]


class SingleWriterQueue:
    """Serialises all writes through one asyncio task."""

    def __init__(self, backend: EventStoreBackend) -> None:
        self.backend = backend
        self._queue: asyncio.Queue[_WriteItem | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.backend.init()
        self._task = asyncio.create_task(self._loop(), name="event-store-writer")

    async def stop(self) -> None:
        await self._queue.put(None)
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        await self.backend.close()

    async def submit(self, req: AppendRequest) -> tuple[int, bool]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[int, bool]] = loop.create_future()
        await self._queue.put(_WriteItem(req, future))
        return await future

    async def _loop(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            try:
                payload_json = json.dumps(item.request.payload, ensure_ascii=False)
                seq, inserted = await self.backend.append(
                    run_id=item.request.run_id,
                    effect_id=item.request.effect_id,
                    kind=item.request.kind,
                    agent=item.request.agent,
                    payload_json=payload_json,
                    causation_id=item.request.causation_id,
                    correlation_id=item.request.correlation_id,
                    trace_id=item.request.trace_id,
                    producer=item.request.producer or item.request.agent,
                    schema_version=EVENT_STORE_SCHEMA_VERSION,
                    occurred_at=item.request.timestamp,
                )
                item.future.set_result((seq, inserted))
            except Exception as exc:
                item.future.set_exception(exc)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    db_url = EVENT_STORE_DB_URL
    backend = _make_backend(db_url)
    writer = SingleWriterQueue(backend)
    await writer.start()
    app.state.writer = writer
    app.state.backend = backend
    logger.info("Event store server started on port %s (backend=%s)",
                EVENT_STORE_PORT, type(backend).__name__)
    yield
    await writer.stop()
    logger.info("Event store server stopped")


app = FastAPI(title="Documentary Event Store", lifespan=_lifespan)


@app.get("/")
async def health() -> HealthResponse:
    backend: EventStoreBackend = app.state.backend
    total, runs, seqs = await backend.stats()
    return HealthResponse(
        status="ok",
        backend=type(backend).__name__,
        total_events=total,
        runs=runs,
        last_sequence_per_run=seqs,
    )


@app.post("/")
async def dispatch(req: CommandRequest) -> Any:
    """Single POST endpoint — command is dispatched by `cmd` field."""
    writer: SingleWriterQueue = app.state.writer
    backend: EventStoreBackend = app.state.backend

    if isinstance(req, AppendRequest):
        seq, inserted = await writer.submit(req)
        return AppendResponse(sequence=seq, inserted=inserted, effect_id=req.effect_id)

    if isinstance(req, ReadSinceRequest):
        rows = await backend.read_since(req.run_id, req.sequence)
        return {"events": [r.model_dump() for r in rows]}

    if isinstance(req, ReplayRequest):
        rows = await backend.replay(req.run_id)
        return {"events": [r.model_dump() for r in rows]}

    if isinstance(req, ReadLastNRequest):
        rows = await backend.read_last_n(req.run_id, req.n)
        return {"events": [r.model_dump() for r in rows]}

    raise HTTPException(status_code=400, detail=f"Unknown command: {req}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=EVENT_STORE_PORT)
