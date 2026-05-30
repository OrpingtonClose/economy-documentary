---
{
  "title": "Event Store",
  "section": "5",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[04 - Rules as Prompt No State Machine No Rules Engine Code|Rules as Prompt (No State Machine, No Rules Engine Code)]] | [[00 - Index|Index]] | [[A. Appendix EventStoreDB Migration Path|A. Appendix: EventStoreDB Migration Path]] ->

# Event Store


> **V7.1 architectural decision:** The event store uses **SQLite** (single file, WAL mode) with one `events` table per run. SQLite provides cross-process atomicity, global sequence allocation, and enforced deduplication via `UNIQUE` constraints — properties impossible to guarantee with JSONL across independent ASGI processes. EventStoreDB remains a future scalability path for distributed deployments.

The event store is the single source of truth for all pipeline effects. It must be:
- **Append-only:** Events are never modified or deleted.
- **Ordered:** Events within a run have a strict monotonic sequence.
- **Replayable:** Any projection can reconstruct state by reading from sequence 0.
- **Idempotent:** Duplicate appends with the same `effect_id` are silently ignored.
- **Cross-process safe:** Multiple agents on different ports append concurrently without corruption.

### 5.1 SQLite Implementation (Current)

The implementation uses SQLite in WAL (Write-Ahead Log) mode. One database file per run: `events_{run_id}.db`. WAL mode allows readers to not block writers and provides crash recovery.

#### 5.1.1 Why SQLite, not JSONL

JSONL was evaluated and rejected for production use because it fails under multi-process concurrency:

| Requirement | JSONL | SQLite (WAL mode) |
|---|---|---|
| Append-only | `"a"` mode (no in-place edit) | `INSERT` only, no `UPDATE`/`DELETE` |
| Ordered | In-memory `_seq` counter (per-process drift) | `MAX(seq)` query, global per DB |
| Replayable | Line-by-line read | `SELECT * ORDER BY seq` |
| Idempotent | In-memory `_seen` set (per-process) | `UNIQUE(effect_id)` constraint |
| Cross-process safe | **No** — `asyncio.Lock` is thread-only, not IPC | `BEGIN IMMEDIATE` serializes writers at OS level |
| Concurrent readers/writers | Readers block on `open()` | WAL mode: readers don't block writers |
| Crash recovery | Corrupted partial line possible | WAL replay on open |
| External deps | None | `sqlite3` in Python stdlib |
| Human inspectable | Plain text | `sqlite3` CLI, or JSON export |

The fatal JSONL flaw: each agent runs as an independent ASGI process on its own port (§2). `asyncio.Lock` only works within a single Python process. Two agents appending to the same JSONL file produce interleaved bytes and corrupted lines. In-memory `_seq` and `_seen` dictionaries are isolated per process, causing sequence number collisions and deduplication failure.

SQLite solves all of this with a single file and zero external dependencies.

#### 5.1.2 Schema

```text
CREATE TABLE events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    effect_id      TEXT UNIQUE NOT NULL,   -- idempotency key (UUIDv7)
    kind           TEXT NOT NULL,           -- effect discriminant
    effect_json    TEXT NOT NULL,           -- Pydantic model_dump_json()
    otio_hash_before TEXT NOT NULL,         -- OTIO state hash at append time
    agent          TEXT NOT NULL,           -- agent that produced the text
    timestamp      REAL NOT NULL,           -- wall-clock epoch seconds
    appended_at    REAL DEFAULT (unixepoch())
);

CREATE INDEX idx_events_kind ON events(kind);
CREATE INDEX idx_events_agent ON events(agent);
```

`seq` is an `AUTOINCREMENT` primary key — SQLite guarantees global monotonic allocation across processes. `effect_id` has a `UNIQUE` constraint — duplicate inserts are rejected at the database level, eliminating the need for an in-memory `_seen` set.

#### 5.1.3 EventStore class

```python
import sqlite3
from pathlib import Path
from contextlib import contextmanager

class EventStore:
    """Append-only SQLite event store. One DB file per run.

    Cross-process safe via SQLite WAL mode + BEGIN IMMEDIATE.
    Interface designed to be swappable with EventStoreDB backend.
    """

    def __init__(self, log_dir: str) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._dbs: dict[str, Path] = {}  # run_id -> db path

    def _path(self, run_id: str) -> Path:
        path = self.log_dir / f"events_{run_id}.db"
        self._dbs[run_id] = path
        return path

    def _init_db(self, run_id: str) -> None:
        """Create schema if DB does not exist."""
        path = self._path(run_id)
        with sqlite3.connect(path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    effect_id TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL,
                    effect_json TEXT NOT NULL,
                    otio_hash_before TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    appended_at REAL DEFAULT (unixepoch())
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent)")
            conn.commit()

    @contextmanager
    def _connect(self, run_id: str):
        """Yield a connection with WAL mode and busy-timeout."""
        path = self._path(run_id)
        conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def append(self, run_id: str, effect: Effect, otio_hash_before: str) -> EventRecord:
        """Append an effect. Idempotent via UNIQUE(effect_id).

        V7.1: Pessimistic locking only. SQLite BEGIN IMMEDIATE acquires the
        write lock at the OS level, serializing all writers across processes.
        No optimistic concurrency check — if another agent wrote since we read
        state, our next turn will see it. Simplicity over theoretical correctness.
        """
        self._init_db(run_id)

        effect_id = str(effect.effect_id)
        kind = effect.kind
        effect_json = effect.model_dump_json()
        agent = effect.agent
        timestamp = effect.timestamp.timestamp() if hasattr(effect.timestamp, "timestamp") else float(effect.timestamp)

        with self._connect(run_id) as conn:
            conn.execute("BEGIN IMMEDIATE")

            # Insert with idempotency — UNIQUE constraint rejects duplicates
            try:
                conn.execute(
                    """INSERT INTO events
                       (effect_id, kind, effect_json, otio_hash_before, agent, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (effect_id, kind, effect_json, otio_hash_before, agent, timestamp),
                )
            except sqlite3.IntegrityError:
                # Duplicate effect_id — idempotent no-op
                conn.execute("ROLLBACK")
                return self._find_by_effect_id(run_id, effect_id)

            # Fetch the auto-incremented sequence number
            cur = conn.execute("SELECT seq FROM events WHERE effect_id = ?", (effect_id,))
            seq = cur.fetchone()[0]

            conn.execute("COMMIT")

        return EventRecord(
            seq=seq,
            effect=effect,
            otio_hash_before=otio_hash_before,
        )

    def _find_by_effect_id(self, run_id: str, effect_id: str) -> EventRecord:
        """Return existing record by effect_id (used for idempotent dedup)."""
        with self._connect(run_id) as conn:
            cur = conn.execute(
                "SELECT seq, effect_json, otio_hash_before FROM events WHERE effect_id = ?",
                (effect_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"effect_id {effect_id} not found")
            seq, effect_json, otio_hash = row
            effect = Effect.model_validate_json(effect_json)
            return EventRecord(seq=seq, effect=effect, otio_hash_before=otio_hash)

    def read_all(self, run_id: str) -> list[EventRecord]:
        """Read all events for a run, in sequence order."""
        self._init_db(run_id)
        records: list[EventRecord] = []
        with self._connect(run_id) as conn:
            cur = conn.execute(
                "SELECT seq, effect_json, otio_hash_before FROM events ORDER BY seq"
            )
            for row in cur:
                seq, effect_json, otio_hash = row
                try:
                    effect = Effect.model_validate_json(effect_json)
                    records.append(EventRecord(seq=seq, effect=effect, otio_hash_before=otio_hash))
                except Exception:
                    continue
        return records

    def read_since(self, run_id: str, from_seq: int) -> list[EventRecord]:
        """Return events with sequence > from_seq."""
        self._init_db(run_id)
        records: list[EventRecord] = []
        with self._connect(run_id) as conn:
            cur = conn.execute(
                "SELECT seq, effect_json, otio_hash_before FROM events WHERE seq > ? ORDER BY seq",
                (from_seq,),
            )
            for row in cur:
                seq, effect_json, otio_hash = row
                try:
                    effect = Effect.model_validate_json(effect_json)
                    records.append(EventRecord(seq=seq, effect=effect, otio_hash_before=otio_hash))
                except Exception:
                    continue
        return records

    def replay(self, run_id: str) -> list[EventRecord]:
        """Full replay from sequence 1."""
        return self.read_all(run_id)

    def export_to_jsonl(self, run_id: str, out_path: str) -> None:
        """Export events to JSONL for human inspection or backup."""
        with self._connect(run_id) as conn:
            cur = conn.execute(
                "SELECT seq, effect_json, otio_hash_before FROM events ORDER BY seq"
            )
            with open(out_path, "w") as f:
                for row in cur:
                    seq, effect_json, otio_hash = row
                    record = EventRecord(
                        seq=seq,
                        effect=Effect.model_validate_json(effect_json),
                        otio_hash_before=otio_hash,
                    )
                    f.write(record.model_dump_json() + "\n")
```

#### 5.1.4 _parse_payload() — effect deserialization glue

**V7.1 note:** SQLite stores JSON text in the `effect_json` column. `_parse_payload()` is still useful for scenarios where raw JSON text is read from external sources (e.g., backup files, ESDB migration). It is not needed for normal projection replay from SQLite.

```python
import json

def _parse_payload(kind: str, payload_json: str) -> Effect:
    """Deserialize a JSON payload into the correct Effect subclass.
    Raises ValueError for unknown kind strings.
    """
    model_class = KIND_TO_MODEL.get(kind)
    if model_class is None:
        raise ValueError(f"Unknown effect kind: {kind!r}")
    data = json.loads(payload_json)
    return model_class.model_validate(data)
```

| Step | Logic | Failure Mode |
|---|---|---|
| 1 | `KIND_TO_MODEL[kind]` lookup | `ValueError` if kind not registered |
| 2 | `json.loads(payload_json)` | `JSONDecodeError` on malformed JSON |
| 3 | `model_validate(data)` | `ValidationError` if fields mismatch schema |

All three failure modes surface as exceptions in the projection's `tick()` loop. The projection logs the error and skips the offending event; it does not crash. A malformed event indicates a schema mismatch between writer and reader and requires operator intervention.

**Event metadata.** Every `EventRecord` carries attribution fields:

| Field | Type | Source | Purpose |
|---|---|---|---|
| `effect.agent` | `str` | Effect itself | Which component produced this effect |
| `effect.timestamp` | `float` | Effect itself | Wall-clock time at creation |
| `effect.effect_id` | `UUID` | Effect itself | Client-side idempotency key |
| `appended_at` | `REAL` | SQLite default | When the store received the event |
| `otio_hash_before` | `str` | Handler | OTIO state hash at append time |

**Causation and correlation.** Causation chains are tracked in the `Effect` model fields (e.g., `job_id`, `block_id`) rather than a separate metadata envelope. The `ProvenanceCapability` (§8.1) provides causal logging at the agent level.

---

### 5.2 Deduplication on effect_id

SQLite deduplicates via the `UNIQUE` constraint on `effect_id`. No in-memory state is required:

```
Agent A (port 8001)         SQLite DB (WAL mode)          Agent B (port 8002)
     |                              |                              |
     |-- append(effect_id=X) ------>|                              |
     |   BEGIN IMMEDIATE            |                              |
     |   INSERT ...                 |                              |
     |   COMMIT                     |                              |
     |<-- returns record -----------|                              |
     |                              |                              |
     |                              |<-- append(effect_id=X) ------|
     |                              |   BEGIN IMMEDIATE (waits)    |
     |                              |   INSERT → IntegrityError    |
     |                              |   ROLLBACK → returns existing  |
     |                              |-- returns record ------------>|
```

The `UNIQUE` constraint is enforced at the database level, globally across all processes. No `_seen` set, no `_last_seq()` scan, no in-memory state to rebuild on restart.

---

### 5.3 Replay

#### 5.3.1 read_since() for incremental projection updates

Every projection tracks `last_sequence` — the highest sequence it has processed. On activation, the projection calls `read_since(run_id, last_sequence)` and receives only new events.

```python
class Timeline:
    """Example: incremental update via read_since()."""

    def __init__(self):
        self.timeline = otio.schema.Timeline(name="Documentary")
        self.tracks: dict[str, Any] = {}
        self.last_sequence = 0

    async def tick(self, run_id: str, store: EventStore):
        """Process only events newer than last_sequence."""
        records = store.read_since(run_id, self.last_sequence)
        for record in records:
            self._apply(record.effect)
            self.last_sequence = record.seq
        return len(records)
```

**Invariants:**
- Results are strictly ordered by `seq` ascending.
- Each `seq` is greater than the input `from_seq`.
- Empty list if no new events.
- Method is read-only: never mutates the store.

#### 5.3.2 Full replay for state reconstruction

`replay(run_id)` returns every event from sequence 1 to highest assigned. Used for:

| Scenario | Trigger | Action |
|---|---|---|
| Projection schema change | New field added | Rebuild projection from seq 0 |
| Process restart | Agent crash + restart | Replay to restore in-memory state |
| Run audit | Operator inspection | Return full event history |

```python
async def rebuild_job_projection(run_id: str, store: EventStore) -> Jobs:
    """Construct fresh Jobs by replaying all events."""
    proj = Jobs()
    for record in store.replay(run_id):
        proj.apply(record.effect)
    return proj
```

---

### 5.4 Operational Concerns

#### 5.4.1 Disk usage monitoring

SQLite files grow linearly with event count. A typical documentary run (500–2000 events) produces a file of ~1–3 MB. For 100 concurrent runs: 100–300 MB total. WAL files are auto-truncated by SQLite when checkpoints occur.

Monitor via `du -sh log_dir/`. If disk fills, SQLite raises `OperationalError`. The handler catches this and returns `AgentResponse(status="error", error_message="disk full")`.

#### 5.4.2 Backup strategy

SQLite files are binary but portable. Backup by copying the file while no writers hold locks:

```bash
sqlite3 events_run_123.db ".backup backup_run_123.db"
```

Or programmatically:

```python
import sqlite3

def backup_run(run_id: str, backup_path: str, store: EventStore) -> None:
    src = store._path(run_id)
    with sqlite3.connect(src) as src_conn:
        with sqlite3.connect(backup_path) as dst_conn:
            src_conn.backup(dst_conn)
```

**Recovery:** Copy the backup file back to `log_dir/` and restart agents. Projections replay automatically.

#### 5.4.3 WAL checkpointing

SQLite automatically checkpoints the WAL into the main database. Under heavy write load, the WAL file may grow. Agents can force a checkpoint during idle periods:

```python
with store._connect(run_id) as conn:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
```

---

### 5.5 GSA Catch-Up (V7.1: No Checkpointing)

**V7.1 architectural decision:** Checkpointing is deleted entirely. The GSA is a pure, stateless fold over the event log. For documentary runs (500–2000 events), replay from sequence 0 takes milliseconds.

```python
async def gsa_catch_up(run_id: str, projections: ProjectionBundle, store: EventStore):
    """Catch up a single run's projections from sequence 0."""
    for record in store.replay(run_id):
        for proj in projections:
            proj.apply(record.effect)
            proj.last_sequence = record.seq

    # Live tail: poll read_since() every second
    while True:
        await asyncio.sleep(1.0)
        records = store.read_since(run_id, projections[0].last_sequence)
        for record in records:
            for proj in projections:
                proj.apply(record.effect)
                proj.last_sequence = record.seq
```

**Why no checkpoints:**
- File I/O eliminated
- Corrupted checkpoint edge cases eliminated
- Stale-cache-at-startup risk eliminated
- Replay from 0 for 2000 events is <10ms on local SSD
- One less component to break

---

### 5.6 Concurrency and Race Condition Handling

#### 5.6.1 Cross-process writer serialization

**V7.1 correction:** The previous JSONL design relied on `asyncio.Lock` per `run_id`, which only works within a single Python process. Each agent runs as an independent ASGI process on its own port (§2). SQLite `BEGIN IMMEDIATE` acquires the write lock at the OS level, serializing writers across all processes:

```
Process A (Audio Agent, port 8002)
  BEGIN IMMEDIATE        → acquires write lock
  INSERT ...             → writes event
  COMMIT                 → releases lock

Process B (Video Agent, port 8003)
  BEGIN IMMEDIATE        → waits if lock held
  INSERT ...             → proceeds after A commits
```

SQLite's busy timeout (30s) handles transient contention. If a process crashes while holding the lock, the OS releases it automatically.

#### 5.6.2 Simultaneous appends across different runs

Different runs use different database files. Appends are independent and fully concurrent — no locking contention between runs.

#### 5.6.3 What if the GSA is down?

Agents return `AgentResponse(status="error", error_message="GSA unreachable")`. No effects are appended without state. Agents query the GSA; they never read the event store directly.

#### 5.6.4 Why no optimistic concurrency check?

`BEGIN IMMEDIATE` is **pessimistic** — it acquires the write lock before any work is done. If another agent is writing, this agent waits. When the lock is acquired, the agent's read-state may be stale (another agent wrote in between), but that is acceptable:

- The agent appends its effect based on the state it read.
- On its **next turn**, it will read fresh state (including the other agent's effect) from the GSA.
- No retry loop, no hash validation, no complexity.

The documentary pipeline is not a financial ledger. Occasional stale-read turns are harmless — the agent will self-correct on the next cycle. Pessimistic locking keeps the architecture simple.

---

### 5.7 JSONL to SQLite Migration

Existing JSONL files from testing can be imported:

```python
def migrate_jsonl_to_sqlite(jsonl_path: str, store: EventStore, run_id: str) -> int:
    """Import a JSONL file into SQLite. Returns count of imported events."""
    count = 0
    with open(jsonl_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = EventRecord.model_validate_json(line)
                store.append(run_id, record.effect, record.otio_hash_before)
                count += 1
            except Exception:
                continue
    return count
```

