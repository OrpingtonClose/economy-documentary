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

import json
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

    Uses the AudioProvisionerAgent — merged audio + provisioner.
    The agent owns TTS end-to-end: allocate workers, generate
    narration, evaluate quality, scale workers.
    """
    from strands import tool

    @tool
    def read_pipeline_data(key: str) -> str:
        """Read pipeline metadata from the OTIO agent."""
        from agents.otio_agent import _tool_read_pipeline_data
        return _tool_read_pipeline_data(key)

    @tool
    def find_narration_gaps() -> str:
        """Scan OTIO timeline for missing narration. Returns TTS job list."""
        from agents.audio_provisioner_agent import _tool_find_narration_gaps
        return _tool_find_narration_gaps()

    @tool
    def get_scene_durations() -> str:
        """Get per-scene duration budgets from OTIO."""
        from agents.otio_agent import _tool_get_scene_durations
        return _tool_get_scene_durations()

    @tool
    def write_pipeline_data(key: str, value_json: str, provenance_json: str = "{}") -> str:
        """Write pipeline metadata to the OTIO agent."""
        from agents.otio_agent import _tool_write_pipeline_data
        return _tool_write_pipeline_data(key, value_json, provenance_json)

    @tool
    def add_clip(track: str, scene_num: int, phrase_idx: int,
                 clip_path: str, duration: float,
                 provenance_json: str = "{}") -> str:
        """Add a clip to the OTIO timeline."""
        from agents.otio_agent import _tool_add_clip
        return _tool_add_clip(track, scene_num, phrase_idx, clip_path, duration, provenance_json)

    @tool
    def validate_timeline(phase: str) -> str:
        """Validate timeline structural integrity."""
        from agents.otio_agent import _tool_validate_timeline
        return _tool_validate_timeline(phase)

    @tool
    def search_gpu_offers(query: str) -> str:
        """Search Vast.ai for available GPU offers. Construct your own query."""
        from agents.audio_provisioner_agent import _tool_search_gpu_offers
        return _tool_search_gpu_offers(query)

    @tool
    def provision_vm(offer_id: str, disk_gb: int = 64,
                     worker_mode: str = "tts",
                     docker_image: str = "",
                     env_vars_json: str = "{}") -> str:
        """Provision a GPU VM on Vast.ai."""
        from agents.audio_provisioner_agent import _tool_provision_vm
        return _tool_provision_vm(offer_id, disk_gb, worker_mode, docker_image, env_vars_json)

    @tool
    def check_vm_status(vm_id: str) -> str:
        """Check if a VM is running and healthy."""
        from agents.audio_provisioner_agent import _tool_check_vm_status
        return _tool_check_vm_status(vm_id)

    @tool
    def check_worker_health(url: str, capability: str = "tts") -> str:
        """Check if a worker's /health endpoint is OK."""
        from worker_provisioner import check_worker_health as _check
        return json.dumps({"healthy": _check(url, capability)})

    @tool
    def terminate_vm(vm_id: str) -> str:
        """Destroy a VM on Vast.ai to stop billing."""
        from agents.audio_provisioner_agent import _tool_terminate_vm
        return _tool_terminate_vm(vm_id)

    @tool
    def list_active_vms() -> str:
        """List all running VMs on the Vast.ai account."""
        from agents.audio_provisioner_agent import _tool_list_active_vms
        return _tool_list_active_vms()

    @tool
    def get_account_credits() -> str:
        """Get Vast.ai account credit balance."""
        from agents.audio_provisioner_agent import _tool_get_account_credits
        return _tool_get_account_credits()

    @tool
    def generate_narration(scene_num: int, voice_role: str, text: str,
                           output_dir: str = "", language: str = "",
                           worker_url: str = "") -> str:
        """Generate narration WAV via Qwen3-TTS worker."""
        from agents.audio_provisioner_agent import _tool_generate_narration
        return _tool_generate_narration(scene_num, voice_role, text, output_dir, language, worker_url)

    @tool
    def align_narration(wav_path: str) -> str:
        """Run WhisperX alignment on a WAV file."""
        from agents.audio_provisioner_agent import _tool_align_narration
        return _tool_align_narration(wav_path)

    @tool
    def evaluate_narration_quality() -> str:
        """Evaluate narration quality: duration vs budget, total projection."""
        from agents.audio_provisioner_agent import _tool_evaluate_narration_quality
        return _tool_evaluate_narration_quality()

    return Agent(
        name="audio",
        system_prompt=(
            "You are the Audio+Provisioner Agent for a documentary pipeline.\n\n"
            "You own TTS end-to-end: allocate workers, generate narration, "
            "evaluate quality, scale workers.\n\n"
            "PHASE 1: READ — find narration gaps in OTIO (find_narration_gaps)\n"
            "PHASE 2: PLAN — compute workload from gaps\n"
            "PHASE 3: PROVISION — search Vast.ai GPUs, pick one, create VM, "
            "wait for health. Use search_gpu_offers with your own query. "
            "Read the results and reason about which GPU to pick. "
            "Qwen3-TTS needs ~8GB VRAM minimum.\n"
            "PHASE 4: GENERATE — for each gap, call generate_narration, "
            "add_clip to OTIO, align_narration\n"
            "PHASE 5: EVALUATE — evaluate_narration_quality, rebalance if needed\n"
            "PHASE 6: CLEANUP — terminate_vm to stop billing\n\n"
            "RULES:\n"
            "- ALL data flows through OTIO. No agent state.\n"
            "- Every write carries provenance.\n"
            "- If scenes are missing, report error — that's a contract violation.\n"
            "- VMs are ephemeral. Track them in your working memory, not OTIO.\n"
            "- Always terminate VMs when done to minimize cost.\n"
        ),
        tools=[
            read_pipeline_data, find_narration_gaps, get_scene_durations,
            write_pipeline_data, add_clip, validate_timeline,
            search_gpu_offers, provision_vm, check_vm_status,
            check_worker_health, terminate_vm, list_active_vms,
            get_account_credits,
            generate_narration, align_narration, evaluate_narration_quality,
        ],
        model=model,
    )


def _build_video_agent(otio_unit, otio_manager, model) -> Agent:
    """Build a Strands Agent for the video stage.

    Uses the VideoProvisionerAgent — merged video + provisioner.
    The agent owns rendering end-to-end: visual planning, allocate
    GPU workers, render clips, evaluate quality, scale fleet.
    """
    from strands import tool

    @tool
    def read_timeline() -> str:
        """Read the full OTIO timeline structure."""
        from agents.otio_agent import _tool_read_timeline
        return _tool_read_timeline()

    @tool
    def read_pipeline_data(key: str) -> str:
        """Read pipeline metadata from the OTIO agent."""
        from agents.otio_agent import _tool_read_pipeline_data
        return _tool_read_pipeline_data(key)

    @tool
    def get_scene_durations() -> str:
        """Get per-scene duration budgets from OTIO."""
        from agents.otio_agent import _tool_get_scene_durations
        return _tool_get_scene_durations()

    @tool
    def find_video_gaps() -> str:
        """Scan OTIO timeline for missing video. Returns render job list."""
        from agents.video_provisioner_agent import _tool_find_video_gaps
        return _tool_find_video_gaps()

    @tool
    def write_pipeline_data(key: str, value_json: str, provenance_json: str = "{}") -> str:
        """Write pipeline metadata to the OTIO agent."""
        from agents.otio_agent import _tool_write_pipeline_data
        return _tool_write_pipeline_data(key, value_json, provenance_json)

    @tool
    def add_clip(track: str, scene_num: int, phrase_idx: int,
                 clip_path: str, duration: float,
                 provenance_json: str = "{}") -> str:
        """Add a clip to the OTIO timeline."""
        from agents.otio_agent import _tool_add_clip
        return _tool_add_clip(track, scene_num, phrase_idx, clip_path, duration, provenance_json)

    @tool
    def validate_timeline(phase: str) -> str:
        """Validate timeline structural integrity."""
        from agents.otio_agent import _tool_validate_timeline
        return _tool_validate_timeline(phase)

    @tool
    def rebalance_durations(adjustments_json: str, reason: str) -> str:
        """Redistribute time between scenes."""
        from agents.otio_agent import _tool_rebalance_durations
        return _tool_rebalance_durations(adjustments_json, reason)

    @tool
    def query_lora_catalog(content_type: str = "", mood: str = "") -> str:
        """Search LoRA style catalog."""
        from agents.video_provisioner_agent import _tool_query_lora_catalog
        return _tool_query_lora_catalog(content_type, mood)

    @tool
    def get_lora_details(lora_id: str) -> str:
        """Get full LoRA style details."""
        from agents.video_provisioner_agent import _tool_get_lora_details
        return _tool_get_lora_details(lora_id)

    @tool
    def search_gpu_offers(query: str) -> str:
        """Search Vast.ai for available GPU offers. Construct your own query."""
        from agents.video_provisioner_agent import _tool_search_gpu_offers
        return _tool_search_gpu_offers(query)

    @tool
    def provision_vm(offer_id: str, disk_gb: int = 128,
                     worker_mode: str = "ltx",
                     docker_image: str = "",
                     env_vars_json: str = "{}") -> str:
        """Provision a GPU VM on Vast.ai."""
        from agents.video_provisioner_agent import _tool_provision_vm
        return _tool_provision_vm(offer_id, disk_gb, worker_mode, docker_image, env_vars_json)

    @tool
    def check_vm_status(vm_id: str) -> str:
        """Check if a VM is running and healthy."""
        from agents.video_provisioner_agent import _tool_check_vm_status
        return _tool_check_vm_status(vm_id)

    @tool
    def check_worker_health(url: str, capability: str = "ltx") -> str:
        """Check if a worker's /health endpoint is OK."""
        from worker_provisioner import check_worker_health as _check
        return json.dumps({"healthy": _check(url, capability)})

    @tool
    def terminate_vm(vm_id: str) -> str:
        """Destroy a VM on Vast.ai to stop billing."""
        from agents.video_provisioner_agent import _tool_terminate_vm
        return _tool_terminate_vm(vm_id)

    @tool
    def list_active_vms() -> str:
        """List all running VMs on the Vast.ai account."""
        from agents.video_provisioner_agent import _tool_list_active_vms
        return _tool_list_active_vms()

    @tool
    def get_account_credits() -> str:
        """Get Vast.ai account credit balance."""
        from agents.video_provisioner_agent import _tool_get_account_credits
        return _tool_get_account_credits()

    @tool
    def render_clip(scene_num: int, phrase_idx: int, prompt: str,
                    negative_prompt: str = "", duration: float = 5.0,
                    lora_id: str = "", worker_url: str = "") -> str:
        """Submit a video clip render job to a GPU worker."""
        from agents.video_provisioner_agent import _tool_render_clip
        return _tool_render_clip(scene_num, phrase_idx, prompt,
                                 negative_prompt, duration, lora_id, worker_url)

    @tool
    def check_clip(job_id: str, worker_url: str = "") -> str:
        """Check the status of a GPU render job."""
        from agents.video_provisioner_agent import _tool_check_clip
        return _tool_check_clip(job_id, worker_url)

    return Agent(
        name="video",
        system_prompt=(
            "You are the Video+Provisioner Agent for a documentary pipeline.\n\n"
            "You own rendering end-to-end: visual planning, allocate GPU workers, "
            "render clips, evaluate quality, scale fleet.\n\n"
            "PHASE 1: SURVEY — find_video_gaps, read_pipeline_data for scenes/alignment\n"
            "PHASE 2: VISUAL PLANNING — generate LTX-2.3 prompts, "
            "write_pipeline_data(\"visual_concepts\"), "
            "query_lora_catalog if needed\n"
            "PHASE 3: INFRASTRUCTURE — search_gpu_offers with your own query, "
            "read the results, reason about which GPU to pick. "
            "LTX-2.3 needs significant VRAM (80GB+ ideal, 48GB minimum). "
            "provision_vm, wait for health.\n"
            "PHASE 4: PRODUCTION — render_clip for each concept, "
            "check_clip, add_clip to OTIO. Scale fleet if needed.\n"
            "PHASE 5: CLEANUP — validate_timeline, terminate_vm\n\n"
            "RULES:\n"
            "- ALL data flows through OTIO. No agent state.\n"
            "- Every write carries provenance.\n"
            "- If data is missing, report error — that's a contract violation.\n"
            "- VMs are ephemeral. Track them in your working memory, not OTIO.\n"
            "- Always terminate VMs when done to minimize cost.\n"
            "- VRAM is a hard floor — never lower for cost.\n"
        ),
        tools=[
            read_timeline, read_pipeline_data, get_scene_durations, find_video_gaps,
            write_pipeline_data, add_clip, validate_timeline, rebalance_durations,
            query_lora_catalog, get_lora_details,
            search_gpu_offers, provision_vm, check_vm_status,
            check_worker_health, terminate_vm, list_active_vms, get_account_credits,
            render_clip, check_clip,
        ],
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
