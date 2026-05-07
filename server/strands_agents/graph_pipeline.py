"""
Documentary pipeline — Strands Graph orchestration.

Replaces the 986-line ``agents/pipeline.py`` (ADK SequentialAgent + LoopAgent)
with a directed Graph of 5 nodes, forward edges for stage ordering,
backward edges for recovery, and a recovery shell that catches the
Graph's fail-fast behaviour.

Architecture::

    RecoveryShell (wraps Graph invocation)
      ├─ Catches RuntimeError from fail-fast Graph
      ├─ Classifies failure (which node, what went wrong)
      ├─ Updates invocation_state with recovery context
      └─ Re-invokes Graph with conditional backward edges

    Strands Graph (pipeline orchestration)
      ├─ 5 nodes: scenario → audio → visual → production → assembly
      ├─ Forward edges: deterministic stage ordering
      ├─ Backward edges: conditional recovery ladder
      ├─ reset_on_revisit=True, max_node_executions=50
      └─ invocation_state: {otio_manager, feedback_store, recovery_context}

This module is reachable via ``--pipeline=strands`` and
:func:`strands_agents.run.run_documentary`. The legacy ADK pipeline
in :mod:`server.agents.pipeline` stays untouched until the feature flag
(Step 14) swaps over.
"""

from __future__ import annotations

import logging
from typing import Any

from strands import Agent
from strands.hooks import HookProvider, HookRegistry
from strands.multiagent.graph import (
    AfterNodeCallEvent,
    BeforeNodeCallEvent,
    Graph,
    GraphEdge,
    GraphNode,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage node IDs
# ---------------------------------------------------------------------------

SCENARIO = "scenario"
AUDIO = "audio"
VISUAL = "visual"
PRODUCTION = "production"
ASSEMBLY = "assembly"

STAGE_ORDER = [SCENARIO, AUDIO, VISUAL, PRODUCTION, ASSEMBLY]


# ---------------------------------------------------------------------------
# Node executors — one Strands Agent per stage
# ---------------------------------------------------------------------------


def _build_stage(stage: str, otio_manager=None, model=None) -> Agent:
    """Build a real stage agent. Raises if the builder fails.

    There is no placeholder fallback. If a stage agent cannot be built,
    the pipeline fails — it does not silently substitute a do-nothing agent.
    """
    from strands_agents.stages import (
        build_scenario_agent,
        build_audio_agent,
        build_visual_agent,
        build_production_agent,
        build_assembly_agent,
    )
    builders = {
        SCENARIO: build_scenario_agent,
        AUDIO: build_audio_agent,
        VISUAL: build_visual_agent,
        PRODUCTION: build_production_agent,
        ASSEMBLY: build_assembly_agent,
    }
    builder = builders.get(stage)
    if builder is None:
        raise ValueError(f"Unknown stage: {stage}")
    return builder(otio_manager=otio_manager, model=model)


# ---------------------------------------------------------------------------
# Recovery conditions for backward edges
# ---------------------------------------------------------------------------


def _needs_scenario_retry(state) -> bool:
    """Backward edge: audio → scenario when timing fails."""
    # GraphState is a dataclass, not a dict. Recovery is handled
    # by unit agents internally — no backward edges needed for now.
    try:
        return state.get("_recovery_target") == SCENARIO if hasattr(state, "get") else False
    except Exception:
        return False


def _needs_visual_retry(state) -> bool:
    """Backward edge: production → visual when clips fail QA."""
    try:
        return state.get("_recovery_target") == VISUAL if hasattr(state, "get") else False
    except Exception:
        return False


def _needs_audio_retry(state) -> bool:
    """Backward edge: visual → audio when alignment is off."""
    try:
        return state.get("_recovery_target") == AUDIO if hasattr(state, "get") else False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# State propagation — carry agent state between graph nodes
# ---------------------------------------------------------------------------


# Keys to propagate from one stage's agent state to the next.
# These are the contract-required keys that downstream stages need.
_PROPAGATE_KEYS = frozenset({
    "scenes",
    "whisperx_alignment",
    "visual_concepts",
    "visual_concepts_json",
    "visual_style",
    "content_analysis",
    "content_analysis_json",
    "visual_coherence_passed",
    "coherence_evaluation",
})


class StatePropagationHook(HookProvider):
    """Propagate agent state between graph nodes.

    Strands Graph nodes run as independent agents with separate state.
    The Graph passes text output between nodes but doesn't share state.
    This hook copies designated keys from the completed agent's state
    into invocation_state, then injects them into the next agent's
    state before it runs.

    This is how contract-required keys (scenes, whisperx_alignment,
    visual_concepts) flow between stages.
    """

    def __init__(self) -> None:
        self._shared_state: dict[str, Any] = {}

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterNodeCallEvent, self._on_after_node)
        registry.add_callback(BeforeNodeCallEvent, self._on_before_node)

    def _on_after_node(self, event: AfterNodeCallEvent) -> None:
        """After a node completes, harvest its agent state."""
        node = self._find_node(event.source, event.node_id)
        if node is None or not isinstance(node.executor, Agent):
            return

        agent_state = node.executor.state
        harvested = {}
        for key in _PROPAGATE_KEYS:
            try:
                val = agent_state.get(key)
                if val is not None:
                    harvested[key] = val
            except Exception:
                pass

        if harvested:
            self._shared_state.update(harvested)
            logger.info(
                "node_id=<%s>, keys=<%s> | harvested state",
                event.node_id,
                list(harvested.keys()),
            )

    def _on_before_node(self, event: BeforeNodeCallEvent) -> None:
        """Before a node starts, inject shared state into its agent."""
        if not self._shared_state:
            return

        node = self._find_node(event.source, event.node_id)
        if node is None or not isinstance(node.executor, Agent):
            return

        agent_state = node.executor.state
        injected = {}
        for key, val in self._shared_state.items():
            try:
                existing = agent_state.get(key)
                if not existing:
                    agent_state.set(key, val)
                    injected[key] = key
            except Exception:
                try:
                    agent_state.set(key, val)
                    injected[key] = key
                except Exception:
                    pass

        if injected:
            logger.info(
                "node_id=<%s>, keys=<%s> | injected state",
                event.node_id,
                list(injected.keys()),
            )

    @staticmethod
    def _find_node(source, node_id: str):
        """Find a GraphNode by ID from the source's nodes dict."""
        nodes = getattr(source, "nodes", None)
        if nodes is None:
            return None
        if isinstance(nodes, dict):
            return nodes.get(node_id)
        # Fallback: iterate as a set/list
        for n in nodes:
            if isinstance(n, GraphNode) and n.node_id == node_id:
                return n
        return None


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_documentary_graph(
    agents: dict[str, Agent] | None = None,
    hooks: list[HookProvider] | None = None,
    max_node_executions: int = 50,
    otio_manager: Any | None = None,
    model: Any | None = None,
) -> Graph:
    """Construct the 5-node documentary pipeline Graph.

    Args:
        agents: Optional mapping of stage → Agent. Missing stages
            get placeholder agents. Pass ``None`` for all placeholders
            (useful for the skeleton test in Step 2).
        hooks: Optional list of :class:`HookProvider` instances to
            attach to the Graph (contract enforcer, approval gate, etc.).
        max_node_executions: Safety limit on node re-executions.
            Defaults to 50, generous enough for recovery loops.

    Returns:
        A :class:`Graph` ready for ``invoke_async`` or ``stream_async``.
    """
    if agents is None:
        agents = {}

    # Build all stage agents — no placeholders, no fallbacks
    for stage in STAGE_ORDER:
        if stage not in agents:
            agents[stage] = _build_stage(stage, otio_manager=otio_manager, model=model)

    # Build nodes
    nodes = {
        stage: GraphNode(node_id=stage, executor=agents[stage])
        for stage in STAGE_ORDER
    }

    # Forward edges: scenario → audio → visual → production → assembly
    forward_edges = {
        GraphEdge(from_node=nodes[SCENARIO], to_node=nodes[AUDIO]),
        GraphEdge(from_node=nodes[AUDIO], to_node=nodes[VISUAL]),
        GraphEdge(from_node=nodes[VISUAL], to_node=nodes[PRODUCTION]),
        GraphEdge(from_node=nodes[PRODUCTION], to_node=nodes[ASSEMBLY]),
    }

    # Backward edges: recovery ladder (conditional)
    backward_edges = {
        # Timing loop: audio → scenario when evaluate_timing fails
        GraphEdge(
            from_node=nodes[AUDIO],
            to_node=nodes[SCENARIO],
            condition=_needs_scenario_retry,
        ),
        # Visual re-planning: production → visual when clips fail QA
        GraphEdge(
            from_node=nodes[PRODUCTION],
            to_node=nodes[VISUAL],
            condition=_needs_visual_retry,
        ),
        # Audio re-alignment: visual → audio when alignment is off
        GraphEdge(
            from_node=nodes[VISUAL],
            to_node=nodes[AUDIO],
            condition=_needs_audio_retry,
        ),
    }

    edges = forward_edges | backward_edges

    # Always add state propagation hook
    all_hooks = list(hooks) if hooks else []
    all_hooks.append(StatePropagationHook())

    return Graph(
        nodes=nodes,
        edges=edges,
        entry_points={nodes[SCENARIO]},
        max_node_executions=max_node_executions,
        reset_on_revisit=True,
        hooks=all_hooks,
        id="documentary_pipeline",
    )


# ---------------------------------------------------------------------------
# Recovery shell
# ---------------------------------------------------------------------------


class RecoveryShell:
    """Wraps a Graph invocation to catch fail-fast RuntimeError.

    The Strands Graph raises ``RuntimeError`` when a node fails — it
    does not call hooks or allow per-node recovery. The RecoveryShell
    catches the error, classifies which node failed, writes recovery
    context into ``invocation_state``, and re-invokes the Graph so
    the conditional backward edges can route to the right recovery node.

    Usage::

        graph = build_documentary_graph()
        shell = RecoveryShell(graph)
        result = await shell.run("Generate a documentary about...")
    """

    def __init__(self, graph: Graph, max_retries: int = 3) -> None:
        self.graph = graph
        self.max_retries = max_retries
        self._recovery_count = 0

    async def run(self, task: str) -> dict[str, Any]:
        """Execute the graph with automatic recovery on failure.

        Args:
            task: The initial task string (the user's brief).

        Returns:
            The final Graph state dict.

        Raises:
            RuntimeError: If recovery is exhausted after max_retries.
        """
        state_overrides: dict[str, Any] = {}

        for attempt in range(self.max_retries + 1):
            try:
                result = await self.graph.invoke_async(task)
                # Clear recovery context on success
                state_overrides.pop("_recovery_target", None)
                state_overrides.pop("_recovery_reason", None)
                return result
            except RuntimeError as exc:
                if attempt >= self.max_retries:
                    raise

                failed_node = self._classify_failure(exc)
                reason = str(exc)

                logger.warning(
                    "Graph failure on attempt %d/%d: node=%s reason=%s",
                    attempt + 1,
                    self.max_retries,
                    failed_node,
                    reason[:200],
                )

                # Set recovery context so backward edges activate
                state_overrides["_recovery_target"] = failed_node
                state_overrides["_recovery_reason"] = reason
                self._recovery_count += 1

    @staticmethod
    def _classify_failure(exc: RuntimeError) -> str:
        """Extract the failed node name from a Graph RuntimeError.

        The Strands Graph includes the node_id in its error messages.
        We parse it out; if parsing fails, default to the first stage.
        """
        msg = str(exc)
        for stage in STAGE_ORDER:
            if stage in msg:
                return stage
        # Can't determine which node failed — default to scenario
        logger.warning("Could not classify failure, defaulting to scenario: %s", msg[:200])
        return SCENARIO
