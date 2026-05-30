import sqlite3
from pathlib import Path
from contextlib import contextmanager
from pydantic import BaseModel
from effects import Effect, EffectUnion, KIND_TO_MODEL


class EventRecord(BaseModel):
    """A single immutable event in the log."""
    seq: int
    effect: EffectUnion
    otio_hash_before: str


class EventStore:
    """Append-only SQLite event store. One DB file per run.

    Cross-process safe via SQLite WAL mode + BEGIN IMMEDIATE.
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

        SQLite BEGIN IMMEDIATE acquires the write lock at the OS level,
        serializing all writers across processes.
        """
        self._init_db(run_id)

        effect_id = str(effect.effect_id)
        kind = effect.kind
        effect_json = effect.model_dump_json()
        agent = effect.agent
        timestamp = effect.timestamp.timestamp() if hasattr(effect.timestamp, "timestamp") else float(effect.timestamp)

        with self._connect(run_id) as conn:
            conn.execute("BEGIN IMMEDIATE")

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
            # Validate through EventRecord which handles validation of unions automatically
            record = EventRecord.model_validate({
                "seq": seq,
                "effect": KIND_TO_MODEL[Effect.model_validate_json(effect_json).kind].model_validate_json(effect_json),
                "otio_hash_before": otio_hash
            })
            return record

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
                    # Validate through EventRecord to load concrete Pydantic models
                    record = EventRecord.model_validate({
                        "seq": seq,
                        "effect": KIND_TO_MODEL[Effect.model_validate_json(effect_json).kind].model_validate_json(effect_json),
                        "otio_hash_before": otio_hash
                    })
                    records.append(record)
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
                    record = EventRecord.model_validate({
                        "seq": seq,
                        "effect": KIND_TO_MODEL[Effect.model_validate_json(effect_json).kind].model_validate_json(effect_json),
                        "otio_hash_before": otio_hash
                    })
                    records.append(record)
                except Exception:
                    continue
        return records

    def replay(self, run_id: str) -> list[EventRecord]:
        """Full replay from sequence 1."""
        return self.read_all(run_id)

    def export_to_jsonl(self, run_id: str, out_path: str) -> None:
        """Export events to JSONL for human inspection or backup."""
        records = self.read_all(run_id)
        with open(out_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(rec.model_dump_json() + "\n")
