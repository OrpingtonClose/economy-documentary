"""
Pipeline collector -- thread-safe event accumulator for pipeline runs.

Ported from MiroThinker. Adapted for documentary pipeline phases.
Tracks phases, tool calls, LLM calls, and produces snapshots for
SSE streaming and full dicts for JSON/HTML reports.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolEvent:
    tool_name: str
    agent: str
    args_summary: str
    start_time: float
    end_time: float = 0.0
    duration: float = 0.0
    result_chars: int = 0
    status: str = "running"


@dataclass
class LLMEvent:
    agent: str
    start_time: float
    end_time: float = 0.0
    estimated_tokens: int = 0
    output_tokens: int = 0
    status: str = "running"


@dataclass
class PhaseEvent:
    name: str
    start_time: float
    end_time: float = 0.0
    status: str = "running"


class PipelineCollector:
    """Thread-safe event accumulator for a single pipeline run."""

    def __init__(self, run_id: str = "", topic: str = ""):
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:8]}"
        self.topic = topic
        self.start_time = time.time()
        self.end_time: float = 0.0
        self.status: str = "running"

        self._lock = threading.Lock()
        self._phases: list[PhaseEvent] = []
        self._tools: list[ToolEvent] = []
        self._llm_calls: list[LLMEvent] = []
        self._events: list[dict] = []
        self._force_end: bool = False
        self._force_end_tokens: int = 0

    def phase_start(self, name: str) -> None:
        with self._lock:
            self._phases.append(PhaseEvent(name=name, start_time=time.time()))
            self._events.append(
                {
                    "type": "phase_start",
                    "name": name,
                    "time": time.time(),
                }
            )

    def phase_end(self, name: str, status: str = "completed") -> None:
        with self._lock:
            for phase in reversed(self._phases):
                if phase.name == name and phase.status == "running":
                    phase.end_time = time.time()
                    phase.status = status
                    break
            self._events.append(
                {
                    "type": "phase_end",
                    "name": name,
                    "status": status,
                    "time": time.time(),
                }
            )

    def tool_start(self, tool_name: str, agent: str, args_summary: str) -> None:
        with self._lock:
            self._tools.append(
                ToolEvent(
                    tool_name=tool_name,
                    agent=agent,
                    args_summary=args_summary,
                    start_time=time.time(),
                )
            )
            self._events.append(
                {
                    "type": "tool_start",
                    "tool": tool_name,
                    "agent": agent,
                    "time": time.time(),
                }
            )

    def tool_end(
        self,
        tool_name: str,
        agent: str,
        duration: float,
        result_chars: int = 0,
    ) -> None:
        with self._lock:
            for te in reversed(self._tools):
                if (
                    te.tool_name == tool_name
                    and te.agent == agent
                    and te.status == "running"
                ):
                    te.end_time = time.time()
                    te.duration = duration
                    te.result_chars = result_chars
                    te.status = "completed"
                    break
            self._events.append(
                {
                    "type": "tool_end",
                    "tool": tool_name,
                    "agent": agent,
                    "duration": round(duration, 2),
                    "result_chars": result_chars,
                    "time": time.time(),
                }
            )

    def llm_start(self, agent: str, estimated_tokens: int) -> None:
        with self._lock:
            self._llm_calls.append(
                LLMEvent(
                    agent=agent,
                    start_time=time.time(),
                    estimated_tokens=estimated_tokens,
                )
            )

    def llm_end(self, agent: str, duration: float, output_tokens: int) -> None:
        with self._lock:
            for le in reversed(self._llm_calls):
                if le.agent == agent and le.status == "running":
                    le.end_time = time.time()
                    le.output_tokens = output_tokens
                    le.status = "completed"
                    break

    def force_end(self, token_estimate: int) -> None:
        with self._lock:
            self._force_end = True
            self._force_end_tokens = token_estimate
            self._events.append(
                {
                    "type": "force_end",
                    "token_estimate": token_estimate,
                    "time": time.time(),
                }
            )

    def snapshot(self) -> dict:
        """Lightweight snapshot for SSE streaming."""
        with self._lock:
            elapsed = time.time() - self.start_time
            active_phase = None
            for p in reversed(self._phases):
                if p.status == "running":
                    active_phase = p.name
                    break

            return {
                "run_id": self.run_id,
                "topic": self.topic,
                "status": self.status,
                "elapsed_sec": round(elapsed, 1),
                "active_phase": active_phase,
                "phases_completed": sum(
                    1 for p in self._phases if p.status == "completed"
                ),
                "total_tools": len(self._tools),
                "total_llm_calls": len(self._llm_calls),
                "force_end": self._force_end,
                "recent_events": self._events[-10:],
            }

    def _build_report(self) -> dict:
        """Build the full report dict (must be called under ``self._lock``)."""
        now = time.time()
        elapsed = now - self.start_time
        return {
            "run_id": self.run_id,
            "topic": self.topic,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time or now,
            "elapsed_sec": round(elapsed, 1),
            "phases": [
                {
                    "name": p.name,
                    "status": p.status,
                    "duration": round(
                        (p.end_time or now) - p.start_time, 2
                    ),
                }
                for p in self._phases
            ],
            "tools": [
                {
                    "tool": t.tool_name,
                    "agent": t.agent,
                    "duration": round(t.duration, 2),
                    "result_chars": t.result_chars,
                }
                for t in self._tools
            ],
            "llm_calls": [
                {
                    "agent": le.agent,
                    "estimated_tokens": le.estimated_tokens,
                    "output_tokens": le.output_tokens,
                }
                for le in self._llm_calls
            ],
            "events": list(self._events),
            "force_end": self._force_end,
        }

    def to_report_dict(self) -> dict:
        """Full dict for HTML reports without changing collector status.

        Unlike finalize(), this does not mark the run as completed —
        safe to call on active collectors for live HTML reports.
        """
        with self._lock:
            return self._build_report()

    def finalize(self, status: str = "completed") -> dict:
        """Mark run as completed and return full report dict."""
        with self._lock:
            self.end_time = time.time()
            self.status = status
            return self._build_report()
