"""
Documentary pipeline — Strands Graph orchestration.

4 agent nodes: Scenario → Audio → Video, with OTIO as hub.

Architecture::

    RecoveryShell (wraps Graph invocation)
      ├─ Catches RuntimeError from fail-fast Graph
      ├─ Classifies failure (which node, what went wrong)
      └─ Re-invokes Graph with recovery context

    Strands Graph (pipeline orchestration)
      ├─ 4 nodes: scenario → audio → video
      ├─ OTIO agent: accessible to all nodes (hub)
      ├─ Forward edges: deterministic stage ordering
      ├─ Backward edges: conditional recovery ladder
      └─ Data flows through OTIO agent conversations

Agents:
    - Scenario agent (agents/scenario_director.py)
    - Audio agent (agents/audio_agent.py)
    - Video agent (agents/video_agent.py) — visual + production
    - OTIO agent (agents/otio_agent.py) — timeline, contracts, data
"""

from __future__ import annotations

import logging
from typing import Any

from strands import Agent
from strands.hooks import HookProvider
from strands.multiagent.graph import (
    Graph,
    GraphEdge,
    GraphNode,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent node IDs
# ---------------------------------------------------------------------------

SCENARIO = "scenario"
AUDIO = "audio"
VIDEO = "video"
OTIO = "otio"

STAGE_ORDER = [SCENARIO, AUDIO, VIDEO]


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_documentary_graph(
    hooks: list[HookProvider] | None = None,
    max_node_executions: int = 50,
    otio_manager: Any | None = None,
    model: Any | None = None,
) -> Graph:
    """Construct the documentary pipeline Graph.

    3 stage nodes + 1 OTIO hub node. Data flows through OTIO
    conversations. No state propagation. No contract hooks.
    The OTIO agent enforces contracts at read/write boundaries.

    Args:
        hooks: Safety hooks (ImmutabilityHook, BudgetHook, etc.)
        max_node_executions: Safety limit on node re-executions.
        otio_manager: OTIOStateManager (internal to OTIO agent).
        model: Optional model configuration.

    Returns:
        A :class:`Graph` ready for ``invoke_async`` or ``stream_async``.
    """
    # Build the OTIO agent — shared service for all nodes
    from agents.otio_agent import OTIOUnitAgent
    otio_unit = OTIOUnitAgent()

    # Build stage agents as Strands Agents with OTIO agent tools
    scenario_agent = _build_scenario_agent(otio_unit, otio_manager, model)
    audio_agent = _build_audio_agent(otio_unit, otio_manager, model)
    video_agent = _build_video_agent(otio_unit, otio_manager, model)

    # Build nodes
    nodes = {
        SCENARIO: GraphNode(node_id=SCENARIO, executor=scenario_agent),
        AUDIO: GraphNode(node_id=AUDIO, executor=audio_agent),
        VIDEO: GraphNode(node_id=VIDEO, executor=video_agent),
    }

    # Forward edges: scenario → audio → video
    forward_edges = {
        GraphEdge(from_node=nodes[SCENARIO], to_node=nodes[AUDIO]),
        GraphEdge(from_node=nodes[AUDIO], to_node=nodes[VIDEO]),
    }

    # Backward edges: recovery via OTIO agent conversations
    backward_edges = {
        # Timing loop: audio → scenario
        GraphEdge(
            from_node=nodes[AUDIO],
            to_node=nodes[SCENARIO],
            condition=_needs_scenario_retry,
        ),
        # Video → audio when alignment is off
        GraphEdge(
            from_node=nodes[VIDEO],
            to_node=nodes[AUDIO],
            condition=_needs_audio_retry,
        ),
    }

    edges = forward_edges | backward_edges

    all_hooks = list(hooks) if hooks else []

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
# Agent builders — wrap existing agents as Strands Agents
# ---------------------------------------------------------------------------


def _build_scenario_agent(otio_unit, otio_manager, model) -> Agent:
    """Build a Strands Agent for the scenario stage.

    Uses the existing OTIO agent's write_pipeline_data tool
    to persist scenes, visual_style, and style_lock.
    """
    from strands import tool

    @tool
    def write_scenes(scenes_json: str, provenance_json: str = "{}") -> str:
        """Write scenes to the OTIO agent."""
        from agents.otio_agent import _tool_write_pipeline_data
        return _tool_write_pipeline_data("scenes", scenes_json, provenance_json)

    @tool
    def write_visual_style(style_json: str, provenance_json: str = "{}") -> str:
        """Write visual style to the OTIO agent."""
        from agents.otio_agent import _tool_write_pipeline_data
        return _tool_write_pipeline_data("visual_style", style_json, provenance_json)

    @tool
    def write_style_lock(lock_json: str, provenance_json: str = "{}") -> str:
        """Write style lock to the OTIO agent."""
        from agents.otio_agent import _tool_write_pipeline_data
        return _tool_write_pipeline_data("style_lock", lock_json, provenance_json)

    return Agent(
        name="scenario",
        system_prompt=(
            "You are the Scenario Agent for a documentary pipeline.\n\n"
            "Your job is to generate a documentary scenario: scenes, visual "
            "style, and style lock. Write ALL output to the OTIO agent "
            "using write_scenes, write_visual_style, and write_style_lock.\n\n"
            "RULES:\n"
            "- ALL data goes through the OTIO agent. No agent state.\n"
            "- Every write carries provenance.\n"
            "- Persist immediately, even on error. The OTIO agent stores it.\n"
        ),
        tools=[write_scenes, write_visual_style, write_style_lock],
        model=model,
    )


def _build_audio_agent(otio_unit, otio_manager, model) -> Agent:
    """Build a Strands Agent for the audio stage.

    Reads scenes from OTIO agent, generates narration, writes
    alignment to OTIO agent.
    """
    from strands import tool

    @tool
    def read_scenes() -> str:
        """Read scenes from the OTIO agent."""
        from agents.otio_agent import _tool_read_pipeline_data
        return _tool_read_pipeline_data("scenes")

    @tool
    def write_alignment(alignment_json: str, provenance_json: str = "{}") -> str:
        """Write WhisperX alignment to the OTIO agent."""
        from agents.otio_agent import _tool_write_pipeline_data
        return _tool_write_pipeline_data("whisperx_alignment", alignment_json, provenance_json)

    @tool
    def add_narration_clip(track: str, scene_num: int, phrase_idx: int,
                           clip_path: str, duration: float,
                           provenance_json: str = "{}") -> str:
        """Add a narration clip to the OTIO timeline."""
        from agents.otio_agent import _tool_add_clip
        return _tool_add_clip(track, scene_num, phrase_idx, clip_path,
                              duration, provenance_json)

    return Agent(
        name="audio",
        system_prompt=(
            "You are the Audio Agent for a documentary pipeline.\n\n"
            "Your job is to generate narration audio for every scene.\n"
            "1. Read scenes from the OTIO agent (read_scenes)\n"
            "2. Generate narration for each scene/voice\n"
            "3. Add each clip to the OTIO timeline (add_narration_clip)\n"
            "4. Run WhisperX alignment\n"
            "5. Write alignment to the OTIO agent (write_alignment)\n\n"
            "RULES:\n"
            "- ALL data goes through the OTIO agent. No agent state.\n"
            "- Every write carries provenance.\n"
            "- If scenes are missing, report the error — that's a contract violation.\n"
            "- If TTS worker is unavailable, report the error.\n"
        ),
        tools=[read_scenes, write_alignment, add_narration_clip],
        model=model,
    )


def _build_video_agent(otio_unit, otio_manager, model) -> Agent:
    """Build a Strands Agent for the video stage.

    Reads scenes + alignment from OTIO agent, produces visual
    concepts, renders video clips, adds to timeline.
    """
    from strands import tool

    @tool
    def read_scenes() -> str:
        """Read scenes from the OTIO agent."""
        from agents.otio_agent import _tool_read_pipeline_data
        return _tool_read_pipeline_data("scenes")

    @tool
    def read_alignment() -> str:
        """Read alignment from the OTIO agent."""
        from agents.otio_agent import _tool_read_pipeline_data
        return _tool_read_pipeline_data("whisperx_alignment")

    @tool
    def write_visual_concepts(concepts_json: str, provenance_json: str = "{}") -> str:
        """Write visual concepts to the OTIO agent."""
        from agents.otio_agent import _tool_write_pipeline_data
        return _tool_write_pipeline_data("visual_concepts", concepts_json, provenance_json)

    @tool
    def render_clip(scene_num: int, phrase_idx: int, prompt: str,
                    negative_prompt: str = "", duration: float = 5.0,
                    lora_id: str = "") -> str:
        """Submit a video clip render job to a GPU worker."""
        from agents.video_agent import _tool_render_clip
        return _tool_render_clip(scene_num, phrase_idx, prompt,
                                 negative_prompt, duration, lora_id)

    @tool
    def check_clip(job_id: str) -> str:
        """Check the status of a GPU render job."""
        from agents.video_agent import _tool_check_clip
        return _tool_check_clip(job_id)

    @tool
    def add_video_clip(track: str, scene_num: int, phrase_idx: int,
                       clip_path: str, duration: float,
                       provenance_json: str = "{}") -> str:
        """Add a rendered video clip to the OTIO timeline."""
        from agents.otio_agent import _tool_add_clip
        return _tool_add_clip(track, scene_num, phrase_idx, clip_path,
                              duration, provenance_json)

    return Agent(
        name="video",
        system_prompt=(
            "You are the Video Agent for a documentary pipeline.\n\n"
            "PHASE 1: VISUAL PLANNING\n"
            "1. Read scenes and alignment from the OTIO agent\n"
            "2. Analyze narration content — identify semantic breakpoints\n"
            "3. Generate visual concepts (LTX-2.3 prompts)\n"
            "4. Write visual concepts to the OTIO agent\n\n"
            "PHASE 2: PRODUCTION\n"
            "1. For each visual concept, render a video clip (render_clip)\n"
            "2. Check render status (check_clip)\n"
            "3. Add completed clips to the OTIO timeline (add_video_clip)\n\n"
            "RULES:\n"
            "- ALL data goes through the OTIO agent. No agent state.\n"
            "- Every write carries provenance.\n"
            "- If data is missing, report the error — that's a contract violation.\n"
            "- If GPU worker is unavailable, report the error.\n"
        ),
        tools=[read_scenes, read_alignment, write_visual_concepts,
               render_clip, check_clip, add_video_clip],
        model=model,
    )


# ---------------------------------------------------------------------------
# Recovery conditions for backward edges
# ---------------------------------------------------------------------------


def _needs_scenario_retry(state) -> bool:
    """Backward edge: audio → scenario when timing fails."""
    try:
        return state.get("_recovery_target") == SCENARIO if hasattr(state, "get") else False
    except Exception:
        return False


def _needs_audio_retry(state) -> bool:
    """Backward edge: video → audio when alignment is off."""
    try:
        return state.get("_recovery_target") == AUDIO if hasattr(state, "get") else False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Recovery shell
# ---------------------------------------------------------------------------


class RecoveryShell:
    """Wraps a Graph invocation to catch fail-fast RuntimeError.

    Catches the error, classifies which node failed, writes recovery
    context, and re-invokes the Graph so backward edges can route
    to the right recovery node.
    """

    def __init__(self, graph: Graph, max_retries: int = 3) -> None:
        self.graph = graph
        self.max_retries = max_retries
        self._recovery_count = 0

    async def run(self, task: str) -> dict[str, Any]:
        """Execute the graph with automatic recovery on failure."""
        state_overrides: dict[str, Any] = {}

        for attempt in range(self.max_retries + 1):
            try:
                result = await self.graph.invoke_async(task)
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

                state_overrides["_recovery_target"] = failed_node
                state_overrides["_recovery_reason"] = reason
                self._recovery_count += 1

    @staticmethod
    def _classify_failure(exc: RuntimeError) -> str:
        """Extract the failed node name from a Graph RuntimeError."""
        from contracts import ContractViolation
        if isinstance(exc, ContractViolation):
            return exc.stage
        msg = str(exc)
        for stage in STAGE_ORDER:
            if stage in msg:
                return stage
        logger.warning("Could not classify failure, defaulting to scenario: %s", msg[:200])
        return SCENARIO
