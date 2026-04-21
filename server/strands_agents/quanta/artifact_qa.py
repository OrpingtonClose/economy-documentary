"""Component 10 atom — per-clip deterministic QA.

One pure atom: :func:`evaluate_visual_artifact_quality`. Given a
rendered clip's metadata (path, frames, duration, codec,
black-frame fraction) and the target duration, return a pass / warn /
fail verdict with per-check breakdown.

The production-supervisor SubAgent around this atom is a connector:
it dispatches GPU render jobs, routes retries, assembles escalation
payloads, and talks to the operator. The QA grading itself is pure.
"""

from __future__ import annotations

from strands_agents.artifact_qa import (
    ALLOWED_CODECS,
    BLACK_FRAME_CEILING,
    DEFAULT_FPS,
    DURATION_TOLERANCE_SEC,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_WARN,
    evaluate_visual_artifact_quality as _evaluate_visual_artifact_quality_tool,
)

# Expose the underlying Python function, not the Strands @tool wrapper,
# so direct callers (tests, offline replays, orchestrator simulator)
# don't need to plumb a ToolContext.
evaluate_visual_artifact_quality = _evaluate_visual_artifact_quality_tool.__wrapped__

__all__ = [
    "ALLOWED_CODECS",
    "BLACK_FRAME_CEILING",
    "DEFAULT_FPS",
    "DURATION_TOLERANCE_SEC",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "VERDICT_WARN",
    "evaluate_visual_artifact_quality",
]
