"""Watcher loop — no state machine, autonomous agents only.

The watcher ticks projections and gives each agent a turn.
Agents are free peers: they scan projections, decide what to do, and emit effects.
There is no global state machine, no slot graph, no prescribed sequence.

The pipeline "phase" is an emergent projection (PhaseTracker) for human
observation only. It does not control anything.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase Tracker — emergent phase for human observation ONLY
# ---------------------------------------------------------------------------
# This projection reads the event log and computes what phase the pipeline
# appears to be in. It is NOT used for control. Agents do not read it.
# It exists solely for human dashboards and debugging.
# ---------------------------------------------------------------------------

class PhaseTracker:
    """Emergent phase computed from projections. For observation only."""

    def __init__(self) -> None:
        self.phase: str = "init"

    def tick(self, otio: Any, jobs: Any) -> str:
        """Compute phase from current projection state."""
        narration = getattr(otio, "tracks", {}).get("A1_Narration", {})
        slots = narration.get("slots", [])

        if not slots:
            self.phase = "script"
            return self.phase

        has_gaps = any(s.get("status") in ("gap", "pending") for s in slots)
        if has_gaps:
            self.phase = "script"
            return self.phase

        job_states = _count_job_statuses(jobs)
        reconciliation_complete = getattr(jobs, "reconciliation_complete", False)

        if job_states["tts_pending"] > 0 or job_states["tts_running"] > 0:
            self.phase = "audio_production"
            return self.phase
        if not reconciliation_complete:
            self.phase = "audio_production"
            return self.phase

        if job_states["ltx_pending"] > 0 or job_states["ltx_running"] > 0:
            self.phase = "video_production"
            return self.phase

        all_merged = all(
            s.get("status") == "merged"
            for t in getattr(otio, "tracks", {}).values()
            for s in t.get("slots", [])
            if s.get("status") != "gap"
        )
        if not all_merged:
            self.phase = "media_production"
            return self.phase

        self.phase = "assembly"
        return self.phase


def _count_job_statuses(jobs: Any) -> dict[str, int]:
    counts: dict[str, int] = {
        "tts_pending": 0, "tts_running": 0, "tts_completed": 0,
        "ltx_pending": 0, "ltx_running": 0, "ltx_completed": 0,
    }
    for job in getattr(jobs, "jobs", {}).values():
        jt = job.get("job_type", "")
        st = job.get("status", "")
        key = f"{jt}_{st}"
        if key in counts:
            counts[key] += 1
    return counts


# ---------------------------------------------------------------------------
# Agent base — autonomous, data-driven
# ---------------------------------------------------------------------------

class Agent(ABC):
    """Autonomous agent. Scans projections and emits effects freely."""

    name: str = ""

    def __init__(self, event_store: Any) -> None:
        self.event_store = event_store

    @abstractmethod
    def tick(self, projections: dict[str, Any]) -> list[Any]:
        """Scan projections, return effects to emit."""
        ...

    def emit(self, effect: Any) -> None:
        self.event_store.append(effect)


# ---------------------------------------------------------------------------
# Watcher loop
# ---------------------------------------------------------------------------

async def run_watcher(
    projections: dict[str, Any],
    agents: list[Agent],
    event_store: Any,
    tick_interval: float = 1.0,
) -> None:
    """Run forever: tick projections, then let agents act.

    Order:
    1. Tick all projections (rebuild from new events)
    2. Compute phase for human observation
    3. Let each agent scan and emit effects
    4. Repeat
    """
    phase_tracker = PhaseTracker()
    logger.info("Watcher loop starting (interval=%ss, agents=%s)",
                tick_interval, [a.name for a in agents])

    while True:
        try:
            for proj in projections.values():
                if hasattr(proj, "tick"):
                    proj.tick()

            phase = phase_tracker.tick(
                projections.get("otio"),
                projections.get("jobs"),
            )
            logger.debug("Phase: %s", phase)

            for agent in agents:
                try:
                    effects = agent.tick(projections)
                    for effect in effects:
                        agent.emit(effect)
                except Exception:
                    logger.exception("Agent %s failed", agent.name)

        except Exception:
            logger.exception("Watcher loop error")

        await asyncio.sleep(tick_interval)
