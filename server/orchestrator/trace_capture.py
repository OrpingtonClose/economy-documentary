"""
Trace capture for across-run learning.

Captures production run traces (agent decisions, tool calls, QA outcomes,
timing data) and persists them so the eval runner and optimizer can analyze
patterns across multiple production runs.

Architecture:
    Production run → TraceCapture collects events → saves to traces/ dir
    → run_eval.py loads traces → evaluates metrics → run_eval --optimize
    → reads eval history → generates improved instructions

Usage:
    # At pipeline start:
    from orchestrator.trace_capture import get_trace_capture
    capture = get_trace_capture()
    capture.start_run(pipeline_key="pag-2024-001", topic="PAG brain region")

    # During production:
    capture.record_event("plan_created", {"batches": 5, "clips": 30})
    capture.record_clip_result(clip_id="scene_001_phrase_001", ...)
    capture.record_agent_decision("content_analyst", "visual_concept", {...})

    # At pipeline end:
    capture.end_run()
    trace_path = capture.save()
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

TRACES_DIR = Path(__file__).parent.parent / "traces"


class TraceCapture:
    """Captures production run traces for across-run learning."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_id: str = ""
        self._pipeline_key: str = ""
        self._topic: str = ""
        self._start_time: float = 0.0
        self._end_time: float = 0.0
        self._events: list[dict[str, Any]] = []
        self._clip_results: list[dict[str, Any]] = []
        self._agent_decisions: list[dict[str, Any]] = []
        self._metadata: dict[str, Any] = {}

    def start_run(
        self,
        pipeline_key: str = "",
        topic: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Start capturing a new production run."""
        with self._lock:
            self._run_id = f"run_{int(time.time())}"
            self._pipeline_key = pipeline_key
            self._topic = topic
            self._start_time = time.time()
            self._end_time = 0.0
            self._events = []
            self._clip_results = []
            self._agent_decisions = []
            self._metadata = metadata or {}

        logger.info(
            "TraceCapture: started run %s (pipeline=%s, topic=%s)",
            self._run_id, pipeline_key, topic,
        )

    def record_event(self, event_type: str, data: Optional[dict] = None) -> None:
        """Record a pipeline event (phase transitions, errors, etc)."""
        with self._lock:
            self._events.append({
                "timestamp": time.time(),
                "type": event_type,
                "data": data or {},
            })

    def record_clip_result(
        self,
        clip_id: str,
        success: bool,
        gen_time: float = 0.0,
        qa_quality: str = "",
        qa_reason: str = "",
        worker_id: str = "",
        attempt: int = 1,
        error: str = "",
        prompt: str = "",
        duration_target: float = 0.0,
        duration_actual: float = 0.0,
    ) -> None:
        """Record the result of a single clip generation."""
        with self._lock:
            self._clip_results.append({
                "timestamp": time.time(),
                "clip_id": clip_id,
                "success": success,
                "gen_time": gen_time,
                "qa_quality": qa_quality,
                "qa_reason": qa_reason,
                "worker_id": worker_id,
                "attempt": attempt,
                "error": error,
                "prompt": prompt[:500],  # truncate long prompts
                "duration_target": duration_target,
                "duration_actual": duration_actual,
            })

    def record_agent_decision(
        self,
        agent_name: str,
        decision_type: str,
        data: Optional[dict] = None,
    ) -> None:
        """Record an ADK agent decision for trajectory analysis."""
        with self._lock:
            self._agent_decisions.append({
                "timestamp": time.time(),
                "agent": agent_name,
                "decision_type": decision_type,
                "data": data or {},
            })

    def end_run(self, summary: str = "") -> None:
        """Mark the run as complete."""
        with self._lock:
            self._end_time = time.time()
            if summary:
                self._metadata["summary"] = summary

        logger.info(
            "TraceCapture: ended run %s (%.1fs, %d clips, %d events)",
            self._run_id,
            self._end_time - self._start_time,
            len(self._clip_results),
            len(self._events),
        )

    def save(self, output_dir: Optional[str] = None) -> str:
        """Save the captured trace to a JSON file.

        Returns the path to the saved trace file.
        """
        save_dir = Path(output_dir) if output_dir else TRACES_DIR
        save_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            trace_data = {
                "run_id": self._run_id,
                "pipeline_key": self._pipeline_key,
                "topic": self._topic,
                "start_time": self._start_time,
                "end_time": self._end_time or time.time(),
                "duration_sec": (self._end_time or time.time()) - self._start_time,
                "metadata": self._metadata,
                "events": list(self._events),
                "clip_results": list(self._clip_results),
                "agent_decisions": list(self._agent_decisions),
                "summary_stats": self._compute_stats(),
            }

        filename = f"trace_{self._run_id}.json"
        filepath = save_dir / filename

        with open(filepath, "w") as f:
            json.dump(trace_data, f, indent=2, default=str)

        logger.info("TraceCapture: saved trace to %s", filepath)
        return str(filepath)

    def _compute_stats(self) -> dict[str, Any]:
        """Compute summary statistics for the trace."""
        if not self._clip_results:
            return {"total_clips": 0}

        successes = [c for c in self._clip_results if c["success"]]
        failures = [c for c in self._clip_results if not c["success"]]
        gen_times = [c["gen_time"] for c in successes if c["gen_time"] > 0]

        # QA analysis
        qa_passed = [c for c in successes if c["qa_quality"] in ("good", "excellent")]
        retried = [c for c in self._clip_results if c["attempt"] > 1]

        # Duration accuracy
        dur_diffs = []
        for c in successes:
            if c["duration_target"] > 0 and c["duration_actual"] > 0:
                diff = abs(c["duration_actual"] - c["duration_target"]) / c["duration_target"]
                dur_diffs.append(diff)

        return {
            "total_clips": len(self._clip_results),
            "successes": len(successes),
            "failures": len(failures),
            "success_rate": len(successes) / len(self._clip_results) if self._clip_results else 0,
            "qa_passed": len(qa_passed),
            "qa_pass_rate": len(qa_passed) / len(successes) if successes else 0,
            "retried": len(retried),
            "retry_rate": len(retried) / len(self._clip_results) if self._clip_results else 0,
            "avg_gen_time": sum(gen_times) / len(gen_times) if gen_times else 0,
            "total_gen_time": sum(gen_times),
            "avg_duration_error": sum(dur_diffs) / len(dur_diffs) if dur_diffs else 0,
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_capture: Optional[TraceCapture] = None
_capture_lock = threading.Lock()


def get_trace_capture() -> TraceCapture:
    """Get or create the global trace capture instance."""
    global _capture
    if _capture is None:
        with _capture_lock:
            if _capture is None:
                _capture = TraceCapture()
    return _capture


def reset_trace_capture() -> None:
    """Reset the global trace capture (for testing)."""
    global _capture
    with _capture_lock:
        _capture = None
