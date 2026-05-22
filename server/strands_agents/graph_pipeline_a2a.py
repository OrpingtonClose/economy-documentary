"""Documentary pipeline — Strands Graph with A2AAgent nodes.

Replaces the sequential pipeline_full.py runner with a DAG-based
scheduler that:
  - Uses A2AAgentNode for remote leaf agents
  - Runs audio and visual in parallel (same ready batch)
  - Routes through otio_gate between stages for validation
  - Has backward edges for recovery (driven by otio-agent gate results)
  - Has guardian as L3 escalation node

The state of the pipeline is derived from the OTIO timeline:
what media is present, what quality checks pass, what's missing.
No synthetic stage markers. The otio-agent is the single source
of truth for pipeline state.

Architecture::

    Strands Graph (pipeline orchestration)
      ├─ scenario:    A2AAgentNode → scenario-agent:9002
      ├─ otio_gate:   A2AAgentNode → otio-agent:9001
      ├─ timing:      A2AAgentNode → timing-agent:9003
      ├─ audio:       A2AAgentNode → audio-agent:9004     ┐ parallel batch
      ├─ visual:      A2AAgentNode → visual-agent:9005    ┘
      ├─ production:  A2AAgentNode → production-agent:9006
      ├─ assembly:    A2AAgentNode → assembly-agent:9007
      └─ guardian:    A2AAgentNode → guardian-agent:9008

    Forward edges (deterministic stage ordering):
      scenario → otio_gate → timing → otio_gate → [audio, visual]
      → otio_gate → production → otio_gate → assembly → otio_gate

    Backward edges (recovery — driven by otio-agent gate result):
      otio_gate → stage (when gate returns recovery_target)

    Escalation edges (L3):
      otio_gate → guardian (when gate returns escalation_level >= 3)
      guardian → stage (when guardian coaches it)
"""

from __future__ import annotations


import json
import logging
from typing import Any

from strands.hooks import HookProvider
from strands.multiagent.graph import (
    Graph,
    GraphEdge,
    GraphNode,
    GraphState,
)

from .shared_a2a.a2a_agent_node import A2AAgentNode
from .shared_a2a.pipeline_state import (
    get_pipeline_state,
    set_pipeline_state,
    clear_pipeline_state,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Node IDs
# ---------------------------------------------------------------------------

SCENARIO = "scenario"
OTIO_GATE = "otio_gate"
TIMING = "timing"
AUDIO = "audio"
VISUAL = "visual"
PRODUCTION = "production"
ASSEMBLY = "assembly"
GUARDIAN = "guardian"

STAGES = [SCENARIO, AUDIO, VISUAL, PRODUCTION, ASSEMBLY]

# ---------------------------------------------------------------------------
# Pipeline state helpers — thin wrappers over shared state module
# ---------------------------------------------------------------------------


def _reset_pipeline_state() -> None:
    """Reset shared pipeline state at start of new run."""
    clear_pipeline_state()


def _set_recovery(target: str, reason: str = "") -> None:
    """Set recovery target after otio_gate returns failure."""
    set_pipeline_state("_recovery_target", target)
    set_pipeline_state("_recovery_reason", reason)


def _clear_recovery() -> None:
    """Clear recovery state after successful re-validation."""
    for key in ("_recovery_target", "_recovery_reason"):
        set_pipeline_state(key, None)


def _set_escalation(stage: str, level: int) -> None:
    """Set escalation state when ladder reaches L3."""
    set_pipeline_state("_escalation_stage", stage)
    set_pipeline_state("_escalation_level", level)


def _clear_escalation() -> None:
    """Clear escalation state after guardian coaching completes."""
    for key in ("_escalation_stage", "_escalation_level"):
        set_pipeline_state(key, None)


def _set_guardian_coach_target(stage: str) -> None:
    """Set coaching target after guardian returns hint for a stage."""
    set_pipeline_state("_guardian_coach_target", stage)


def _clear_guardian_coach_target() -> None:
    """Clear coaching target after coached stage re-completes."""
    set_pipeline_state("_guardian_coach_target", None)


# ---------------------------------------------------------------------------
# Edge condition helpers — derive pipeline phase from execution history
# ---------------------------------------------------------------------------


def _gate_count(state: GraphState) -> int:
    """Count how many times otio_gate has completed in execution history."""
    return sum(
        1 for node in state.execution_order
        if node.node_id == OTIO_GATE
        and node in state.completed_nodes
    )


def _last_non_gate_stage(state: GraphState) -> str | None:
    """Return the most recent completed non-gate stage from execution_order."""
    for node in reversed(state.execution_order):
        if node.node_id != OTIO_GATE and node in state.completed_nodes:
            return node.node_id
    return None


def _gate_passed(state: GraphState) -> bool:
    """Check if the most recent otio_gate execution returned a passing result.

    Reads the gate result from node.results. If the gate returned
    `{"valid": True, ...}` (or `pipeline_complete`), we consider it passed.
    """
    # Find the most recent otio_gate result
    for node in reversed(state.execution_order):
        if node.node_id == OTIO_GATE and node.result is not None:
            try:
                result = node.result.result
                # result may be AgentResult or dict-like
                if isinstance(result, dict):
                    return bool(result.get("valid", False) or result.get("pipeline_complete", False))
                # Try to parse as JSON string
                text = str(result)
                if text.startswith("{"):
                    data = json.loads(text)
                    return bool(data.get("valid", False) or data.get("pipeline_complete", False))
            except Exception:
                pass
            break
    # Fallback: no recovery target means gate passed
    return get_pipeline_state("_recovery_target") is None


# ---------------------------------------------------------------------------
# Edge conditions — forward routing derived from execution history
# ---------------------------------------------------------------------------


def _validation_passed(state: GraphState) -> bool:
    """Forward edges fire only when no recovery is active."""
    return get_pipeline_state("_recovery_target") is None and _gate_passed(state)


def _after_scenario(state: GraphState) -> bool:
    """Forward: otio_gate → timing, after scenario completes and gate passes."""
    if not _validation_passed(state):
        return False
    completed = {n.node_id for n in state.completed_nodes}
    return SCENARIO in completed and TIMING not in completed


def _after_scenario_audio(state: GraphState) -> bool:
    if not _validation_passed(state):
        return False
    completed = {n.node_id for n in state.completed_nodes}
    return SCENARIO in completed and AUDIO not in completed

def _after_scenario_visual(state: GraphState) -> bool:
    if not _validation_passed(state):
        return False
    completed = {n.node_id for n in state.completed_nodes}
    return SCENARIO in completed and VISUAL not in completed

def _after_timing_audio(state: GraphState) -> bool:
    """Forward: otio_gate → audio, after timing + gate pass."""
    if not _validation_passed(state):
        return False
    completed = {n.node_id for n in state.completed_nodes}
    return TIMING in completed and AUDIO not in completed


def _after_timing_visual(state: GraphState) -> bool:
    """Forward: otio_gate → visual, after timing + gate pass."""
    if not _validation_passed(state):
        return False
    completed = {n.node_id for n in state.completed_nodes}
    return TIMING in completed and VISUAL not in completed


def _after_audio_visual(state: GraphState) -> bool:
    """Forward: otio_gate → production, after audio+visual complete."""
    if not _validation_passed(state):
        return False
    completed = {n.node_id for n in state.completed_nodes}
    return (
        AUDIO in completed
        and VISUAL in completed
        and PRODUCTION not in completed
    )


def _after_production(state: GraphState) -> bool:
    """Forward: otio_gate → assembly, after production + gate pass."""
    if not _validation_passed(state):
        return False
    completed = {n.node_id for n in state.completed_nodes}
    return PRODUCTION in completed and ASSEMBLY not in completed


def _after_assembly(state: GraphState) -> bool:
    """Forward: otio_gate → end, after assembly + gate pass."""
    if not _validation_passed(state):
        return False
    completed = {n.node_id for n in state.completed_nodes}
    return ASSEMBLY in completed


# ---------------------------------------------------------------------------
# Edge conditions — backward routing (recovery)
# ---------------------------------------------------------------------------


def _needs_recovery(stage: str):
    """Backward edge: otio_gate → stage when gate returned recovery_target."""
    def condition(state: GraphState) -> bool:
        return get_pipeline_state("_recovery_target") == stage
    condition.__name__ = f"_needs_{stage}_retry"
    return condition


# ---------------------------------------------------------------------------
# Edge conditions — escalation routing
# ---------------------------------------------------------------------------


def _needs_guardian_escalation(state: GraphState) -> bool:
    """Forward: otio_gate → guardian when ladder >= 3.

    Mutually exclusive with forward/backward: only fires when no
    recovery target is set but escalation level is high enough.
    """
    if get_pipeline_state("_recovery_target") is not None:
        return False
    level = get_pipeline_state("_escalation_level", 0)
    return isinstance(level, int) and level >= 3


def _guardian_routes_to(stage: str):
    """Backward edge: guardian → stage when guardian coaches it."""
    def condition(state: GraphState) -> bool:
        return get_pipeline_state("_guardian_coach_target") == stage
    condition.__name__ = f"_guardian_routes_to_{stage}"
    return condition


# ---------------------------------------------------------------------------
# Default agent URLs
# ---------------------------------------------------------------------------

DEFAULT_URLS = {
    "otio": "http://localhost:9001",
    "scenario": "http://localhost:9002",
    "timing": "http://localhost:9003",
    "audio": "http://localhost:9004",
    "visual": "http://localhost:9005",
    "production": "http://localhost:9006",
    "assembly": "http://localhost:9007",
    "guardian": "http://localhost:9008",
}


# ---------------------------------------------------------------------------
# Default payloads per stage
# ---------------------------------------------------------------------------

DEFAULT_PAYLOADS = {
    SCENARIO: {
        "topic": "the cost of living",
        "num_scenes": 3,
        "style": "cinematic_documentary",
        "language": "en-US",
        "target_duration_sec": 90.0,
    },
    AUDIO: {
        "topic": "the cost of living",
        "voice": "default",
        "language": "en",
        "speed": 1.0,
    },
    VISUAL: {
        "topic": "the cost of living",
    },
    PRODUCTION: {
        "topic": "the cost of living",
    },
    ASSEMBLY: {
        "topic": "the cost of living",
    },
    OTIO_GATE: {},
    GUARDIAN: {},
}


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------
