> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Event Store Alternatives Research Brief
## Beyond KurrentDB: SQLite, PostgreSQL/MessageDB, NATS JetStream, Redis Streams

**Date:** 2026-05-27
**Sources:** Exa AI Search, KurrentDB docs, MessageDB GitHub, Python eventsourcing docs, NATS docs

---

## The Real Problem

The auditor identified **three fatal flaws** in the JSONL design:

1. **Concurrent append corruption** — `asyncio.Lock` is thread-only, not IPC-safe across ASGI processes
2. **Sequence number drift** — per-process `_seq` counters collide
3. **Deduplication failure** — per-process `_seen` sets don't share state

These are **concurrency safety** problems, not projection problems. The question is: what store gives us cross-process atomic append + global ordering + deduplication with minimal operational overhead?

---

## Alternative 1: SQLite (Current V7.1 Direction)

**Status:** Already written into vault §5
**Deps:** `sqlite3` (Python stdlib)
**Deployment:** Single file per run (`events_{run_id}.db`)

### What It Gives Us

| Requirement | SQLite Solution |
|---|---|
| Cross-process append | `BEGIN IMMEDIATE` serializes writers at OS level |
| Global sequence | `AUTOINCREMENT` primary key — no in-memory counter |
| Deduplication | `UNIQUE(effect_id)` constraint — database-enforced |
| Ordered replay | `SELECT * ORDER BY seq` |
| Crash recovery | WAL mode replay on open |
| Concurrent readers | WAL mode: readers don't block writers |

### What It Costs

- One file per run = many files for many concurrent runs
- No native pub/sub — agents must poll or use external wake notifications
- No clustering — single-machine only
- No built-in stream partitioning

### Verdict

**Correct for the problem.** SQLite WAL + `BEGIN IMMEDIATE` directly solves all three fatal flaws. The trade-off is operational (single-machine, file-per-run) not architectural.

---

## Alternative 2: PostgreSQL + MessageDB

**Repo:** `message-db/message-db`  
**Python client:** `message-db-py` (`pip install message-db-py`)  
**Deps:** PostgreSQL server + Python `psycopg2`

### What MessageDB Is

A fully-featured event store and message store implemented **inside PostgreSQL** as stored functions and tables. No separate server — it's a schema + function library that runs in Postgres.

### Features

- Pub/Sub via `LISTEN`/`NOTIFY`
- JSON message payloads
- Event streams with categories
- Consumer groups
- Message queues
- Stream versioning

### Schema (simplified)

```sql
CREATE TABLE messages (
    id UUID PRIMARY KEY,
    stream_name TEXT NOT NULL,
    position BIGINT NOT NULL,
    type TEXT NOT NULL,
    data JSONB NOT NULL,
    metadata JSONB,
    time TIMESTAMP DEFAULT NOW()
);
```

Stored functions handle: `write_message()`, `get_stream_messages()`, `get_category_messages()`, `get_last_message()`.

### What It Gives Us Over SQLite

| Capability | MessageDB | SQLite |
|---|---|---|
| Cross-process safe | Yes (Postgres MVCC) | Yes (WAL + IMMEDIATE) |
| Global ordering | Yes (`position` per stream) | Yes (`seq` globally) |
| Deduplication | Yes (UUID primary key) | Yes (UNIQUE constraint) |
| **Pub/Sub** | **Yes (`LISTEN`/`NOTIFY`)** | No |
| **Single DB for all runs** | **Yes** | No (file per run) |
| **Stream categories** | **Yes** | No |
| Consumer groups | Yes | No |
| Operational complexity | Higher (Postgres server) | Lower (file) |

### The Pub/Sub Advantage

With MessageDB, the GSA could subscribe to events via Postgres `LISTEN`:

```python
import psycopg2

conn = psycopg2.connect("dbname=message_store")
cur = conn.cursor()
cur.execute("LISTEN new_events")
conn.commit()

# Block until NOTIFY arrives — no polling loop
while True:
    conn.poll()
    while conn.notifies:
        notify = conn.notifies.pop(0)
        # Rebuild projections
```

This eliminates the GSA 1-second polling race entirely.

### What It Costs

- Requires a running PostgreSQL server (even for local dev)
- Schema migration on upgrade
- More operational surface area than a file
- `message-db-py` client is community-maintained, less mature than the Ruby/Node originals

### Verdict

**Strong upgrade path.** Solves the same concurrency problems as SQLite but adds pub/sub and single-database management. The operational cost is a Postgres server — acceptable for production, heavy for local dev.

---

## Alternative 3: Python `eventsourcing` Library

**PyPI:** `eventsourcing` (v9.5.5)  
**Author:** John Bywater  
**Backends:** SQLite, PostgreSQL, DynamoDB, In-memory

### What It Is

A mature Python library for event sourcing with aggregate roots, applications, repositories, and notification logs. Not a database — an abstraction layer over different persistence backends.

### Key Concepts

```python
from eventsourcing.domain import Aggregate, event
from eventsourcing.application import Application

class PipelineRun(Aggregate):
    @event("EffectAppended")
    def append_effect(self, effect_data: dict):
        self.effects.append(effect_data)

class DocumentaryApp(Application):
    def append_effect(self, run_id: str, effect: dict):
        run = self.repository.get(UUID(run_id))
        run.append_effect(effect)
        self.save(run)
```

### SQLite Backend

The library includes a first-class SQLite backend with connection pooling, cursor management, and notification logs:

```python
from eventsourcing.persistence import SQLiteApplicationRecorder

# Configured via constructor parameters (not environment variables)
recorder = SQLiteApplicationRecorder(db_name="pipeline.db")
```

### What It Gives Us

| Capability | `eventsourcing` Library |
|---|---|
| Backend abstraction | SQLite, Postgres, DynamoDB |
| Aggregate roots | Built-in event-sourced aggregates |
| Notification log | `select()` for incremental reads |
| Connection pooling | Built-in |
| **Schema management** | **Built-in** (auto-creates tables) |
| Replay | `repository.get(id)` reconstructs state |

### What It Costs

- **Learning curve:** Heavy abstraction — aggregates, repositories, applications, notification logs, mappers, transcoding
- **Opinionated:** Forces an aggregate-root model that may not match the pipeline's flat event stream
- **Indirection:** The library does a lot. Debugging means understanding its internals.
- **Overkill:** The pipeline doesn't need aggregate roots or CQRS. It just needs an append-only ordered log.

### Verdict

**Too heavy.** The library is excellent for domain-driven design with aggregates. The documentary pipeline is a flat event log with projections — no aggregates, no domain model. Using `eventsourcing` would be fighting its abstractions.

---

## Alternative 4: NATS JetStream

**What it is:** Persistence layer built into NATS server (single binary, no deps)  
**Model:** Streams (ordered message logs) + Consumers (durable subscriptions)

### What It Gives Us

| Capability | JetStream |
|---|---|
| Persistence | Messages stored on disk, replayable |
| Pub/Sub | Built-in — consumers subscribe to streams |
| Ordering | Per-subject ordering guarantees |
| Deduplication | Message dedup via `Nats-Msg-Id` header |
| Clustering | Built-in replication |
| **Push-based wake** | **Consumers can be push-based** |

### What It Costs

- **Not an event store** — it's a message broker. Messages can expire, be limited by count/size, or be auto-deleted.
- **No arbitrary replay** — consumers track their position, but you don't "query" a stream like a database.
- **Operational complexity** — running NATS server, configuring streams, managing consumers.
- **Schema drift** — no enforcement of event structure.

### Verdict

**Wrong tool.** JetStream is for messaging, not for being a source of truth. The pipeline needs an append-only, never-delete, queryable event log. NATS is designed for temporal decoupling of publishers/subscribers, not for long-term state reconstruction.

---

## Alternative 5: Redis Streams

**What it is:** Redis data type: ordered log of messages, persistent
**Model:** Streams (`XADD`, `XREAD`, `XRANGE`) + Consumer Groups

### What It Gives Us

| Capability | Redis Streams |
|---|---|
| Ordered append | Yes (millisecond timestamps + sequence) |
| Pub/Sub | `XREAD BLOCK` — blocking read on stream |
| Consumer groups | Yes — parallel consumption with ACK |
| Deduplication | No native dedup — must handle in app |
| Persistence | RDB + AOF configurable |

### What It Costs

- **In-memory first** — can be configured to persist, but memory is the primary store
- **No transactions** across multiple streams
- **No query language** — `XRANGE` for range reads only
- **Operational complexity** — Redis server, memory limits, eviction policies
- **Deduplication** — must be implemented client-side

### Verdict

**Wrong tool.** Redis Streams are great for real-time analytics and queueing. They're not designed to be a durable, queryable, never-delete source of truth. Memory constraints make them unsuitable for a documentary pipeline that accumulates thousands of events.

---

## Comparative Summary

| Alternative | Cross-Process Safe | Pub/Sub | Single DB | Operational Cost | Maturity | Fit for Pipeline |
|---|---|---|---|---|---|---|
| **SQLite (current)** | Yes WAL + IMMEDIATE | No | No file per run | None (stdlib) | Very high | Direct fit |
| **PostgreSQL + MessageDB** | Yes MVCC | Yes LISTEN/NOTIFY | Yes | Postgres server | High (Ruby/Node), Medium Python | Strong upgrade |
| **Python `eventsourcing`** | Yes (via backend) | No | Yes | pip install | High | Over-abstracted |
| **NATS JetStream** | Yes | Yes Built-in | Yes | NATS server | High | Not an event store |
| **Redis Streams** | Yes | Yes Built-in | Yes | Redis server | High | Memory-first, no dedup |

---

## Recommendation

### Immediate (V7.1): Stay with SQLite

SQLite with WAL mode solves all three fatal flaws identified by the auditor. No external dependencies. Zero operational cost. File-per-run is acceptable for documentary workloads (500–2000 events, <5 MB per run).

### Medium-term (V8.0): Evaluate PostgreSQL + MessageDB

If the pipeline needs:
- A single database for all runs (operational simplicity)
- Native pub/sub to eliminate GSA polling
- Stream categories for multi-run querying
- Consumer groups for fan-out processing

Then PostgreSQL + MessageDB is the natural upgrade. It preserves all SQLite semantics while adding pub/sub and single-DB management. The migration path is straightforward: MessageDB stores JSON payloads — the `Effect` serialization format remains identical.

### Not Recommended

- **`eventsourcing` library** — too opinionated, forces aggregate-root model
- **NATS JetStream** — not an event store, messages can expire
- **Redis Streams** — memory-first, no native deduplication, no querying
- **KurrentDB** — no macOS binary, licensed SQL feature, operational complexity

---

## The Pub/Sub Question

The GSA timing paradox (stale reads, lost wakes) is **orthogonal** to the event store choice. It can be solved three ways:

| Solution | Mechanism | Works With |
|---|---|---|
| **Polling** | GSA queries store every 1s | Any store |
| **Postgres LISTEN/NOTIFY** | Database pushes on INSERT | PostgreSQL + MessageDB |
| **Sweeper agent** | External process monitors agent health, POSTs wakes | Any store |
| **Self-polling** | Agent records last wake time; POSTs to self if idle too long | Any store |

SQLite cannot do pub/sub. The GSA must poll or a sweeper must exist. This is a conscious trade-off: SQLite gives us zero operational overhead at the cost of polling.

If pub/sub becomes critical, upgrade to PostgreSQL + MessageDB or add a lightweight sweeper process.

---

*Research conducted via Exa AI Search API. Total Exa cost: ~0.03 USD.*
