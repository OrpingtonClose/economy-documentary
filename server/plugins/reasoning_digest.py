"""
ReasoningDigestEngine — batch-processes raw reasoning traces into concise
summaries that won't overwhelm the observer but preserve important specifics.

Sits between the raw ``reasoning_traces.db`` (written by ReasoningTracePlugin)
and the frontend.  Runs as a background thread, periodically reading new raw
traces, grouping them by agent + time window, and producing structured digests.

No LLM calls — this is pure rule-based pattern extraction.  Signals preserved:

- Quality ratings from evaluator agents (EXCELLENT / GOOD / FAIR / POOR)
- Errors and retry counts
- Token costs (input/output per agent burst)
- Tool results and failures
- Agent transitions (handoffs)
- Production planning decisions (batch structure, worker assignments)
- Duration and progress metrics

Usage::

    from plugins.reasoning_digest import DigestEngine, get_digest_engine

    engine = get_digest_engine()       # singleton, auto-starts background thread
    digests = engine.get_recent(50)    # latest 50 digests for frontend
    digests = engine.get_since(ts)     # incremental poll
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

_FINDINGS_DIR = os.environ.get(
    "FINDINGS_DIR",
    os.path.join(os.path.expanduser("~"), ".documentary-pipeline"),
)
_REASONING_DB = os.path.join(_FINDINGS_DIR, "reasoning_traces.db")
_DIGEST_DB = os.path.join(_FINDINGS_DIR, "reasoning_digests.db")

# How often the background thread processes new raw traces (seconds)
_POLL_INTERVAL = 3.0

# Time window to group events from the same agent into one digest (seconds)
_GROUP_WINDOW = 10.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class Importance(str, Enum):
    LOW = "low"          # routine lifecycle events
    MEDIUM = "medium"    # completions, transitions, normal results
    HIGH = "high"        # errors, retries, quality issues, decisions


@dataclass
class Digest:
    """A concise summary of a burst of agent activity."""
    id: int = 0
    timestamp: float = 0.0
    agent: str = ""
    phase: str = ""         # pipeline phase (scenario, audio, visual, production, assembly)
    importance: Importance = Importance.MEDIUM
    summary: str = ""       # 1-2 sentence human-readable summary
    details: dict = field(default_factory=dict)   # structured specifics
    raw_trace_ids: list = field(default_factory=list)  # link back to raw traces

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "agent": self.agent,
            "phase": self.phase,
            "importance": self.importance.value,
            "summary": self.summary,
            "details": self.details,
            "raw_trace_ids": self.raw_trace_ids,
        }


# ---------------------------------------------------------------------------
# Signal extractors — mine structured info from raw trace content
# ---------------------------------------------------------------------------

# Rating patterns from evaluator agents
_RATING_RE = re.compile(
    r'"?rating"?\s*[:=]\s*"?(\d|EXCELLENT|GOOD|FAIR|POOR)"?',
    re.IGNORECASE,
)

# Feedback / focus_areas from evaluator responses
_FEEDBACK_RE = re.compile(
    r'"?feedback"?\s*:\s*"([^"]{5,300})"',
    re.IGNORECASE,
)

_FOCUS_RE = re.compile(
    r'"?focus_areas"?\s*:\s*\[([^\]]{5,300})\]',
    re.IGNORECASE,
)

# Token cost patterns
_DURATION_RE = re.compile(r'duration[_\s]*(?:sec|s)?\s*[:=]\s*(\d+\.?\d*)', re.IGNORECASE)

# Error patterns
_ERROR_KEYWORDS = {"error", "failed", "exception", "traceback", "timeout", "refused", "oom"}

# Production plan patterns
_BATCH_RE = re.compile(r'"?batches"?\s*:\s*(\d+)', re.IGNORECASE)
_STRATEGY_RE = re.compile(r'"?strategy"?\s*:\s*"([^"]{5,100})"', re.IGNORECASE)
_GPU_MIN_RE = re.compile(r'"?estimated_gpu_minutes"?\s*:\s*(\d+\.?\d*)', re.IGNORECASE)

# Agent → phase mapping
_AGENT_PHASE = {
    "scenario_generator": "scenario",
    "scenario_evaluator": "scenario",
    "scenario_director": "scenario",
    "content_analyst": "visual_direction",
    "visual_concepter": "visual_direction",
    "coherence_evaluator": "visual_direction",
    "audio_agent": "audio",
    "production_supervisor": "production",
    "production_planner": "production",
    "production_evaluator": "production",
    "production_replanner": "production",
    "assembler_agent": "assembly",
    "documentary_pipeline": "pipeline",
}


def _infer_phase(agent_name: str) -> str:
    """Infer pipeline phase from agent name."""
    return _AGENT_PHASE.get(agent_name, "unknown")


def _extract_rating(text: str) -> Optional[str]:
    m = _RATING_RE.search(text)
    if m:
        val = m.group(1).upper()
        rating_map = {"0": "POOR", "1": "FAIR", "2": "GOOD", "3": "EXCELLENT"}
        return rating_map.get(val, val)
    return None


def _extract_feedback(text: str) -> Optional[str]:
    m = _FEEDBACK_RE.search(text)
    return m.group(1) if m else None


def _extract_focus_areas(text: str) -> list[str]:
    m = _FOCUS_RE.search(text)
    if m:
        return [a.strip().strip('"') for a in m.group(1).split(",")]
    return []


def _has_error_signal(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _ERROR_KEYWORDS)


def _extract_plan_info(text: str) -> dict:
    info = {}
    m = _BATCH_RE.search(text)
    if m:
        info["batches"] = int(m.group(1))
    m = _STRATEGY_RE.search(text)
    if m:
        info["strategy"] = m.group(1)
    m = _GPU_MIN_RE.search(text)
    if m:
        info["estimated_gpu_minutes"] = float(m.group(1))
    return info


# ---------------------------------------------------------------------------
# Digest generation — converts a group of raw traces into a Digest
# ---------------------------------------------------------------------------

def _generate_digest(agent: str, traces: list[dict]) -> Digest:
    """Convert a burst of raw traces from one agent into a concise digest."""
    phase = _infer_phase(agent)
    importance = Importance.MEDIUM
    raw_ids = [t["id"] for t in traces]

    # Classify events
    llm_requests = [t for t in traces if t["event_type"] == "llm_request"]
    llm_responses = [t for t in traces if t["event_type"] == "llm_response"]
    tool_starts = [t for t in traces if t["event_type"] == "tool_started"]
    tool_completes = [t for t in traces if t["event_type"] == "tool_completed"]
    errors = [t for t in traces if t["event_type"] in ("llm_error", "tool_error")]
    agent_events = [t for t in traces if t["event_type"] == "agent_event"]
    agent_started = [t for t in traces if t["event_type"] == "agent_started"]
    agent_completed = [t for t in traces if t["event_type"] == "agent_completed"]

    # Token totals
    total_in = sum(t.get("tokens_in") or 0 for t in llm_responses)
    total_out = sum(t.get("tokens_out") or 0 for t in llm_responses)

    # Extract signals from response content
    all_response_text = " ".join(t.get("content", "") for t in llm_responses)
    rating = _extract_rating(all_response_text)
    feedback = _extract_feedback(all_response_text)
    focus_areas = _extract_focus_areas(all_response_text)
    plan_info = _extract_plan_info(all_response_text)

    # Extract tool names
    tool_names = []
    for t in tool_starts + tool_completes:
        meta = t.get("metadata", {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        tool_name = meta.get("tool", "")
        if tool_name and tool_name not in tool_names:
            tool_names.append(tool_name)

    # Build details dict
    details: dict = {}
    if total_in or total_out:
        details["tokens"] = {"in": total_in, "out": total_out}
    if rating:
        details["rating"] = rating
    if feedback:
        details["feedback"] = feedback
    if focus_areas:
        details["focus_areas"] = focus_areas
    if plan_info:
        details["plan"] = plan_info
    if tool_names:
        details["tools_used"] = tool_names
    if errors:
        details["errors"] = [e.get("content", "")[:200] for e in errors]
        importance = Importance.HIGH

    # Build summary
    parts = []

    if agent_started and not llm_requests and not tool_starts:
        # Pure lifecycle event
        parts.append(f"**{agent}** started")
        importance = Importance.LOW
    elif agent_completed and not llm_requests and not tool_starts:
        parts.append(f"**{agent}** completed")
        importance = Importance.LOW
    elif errors:
        err_types = set()
        for e in errors:
            if "timeout" in e.get("content", "").lower():
                err_types.add("timeout")
            elif "oom" in e.get("content", "").lower():
                err_types.add("OOM")
            else:
                err_types.add("error")
        parts.append(
            f"**{agent}** hit {len(errors)} {'error' if len(errors) == 1 else 'errors'} "
            f"({', '.join(err_types)})"
        )
        if errors[0].get("content"):
            parts.append(f": {errors[0]['content'][:150]}")
    elif rating:
        # Evaluator result
        parts.append(f"**{agent}** rated: **{rating}**")
        if feedback:
            parts.append(f". {feedback[:150]}")
        if focus_areas:
            parts.append(f" Focus: {', '.join(focus_areas[:3])}")
    elif plan_info:
        # Production planning
        parts.append(f"**{agent}** produced plan")
        if plan_info.get("batches"):
            parts.append(f": {plan_info['batches']} batches")
        if plan_info.get("strategy"):
            parts.append(f", strategy={plan_info['strategy']}")
        if plan_info.get("estimated_gpu_minutes"):
            parts.append(f", ~{plan_info['estimated_gpu_minutes']:.0f} GPU-min")
    elif tool_names:
        # Tool-heavy burst
        n_tools = len(tool_starts)
        tools_str = ", ".join(tool_names[:3])
        if len(tool_names) > 3:
            tools_str += f" +{len(tool_names) - 3} more"
        parts.append(f"**{agent}** called {n_tools} tool(s): {tools_str}")
        # Add result preview from last tool completion
        if tool_completes:
            last_result = tool_completes[-1].get("content", "")
            if last_result and len(last_result) > 10:
                preview = last_result[:100]
                if len(last_result) > 100:
                    preview += "…"
                parts.append(f" → {preview}")
    elif llm_responses:
        # LLM conversation burst
        n_calls = len(llm_responses)
        parts.append(f"**{agent}** made {n_calls} LLM call{'s' if n_calls > 1 else ''}")
        if total_in or total_out:
            parts.append(f" [{total_in:,}→{total_out:,} tokens]")
        # Preview the last response (most likely the meaningful one)
        last_content = llm_responses[-1].get("content", "")
        if last_content:
            preview = last_content[:200]
            if len(last_content) > 200:
                preview += "…"
            parts.append(f". Output: {preview}")
    elif agent_events:
        # Agent text events
        last_event = agent_events[-1].get("content", "")
        parts.append(f"**{agent}**: {last_event[:200]}")
    else:
        parts.append(f"**{agent}** activity ({len(traces)} events)")

    summary = "".join(parts)

    # Elevate importance for key signals
    if rating in ("POOR", "FAIR"):
        importance = Importance.HIGH
    elif rating == "EXCELLENT":
        importance = Importance.MEDIUM
    if plan_info:
        importance = Importance.HIGH  # production planning is always important

    return Digest(
        timestamp=traces[0]["timestamp"],
        agent=agent,
        phase=phase,
        importance=importance,
        summary=summary,
        details=details,
        raw_trace_ids=raw_ids,
    )


# ---------------------------------------------------------------------------
# SQLite digest store
# ---------------------------------------------------------------------------

_CREATE_DIGEST_TABLE = """\
CREATE TABLE IF NOT EXISTS digests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL    NOT NULL,
    agent       TEXT    NOT NULL,
    phase       TEXT    NOT NULL DEFAULT '',
    importance  TEXT    NOT NULL DEFAULT 'medium',
    summary     TEXT    NOT NULL,
    details     TEXT    NOT NULL DEFAULT '{}',
    raw_trace_ids TEXT  NOT NULL DEFAULT '[]'
);
"""


class _DigestStore:
    """Thread-safe SQLite store for reasoning digests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        os.makedirs(_FINDINGS_DIR, exist_ok=True)
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                _DIGEST_DB, timeout=10, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_schema(self) -> None:
        with self._lock:
            self._get_conn().execute(_CREATE_DIGEST_TABLE)
            self._get_conn().commit()

    def write(self, digest: Digest) -> int:
        with self._lock:
            cur = self._get_conn().execute(
                "INSERT INTO digests "
                "(timestamp, agent, phase, importance, summary, details, raw_trace_ids) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    digest.timestamp,
                    digest.agent,
                    digest.phase,
                    digest.importance.value,
                    digest.summary,
                    json.dumps(digest.details, default=str),
                    json.dumps(digest.raw_trace_ids),
                ),
            )
            self._get_conn().commit()
            return cur.lastrowid or 0

    def get_recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._get_conn().execute(
                "SELECT * FROM digests ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        result = [self._row_to_dict(r) for r in rows]
        result.reverse()  # chronological
        return result

    def get_since(self, timestamp: float, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._get_conn().execute(
                "SELECT * FROM digests WHERE timestamp > ? ORDER BY id ASC LIMIT ?",
                (timestamp, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "agent": row["agent"],
            "phase": row["phase"],
            "importance": row["importance"],
            "summary": row["summary"],
            "details": json.loads(row["details"]) if row["details"] else {},
            "raw_trace_ids": json.loads(row["raw_trace_ids"]) if row["raw_trace_ids"] else [],
        }


# ---------------------------------------------------------------------------
# DigestEngine — background processor
# ---------------------------------------------------------------------------

class DigestEngine:
    """Background processor that reads raw traces and produces digests.

    Call ``start()`` to begin processing.  The engine reads raw traces from
    ``reasoning_traces.db``, groups them by agent + time window, generates
    concise digests, and writes them to ``reasoning_digests.db``.
    """

    def __init__(self) -> None:
        self._store = _DigestStore()
        self._last_raw_id = 0  # track how far we've read
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="digest-engine"
        )
        self._thread.start()
        logger.info("DigestEngine started — polling every %.1fs", _POLL_INTERVAL)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_recent(self, limit: int = 50) -> list[dict]:
        return self._store.get_recent(limit)

    def get_since(self, timestamp: float, limit: int = 100) -> list[dict]:
        return self._store.get_since(timestamp, limit)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._process_new_traces()
            except Exception as e:
                logger.error("DigestEngine error: %s", e)
            self._stop.wait(_POLL_INTERVAL)

    def _process_new_traces(self) -> None:
        """Read new raw traces and group them into digests."""
        try:
            conn = sqlite3.connect(_REASONING_DB, timeout=5)
            conn.row_factory = sqlite3.Row
        except Exception:
            return  # DB not created yet

        try:
            rows = conn.execute(
                "SELECT * FROM reasoning_log WHERE id > ? ORDER BY id ASC LIMIT 500",
                (self._last_raw_id,),
            ).fetchall()
        except Exception:
            conn.close()
            return

        conn.close()

        if not rows:
            return

        # Convert to dicts
        raw: list[dict] = []
        for row in rows:
            meta = row["metadata"]
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            raw.append({
                "id": row["id"],
                "timestamp": row["timestamp"],
                "event_type": row["event_type"],
                "agent_name": row["agent_name"],
                "model": row["model"],
                "content": row["content"] or "",
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "metadata": meta,
            })

        self._last_raw_id = raw[-1]["id"]

        # Group by agent + time window
        groups = self._group_traces(raw)

        # Generate digests
        for agent, traces in groups.items():
            try:
                digest = _generate_digest(agent, traces)
                self._store.write(digest)
            except Exception as e:
                logger.error("DigestEngine: failed to digest %s: %s", agent, e)

    @staticmethod
    def _group_traces(raw: list[dict]) -> dict[str, list[dict]]:
        """Group traces by agent, splitting on time gaps > _GROUP_WINDOW."""
        groups: dict[str, list[dict]] = {}
        agent_last_ts: dict[str, float] = {}

        for trace in raw:
            agent = trace["agent_name"]
            ts = trace["timestamp"]

            # If this agent had a previous event and the gap is large,
            # flush the current group and start a new one
            key = agent
            if agent in agent_last_ts:
                gap = ts - agent_last_ts[agent]
                if gap > _GROUP_WINDOW and agent in groups:
                    # This burst is separate — use a numbered key
                    key = f"{agent}___{ts}"

            if key not in groups:
                groups[key] = []
            groups[key].append(trace)
            agent_last_ts[agent] = ts

        # Flatten keys back to agent names (the key is just for grouping)
        result: dict[str, list[dict]] = {}
        for key, traces in groups.items():
            agent = key.split("___")[0]
            # Use a unique key per group
            group_key = f"{agent}_{traces[0]['id']}"
            result[group_key] = traces

        return result


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_engine: Optional[DigestEngine] = None
_engine_lock = threading.Lock()


def get_digest_engine() -> DigestEngine:
    """Get or create the singleton DigestEngine (auto-starts background thread)."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = DigestEngine()
            _engine.start()
    return _engine
