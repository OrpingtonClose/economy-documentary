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


def build_a2a_pipeline_graph(
    agent_urls: dict[str, str] | None = None,
    stage_payloads: dict[str, dict[str, Any]] | None = None,
    hooks: list[HookProvider] | None = None,
    max_node_executions: int = 50,
    timeout_per_node: int = 300,
) -> Graph:
    """Construct the documentary pipeline Graph with A2AAgent nodes.

    Args:
        agent_urls: Override default agent URLs.
        stage_payloads: Override default payloads per stage.
        hooks: Safety hooks (RecoveryHook, EscalationHook, etc.)
        max_node_executions: Safety limit on node re-executions.
        timeout_per_node: A2A timeout per node in seconds.

    Returns:
        A Graph ready for invoke_async or stream_async.
    """
    urls = dict(DEFAULT_URLS)
    if agent_urls:
        urls.update(agent_urls)

    payloads = dict(DEFAULT_PAYLOADS)
    if stage_payloads:
        payloads.update(stage_payloads)

    nodes = {
        SCENARIO: GraphNode(
            node_id=SCENARIO,
            executor=A2AAgentNode(
                endpoint=urls["scenario"],
                stage=SCENARIO,
                default_payload=payloads.get(SCENARIO, {}),
                timeout=timeout_per_node,
            ),
        ),
        OTIO_GATE: GraphNode(
            node_id=OTIO_GATE,
            executor=A2AAgentNode(
                endpoint=urls["otio"],
                stage=OTIO_GATE,
                default_payload=payloads.get(OTIO_GATE, {}),
                timeout=timeout_per_node,
            ),
        ),
        AUDIO: GraphNode(
            node_id=AUDIO,
            executor=A2AAgentNode(
                endpoint=urls["audio"],
                stage=AUDIO,
                default_payload=payloads.get(AUDIO, {}),
                timeout=timeout_per_node,
            ),
        ),
        VISUAL: GraphNode(
            node_id=VISUAL,
            executor=A2AAgentNode(
                endpoint=urls["visual"],
                stage=VISUAL,
                default_payload=payloads.get(VISUAL, {}),
                timeout=timeout_per_node,
            ),
        ),
        PRODUCTION: GraphNode(
            node_id=PRODUCTION,
            executor=A2AAgentNode(
                endpoint=urls["production"],
                stage=PRODUCTION,
                default_payload=payloads.get(PRODUCTION, {}),
                timeout=timeout_per_node,
            ),
        ),
        ASSEMBLY: GraphNode(
            node_id=ASSEMBLY,
            executor=A2AAgentNode(
                endpoint=urls["assembly"],
                stage=ASSEMBLY,
                default_payload=payloads.get(ASSEMBLY, {}),
                timeout=timeout_per_node,
            ),
        ),
        GUARDIAN: GraphNode(
            node_id=GUARDIAN,
            executor=A2AAgentNode(
                endpoint=urls["guardian"],
                stage=GUARDIAN,
                default_payload=payloads.get(GUARDIAN, {}),
                timeout=timeout_per_node,
            ),
        ),
    }

    # ------------------------------------------------------------------
    # Forward edges: deterministic stage ordering via otio_gate
    # Edges FROM otio_gate are stage-aware — each forward edge only
    # fires when the specific previous stage has completed.
    # ------------------------------------------------------------------
    forward_edges = {
        # scenario → otio_gate → [audio, visual] (parallel batch)
        GraphEdge(from_node=nodes[SCENARIO], to_node=nodes[OTIO_GATE]),
        GraphEdge(from_node=nodes[OTIO_GATE], to_node=nodes[AUDIO],
                  condition=_after_scenario_audio),
        GraphEdge(from_node=nodes[OTIO_GATE], to_node=nodes[VISUAL],
                  condition=_after_scenario_visual),
        # [audio, visual] → otio_gate → production
        GraphEdge(from_node=nodes[AUDIO], to_node=nodes[OTIO_GATE]),
        GraphEdge(from_node=nodes[VISUAL], to_node=nodes[OTIO_GATE]),
        GraphEdge(from_node=nodes[OTIO_GATE], to_node=nodes[PRODUCTION],
                  condition=_after_audio_visual),
        # production → otio_gate → assembly
        GraphEdge(from_node=nodes[PRODUCTION], to_node=nodes[OTIO_GATE]),
        GraphEdge(from_node=nodes[OTIO_GATE], to_node=nodes[ASSEMBLY],
                  condition=_after_production),
        # assembly → otio_gate (final validation)
        GraphEdge(from_node=nodes[ASSEMBLY], to_node=nodes[OTIO_GATE]),
    }

    # ------------------------------------------------------------------
    # Backward edges: recovery — otio_gate → stage when gate fails
    # ------------------------------------------------------------------
    backward_edges = {
        GraphEdge(
            from_node=nodes[OTIO_GATE],
            to_node=nodes[SCENARIO],
            condition=_needs_recovery(SCENARIO),
        ),
        GraphEdge(
            from_node=nodes[OTIO_GATE],
            to_node=nodes[AUDIO],
            condition=_needs_recovery(AUDIO),
        ),
        GraphEdge(
            from_node=nodes[OTIO_GATE],
            to_node=nodes[VISUAL],
            condition=_needs_recovery(VISUAL),
        ),
        GraphEdge(
            from_node=nodes[OTIO_GATE],
            to_node=nodes[PRODUCTION],
            condition=_needs_recovery(PRODUCTION),
        ),
        GraphEdge(
            from_node=nodes[OTIO_GATE],
            to_node=nodes[ASSEMBLY],
            condition=_needs_recovery(ASSEMBLY),
        ),
    }

    # ------------------------------------------------------------------
    # Escalation edges: L3 — otio_gate → guardian → stage
    # ------------------------------------------------------------------
    escalation_edges = {
        # otio_gate → guardian when ladder level >= 3
        GraphEdge(
            from_node=nodes[OTIO_GATE],
            to_node=nodes[GUARDIAN],
            condition=_needs_guardian_escalation,
        ),
        # guardian → any stage (when guardian coaches it)
        GraphEdge(
            from_node=nodes[GUARDIAN],
            to_node=nodes[SCENARIO],
            condition=_guardian_routes_to(SCENARIO),
        ),
        GraphEdge(
            from_node=nodes[GUARDIAN],
            to_node=nodes[AUDIO],
            condition=_guardian_routes_to(AUDIO),
        ),
        GraphEdge(
            from_node=nodes[GUARDIAN],
            to_node=nodes[VISUAL],
            condition=_guardian_routes_to(VISUAL),
        ),
        GraphEdge(
            from_node=nodes[GUARDIAN],
            to_node=nodes[PRODUCTION],
            condition=_guardian_routes_to(PRODUCTION),
        ),
        GraphEdge(
            from_node=nodes[GUARDIAN],
            to_node=nodes[ASSEMBLY],
            condition=_guardian_routes_to(ASSEMBLY),
        ),
    }

    edges = forward_edges | backward_edges | escalation_edges

    all_hooks = list(hooks) if hooks else []

    return Graph(
        nodes=nodes,
        edges=edges,
        entry_points={nodes[SCENARIO]},
        max_node_executions=max_node_executions,
        reset_on_revisit=True,
        hooks=all_hooks,
        id="documentary_pipeline_a2a",
    )
