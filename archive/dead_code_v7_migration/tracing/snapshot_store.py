"""Event sourcing / state snapshot system for the documentary pipeline.

Synchronous SQLite store attached to the existing trace DB.
Every agent action is recorded with full fidelity for resume support.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = os.path.expanduser(
    "~/Documents/documentary-pipeline/traces/pipeline.db"
)
_DB_PATH = os.environ.get("PIPELINE_TRACE_DB", _DEFAULT_DB_PATH)

# Valid agents in the documentary pipeline
AGENT_NAMES = frozenset({
    "orchestrator",
    "scenario",
    "audio",
    "video",
    "assembly",
    "production",
    "visual",
    "escalation",
    "otio",
})

# Valid event types
EVENT_TYPES = frozenset({
    "tool_call",
    "llm_turn",
    "graph_transition",
    "vm_state",
    "otio_state",
    "file_state",
    "decision",
})

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    agent_name TEXT,
    payload TEXT NOT NULL,
    sequence_num INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_run_seq
    ON snapshots(run_id, sequence_num);
CREATE INDEX IF NOT EXISTS idx_snapshots_run_ts
    ON snapshots(run_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_snapshots_run_event
    ON snapshots(run_id, event_type);
"""


@dataclass
class ResumeContext:
    """Reconstructable state bundle produced from a snapshot query."""

    run_id: str
    timestamp: float
    sequence_num: int
    current_stage: str = ""
    otio_state: dict[str, Any] = field(default_factory=dict)
    vm_state: dict[str, Any] = field(default_factory=dict)
    file_state: dict[str, Any] = field(default_factory=dict)
    latest_decision: dict[str, Any] = field(default_factory=dict)
    last_llm_turn: dict[str, Any] = field(default_factory=dict)
    last_tool_call: dict[str, Any] = field(default_factory=dict)
    graph_history: list[dict[str, Any]] = field(default_factory=list)

    def to_state_dict(self) -> dict[str, Any]:
        """Flatten into a dict suitable for seeding a LangGraph state."""
        return {
            "_resume_run_id": self.run_id,
            "_resume_timestamp": self.timestamp,
            "_resume_sequence": self.sequence_num,
            "_resume_stage": self.current_stage,
            "_otio_state": self.otio_state,
            "_vm_state": self.vm_state,
            "_file_state": self.file_state,
            "_latest_decision": self.latest_decision,
            "_last_llm_turn": self.last_llm_turn,
            "_last_tool_call": self.last_tool_call,
            "_graph_history": self.graph_history,
        }

    @classmethod
    def from_snapshot_row(cls, row: sqlite3.Row) -> "ResumeContext":
        payload = json.loads(row["payload"])
        base = cls(
            run_id=row["run_id"],
            timestamp=row["timestamp"],
            sequence_num=row["sequence_num"],
        )
        # The payload itself may contain the sub-state; if not, keep defaults.
        base.current_stage = payload.get("stage", payload.get("current_stage", ""))
        base.otio_state = payload.get("otio_state", {})
        base.vm_state = payload.get("vm_state", {})
        base.file_state = payload.get("file_state", {})
        base.latest_decision = payload.get("decision", {})
        base.last_llm_turn = payload.get("llm_turn", {})
        base.last_tool_call = payload.get("tool_call", {})
        base.graph_history = payload.get("graph_history", [])
        return base


class SnapshotStore:
    """Synchronous, unbuffered event-sourcing store for pipeline snapshots.

    All write methods commit immediately so that a crash never loses
    critical resume state.  Reads are served from the same WAL-enabled
    SQLite file that the auto-tracer already uses.
    """

    _instance_lock = threading.Lock()
    _instance: Optional["SnapshotStore"] = None
    _db_path: str
    _local: threading.local

    def __new__(cls, db_path: Optional[str] = None) -> "SnapshotStore":
        # Singleton per db_path so multiple hooks share one connection pool.
        path = db_path or _DB_PATH
        with cls._instance_lock:
            if cls._instance is None or cls._instance._db_path != path:
                instance = super().__new__(cls)
                instance._db_path = path
                instance._local = threading.local()
                instance._init_db()
                cls._instance = instance
            return cls._instance

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(self._db_path, timeout=10)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _next_sequence(self, run_id: str) -> int:
        row = (
            self._conn()
            .execute(
                "SELECT MAX(sequence_num) FROM snapshots WHERE run_id = ?",
                (run_id,),
            )
            .fetchone()
        )
        return (row[0] or 0) + 1

    def _insert(
        self,
        run_id: str,
        event_type: str,
        agent_name: str,
        payload: dict[str, Any],
    ) -> int:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Invalid event_type {event_type!r}")
        ts = time.time()
        seq = self._next_sequence(run_id)
        conn = self._conn()
        cur = conn.execute(
            """
            INSERT INTO snapshots
                (run_id, timestamp, event_type, agent_name, payload, sequence_num)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, ts, event_type, agent_name, json.dumps(payload, default=str), seq),
        )
        conn.commit()
        row_id = cur.lastrowid
        logger.debug(
            "snapshot #%d %s/%s seq=%d",
            row_id,
            run_id,
            event_type,
            seq,
        )
        return row_id if row_id is not None else -1

    # ------------------------------------------------------------------
    # Public record API
    # ------------------------------------------------------------------
    def record_tool_call(
        self,
        agent: str,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        duration_ms: float,
        *,
        run_id: str,
    ) -> int:
        """Record a complete tool invocation with full fidelity."""
        return self._insert(
            run_id=run_id,
            event_type="tool_call",
            agent_name=agent,
            payload={
                "tool_name": tool_name,
                "args": args,
                "result": result,
                "duration_ms": duration_ms,
                "stage": self._infer_stage(agent),
            },
        )

    def record_llm_request(
        self,
        agent: str,
        messages: list[dict[str, Any]],
        model: str,
        params: dict[str, Any],
        *,
        run_id: str,
    ) -> int:
        """Record an outgoing LLM request before it is sent."""
        return self._insert(
            run_id=run_id,
            event_type="llm_turn",
            agent_name=agent,
            payload={
                "phase": "request",
                "model": model,
                "messages": messages,
                "params": params,
                "stage": self._infer_stage(agent),
            },
        )

    def record_llm_response(
        self,
        agent: str,
        response_text: str,
        usage: dict[str, Any],
        duration_ms: float,
        *,
        run_id: str,
    ) -> int:
        """Record the LLM response after it returns."""
        return self._insert(
            run_id=run_id,
            event_type="llm_turn",
            agent_name=agent,
            payload={
                "phase": "response",
                "response_text": response_text,
                "usage": usage,
                "duration_ms": duration_ms,
                "stage": self._infer_stage(agent),
            },
        )

    def record_graph_transition(
        self,
        from_node: str,
        to_node: str,
        reason: str,
        *,
        run_id: str,
        agent: str = "orchestrator",
    ) -> int:
        """Record a LangGraph (or Strands) node transition."""
        return self._insert(
            run_id=run_id,
            event_type="graph_transition",
            agent_name=agent,
            payload={
                "from_node": from_node,
                "to_node": to_node,
                "reason": reason,
                "stage": to_node,
            },
        )

    def record_vm_state(
        self,
        vms_json: dict[str, Any],
        *,
        run_id: str,
        agent: str = "orchestrator",
    ) -> int:
        """Record a complete snapshot of all VM IDs, IPs, ports, health."""
        return self._insert(
            run_id=run_id,
            event_type="vm_state",
            agent_name=agent,
            payload={
                "vms": vms_json,
                "timestamp": time.time(),
                "stage": self._infer_stage(agent),
            },
        )

    def record_otio_state(
        self,
        otio_json: dict[str, Any],
        *,
        run_id: str,
        agent: str = "otio",
    ) -> int:
        """Record the full OTIO timeline serialized to JSON."""
        return self._insert(
            run_id=run_id,
            event_type="otio_state",
            agent_name=agent,
            payload={
                "otio": otio_json,
                "timestamp": time.time(),
                "stage": self._infer_stage(agent),
            },
        )

    def record_file_state(
        self,
        files_json: dict[str, Any],
        *,
        run_id: str,
        agent: str = "orchestrator",
    ) -> int:
        """Record which audio/video files exist and their sizes."""
        return self._insert(
            run_id=run_id,
            event_type="file_state",
            agent_name=agent,
            payload={
                "files": files_json,
                "timestamp": time.time(),
                "stage": self._infer_stage(agent),
            },
        )

    def record_agent_decision(
        self,
        agent: str,
        decision_type: str,
        payload: dict[str, Any],
        *,
        run_id: str,
    ) -> int:
        """Record a high-level agent decision (escalation, retry, skip, etc.)."""
        return self._insert(
            run_id=run_id,
            event_type="decision",
            agent_name=agent,
            payload={
                "decision_type": decision_type,
                "decision_payload": payload,
                "stage": self._infer_stage(agent),
            },
        )

    # ------------------------------------------------------------------
    # Query / resume API
    # ------------------------------------------------------------------
    def get_latest_snapshot(self, run_id: str) -> Optional[ResumeContext]:
        """Return the most recent complete snapshot for a run."""
        row = (
            self._conn()
            .execute(
                "SELECT * FROM snapshots WHERE run_id = ? ORDER BY sequence_num DESC LIMIT 1",
                (run_id,),
            )
            .fetchone()
        )
        return ResumeContext.from_snapshot_row(row) if row else None

    def get_snapshot_at_time(
        self, run_id: str, timestamp: float
    ) -> Optional[ResumeContext]:
        """Return the snapshot closest to the given timestamp."""
        row = (
            self._conn()
            .execute(
                """
                SELECT * FROM snapshots
                WHERE run_id = ?
                ORDER BY ABS(timestamp - ?) ASC, sequence_num DESC
                LIMIT 1
                """,
                (run_id, timestamp),
            )
            .fetchone()
        )
        return ResumeContext.from_snapshot_row(row) if row else None

    def list_snapshots(
        self,
        run_id: str,
        event_type: Optional[str] = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return all snapshot points for a run, newest first."""
        conn = self._conn()
        if event_type:
            rows = conn.execute(
                """
                SELECT snapshot_id, run_id, timestamp, event_type,
                       agent_name, payload, sequence_num
                FROM snapshots
                WHERE run_id = ? AND event_type = ?
                ORDER BY sequence_num DESC
                LIMIT ?
                """,
                (run_id, event_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT snapshot_id, run_id, timestamp, event_type,
                       agent_name, payload, sequence_num
                FROM snapshots
                WHERE run_id = ?
                ORDER BY sequence_num DESC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def reconstruct_state(self, run_id: str) -> ResumeContext:
        """Rebuild the best possible ResumeContext from the latest events.

        Walks backward through the snapshot stream, overlaying the most
        recent otio_state, vm_state, file_state, decision, and graph
        transition onto a single ResumeContext.
        """
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT * FROM snapshots
            WHERE run_id = ?
            ORDER BY sequence_num DESC
            LIMIT 5000
            """,
            (run_id,),
        ).fetchall()

        ctx = ResumeContext(run_id=run_id, timestamp=time.time(), sequence_num=0)
        if not rows:
            return ctx

        # First row is the latest overall
        ctx.timestamp = rows[0]["timestamp"]
        ctx.sequence_num = rows[0]["sequence_num"]

        # Walk backward, filling in missing state fragments
        seen_otio = False
        seen_vm = False
        seen_file = False
        seen_decision = False
        seen_llm = False
        seen_tool = False

        for row in rows:
            payload = json.loads(row["payload"])
            etype = row["event_type"]

            if etype == "graph_transition":
                ctx.graph_history.append({
                    "from": payload.get("from_node"),
                    "to": payload.get("to_node"),
                    "reason": payload.get("reason"),
                    "timestamp": row["timestamp"],
                })
                if not ctx.current_stage:
                    ctx.current_stage = payload.get("to_node", "")

            elif etype == "otio_state" and not seen_otio:
                ctx.otio_state = payload.get("otio", {})
                seen_otio = True

            elif etype == "vm_state" and not seen_vm:
                ctx.vm_state = payload.get("vms", {})
                seen_vm = True

            elif etype == "file_state" and not seen_file:
                ctx.file_state = payload.get("files", {})
                seen_file = True

            elif etype == "decision" and not seen_decision:
                ctx.latest_decision = payload.get("decision_payload", {})
                ctx.latest_decision["_type"] = payload.get("decision_type", "")
                seen_decision = True

            elif etype == "llm_turn" and not seen_llm:
                ctx.last_llm_turn = payload
                seen_llm = True

            elif etype == "tool_call" and not seen_tool:
                ctx.last_tool_call = payload
                seen_tool = True

        # Reverse graph history so it's chronological
        ctx.graph_history.reverse()
        return ctx

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _infer_stage(agent: str) -> str:
        """Best-effort stage inference from agent name."""
        agent_lower = agent.lower()
        for stage in ("scenario", "audio", "video", "visual", "production", "assembly", "otio"):
            if stage in agent_lower:
                return stage
        return agent_lower


# ------------------------------------------------------------------
# Convenience module-level helpers for one-off recording
# ------------------------------------------------------------------

def get_store(db_path: Optional[str] = None) -> SnapshotStore:
    """Return the singleton SnapshotStore instance."""
    return SnapshotStore(db_path)


def record_tool_call(
    agent: str,
    tool_name: str,
    args: dict[str, Any],
    result: Any,
    duration_ms: float,
    *,
    run_id: str,
) -> int:
    return get_store().record_tool_call(
        agent, tool_name, args, result, duration_ms, run_id=run_id
    )


def record_llm_request(
    agent: str,
    messages: list[dict[str, Any]],
    model: str,
    params: dict[str, Any],
    *,
    run_id: str,
) -> int:
    return get_store().record_llm_request(
        agent, messages, model, params, run_id=run_id
    )


def record_llm_response(
    agent: str,
    response_text: str,
    usage: dict[str, Any],
    duration_ms: float,
    *,
    run_id: str,
) -> int:
    return get_store().record_llm_response(
        agent, response_text, usage, duration_ms, run_id=run_id
    )


def record_graph_transition(
    from_node: str,
    to_node: str,
    reason: str,
    *,
    run_id: str,
    agent: str = "orchestrator",
) -> int:
    return get_store().record_graph_transition(
        from_node, to_node, reason, run_id=run_id, agent=agent
    )


def record_vm_state(
    vms_json: dict[str, Any],
    *,
    run_id: str,
    agent: str = "orchestrator",
) -> int:
    return get_store().record_vm_state(vms_json, run_id=run_id, agent=agent)


def record_otio_state(
    otio_json: dict[str, Any],
    *,
    run_id: str,
    agent: str = "otio",
) -> int:
    return get_store().record_otio_state(otio_json, run_id=run_id, agent=agent)


def record_file_state(
    files_json: dict[str, Any],
    *,
    run_id: str,
    agent: str = "orchestrator",
) -> int:
    return get_store().record_file_state(files_json, run_id=run_id, agent=agent)


def record_agent_decision(
    agent: str,
    decision_type: str,
    payload: dict[str, Any],
    *,
    run_id: str,
) -> int:
    return get_store().record_agent_decision(
        agent, decision_type, payload, run_id=run_id
    )


def get_latest_snapshot(run_id: str) -> Optional[ResumeContext]:
    return get_store().get_latest_snapshot(run_id)


def get_snapshot_at_time(run_id: str, timestamp: float) -> Optional[ResumeContext]:
    return get_store().get_snapshot_at_time(run_id, timestamp)


def list_snapshots(
    run_id: str,
    event_type: Optional[str] = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    return get_store().list_snapshots(run_id, event_type, limit)


def reconstruct_state(run_id: str) -> ResumeContext:
    return get_store().reconstruct_state(run_id)
