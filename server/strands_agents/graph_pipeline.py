from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any

from strands import Agent
from strands.hooks import HookProvider
from strands.multiagent.base import Status
from strands.multiagent.graph import (
    Graph,
    GraphEdge,
    GraphNode,
    GraphState,
)

# Search tools for all agents
from search_tools import search_brave, search_perplexity, search_exa
_SEARCH_TOOLS = [search_brave, search_perplexity, search_exa]

logger = logging.getLogger(__name__)




# ---------------------------------------------------------------------------
# Per-agent memory tools
# ---------------------------------------------------------------------------

def _make_memory_tools(agent_name: str) -> list:
    """Create remember + recall_memory tools scoped to a specific agent.

    MEMORY POISONING DEFENSE:
    - NEVER remember failures. Only remember successes and facts.
    - Failed runs produce stale, misleading memories that poison future runs.
    - If you need to clear old memories, call forget_all.
    """
    from strands import tool

    @tool
    def remember(text: str, category: str = "fact") -> str:
        """Write a durable memory — ONLY successes and facts. NEVER failures.

        category MUST be 'success' or 'fact'. 'failure' is rejected.
        Examples:
        - success: "GPU offer 123 worked with 24GB VRAM"
        - fact: "Disk needs 150GB for LTX-2.3"
        - failure: REJECTED — failures poison future runs
        """
        if category == "failure":
            return json.dumps({"remembered": False, "reason": "FAILURES ARE NOT REMEMBERED. They poison future runs. Only success and fact categories allowed."})
        from agent_memory import remember as _remember
        return _remember(agent_name, text, category)

    remember.__name__ = f"remember_{agent_name}"

    @tool
    def recall_memory(query: str = "", category: str = "", limit: int = 20) -> str:
        """Recall memories from previous pipeline runs."""
        from agent_memory import recall_memory as _recall
        return _recall(agent_name, query, category, limit)

    recall_memory.__name__ = f"recall_memory_{agent_name}"

    @tool
    def forget_all() -> str:
        """Clear ALL memories for this agent. Use at start of every run."""
        import shutil
        from pathlib import Path
        d = Path(__file__).parent.parent / "agent_memory" / agent_name
        if d.exists():
            for f in d.glob("*"):
                if f.is_file():
                    f.unlink()
        return json.dumps({"forgotten": True, "agent": agent_name})

    forget_all.__name__ = f"forget_all_{agent_name}"

    return [remember, recall_memory, forget_all]


# ---------------------------------------------------------------------------
# Agent node IDs
# ---------------------------------------------------------------------------

SCENARIO = "scenario"
AUDIO = "audio"
VIDEO = "video"
OTIO = "otio"
ASSEMBLY = "assembly"
PROVISIONER = "provisioner"

STAGE_ORDER = [SCENARIO, AUDIO, VIDEO, ASSEMBLY]


# ---------------------------------------------------------------------------
# Checkpoint directory layout & metadata schema
# ---------------------------------------------------------------------------

CHECKPOINT_SCHEMA_VERSION: str = "1.0"
"""Version of the checkpoint metadata schema. Bumped on incompatible changes."""

CHECKPOINT_SUBDIRS: tuple[str, ...] = ("otio", "agents", "renders", "previews", "logs")
"""Subdirectories created under each run checkpoint root."""


class MetadataSchema:
    """Canonical metadata keys for the documentary pipeline checkpoint schema."""

    SCENES = "scenes"
    VISUAL_STYLE = "visual_style"
    STYLE_LOCK = "style_lock"
    WHISPERX_ALIGNMENT = "whisperx_alignment"
    ASSEMBLY_OUTPUT_PATH = "assembly_output_path"
    GATE_RESULT_PREFIX = "gate_"
    LADDER_SUFFIX = "_ladder"
    LIFECYCLE_STATE = "lifecycle_state"
    SCHEMA_VERSION = "checkpoint_schema_version"


def checkpoint_dir(run_id: str) -> str:
    """Return the canonical checkpoint directory for a pipeline run.

    Layout::

        {pipeline_dir}/checkpoints/{run_id}/
        ├── otio/              → OTIO timeline drafts and authoritative files
        ├── agents/            → Per-agent working state and outputs
        ├── renders/           → Final and intermediate video renders
        ├── previews/          → QA preview artifacts
        ├── logs/              → Execution logs and critique records
        └── metadata.json      → Run-level metadata schema envelope
    """
    from tools.otio_file_ops import resolve_timeline_path
    tp = resolve_timeline_path()
    base = os.path.dirname(os.path.dirname(tp))  # timelines/file.otio -> parent
    return os.path.join(base, "checkpoints", run_id)


def _update_completed_stages(run_id: str, stage: str) -> None:
    """Append *stage* to the metadata.json envelope's completed_stages list.

    Deduplicates and writes atomically.  This is the single source of truth
    for whether a stage finished successfully.
    """
    root = checkpoint_dir(run_id)
    envelope = os.path.join(root, "metadata.json")
    meta: dict[str, Any] = {
        MetadataSchema.SCHEMA_VERSION: CHECKPOINT_SCHEMA_VERSION,
        "run_id": run_id,
        "stages": STAGE_ORDER,
        "created_by": "graph_pipeline",
        "completed_stages": [],
    }
    if os.path.isfile(envelope):
        try:
            with open(envelope, "r") as f:
                meta.update(json.load(f))
        except Exception as exc:
            logger.warning("checkpoint read failed for %s: %s", envelope, exc)
    completed: list[str] = meta.get("completed_stages", [])
    if stage not in completed:
        completed.append(stage)
        meta["completed_stages"] = completed
        with open(envelope, "w") as f:
            json.dump(meta, f, indent=2)


def ensure_checkpoint_layout(run_id: str) -> str:
    """Create the full checkpoint directory layout for a run.

    Returns the root checkpoint path.
    """
    root = checkpoint_dir(run_id)
    for sub in CHECKPOINT_SUBDIRS:
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    envelope = os.path.join(root, "metadata.json")
    if not os.path.exists(envelope):
        with open(envelope, "w") as f:
            json.dump(
                {
                    MetadataSchema.SCHEMA_VERSION: CHECKPOINT_SCHEMA_VERSION,
                    "run_id": run_id,
                    "stages": STAGE_ORDER,
                    "created_by": "graph_pipeline",
                    "completed_stages": [],
                },
                f,
                indent=2,
            )
    return root


def _discover_completed_stages(run_id: str) -> list[str]:
    """Discover which stages completed before a crash by reading checkpoint metadata.

    Reads the metadata.json envelope first; falls back to inspecting agent
    checkpoint directories for .otio files.
    """
    root = checkpoint_dir(run_id)
    envelope = os.path.join(root, "metadata.json")
    if os.path.isfile(envelope):
        try:
            with open(envelope, "r") as f:
                meta = json.load(f)
            completed = meta.get("completed_stages")
            if isinstance(completed, list):
                return completed
        except Exception as exc:
            logger.warning("checkpoint read failed for %s: %s", envelope, exc)

    # NOTE: We do NOT infer completion from the presence of .otio files on
    # disk.  A checkpoint file may exist for a partially-failed stage, and
    # treating it as "completed" would cause the skip logic to bypass the
    # stage forever.  Completion is recorded explicitly by save_*_checkpoint
    # via _update_completed_stages().
    return []


# ---------------------------------------------------------------------------
# Recovery shell
# ---------------------------------------------------------------------------

class RecoveryShell:
    """Wraps Graph invocation and holds checkpoint/resume state.

    Merges two roles:
    1. Original: Graph wrapper with retry logic (run_strands.py calls this).
    2. Agent-added: Resume state bag with checkpoint seeding.
    """

    def __init__(
        self,
        graph: Graph | None = None,
        max_retries: int = 3,
        resume: bool = False,
        run_id: str = "",
        latest_checkpoint: str = "",
        completed_stages: list[str] | None = None,
    ) -> None:
        # Original: graph wrapper state
        self.graph = graph
        self.max_retries = max_retries
        self._recovery_count = 0

        # Agent-added: resume state
        self.resume = resume
        self.run_id = run_id
        self.latest_checkpoint = latest_checkpoint
        self.completed_stages = completed_stages if completed_stages is not None else []

    async def run(
        self, task: str, initial_state: dict[str, Any] | None = None
    ) -> Any:
        """Execute the graph, handling interrupts for external intervention.

        The graph may pause when an InterventionHook raises an interrupt
        (e.g. a human queued an instruction via POST /agents/{node_id}).
        This method polls for responses and resumes automatically.

        Seeds the working timeline from the latest checkpoint on resume.
        No algorithmic retry — the graph's backward edges handle recovery.
        """
        if self.graph is None:
            raise RuntimeError("RecoveryShell has no graph — build_documentary_graph() must be called first.")

        # On resume, seed the working timeline before first execution
        if self.resume and self.latest_checkpoint:
            self.seed_timeline()

        from strands_agents.agent_intervention import (
            wait_for_interrupt_response,
            get_intervention_store,
        )

        store = get_intervention_store()
        current_task: Any = task
        rounds = 0
        max_rounds = 50  # safety cap — misbehaving graph cannot loop forever

        while rounds < max_rounds:
            rounds += 1
            result = await self.graph.invoke_async(current_task, invocation_state=initial_state)

            # Record node results for HTTP inspection
            for node_id, node_result in result.results.items():
                store.record_node_result(node_id, {
                    "status": node_result.status.value if hasattr(node_result.status, "value") else str(node_result.status),
                    "execution_time_ms": node_result.execution_time,
                })

            if result.status != Status.INTERRUPTED:
                return result

            # Handle interrupts — wait for external responses
            interrupt_responses: list[dict[str, Any]] = []
            for interrupt in result.interrupts:
                logger.info(
                    "Graph interrupted (%s) for node — waiting for response...",
                    interrupt.id,
                )
                response = await wait_for_interrupt_response(interrupt.id)
                if response is None:
                    raise RuntimeError(
                        f"Interrupt {interrupt.id} timed out waiting for response. "
                        f"POST to /agents/resume/{interrupt.id} to resume."
                    )
                interrupt_responses.append({
                    "interruptResponse": {
                        "interruptId": interrupt.id,
                        "response": response,
                    }
                })

            # Resume the graph with responses
            if interrupt_responses:
                current_task = interrupt_responses
            else:
                # No responses but still interrupted — should not happen
                raise RuntimeError("Graph interrupted but no interrupts found in result")

        raise RuntimeError(f"Max interrupt rounds ({max_rounds}) exceeded")

    def seed_timeline(self) -> str | None:
        """On resume, copy the selected checkpoint to the working timeline path."""
        if not self.resume or not self.latest_checkpoint:
            return None
        from tools.otio_file_ops import resolve_timeline_path

        timeline_path = resolve_timeline_path()
        timeline_dir = os.path.dirname(timeline_path)
        if timeline_dir:
            os.makedirs(timeline_dir, exist_ok=True)
        shutil.copy2(self.latest_checkpoint, timeline_path)
        logger.info(
            "Resumed run %s: copied checkpoint %s -> %s",
            self.run_id,
            self.latest_checkpoint,
            timeline_path,
        )
        return timeline_path

    @staticmethod
    def _classify_failure(exc: RuntimeError) -> str:
        """Extract the failed node name from a Graph RuntimeError."""
        msg = str(exc)
        for stage in STAGE_ORDER:
            if stage in msg:
                return stage
        logger.warning(
            "Could not classify failure, defaulting to scenario: %s", msg[:200]
        )
        return SCENARIO


_recovery_shell: RecoveryShell | None = None


def get_recovery_shell() -> RecoveryShell | None:
    """Return the active recovery shell for this graph execution, if any."""
    return _recovery_shell


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


_DEFAULT_AGENT_PORTS: dict[str, int] = {
    SCENARIO: 9001,
    AUDIO: 9002,
    VIDEO: 9003,
    OTIO: 9004,
    ASSEMBLY: 9005,
    PROVISIONER: 9006,
}


def build_documentary_graph(
    hooks: list[HookProvider] | None = None,
    max_node_executions: int = 50,
    model: Any | None = None,
    run_id: str = "",
    agent_urls: dict[str, str] | None = None,
    pipeline_dir: str = "",
) -> tuple[Graph, RecoveryShell]:
    """Construct the documentary pipeline Graph.

    3 stage nodes + 1 OTIO gate node. Data flows through the OTIO
    file on disk (stateless). No shared Python state. No contract hooks.

    The OTIO gate node validates each stage's output before the next
    stage runs. It enforces the draft→authoritative lifecycle transition
    after the audio stage passes validation.

    Args:
        hooks: Safety hooks (ImmutabilityHook, BudgetHook, etc.)
        max_node_executions: Safety limit on node re-executions.
        model: Optional model configuration.
        use_http: If True, agents are remote HTTP services. Graph nodes
            become HTTP client proxies. Each agent runs independently.
        agent_urls: Override base URLs for remote agents. Keys are node
            IDs (scenario, audio, etc.). Defaults to localhost ports.

    Returns:
        A :class:`Graph` ready for ``invoke_async`` or ``stream_async``.
    """
    global _recovery_shell

    # NO CHECKPOINTS: every run starts from scratch.
    # Ensure layout exists for saving checkpoints during the run, but
    # never resume from previous state.
    if run_id:
        ensure_checkpoint_layout(run_id)

    _recovery_shell = RecoveryShell(
        resume=False,
        run_id=run_id,
        latest_checkpoint="",
        completed_stages=[],
    )

    # Create a shared OTIOStateManager and inject it into stage modules
    # so their tools can read/write pipeline metadata.
    if not pipeline_dir:
        from tools.otio_file_ops import resolve_timeline_path
        tp = resolve_timeline_path()
        pipeline_dir = os.path.dirname(os.path.dirname(tp))
    try:
        from strands_agents.otio_manager import OTIOStateManager
        _shared_otio_manager = OTIOStateManager(output_dir=pipeline_dir)
        # Point it at the existing timeline file (created by run_strands.py)
        timeline_dir = os.path.join(pipeline_dir, "timelines")
        draft_path = os.path.join(timeline_dir, "documentary_draft.otio")
        if os.path.exists(draft_path):
            _shared_otio_manager._timeline_path = draft_path
            _shared_otio_manager.refresh_from_disk()

        import strands_agents.stages.audio_stage as _audio_stage_mod
        import strands_agents.stages.production_stage as _production_stage_mod
        import strands_agents.stages.scenario_stage as _scenario_stage_mod
        _audio_stage_mod._otio_manager = _shared_otio_manager
        _production_stage_mod._otio_manager = _shared_otio_manager
        _scenario_stage_mod._otio_manager = _shared_otio_manager
    except Exception as exc:
        logger.warning("Failed to inject OTIOStateManager into stage modules: %s", exc)

    # All agents are remote HTTP services. The Graph communicates with them
    # via AgentHTTPClient proxies. Each agent runs in its own process.
    from strands_agents.agent_http_client import AgentHTTPClient

    urls = agent_urls or {}
    scenario_agent = AgentHTTPClient(
        urls.get(SCENARIO, f"http://localhost:{_DEFAULT_AGENT_PORTS[SCENARIO]}"), SCENARIO
    )
    audio_agent = AgentHTTPClient(
        urls.get(AUDIO, f"http://localhost:{_DEFAULT_AGENT_PORTS[AUDIO]}"), AUDIO
    )
    video_agent = AgentHTTPClient(
        urls.get(VIDEO, f"http://localhost:{_DEFAULT_AGENT_PORTS[VIDEO]}"), VIDEO
    )
    otio_gate_agent = AgentHTTPClient(
        urls.get(OTIO, f"http://localhost:{_DEFAULT_AGENT_PORTS[OTIO]}"), OTIO
    )
    assembly_agent = AgentHTTPClient(
        urls.get(ASSEMBLY, f"http://localhost:{_DEFAULT_AGENT_PORTS[ASSEMBLY]}"), ASSEMBLY
    )
    provisioner_agent = AgentHTTPClient(
        urls.get(PROVISIONER, f"http://localhost:{_DEFAULT_AGENT_PORTS[PROVISIONER]}"), PROVISIONER
    )

    # Build nodes
    nodes = {
        SCENARIO: GraphNode(node_id=SCENARIO, executor=scenario_agent),
        OTIO: GraphNode(node_id=OTIO, executor=otio_gate_agent),
        AUDIO: GraphNode(node_id=AUDIO, executor=audio_agent),
        VIDEO: GraphNode(node_id=VIDEO, executor=video_agent),
        ASSEMBLY: GraphNode(node_id=ASSEMBLY, executor=assembly_agent),
        PROVISIONER: GraphNode(node_id=PROVISIONER, executor=provisioner_agent),
    }

    # Forward edges: scenario → otio → audio → otio → video → otio → assembly → otio
    # Provisioner interleaves: otio → provisioner → otio whenever jobs are pending
    forward_edges = {
        GraphEdge(from_node=nodes[SCENARIO], to_node=nodes[OTIO]),
        GraphEdge(
            from_node=nodes[OTIO],
            to_node=nodes[AUDIO],
            condition=_audio_not_completed,
        ),
        GraphEdge(from_node=nodes[AUDIO], to_node=nodes[OTIO]),
        GraphEdge(
            from_node=nodes[OTIO],
            to_node=nodes[VIDEO],
            condition=_video_not_completed,
        ),
        GraphEdge(from_node=nodes[VIDEO], to_node=nodes[OTIO]),
        GraphEdge(
            from_node=nodes[OTIO],
            to_node=nodes[ASSEMBLY],
            condition=_assembly_not_completed,
        ),
        GraphEdge(from_node=nodes[ASSEMBLY], to_node=nodes[OTIO]),
        # Provisioner loop — drains the job queue
        GraphEdge(
            from_node=nodes[OTIO],
            to_node=nodes[PROVISIONER],
            condition=_has_pending_jobs,
        ),
        GraphEdge(from_node=nodes[PROVISIONER], to_node=nodes[OTIO]),
    }

    # Backward edges: recovery — routes read from OTIO file
    backward_edges = {
        # OTIO gate → scenario (when scenario output fails validation)
        GraphEdge(
            from_node=nodes[OTIO],
            to_node=nodes[SCENARIO],
            condition=_needs_scenario_retry,
        ),
        # OTIO gate → audio (when audio output fails validation)
        GraphEdge(
            from_node=nodes[OTIO],
            to_node=nodes[AUDIO],
            condition=_needs_audio_retry,
        ),
        # OTIO gate → video (when video output fails validation)
        GraphEdge(
            from_node=nodes[OTIO],
            to_node=nodes[VIDEO],
            condition=_needs_video_retry,
        ),
        # OTIO gate → assembly (when assembly fails)
        GraphEdge(
            from_node=nodes[OTIO],
            to_node=nodes[ASSEMBLY],
            condition=_needs_assembly_retry,
        ),
    }

    edges = forward_edges | backward_edges

    all_hooks = list(hooks) if hooks else []

    graph = Graph(
        nodes=nodes,
        edges=edges,
        entry_points={nodes[SCENARIO]},
        max_node_executions=max_node_executions,
        reset_on_revisit=True,
        hooks=all_hooks,
        id="documentary_pipeline",
    )
    _recovery_shell.graph = graph
    return graph, _recovery_shell


# ---------------------------------------------------------------------------
# OTIO gate agent — validation between stages
# ---------------------------------------------------------------------------


def _build_otio_gate_agent(model, model_id: str = "") -> Agent:
    """Build the OTIO gate agent — the structural authority.

    Imports audio/video clip tools so the gate can ingest agent output into OTIO.

    The gate sits between every stage transition. It:
    1. Reads the OTIO file to get current state
    2. Validates the previous stage's output
    3. If validation fails, writes error to OTIO (backward edge routes)
    4. If validation passes, summarizes what the next stage needs
    5. After audio: transitions timeline from draft → authoritative
    """
    from strands import tool
    from strands_agents.stages.audio_stage import add_narration_to_timeline
    from tools.otio_tools import add_video_clip_simple

    @tool
    def read_pipeline_data(key: str) -> str:
        """Read pipeline metadata from the OTIO file."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import read_pipeline_metadata
        tp = resolve_timeline_path()
        val = read_pipeline_metadata(tp, key)
        if val is None:
            return json.dumps({"error": f"Key '{key}' not found", "contract_violation": True})
        return json.dumps({"key": key, "value": val})

    @tool
    def read_timeline() -> str:
        """Read the full OTIO timeline structure."""
        from tools.otio_file_ops import resolve_timeline_path, otio_read
        tp = resolve_timeline_path()
        timeline = otio_read(tp)
        # Return summary of tracks and clips
        summary = {}
        for track in timeline.tracks:
            clips = []
            for item in track:
                clips.append({
                    "name": item.name,
                    "duration": float(item.duration().value) if hasattr(item.duration(), 'value') else 0,
                    "type": type(item).__name__,
                })
            summary[track.name] = {"clip_count": len(clips), "clips": clips}
        return json.dumps(summary)

    _model_id = model_id

    @tool
    def ingest_scenario(text: str) -> str:
        """Parse raw scenario text via instructor and store structured data to OTIO."""
        if not text.strip():
            return json.dumps({"error": "Empty scenario text received."})

        try:
            from structured_extract import extract
            from pydantic import BaseModel, Field
            from typing import List

            class Scene(BaseModel):
                title: str = Field(description="Scene title")
                duration_sec: int = Field(description="Duration in seconds")
                narration_v1_hook: str = Field(default="", description="V1 Hook narration")
                narration_v2_expert: str = Field(default="", description="V2 Expert narration")
                narration_v3_storyteller: str = Field(default="", description="V3 Storyteller narration")
                visual_notes: str = Field(default="", description="Visual description")
                dopamine_hook: str = Field(default="", description="Dopamine hook phrase")
                pronunciation_hints: str = Field(default="", description="Pronunciation hints")

            class VisualStyle(BaseModel):
                style: str = Field(default="", description="Dominant visual style")
                realism_anchors: List[str] = Field(default_factory=list)
                avoid: List[str] = Field(default_factory=list)
                palette: str = Field(default="")
                camera_language: str = Field(default="")
                reference_genre: str = Field(default="")

            class StyleLock(BaseModel):
                dominant_style: str = Field(default="")
                forbidden_styles: List[str] = Field(default_factory=list)
                positive_fragment: str = Field(default="")
                negative_fragment: str = Field(default="")

            class ScenarioDoc(BaseModel):
                scenes: List[Scene] = Field(description="List of scenes")
                visual_style: VisualStyle = Field(default_factory=VisualStyle)
                style_lock: StyleLock = Field(default_factory=StyleLock)

            doc = extract(
                ScenarioDoc,
                text,
                system_prompt="Parse the documentary scenario text into structured data. Extract all scenes with their narration scripts, visual notes, and timing.",
            )
        except Exception as exc:
            return json.dumps({"error": f"Instructor parsing failed: {exc}"})

        # Write to OTIO
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        write_pipeline_metadata(tp, "scenario_raw", text, provenance={"agent": "otio_gate"})
        write_pipeline_metadata(tp, MetadataSchema.SCENES, [s.model_dump() for s in doc.scenes], provenance={"agent": "otio_gate"})
        write_pipeline_metadata(tp, MetadataSchema.VISUAL_STYLE, doc.visual_style.model_dump(), provenance={"agent": "otio_gate"})
        write_pipeline_metadata(tp, MetadataSchema.STYLE_LOCK, doc.style_lock.model_dump(), provenance={"agent": "otio_gate"})

        return json.dumps({
            "ingested": True,
            "scene_count": len(doc.scenes),
            "raw_length": len(text),
        })

    @tool
    def validate_scenario() -> str:
        """Validate scenario output: raw text must exist and contain scenes."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import read_pipeline_metadata
        tp = resolve_timeline_path()
        text = read_pipeline_metadata(tp, "scenario_raw")
        errors = []
        if not text or not text.strip():
            errors.append("Missing scenario text in OTIO metadata")
        elif "SCENES:" not in text:
            errors.append("Scenario text missing 'SCENES:' section")
        if errors:
            return f"VALIDATION FAILED\n" + "\n".join(errors) + "\nROUTE BACKWARD TO: scenario\n\n--- PREVIOUS SCENARIO TEXT ---\n{text}\n--- END PREVIOUS SCENARIO ---"
        # Return plain text with the full scenario so downstream agents can use it
        return f"VALIDATION PASSED\nNEXT STAGE: audio\n\n--- SCENARIO TEXT ---\n{text}\n--- END SCENARIO ---"

    @tool
    def validate_audio() -> str:
        """Validate audio output: narration clips must exist."""
        from tools.otio_file_ops import resolve_timeline_path, otio_read
        from tools.otio_metadata import read_pipeline_metadata
        tp = resolve_timeline_path()
        errors = []

        try:
            timeline = otio_read(tp)
            a1_track = None
            for track in timeline.tracks:
                if track.name == "A1_Narration":
                    a1_track = track
                    break
            if a1_track is None:
                errors.append("No A1_Narration track found")
            elif len(list(a1_track)) == 0:
                errors.append("A1_Narration track is empty — no narration clips")
        except Exception as e:
            errors.append(f"Error reading timeline: {e}")

        if errors:
            return "VALIDATION FAILED\n" + "\n".join(errors) + "\nROUTE BACKWARD TO: audio"

        # Include scenario text so video agent receives it in its prompt
        text = read_pipeline_metadata(tp, "scenario_raw")
        return f"VALIDATION PASSED\nNEXT STAGE: video\n\n--- SCENARIO TEXT ---\n{text or ''}\n--- END SCENARIO ---"

    @tool
    def validate_video() -> str:
        """Validate video output: clips must exist."""
        import opentimelineio as otio
        from tools.otio_file_ops import resolve_timeline_path, otio_read
        tp = resolve_timeline_path()
        errors = []
        video_clips = []
        audio_clips = []
        try:
            timeline = otio_read(tp)
            v1_track = None
            for track in timeline.tracks:
                if track.name == "V1_Video":
                    v1_track = track
                    for item in track:
                        if isinstance(item, otio.schema.Clip):
                            ref = item.media_reference
                            path = ref.target_url.replace("file://", "") if hasattr(ref, "target_url") else ""
                            duration = float(item.duration().value) / float(item.duration().rate) if hasattr(item.duration(), "value") else 5.0
                            video_clips.append({"path": path, "duration": duration, "name": item.name})
                elif track.name == "A1_Narration":
                    for item in track:
                        if isinstance(item, otio.schema.Clip):
                            ref = item.media_reference
                            path = ref.target_url.replace("file://", "") if hasattr(ref, "target_url") else ""
                            duration = float(item.duration().value) / float(item.duration().rate) if hasattr(item.duration(), "value") else 5.0
                            audio_clips.append({"path": path, "duration": duration, "name": item.name})
            if v1_track is None:
                errors.append("No V1_Video track found")
            elif len(video_clips) == 0:
                errors.append("V1_Video track is empty — no video clips")
        except Exception as e:
            errors.append(f"Error reading timeline: {e}")
        if errors:
            return "VALIDATION FAILED\n" + "\n".join(errors) + "\nROUTE BACKWARD TO: video"
        clips_json = json.dumps({"video_clips": video_clips, "audio_clips": audio_clips})
        return f"VALIDATION PASSED\nNEXT STAGE: assembly\n\n--- CLIP ARTIFACTS ---\n{clips_json}\n--- END CLIP ARTIFACTS ---"

    @tool
    def validate_assembly() -> str:
        """Validate assembly: final output MP4 must exist on disk."""
        import glob
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        pipeline_dir = os.path.dirname(os.path.dirname(tp))
        output_dir = os.path.join(pipeline_dir, "output")
        mp4_files = glob.glob(os.path.join(output_dir, "*.mp4"))
        if not mp4_files:
            return "VALIDATION FAILED\nNo output MP4 found in output directory\nROUTE BACKWARD TO: assembly"
        output_path = mp4_files[0]
        # Record the path so run_strands.py can read it
        write_pipeline_metadata(tp, MetadataSchema.ASSEMBLY_OUTPUT_PATH, output_path, provenance={"agent": "otio_gate"})
        return f"VALIDATION PASSED\nPIPELINE COMPLETE\nOUTPUT: {output_path}"

    @tool
    def transition_to_authoritative() -> str:
        """Transition OTIO from draft to authoritative after audio validation."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_lifecycle import set_otio_lifecycle_state
        tp = resolve_timeline_path()
        return set_otio_lifecycle_state(tp, "authoritative", "end_of_audio_reconciliation")

    @tool
    def write_gate_result(stage: str, valid: bool, errors_json: str = "[]") -> str:
        """Write gate validation result to OTIO metadata."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        result = {
            "stage": stage,
            "valid": valid,
            "errors": json.loads(errors_json) if errors_json else [],
        }
        return write_pipeline_metadata(tp, f"{MetadataSchema.GATE_RESULT_PREFIX}{stage}", result, provenance={"agent": "otio_gate"})

    @tool
    def get_otio_lifecycle_state() -> str:
        """Read the current OTIO lifecycle state (draft/authoritative)."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_lifecycle import get_otio_lifecycle_state as _get
        tp = resolve_timeline_path()
        state = _get(tp)
        return json.dumps({"state": state})

    @tool
    def begin_escalation(escalation_type: str, reason: str, opened_by: str) -> str:
        """Open an escalation window on the OTIO timeline.

        Allows modifying authoritative OTIO under controlled conditions.
        escalation_type must be 'REPLACE' or 'EXTEND'.
        """
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_lifecycle import begin_escalation as _begin
        tp = resolve_timeline_path()
        return _begin(tp, escalation_type, reason, opened_by)

    @tool
    def end_escalation() -> str:
        """Close the escalation window on the OTIO timeline."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_lifecycle import end_escalation as _end
        tp = resolve_timeline_path()
        return _end(tp)

    @tool
    def read_ladder_state(stage: str) -> str:
        """Read escalation ladder state for a stage (audio/video)."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import read_pipeline_metadata
        tp = resolve_timeline_path()
        key = f"{stage}{MetadataSchema.LADDER_SUFFIX}"
        val = read_pipeline_metadata(tp, key)
        if val is None:
            return json.dumps({"stage": stage, "ladder_active": False, "level": 0, "attempts": 0})
        return json.dumps({"stage": stage, "ladder_active": True, **val})

    @tool
    def write_critique_record(stage: str, verdict: str, details_json: str = "{}") -> str:
        """Write a critique record for a stage validation result.

        Fire-and-forget — never blocks the pipeline.  The critique store
        is QA infrastructure, not a gate.
        """
        from critique.record import QaVerdict
        from critique.store import get_critique_store
        try:
            store = get_critique_store()
            store.append_qa(
                artifact_type="scenario",
                artifact_id=f"{stage}_gate",
                verdict=QaVerdict(
                    check_name=f"gate_{stage}",
                    verdict=verdict,  # type: ignore[arg-type]
                    details=json.loads(details_json) if details_json else {},
                    source="otio_gate",
                ),
            )
            return json.dumps({"written": True, "stage": stage, "verdict": verdict})
        except Exception as e:
            # Critique store is best-effort — never block the pipeline
            return json.dumps({"written": False, "error": str(e)})

    @tool
    def trigger_preview(stage: str) -> str:
        """Trigger a preview build after a stage validation.

        Fire-and-forget — the preview is a QA artifact, not a gate.
        The builder reads the OTIO file directly and produces an MP4
        reflecting the actual state of clips in the timeline.
        """
        from previews.builder import build_preview
        try:
            from tools.otio_file_ops import resolve_timeline_path
            tp = resolve_timeline_path()
            pipeline_dir = os.path.dirname(os.path.dirname(tp))
            # Build a minimal state dict for the legacy builder API
            state = {"_timeline_path": tp}
            manifest = build_preview(
                state=state,
                trigger_reason=f"gate_{stage}",
                output_dir=os.path.join(pipeline_dir, "previews"),
            )
            return json.dumps({
                "triggered": True,
                "stage": stage,
                "preview_path": manifest.preview_path if hasattr(manifest, "preview_path") else None,
                "slot_count": len(manifest.slots) if hasattr(manifest, "slots") else 0,
            })
        except Exception as e:
            # Preview is best-effort — never block the pipeline
            return json.dumps({"triggered": False, "error": str(e)})

    return Agent(
        name="otio_gate",
        system_prompt=(
            "You are the OTIO Gate Agent — the structural authority of the documentary pipeline.\n\n"
            "You sit between every stage transition. Your job:\n"
            "1. Read the OTIO file to get the current state\n"
            "2. Validate the previous stage's output\n"
            "3. If validation fails: write gate_result to OTIO with errors, and the graph will route backward\n"
            "4. If validation passes: write gate_result to OTIO, summarize what the next stage needs\n"
            "5. After audio stage validation passes: call transition_to_authoritative\n"
            "6. After every validation (pass OR fail): write a critique record and trigger a preview\n"
            "7. Manage escalation windows via begin_escalation/end_escalation when authoritative OTIO must change\n\n"
            "CRITICAL DATA MODEL:\n"
            "- The scenario agent produces PLAIN TEXT (not JSON).\n"
            "  You receive that text directly — no files pass between agents.\n"
            "  Call ingest_scenario(text=<the text you received>) to parse it and write to OTIO.\n"
            "  Then call validate_scenario to check the structured data.\n"
            "- Downstream agents DERIVE what they need from 'scenes'. Do NOT demand\n"
            "  separate 'scene_data', 'visual_concepts', 'narration_text', or 'target_duration' keys.\n"
            "- Audio agent derives narration_text from scene['description'] and scene['audio_notes'].\n"
            "- Video agent derives visual prompts from scene['visual_notes'] and scene['description'].\n"
            "- The scenario gate checks: scenes exist, visual_style exists, style_lock is true.\n"
            "- The audio gate checks: A1_Narration track has clips OR whisperx_alignment exists.\n"
            "- The video gate checks: V1_Video track has clips.\n"
            "- The assembly gate checks: output/*.mp4 exists on disk.\n\n"
            "WORKFLOW PER STAGE:\n"
            "1. SCENARIO: call ingest_scenario(text=<text from scenario agent>) → validate_scenario → transition_to_authoritative\n"
            "2. AUDIO: Parse the audio agent's output text for WAV file paths.\n"
            "   Call add_narration_to_timeline for each file, THEN validate_audio.\n"
            "3. VIDEO: Parse the video agent's output text for MP4 file paths.\n"
            "   Call add_video_clip_simple for each file, THEN validate_video.\n"
            "4. ASSEMBLY: validate_assembly\n\n"
            "Rules:\n"
            "- You are stateless. All state lives in the OTIO file on disk.\n"
            "- Never assume memory of previous runs. Always read the OTIO file first.\n"
            "- If validation fails, be specific about what is missing so the recovery agent knows what to fix.\n"
            "- If validation passes, return PLAIN TEXT (not JSON). Format:\n"
            "    VALIDATION PASSED\n"
            "    NEXT STAGE: <stage>\n"
            "    --- SCENARIO TEXT ---\n"
            "    <full scenario text>\n"
            "    --- END SCENARIO ---\n"
            "  Downstream agents (audio, video) receive your entire response as their input.\n"
            "  They extract the scenario text from between the markers.\n"
        ),
        tools=[
            read_pipeline_data,
            read_timeline,
            ingest_scenario,
            validate_scenario,
            validate_audio,
            validate_video,
            validate_assembly,
            transition_to_authoritative,
            write_gate_result,
            get_otio_lifecycle_state,
            begin_escalation,
            end_escalation,
            read_ladder_state,
            write_critique_record,
            trigger_preview,
            add_narration_to_timeline,
            add_video_clip_simple,
        ] + _SEARCH_TOOLS + _make_memory_tools("otio_gate"),
        model=model,
    )


# ---------------------------------------------------------------------------
# Stage agents
# ---------------------------------------------------------------------------


def _read_directives() -> dict | None:
    """Read debug-gym directives from the pipeline directory.

    Derives pipeline directory from the OTIO timeline path.
    """
    try:
        from tools.otio_file_ops import resolve_timeline_path
        tp = resolve_timeline_path()
        pipeline_dir = os.path.dirname(os.path.dirname(tp))
    except Exception:
        return None
    try:
        path = os.path.join(pipeline_dir, ".directives.json")
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _build_scenario_agent(model) -> Agent:
    """Build the scenario agent — generates documentary structure as raw text."""
    from strands import tool
    from strands_agents.stages.scenario_stage import evaluate_scenario

    # Inject scope constraints from debug-gym directives
    constraint_text = ""
    directives = _read_directives()
    if directives:
        target = directives.get("target_agent", "")
        action = directives.get("action", "")
        if (target in ("scenario_agent", "all")) and action == "constrain_scope":
            max_scenes = directives.get("max_scenes")
            max_duration = directives.get("max_duration_seconds")
            if max_scenes is not None and max_duration is not None:
                constraint_text = (
                    f"SCOPE CONSTRAINT: Generate exactly {max_scenes} scene(s), "
                    f"total duration must not exceed {max_duration} seconds. "
                    f"Fit content within these limits.\n"
                )

    @tool
    def save_scenario_checkpoint() -> str:
        """Save the current OTIO timeline to the checkpoint directory after narrative planning."""
        from tools.otio_file_ops import resolve_timeline_path
        tp = resolve_timeline_path()
        shell = get_recovery_shell()
        run_id = shell.run_id if shell else ""
        if not run_id:
            run_id = "default"
        root = checkpoint_dir(run_id)
        dest_dir = os.path.join(root, "agents", SCENARIO)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "scenario_timeline.otio")
        shutil.copy2(tp, dest)
        _update_completed_stages(run_id, SCENARIO)
        return json.dumps({"saved": True, "checkpoint_path": dest})

    return Agent(
        name=SCENARIO,
        system_prompt=(
            "You are the Scenario Director for an ADHD-friendly documentary pipeline.\n"
            "You are invoked by the graph when scenario work is needed (new run or recovery).\n"
            "Always generate the scenario text when invoked.\n"
            f"{constraint_text}"
            "\n"
            "The user's message is either:\n"
            "  (a) a BRIEF — the documentary topic and target duration, OR\n"
            "  (b) VALIDATION ERRORS from the OTIO Gate — revise the previous scenario.\n"
            "\n"
            "YOUR JOB: Write the complete documentary scenario as PLAIN TEXT in your response.\n"
            "You do NOT produce JSON. You do NOT write to OTIO. You write TEXT.\n"
            "The OTIO Gate receives your text directly — no files pass between agents.\n"
            "\n"
            "OUTPUT FORMAT — plain text with clear structure:\n"
            "\n"
            "VISUAL STYLE:\n"
            "  Style: <one dominant style for the whole film>\n"
            "  Realism anchors: <list>\n"
            "  Avoid: <list>\n"
            "  Palette: <description>\n"
            "  Camera language: <description>\n"
            "  Reference genre: <description>\n"
            "\n"
            "STYLE LOCK:\n"
            "  Dominant style: <the one style>\n"
            "  Forbidden styles: <list>\n"
            "  Positive fragment: <what to include>\n"
            "  Negative fragment: <what to exclude>\n"
            "\n"
            "SCENES:\n"
            "  Scene 1 — <title> (<duration_sec>s)\n"
            "    Narration (V1 Hook): <script>\n"
            "    Narration (V2 Expert): <script>\n"
            "    Narration (V3 Storyteller): <script>\n"
            "    Visual notes: <description>\n"
            "    Dopamine hook: <concrete phrase>\n"
            "    Pronunciation hints: TOKEN = IPA, ...\n"
            "    Hook spec: topic_specific_motif=<...>, motion_description=<...>, narrative_pull=<...>\n"
            "  ...\n"
            "  Scene N — <title> (<duration_sec>s)\n"
            "    ...\n"
            "    Outro spec: closing_shot=<...>, recap_sentence=<...>, cta=<...>, brand_card=<...>\n"
            "\n"
            "RULES:\n"
            "- Each scene MUST be 30-45 seconds (~75-110 words per voice block at 150 wpm).\n"
            "- Each scene MUST have all 3 voices (V1 Hook, V2 Expert, V3 Storyteller).\n"
            "- No rhetorical questions anywhere.\n"
            "- Sum of all duration_sec MUST be within +/-10% of target_duration_sec.\n"
            "- Visual variety — no repetitive descriptions.\n"
            "- Dopamine hooks must be concrete and specific.\n"
            "\n"
            "WORKFLOW:\n"
            "1. Draft the full scenario text in your response.\n"
            "2. Review it yourself for ADHD compliance.\n"
            "3. When satisfied, call save_scenario_checkpoint and STOP.\n"
            "   Your response text will be passed directly to the OTIO Gate.\n"
            "\n"
            "RETRY: If your task contains validation errors, extract the previous scenario\n"
            "text from between the '--- PREVIOUS SCENARIO TEXT ---' and\n"
            "'--- END PREVIOUS SCENARIO ---' markers in your prompt, revise it,\n"
            "and output the new text.\n"
        ),
        tools=[save_scenario_checkpoint] + _SEARCH_TOOLS + _make_memory_tools(SCENARIO),
        model=model,
    )


def _build_audio_agent(model) -> Agent:
    """Build the audio agent — owns narration end-to-end.

    Uses real production tools from strands_agents.stages.audio_stage.
    Checkpoint/resume tools are added alongside."""
    from strands_agents.stages.audio_stage import (
        align_narration_audio,
        evaluate_audio_timing,
    )

    from strands import tool

    @tool
    def check_resume_status() -> str:
        """Check if this stage was already completed.
        Checks artifacts dir for existing WAV files — does NOT read OTIO."""
        import glob
        from tools.otio_file_ops import resolve_timeline_path
        try:
            tp = resolve_timeline_path()
            pipeline_dir = os.path.dirname(os.path.dirname(tp))
            artifact_dir = os.path.join(pipeline_dir, "artifacts")
            wav_files = glob.glob(os.path.join(artifact_dir, "*.wav"))
            if len(wav_files) > 0:
                return json.dumps({"status": "already_completed", "stage": AUDIO, "reason": "wav_files_exist", "count": len(wav_files)})
        except Exception as exc:
            logger.warning("audio completion check failed: %s", exc)
        return json.dumps({"status": "not_completed", "stage": AUDIO})

    @tool
    def save_audio_checkpoint() -> str:
        """Save the current OTIO timeline after audio generation."""
        from tools.otio_file_ops import resolve_timeline_path
        tp = resolve_timeline_path()
        shell = get_recovery_shell()
        run_id = shell.run_id if shell else ""
        if not run_id:
            run_id = "default"
        root = checkpoint_dir(run_id)
        dest_dir = os.path.join(root, "agents", AUDIO)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "audio_timeline.otio")
        shutil.copy2(tp, dest)
        _update_completed_stages(run_id, AUDIO)
        return json.dumps({"saved": True, "checkpoint_path": dest})

    # Job queue tools — media agents never touch VMs directly
    from tools.job_queue_tools import (
        submit_render_job,
        poll_completed_jobs,
        qa_completed_job,
        check_queue_status,
        get_failed_job_details,
    )

    return Agent(
        name=AUDIO,
        system_prompt=(
            "You are the Audio Agent. You own narration end-to-end.\n"
            "BEFORE doing any work, call forget_all to clear stale memories.\n"
            "Then call check_resume_status.\n"
            "  - 'already_completed' → call save_audio_checkpoint and STOP.\n"
            "  - 'in_progress' → poll completed jobs, download, QA, and proceed.\n"
            "  - 'not_completed' → submit jobs for all scenes, then poll and proceed.\n"
            "\n"
            "JOB QUEUE PROTOCOL — YOU DO NOT PROVISION VMs. YOU DO NOT TROUBLESHOOT.\n"
            "\n"
            "Your only interaction with compute is via the job queue:\n"
            "  1. submit_render_job — create a job for the provisioner to pick up\n"
            "  2. poll_completed_jobs — check which jobs are done\n"
            "  3. check_queue_status — see how many jobs are pending/running/completed/failed\n"
            "  4. qa_completed_job — approve or reject with specific comments\n"
            "\n"
            "You NEVER know worker URLs. You NEVER SSH anywhere.\n"
            "The provisioner executes jobs and saves results locally.\n"
            "The local file path is in the completed job's artifact_path field.\n"
            "\n"
            "NEVER TROUBLESHOOT. ONLY CERTAINTY.\n"
            "  - If a job fails after max attempts, report it and STOP.\n"
            "  - If QA fails, requeue with SPECIFIC comments so the worker knows what to fix.\n"
            "\n"
            "CRITICAL: You make ONE pass per invocation.\n"
            "  - Submit any missing jobs.\n"
            "  - Poll once for completed jobs.\n"
            "  - QA any completed jobs (read the local file from artifact_path).\n"
            "  - If pending or running jobs remain, report status and STOP.\n"
            "    The graph will re-invoke you when the pipeline cycles back.\n"
            "  - If all jobs are completed (or permanently failed), proceed to assembly.\n"
            "\n"
            "WORKFLOW:\n"
            "1. Your prompt is plain text from the OTIO Gate. Extract the scenario text from between the '--- SCENARIO TEXT ---' and '--- END SCENARIO ---' markers.\n"
            "2. Parse the scenario text to get scene titles, durations, and narration scripts.\n"
            "3. Call check_resume_status.\n"
            "4. For EACH scene not yet in queue, call submit_render_job with:\n"
            "     stage='audio', scene_num=N, job_type='narration',\n"
            "     payload='{\"text\":\"...\",\"voice_id\":\"...\"}'\n"
            "5. Call poll_completed_jobs(stage='audio').\n"
            "6. Call check_queue_status(stage='audio').\n"
            "7. For each completed job, read the local file from artifact_path.\n"
            "8. QA each file.\n"
            "9. If QA passes: report the artifact_path in your response.\n"
            "10. If QA fails: qa_completed_job(passed=False, verdict='fail', comments_json='[\"...\"]')\n"
            "11. If pending+running > 0: report status and STOP (graph will re-invoke).\n"
            "12. If failed > 0: call get_failed_job_details('audio'), report, STOP.\n"
            "13. Run WhisperX alignment with align_narration_audio.\n"
            "14. Evaluate timing with evaluate_audio_timing.\n"
            "15. Return a summary of all produced WAV files. The OTIO Gate will add them to the timeline.\n"
            "16. Call save_audio_checkpoint.\n"
        ),
        tools=[
            align_narration_audio,
            evaluate_audio_timing,
            check_resume_status,
            save_audio_checkpoint,
            submit_render_job,
            poll_completed_jobs,
            qa_completed_job,
            check_queue_status,
            get_failed_job_details,
        ] + _SEARCH_TOOLS + _make_memory_tools(AUDIO),
        model=model,
    )


def _build_video_agent(model) -> Agent:
    """Build the video agent — owns visual planning and rendering end-to-end.

    Checkpoint/resume tools added alongside."""
    from strands import tool

    @tool
    def check_resume_status() -> str:
        """Check if this stage was already completed.
        Checks artifacts dir for MP4 files — does NOT read OTIO."""
        import glob
        from tools.otio_file_ops import resolve_timeline_path
        try:
            tp = resolve_timeline_path()
            pipeline_dir = os.path.dirname(os.path.dirname(tp))
            artifact_dir = os.path.join(pipeline_dir, "artifacts")
            mp4_files = glob.glob(os.path.join(artifact_dir, "*.mp4"))
            if len(mp4_files) > 0:
                return json.dumps({"status": "already_completed", "stage": VIDEO, "reason": "mp4_files_exist", "count": len(mp4_files)})
        except Exception as exc:
            logger.warning("video completion check failed: %s", exc)
        return json.dumps({"status": "not_completed", "stage": VIDEO})

    @tool
    def save_video_checkpoint() -> str:
        """Save the current OTIO timeline after video rendering."""
        from tools.otio_file_ops import resolve_timeline_path
        tp = resolve_timeline_path()
        shell = get_recovery_shell()
        run_id = shell.run_id if shell else ""
        if not run_id:
            run_id = "default"
        root = checkpoint_dir(run_id)
        dest_dir = os.path.join(root, "agents", VIDEO)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "video_timeline.otio")
        shutil.copy2(tp, dest)
        _update_completed_stages(run_id, VIDEO)
        return json.dumps({"saved": True, "checkpoint_path": dest})

    # Job queue tools — media agents never touch VMs directly
    from tools.job_queue_tools import (
        submit_render_job,
        poll_completed_jobs,
        qa_completed_job,
        check_queue_status,
        get_failed_job_details,
    )

    return Agent(
        name=VIDEO,
        system_prompt=(
            "You are the Video Agent. You own visual planning and rendering end-to-end.\n"
            "BEFORE doing any work, call forget_all to clear stale memories.\n"
            "Then call check_resume_status. If it returns 'already_completed', "
            "call save_video_checkpoint and then STOP — do not render clips.\n"
            "\n"
            "JOB QUEUE PROTOCOL — YOU DO NOT PROVISION VMs. YOU DO NOT TROUBLESHOOT.\n"
            "\n"
            "Your only interaction with compute is via the job queue:\n"
            "  1. submit_render_job — create a job for the provisioner to pick up\n"
            "  2. poll_completed_jobs — check which jobs are done\n"
            "  3. qa_completed_job — approve or reject with specific comments\n"
            "  4. check_queue_status — see how many jobs are pending/running/completed/failed\n"
            "\n"
            "You NEVER know worker URLs. You NEVER SSH anywhere.\n"
            "The provisioner executes jobs and saves results locally.\n"
            "The local file path is in the completed job's artifact_path field.\n"
            "\n"
            "NEVER TROUBLESHOOT. ONLY CERTAINTY.\n"
            "  - If a job fails after max attempts, report it and STOP.\n"
            "  - If QA fails, requeue with SPECIFIC comments so the worker knows what to fix.\n"
            "\n"
            "CRITICAL: You make ONE pass per invocation.\n"
            "  - Submit any missing jobs.\n"
            "  - Poll once for completed jobs.\n"
            "  - QA any completed jobs (read the local file from artifact_path).\n"
            "  - If pending or running jobs remain, report status and STOP.\n"
            "    The graph will re-invoke you when the pipeline cycles back.\n"
            "  - If all jobs are completed (or permanently failed), proceed to assembly.\n"
            "\n"
            "WORKFLOW:\n"
            "1. Your prompt is plain text from the OTIO Gate. Extract the scenario text from between the '--- SCENARIO TEXT ---' and '--- END SCENARIO ---' markers.\n"
            "2. Parse the scenario text to get scene titles, durations, and visual notes.\n"
            "3. Call check_resume_status.\n"
            "4. For EACH scene not yet in queue, call submit_render_job with:\n"
            "     stage='video', scene_num=N, job_type='video_render',\n"
            "     payload='{\"model_name\":\"LTX Video\",\"prompt\":\"...\",\"width\":...}'\n"
            "5. Call poll_completed_jobs(stage='video').\n"
            "6. Call check_queue_status(stage='video').\n"
            "7. For each completed job, read the local file from artifact_path.\n"
            "8. QA each file.\n"
            "9. If QA passes: report the artifact_path in your response.\n"
            "10. If QA fails: qa_completed_job(passed=False, verdict='fail', comments_json='[\"...\"]')\n"
            "11. If pending+running > 0: report status and STOP (graph will re-invoke).\n"
            "12. If failed > 0: call get_failed_job_details('video'), report, STOP.\n"
            "13. Return a summary of all produced MP4 files. The OTIO Gate will add them to the timeline.\n"
            "14. Call save_video_checkpoint.\n"
        ),
        tools=[
            check_resume_status,
            save_video_checkpoint,
            submit_render_job,
            poll_completed_jobs,
            qa_completed_job,
            check_queue_status,
            get_failed_job_details,
        ] + _SEARCH_TOOLS + _make_memory_tools(VIDEO),
        model=model,
    )


def _build_provisioner_agent(model) -> Agent:
    """Build the standalone provisioner agent."""
    from strands_agents.provisioner_agent import build_provisioner_agent
    return build_provisioner_agent(model)


def _build_assembly_agent(model) -> Agent:
    """Build the assembly agent — produces the final deliverable.

    Uses real assembly tools from strands_agents.stages.assembly_stage.
    Checkpoint/resume tools added alongside."""
    from strands_agents.stages.assembly_stage import assemble_final_cut
    from strands import tool

    @tool
    def check_resume_status() -> str:
        """Check if this stage was already completed.
        Checks local output dir for final MP4 — does NOT read OTIO."""
        import glob
        from tools.otio_file_ops import resolve_timeline_path
        try:
            tp = resolve_timeline_path()
            pipeline_dir = os.path.dirname(os.path.dirname(tp))
            output_dir = os.path.join(pipeline_dir, "output")
            mp4_files = glob.glob(os.path.join(output_dir, "*.mp4"))
            if len(mp4_files) > 0:
                return json.dumps({"status": "already_completed", "stage": ASSEMBLY, "reason": "output_exists", "path": mp4_files[0]})
        except Exception as exc:
            logger.warning("assembly completion check failed: %s", exc)
        return json.dumps({"status": "not_completed", "stage": ASSEMBLY})

    @tool
    def save_assembly_checkpoint() -> str:
        """Save the current OTIO timeline to the checkpoint directory after final assembly."""
        from tools.otio_file_ops import resolve_timeline_path
        tp = resolve_timeline_path()
        shell = get_recovery_shell()
        run_id = shell.run_id if shell else ""
        if not run_id:
            run_id = "default"
        root = checkpoint_dir(run_id)
        dest_dir = os.path.join(root, "agents", ASSEMBLY)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "assembly_timeline.otio")
        shutil.copy2(tp, dest)
        _update_completed_stages(run_id, ASSEMBLY)
        return json.dumps({"saved": True, "checkpoint_path": dest})

    return Agent(
        name=ASSEMBLY,
        system_prompt=(
            "You are the Assembly Agent. You produce the final deliverable.\n"
            "BEFORE doing any work, call forget_all to clear stale memories.\n"
            "Then call check_resume_status. If it returns 'already_completed', "
            "call save_assembly_checkpoint and then STOP — do not re-assemble.\n"
            "\n"
            "WORKFLOW:\n"
            "1. Extract clip artifacts from between the '--- CLIP ARTIFACTS ---' "
            "   and '--- END CLIP ARTIFACTS ---' markers in your prompt.\n"
            "2. Call assemble_final_cut(clip_artifacts=<the extracted JSON string>).\n"
            "   This calls ffmpeg — NO OTIO read happens.\n"
            "3. Return the output file path in your response. The OTIO Gate will record it.\n"
            "4. Call save_assembly_checkpoint to preserve the final movie state.\n"
            "\n"
            "RULES:\n"
            "- You NEVER read the OTIO timeline. All data arrives in your prompt.\n"
            "- If clip artifacts are missing, report the error — do not generate placeholders."
        ),
        tools=[
            assemble_final_cut,
            check_resume_status,
            save_assembly_checkpoint,
        ] + _SEARCH_TOOLS + _make_memory_tools(ASSEMBLY),
        model=model,
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Routing helpers — Strands passes GraphState to edge conditions
# ---------------------------------------------------------------------------

def _gate_recovery_target(state: GraphState) -> str:
    """Extract recovery target from OTIO gate's plain text output.

    Returns the stage name if the gate requested recovery,
    or empty string if validation passed / gate hasn't run yet.
    """
    try:
        from strands.multiagent.graph import GraphState
        if not isinstance(state, GraphState):
            return ""
        otio_result = state.results.get(OTIO)
        if otio_result is None:
            return ""
        text = str(otio_result.result)
        # Parse plain text: "VALIDATION FAILED\n...\nROUTE BACKWARD TO: <stage>"
        if "VALIDATION FAILED" in text:
            for line in text.split("\n"):
                if line.startswith("ROUTE BACKWARD TO:"):
                    return line.split(":", 1)[1].strip()
        return ""
    except Exception:
        return ""


def _gate_next_stage(state: GraphState) -> str:
    """Extract next stage from OTIO gate's plain text output."""
    try:
        from strands.multiagent.graph import GraphState
        if not isinstance(state, GraphState):
            return ""
        otio_result = state.results.get(OTIO)
        if otio_result is None:
            return ""
        text = str(otio_result.result)
        # Parse plain text: "VALIDATION PASSED\nNEXT STAGE: <stage>"
        if "VALIDATION PASSED" in text:
            for line in text.split("\n"):
                if line.startswith("NEXT STAGE:"):
                    return line.split(":", 1)[1].strip()
        return ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Backward edge conditions — gate requests recovery for a specific stage
# ---------------------------------------------------------------------------

def _needs_scenario_retry(state: GraphState) -> bool:
    return _gate_recovery_target(state) == SCENARIO


def _needs_audio_retry(state: GraphState) -> bool:
    return _gate_recovery_target(state) == AUDIO


def _needs_video_retry(state: GraphState) -> bool:
    return _gate_recovery_target(state) == VIDEO


def _needs_assembly_retry(state: GraphState) -> bool:
    return _gate_recovery_target(state) == ASSEMBLY


# ---------------------------------------------------------------------------
# Forward edge conditions — only fire when gate passed (no recovery)
# ---------------------------------------------------------------------------

def _scenario_not_completed(state: GraphState) -> bool:
    """Run Scenario only if raw scenario text doesn't exist in OTIO."""
    if _gate_recovery_target(state):
        return False
    from tools.otio_file_ops import resolve_timeline_path
    from tools.otio_metadata import read_pipeline_metadata
    try:
        tp = resolve_timeline_path()
        text = read_pipeline_metadata(tp, "scenario_raw")
        if text and "SCENES:" in text:
            return False
    except Exception as exc:
        logger.warning("scenario routing check failed: %s", exc)
    return True


def _has_pending_jobs(state: GraphState) -> bool:
    """Route to Provisioner whenever jobs are pending — even during recovery.

    The provisioner must drain the queue regardless of gate validation state.
    Agents that retry will poll completed jobs; the provisioner executes them.
    """
    from job_queue import get_queue_summary
    for stage in ("audio", "video"):
        summary = get_queue_summary(stage)
        if summary.get("pending", 0) > 0 or summary.get("needs_retry", 0) > 0:
            return True
    return False


def _audio_not_completed(state: GraphState) -> bool:
    """Run Audio only if gate passed and no WAV files exist in artifacts."""
    if _gate_recovery_target(state):
        return False
    import glob
    from tools.otio_file_ops import resolve_timeline_path
    try:
        tp = resolve_timeline_path()
        pipeline_dir = os.path.dirname(os.path.dirname(tp))
        artifact_dir = os.path.join(pipeline_dir, "artifacts")
        wav_files = glob.glob(os.path.join(artifact_dir, "*.wav"))
        if len(wav_files) > 0:
            return False
    except Exception as exc:
        logger.warning("audio routing check failed: %s", exc)
    return True


def _video_not_completed(state: GraphState) -> bool:
    """Run Video only if audio WAVs exist and no MP4 renders exist in artifacts."""
    if _gate_recovery_target(state):
        return False
    import glob
    from tools.otio_file_ops import resolve_timeline_path
    try:
        tp = resolve_timeline_path()
        pipeline_dir = os.path.dirname(os.path.dirname(tp))
        artifact_dir = os.path.join(pipeline_dir, "artifacts")
        wav_files = glob.glob(os.path.join(artifact_dir, "*.wav"))
        mp4_files = glob.glob(os.path.join(artifact_dir, "*.mp4"))
        if len(wav_files) == 0:
            return False
        if len(mp4_files) > 0:
            return False
    except Exception as exc:
        logger.warning("video routing check failed: %s", exc)
    return True


def _assembly_not_completed(state: GraphState) -> bool:
    """Run Assembly only if video MP4s exist in artifacts and final output doesn't exist."""
    if _gate_recovery_target(state):
        return False
    import glob
    from tools.otio_file_ops import resolve_timeline_path
    try:
        tp = resolve_timeline_path()
        pipeline_dir = os.path.dirname(os.path.dirname(tp))
        artifact_dir = os.path.join(pipeline_dir, "artifacts")
        output_dir = os.path.join(pipeline_dir, "output")
        mp4_renders = glob.glob(os.path.join(artifact_dir, "*.mp4"))
        if len(mp4_renders) == 0:
            return False
        output_mp4s = glob.glob(os.path.join(output_dir, "*.mp4"))
        if len(output_mp4s) > 0:
            return False
    except Exception as exc:
        logger.warning("assembly routing check failed: %s", exc)
    return True