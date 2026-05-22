from __future__ import annotations

import json
import logging
import os
import shutil
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
# Per-agent memory tools
# ---------------------------------------------------------------------------

def _make_memory_tools(agent_name: str) -> list:
    """Create remember + recall_memory tools scoped to a specific agent."""
    from strands import tool

    @tool
    def remember(text: str, category: str = "fact") -> str:
        """Write a durable memory that survives across pipeline runs.

        Use this when you learn something that future runs should know:
        - GPU offers that failed (and why)
        - Disk size requirements, boot times
        - Tool bugs or workarounds
        - What configurations actually worked
        category: 'failure', 'success', or 'fact'
        """
        from agent_memory import remember as _remember
        return _remember(agent_name, text, category)

    # Rename to avoid collision when multiple agents exist
    remember.__name__ = f"remember_{agent_name}"

    @tool
    def recall_memory(query: str = "", category: str = "", limit: int = 20) -> str:
        """Recall memories from previous pipeline runs.

        Searches your persistent memory by keyword match.
        Use this at the start of your work to check what you've learned before.
        query: search term (case-insensitive). Empty = return all.
        category: 'failure', 'success', 'fact'. Empty = all.
        limit: max results.
        """
        from agent_memory import recall_memory as _recall
        return _recall(agent_name, query, category, limit)

    recall_memory.__name__ = f"recall_memory_{agent_name}"

    return [remember, recall_memory]


# ---------------------------------------------------------------------------
# Agent node IDs
# ---------------------------------------------------------------------------

SCENARIO = "scenario"
AUDIO = "audio"
VIDEO = "video"
OTIO = "otio"
ASSEMBLY = "assembly"

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

        {PIPELINE_DIR}/checkpoints/{run_id}/
        ├── otio/              → OTIO timeline drafts and authoritative files
        ├── agents/            → Per-agent working state and outputs
        ├── renders/           → Final and intermediate video renders
        ├── previews/          → QA preview artifacts
        ├── logs/              → Execution logs and critique records
        └── metadata.json      → Run-level metadata schema envelope
    """
    base = os.environ.get("PIPELINE_DIR", "/tmp/documentary-pipeline")
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
        except Exception:
            pass
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
        except Exception:
            pass

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
    ) -> dict[str, Any]:
        """Execute the graph with automatic recovery on failure.

        Seeds the working timeline from the latest checkpoint on resume.
        """
        # On resume, seed the working timeline before first execution
        if self.resume and self.latest_checkpoint:
            self.seed_timeline()

        state_overrides: dict[str, Any] = initial_state or {}

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


def build_documentary_graph(
    hooks: list[HookProvider] | None = None,
    max_node_executions: int = 50,
    model: Any | None = None,
    run_id: str = "",
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
    pipeline_dir = os.environ.get("PIPELINE_DIR", "/tmp/documentary-pipeline")
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

    # Build stage agents as Strands Agents with stateless OTIO tools
    scenario_agent = _build_scenario_agent(model)
    audio_agent = _build_audio_agent(model, run_id=run_id)
    video_agent = _build_video_agent(model)
    otio_gate_agent = _build_otio_gate_agent(model)
    assembly_agent = _build_assembly_agent(model)

    # Build nodes
    nodes = {
        SCENARIO: GraphNode(node_id=SCENARIO, executor=scenario_agent),
        OTIO: GraphNode(node_id=OTIO, executor=otio_gate_agent),
        AUDIO: GraphNode(node_id=AUDIO, executor=audio_agent),
        VIDEO: GraphNode(node_id=VIDEO, executor=video_agent),
        ASSEMBLY: GraphNode(node_id=ASSEMBLY, executor=assembly_agent),
    }

    # Forward edges: scenario → otio → audio → otio → video → otio → assembly → otio
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


def _build_otio_gate_agent(model) -> Agent:
    """Build the OTIO gate agent — the structural authority.

    The gate sits between every stage transition. It:
    1. Reads the OTIO file to get current state
    2. Validates the previous stage's output
    3. If validation fails, writes error to OTIO (backward edge routes)
    4. If validation passes, summarizes what the next stage needs
    5. After audio: transitions timeline from draft → authoritative
    """
    from strands import tool

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

    @tool
    def validate_scenario() -> str:
        """Validate scenario output: scenes must exist and be well-formed."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import metadata_key_exists
        tp = resolve_timeline_path()
        errors = []
        if not metadata_key_exists(tp, MetadataSchema.SCENES):
            errors.append("Missing 'scenes' in OTIO metadata")
        if not metadata_key_exists(tp, MetadataSchema.VISUAL_STYLE):
            errors.append("Missing 'visual_style' in OTIO metadata")
        if not metadata_key_exists(tp, MetadataSchema.STYLE_LOCK):
            errors.append("Missing 'style_lock' in OTIO metadata")
        if errors:
            return json.dumps({"valid": False, "errors": errors, "recovery_target": SCENARIO})
        return json.dumps({"valid": True, "next_stage": AUDIO})

    @tool
    def validate_audio() -> str:
        """Validate audio output: narration clips must exist, timing within tolerance.

        CRITICAL FIX: whisperx_alignment is OPTIONAL. The gate passes if A1_Narration
        track has clips. This prevents infinite audio loops when the agent doesn't
        write alignment metadata.
        """
        from tools.otio_file_ops import resolve_timeline_path, otio_read
        from tools.otio_metadata import metadata_key_exists
        tp = resolve_timeline_path()
        errors = []
        warnings = []

        # Check A1_Narration track for clips FIRST — this is the real gate
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

        # whisperx_alignment is nice-to-have, not required for gate passage
        if not metadata_key_exists(tp, MetadataSchema.WHISPERX_ALIGNMENT):
            warnings.append("Missing 'whisperx_alignment' in OTIO metadata (optional)")

        if errors:
            return json.dumps({"valid": False, "errors": errors, "warnings": warnings, "recovery_target": AUDIO})
        return json.dumps({"valid": True, "next_stage": VIDEO, "warnings": warnings})

    @tool
    def validate_video() -> str:
        """Validate video output: clips must exist, no gaps."""
        from tools.otio_file_ops import resolve_timeline_path, otio_read
        tp = resolve_timeline_path()
        errors = []
        try:
            timeline = otio_read(tp)
            v1_track = None
            for track in timeline.tracks:
                if track.name == "V1_Video":
                    v1_track = track
                    break
            if v1_track is None:
                errors.append("No V1_Video track found")
            elif len(list(v1_track)) == 0:
                errors.append("V1_Video track is empty — no video clips")
        except Exception as e:
            errors.append(f"Error reading timeline: {e}")
        if errors:
            return json.dumps({"valid": False, "errors": errors, "recovery_target": VIDEO})
        return json.dumps({"valid": True, "next_stage": ASSEMBLY})

    @tool
    def validate_assembly() -> str:
        """Validate assembly: final output file must exist."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import read_pipeline_metadata
        tp = resolve_timeline_path()
        errors = []
        # Check that assembly produced an output
        output_path = read_pipeline_metadata(tp, MetadataSchema.ASSEMBLY_OUTPUT_PATH)
        if not output_path:
            errors.append("No assembly output path in OTIO metadata")
        if errors:
            return json.dumps({"valid": False, "errors": errors, "recovery_target": ASSEMBLY})
        return json.dumps({"valid": True, "pipeline_complete": True, "output_path": output_path})

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
                artifact_type="timeline",
                artifact_id=f"{stage}_gate",
                verdict=QaVerdict(
                    check_name=f"gate_{stage}",
                    status=verdict,  # "pass", "warn", "fail"
                    detail=json.loads(details_json) if details_json else {},
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
            pipeline_dir = os.environ.get("PIPELINE_DIR", "/tmp/documentary-pipeline")
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
                "preview_path": manifest.output_path if hasattr(manifest, "output_path") else None,
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
            "- The scenario agent writes ONE key: 'scenes' (an array of scene dicts).\n"
            "  Each scene dict contains: title, description, visual_notes, audio_notes, duration_seconds, start_time, end_time.\n"
            "- Downstream agents DERIVE what they need from 'scenes'. Do NOT demand\n"
            "  separate 'scene_data', 'visual_concepts', 'narration_text', or 'target_duration' keys.\n"
            "- Audio agent derives narration_text from scene['description'] and scene['audio_notes'].\n"
            "- Video agent derives visual prompts from scene['visual_notes'] and scene['description'].\n"
            "- The scenario gate ONLY checks: scenes exist, visual_style exists, style_lock is true.\n"
            "- The audio gate checks: A1_Narration track has clips OR whisperx_alignment exists.\n"
            "- The video gate checks: V1_Video track has clips.\n"
            "- The assembly gate checks: assembly_output_path exists.\n\n"
            "Rules:\n"
            "- You are stateless. All state lives in the OTIO file on disk.\n"
            "- Never assume memory of previous runs. Always read the OTIO file first.\n"
            "- If validation fails, be specific about what is missing so the recovery agent knows what to fix.\n"
            "- If validation passes, provide a concise summary of what the next stage should expect.\n"
        ),
        tools=[
            read_pipeline_data,
            read_timeline,
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
        ] + _make_memory_tools("otio_gate"),
        model=model,
    )


# ---------------------------------------------------------------------------
# Stage agents
# ---------------------------------------------------------------------------


def _read_directives() -> dict | None:
    """Read debug-gym directives from the pipeline directory.

    Tries PIPELINE_DIR env var first (set before pipeline starts),
    then falls back to resolve_timeline_path().
    """
    pipeline_dir = os.environ.get("PIPELINE_DIR", "")
    if not pipeline_dir:
        try:
            from tools.otio_file_ops import resolve_timeline_path
            tp = resolve_timeline_path()
            pipeline_dir = os.path.dirname(tp)
        except Exception:
            return None
    try:
        path = os.path.join(pipeline_dir, ".directives.json")
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _build_scenario_agent(model) -> Agent:
    """Build the scenario agent — generates scenes and locks visual style."""
    from strands import tool

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
    def write_scenes(scenes_json: str) -> str:
        """Write generated scenes to OTIO metadata."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        scenes = json.loads(scenes_json)
        write_pipeline_metadata(tp, MetadataSchema.SCENES, scenes, provenance={"agent": SCENARIO})
        return json.dumps({"written": True, "scene_count": len(scenes)})

    @tool
    def write_visual_style(style_json: str) -> str:
        """Write visual style and lock to OTIO metadata."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        style = json.loads(style_json)
        write_pipeline_metadata(tp, MetadataSchema.VISUAL_STYLE, style, provenance={"agent": SCENARIO})
        write_pipeline_metadata(tp, MetadataSchema.STYLE_LOCK, True, provenance={"agent": SCENARIO})
        return json.dumps({"written": True})

    @tool
    def check_resume_status() -> str:
        """Check if this stage was already completed in a previous run.
        If completed, call save_scenario_checkpoint and return immediately."""
        shell = get_recovery_shell()
        if shell and SCENARIO in shell.completed_stages:
            return json.dumps({"status": "already_completed", "stage": SCENARIO})
        return json.dumps({"status": "not_completed", "stage": SCENARIO})

    @tool
    def save_scenario_checkpoint() -> str:
        """Save the current OTIO timeline to the checkpoint directory after narrative planning."""
        from tools.otio_file_ops import resolve_timeline_path
        tp = resolve_timeline_path()
        shell = get_recovery_shell()
        run_id = shell.run_id if shell else ""
        if not run_id:
            return json.dumps({"saved": False, "reason": "no run_id"})
        root = checkpoint_dir(run_id)
        dest_dir = os.path.join(root, "agents", SCENARIO)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "scenario_timeline.otio")
        shutil.copy2(tp, dest)
        # Record completion in checkpoint metadata envelope
        _update_completed_stages(run_id, SCENARIO)
        return json.dumps({"saved": True, "checkpoint_path": dest})

    return Agent(
        name=SCENARIO,
        system_prompt=(
            "You are the Scenario Agent. You generate the documentary structure.\n"
            "BEFORE doing any work, call check_resume_status. If it returns 'already_completed', "
            "call save_scenario_checkpoint and then STOP — do not regenerate scenes.\n"
            f"{constraint_text}"
            "1. Create scenes based on the input brief.\n"
            "2. Define visual_style and set style_lock.\n"
            "3. Write scenes, visual_style, and style_lock to the OTIO file.\n"
            "4. Call save_scenario_checkpoint to preserve the narrative planning in the checkpoint directory.\n"
            "You are stateless — all output goes to the OTIO file."
        ),
        tools=[write_scenes, write_visual_style, check_resume_status, save_scenario_checkpoint] + _make_memory_tools(SCENARIO),
        model=model,
    )


def _build_audio_agent(model, run_id: str = "") -> Agent:
    """Build the audio agent — owns narration end-to-end.

    Uses real production tools from strands_agents.stages.audio_stage.
    Checkpoint/resume tools are added alongside."""
    from strands_agents.stages.audio_stage import (
        generate_scene_narration,
        add_narration_to_timeline,
        align_narration_audio,
        evaluate_audio_timing,
        read_scenes_from_otio,
        persist_audio_to_otio,
    )

    from strands import tool

    @tool
    def check_resume_status() -> str:
        """Check if this stage was already completed in a previous run.

        CRITICAL FIX: Also checks disk state — if A1_Narration clips exist in OTIO
        or WAV files exist in the audio directory, the stage is considered done.
        This prevents regeneration when completed_stages is empty (reset_on_revisit).
        """
        shell = get_recovery_shell()
        if shell and AUDIO in shell.completed_stages:
            return json.dumps({"status": "already_completed", "stage": AUDIO, "reason": "checkpoint"})

        # FIX: Check actual timeline for clips (works even after graph reset)
        from tools.otio_file_ops import resolve_timeline_path, otio_read
        try:
            tp = resolve_timeline_path()
            timeline = otio_read(tp)
            for track in timeline.tracks:
                if track.name == "A1_Narration":
                    if len(list(track)) > 0:
                        return json.dumps({"status": "already_completed", "stage": AUDIO, "reason": "otio_clips_exist"})
        except Exception:
            pass

        # FIX: Check audio output directory for existing WAV files
        import glob
        pipeline_dir = os.environ.get("PIPELINE_DIR", "/tmp/documentary-pipeline")
        audio_dir = os.path.join(pipeline_dir, "audio")
        wav_files = glob.glob(os.path.join(audio_dir, "*.wav"))
        if len(wav_files) > 0:
            return json.dumps({"status": "already_completed", "stage": AUDIO, "reason": "wav_files_exist", "count": len(wav_files)})

        return json.dumps({"status": "not_completed", "stage": AUDIO})

    @tool
    def save_audio_checkpoint() -> str:
        """Save the current OTIO timeline after audio generation."""
        from tools.otio_file_ops import resolve_timeline_path
        tp = resolve_timeline_path()
        shell = get_recovery_shell()
        run_id = shell.run_id if shell else ""
        if not run_id:
            return json.dumps({"saved": False, "reason": "no run_id"})
        root = checkpoint_dir(run_id)
        dest_dir = os.path.join(root, "agents", AUDIO)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "audio_timeline.otio")
        shutil.copy2(tp, dest)
        _update_completed_stages(run_id, AUDIO)
        return json.dumps({"saved": True, "checkpoint_path": dest})

    from strands import tool as _tool_decorator
    from agents.audio_provisioner_agent import (
        _tool_search_gpu_offers as _audio_search,
        _tool_provision_vm as _audio_provision,
        _tool_check_vm_status as _audio_check_vm,
        _tool_check_worker_health as _audio_check_health,
    )
    from strands_agents.shared_a2a.vast_provisioning import ssh_run_command as _audio_ssh

    @_tool_decorator
    def search_gpu_offers(query: str) -> str:
        """Search Vast.ai for GPU offers."""
        return _audio_search(query)

    @_tool_decorator
    def provision_vm(offer_id: int, disk_gb: int = 64, worker_mode: str = "tts", docker_image: str = "") -> str:
        """Provision a VM on Vast.ai."""
        return _audio_provision(offer_id, disk_gb, worker_mode, docker_image)

    @_tool_decorator
    def check_vm_status(vm_id: str) -> str:
        """Check the status of a provisioned VM."""
        return _audio_check_vm(vm_id)

    @_tool_decorator
    def check_worker_health(url: str, capability: str = "tts") -> str:
        """Check if a worker is healthy."""
        return _audio_check_health(url, capability)

    @_tool_decorator
    def ssh_run_command(instance_id: int, command: str) -> str:
        """Run a shell command on a Vast.ai instance via SSH."""
        return _audio_ssh(instance_id, command)

    return Agent(
        name=AUDIO,
        system_prompt=(
            "You are the Audio Agent. You own narration end-to-end.\n"
            "BEFORE doing any work, call check_resume_status. If it returns 'already_completed', "
            "call save_audio_checkpoint and then STOP — do not regenerate narration.\n"
            "1. Read scenes from OTIO with read_scenes_from_otio.\n"
            "2. Check if you have a TTS worker. If not, provision one:\n"
            "   a. Call search_gpu_offers to find a suitable GPU.\n"
            "   b. Call provision_vm with the offer_id.\n"
            "   c. Call check_vm_status until the VM is running.\n"
            "   d. Call check_worker_health with the VM URL until it returns healthy.\n"
            "   e. Use ssh_run_command to troubleshoot if the worker fails to start.\n"
            "3. For EACH scene, call generate_scene_narration with the worker_url.\n"
            "   Pass the TTS worker URL explicitly. If it fails, fix the worker and retry.\n"
            "4. Add narration clips to the OTIO timeline with add_narration_to_timeline.\n"
            "5. Run WhisperX alignment with align_narration_audio.\n"
            "6. Evaluate timing with evaluate_audio_timing.\n"
            "7. Persist state with persist_audio_to_otio.\n"
            "8. Call save_audio_checkpoint to preserve the audio work.\n"
            "Use your memory tools to recall GPU provisioning failures."
        ),
        tools=[
            generate_scene_narration,
            add_narration_to_timeline,
            align_narration_audio,
            evaluate_audio_timing,
            read_scenes_from_otio,
            persist_audio_to_otio,
            check_resume_status,
            save_audio_checkpoint,
            search_gpu_offers,
            provision_vm,
            check_vm_status,
            check_worker_health,
            _audio_ssh,
        ] + _make_memory_tools(AUDIO),
        model=model,
    )


def _build_video_agent(model) -> Agent:
    """Build the video agent — owns visual planning and rendering end-to-end.

    Uses real production tools from strands_agents.stages.production_stage
    plus OTIO clip management. Checkpoint/resume tools added alongside."""
    from strands_agents.stages.production_stage import (
        generate_production_plan,
        evaluate_production_plan,
        submit_gpu_production_job,
        finalize_production,
    )
    from strands import tool

    @tool
    def add_video_clip_to_timeline(
        scene_num: int,
        phrase_idx: int,
        mp4_path: str,
        duration: float,
        lora_id: str = "",
    ) -> str:
        """Add a video clip to the V1_Video track on the OTIO timeline.

        If mp4_path does not exist, returns an honest failure — no placeholder is generated.
        """
        import opentimelineio as otio
        from tools.otio_file_ops import resolve_timeline_path, otio_read, otio_write

        tp = resolve_timeline_path()
        timeline = otio_read(tp)

        v1_track = None
        for track in timeline.tracks:
            if track.name == "V1_Video":
                v1_track = track
                break
        if v1_track is None:
            v1_track = otio.schema.Track(name="V1_Video", kind="video")
            timeline.tracks.append(v1_track)

        # No placeholder generation — if the clip doesn't exist, fail honestly.
        if not os.path.exists(mp4_path):
            return json.dumps({
                "status": "failed",
                "error": f"Video clip not found: {mp4_path}",
                "clip": f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}",
            })

        actual_path = mp4_path
        fps = 24
        dur_frames = int(duration * fps)
        clip = otio.schema.Clip(
            name=f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}",
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, fps),
                duration=otio.opentime.RationalTime(dur_frames, fps),
            ),
            media_reference=otio.schema.ExternalReference(
                target_url=f"file://{actual_path}",
                available_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, fps),
                    duration=otio.opentime.RationalTime(dur_frames, fps),
                ),
            ),
        )
        clip.metadata.setdefault("video", {})
        clip.metadata["video"]["lora_id"] = lora_id
        clip.metadata["video"]["scene_num"] = scene_num
        clip.metadata["video"]["phrase_idx"] = phrase_idx
        v1_track.append(clip)

        otio_write(tp, timeline)
        return json.dumps({
            "status": "clip_added",
            "clip": clip.name,
            "duration": duration,
            "path": actual_path,
            "track": "V1_Video",
        })

    @tool
    def check_resume_status() -> str:
        """Check if this stage was already completed in a previous run."""
        shell = get_recovery_shell()
        if shell and VIDEO in shell.completed_stages:
            return json.dumps({"status": "already_completed", "stage": VIDEO})
        return json.dumps({"status": "not_completed", "stage": VIDEO})

    @tool
    def save_video_checkpoint() -> str:
        """Save the current OTIO timeline after video rendering."""
        from tools.otio_file_ops import resolve_timeline_path
        tp = resolve_timeline_path()
        shell = get_recovery_shell()
        run_id = shell.run_id if shell else ""
        if not run_id:
            return json.dumps({"saved": False, "reason": "no run_id"})
        root = checkpoint_dir(run_id)
        dest_dir = os.path.join(root, "agents", VIDEO)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "video_timeline.otio")
        shutil.copy2(tp, dest)
        _update_completed_stages(run_id, VIDEO)
        return json.dumps({"saved": True, "checkpoint_path": dest})

    @tool
    def generate_visual_concepts(style_description: str) -> str:
        """Generate visual concepts from the documentary's visual style and scenes.

        Reads scenes from OTIO and produces structured visual concepts for each scene.
        These concepts guide the video rendering pipeline.
        """
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import read_pipeline_metadata
        tp = resolve_timeline_path()
        scenes = read_pipeline_metadata(tp, MetadataSchema.SCENES) or []
        visual_style = read_pipeline_metadata(tp, MetadataSchema.VISUAL_STYLE) or {}

        concepts = []
        for i, scene in enumerate(scenes, 1):
            desc = scene.get("description", "")
            visual_notes = scene.get("visual_notes", "")
            duration = float(scene.get("duration_seconds", scene.get("duration_sec", 5.0)))
            concept_prompt = f"{style_description}. {visual_notes or desc}".strip()
            concepts.append({
                "scene_num": i,
                "phrase_idx": 0,
                "prompt": concept_prompt[:500],
                "negative_prompt": "blurry, low quality, watermark, text overlay",
                "duration": min(duration, 10.0),
                "lora_id": visual_style.get("lora_id", "documentary-realism"),
                "lora_weight": float(visual_style.get("lora_weight", 0.75)),
            })
        return json.dumps({"concepts": concepts, "count": len(concepts)})

    @tool
    def persist_visual_concepts(concepts_json: str) -> str:
        """Persist generated visual concepts to OTIO metadata."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        data = json.loads(concepts_json)
        concepts = data.get("concepts", data if isinstance(data, list) else [])
        write_pipeline_metadata(tp, "visual_concepts", concepts, provenance={"agent": VIDEO})
        return json.dumps({"persisted": True, "concept_count": len(concepts)})

    from strands import tool as _tool_decorator
    from agents.video_provisioner_agent import (
        _tool_search_gpu_offers as _video_search,
        _tool_provision_vm as _video_provision,
        _tool_check_vm_status as _video_check_vm,
        _tool_check_worker_health as _video_check_health,
    )
    from strands_agents.shared_a2a.vast_provisioning import ssh_run_command as _video_ssh

    @_tool_decorator
    def search_gpu_offers(query: str) -> str:
        """Search Vast.ai for GPU offers."""
        return _video_search(query)

    @_tool_decorator
    def provision_vm(offer_id: int, disk_gb: int = 224, worker_mode: str = "ltx", docker_image: str = "") -> str:
        """Provision a VM on Vast.ai."""
        return _video_provision(offer_id, disk_gb, worker_mode, docker_image)

    @_tool_decorator
    def check_vm_status(vm_id: str) -> str:
        """Check the status of a provisioned VM."""
        return _video_check_vm(vm_id)

    @_tool_decorator
    def check_worker_health(url: str, capability: str = "ltx") -> str:
        """Check if a worker is healthy."""
        return _video_check_health(url, capability)

    @_tool_decorator
    def ssh_run_command(instance_id: int, command: str) -> str:
        """Run a shell command on a Vast.ai instance via SSH."""
        return _video_ssh(instance_id, command)

    return Agent(
        name=VIDEO,
        system_prompt=(
            "You are the Video Agent. You own visual planning and rendering end-to-end.\n"
            "BEFORE doing any work, call check_resume_status. If it returns 'already_completed', "
            "call save_video_checkpoint and then STOP — do not render clips.\n"
            "1. Call generate_visual_concepts with a style description derived from the "
            "visual_style in OTIO. This produces structured concepts for each scene.\n"
            "2. Call persist_visual_concepts to save the concepts to OTIO metadata.\n"
            "3. Read scenes from OTIO and plan visuals with generate_production_plan.\n"
            "4. Evaluate the plan with evaluate_production_plan.\n"
            "5. Check if you have a video worker. If not, provision one:\n"
            "   a. Call search_gpu_offers to find a suitable GPU.\n"
            "   b. Call provision_vm with the offer_id.\n"
            "   c. Call check_vm_status until the VM is running.\n"
            "   d. Call check_worker_health with the VM URL until it returns healthy.\n"
            "   e. Use ssh_run_command to troubleshoot if the worker fails to start.\n"
            "6. For EACH scene, call submit_gpu_production_job with worker_url.\n"
            "   Pass the video worker URL explicitly. If it fails, fix the worker and retry.\n"
            "7. After each render succeeds, call add_video_clip_to_timeline with the video_path.\n"
            "8. Finalize with finalize_production.\n"
            "9. Call save_video_checkpoint to preserve the rendered clips.\n"
            "Use your memory tools to recall rendering failures."
        ),
        tools=[
            generate_production_plan,
            evaluate_production_plan,
            submit_gpu_production_job,
            finalize_production,
            add_video_clip_to_timeline,
            generate_visual_concepts,
            persist_visual_concepts,
            check_resume_status,
            save_video_checkpoint,
            search_gpu_offers,
            provision_vm,
            check_vm_status,
            check_worker_health,
            _video_ssh,
        ] + _make_memory_tools(VIDEO),
        model=model,
    )


def _build_assembly_agent(model) -> Agent:
    """Build the assembly agent — produces the final deliverable.

    Uses real assembly tools from strands_agents.stages.assembly_stage.
    Checkpoint/resume tools added alongside."""
    from strands_agents.stages.assembly_stage import (
        assemble_final_cut,
        read_timeline,
        validate_assembly,
    )
    from strands import tool

    @tool
    def write_assembly_output_path(output_path: str) -> str:
        """Write the final output path to OTIO metadata."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        return write_pipeline_metadata(tp, MetadataSchema.ASSEMBLY_OUTPUT_PATH, output_path, provenance={"agent": ASSEMBLY})

    @tool
    def check_resume_status() -> str:
        """Check if this stage was already completed in a previous run."""
        shell = get_recovery_shell()
        if shell and ASSEMBLY in shell.completed_stages:
            return json.dumps({"status": "already_completed", "stage": ASSEMBLY})
        return json.dumps({"status": "not_completed", "stage": ASSEMBLY})

    @tool
    def save_assembly_checkpoint() -> str:
        """Save the current OTIO timeline to the checkpoint directory after final assembly."""
        from tools.otio_file_ops import resolve_timeline_path
        tp = resolve_timeline_path()
        shell = get_recovery_shell()
        run_id = shell.run_id if shell else ""
        if not run_id:
            return json.dumps({"saved": False, "reason": "no run_id"})
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
            "BEFORE doing any work, call check_resume_status. If it returns 'already_completed', "
            "call save_assembly_checkpoint and then STOP — do not re-assemble.\n"
            "1. Read the OTIO timeline with read_timeline.\n"
            "2. Validate with validate_assembly.\n"
            "3. Assemble the final cut with assemble_final_cut (this calls ffmpeg).\n"
            "4. Write assembly_output_path to OTIO metadata with write_assembly_output_path.\n"
            "5. Call save_assembly_checkpoint to preserve the final movie state."
        ),
        tools=[
            assemble_final_cut,
            read_timeline,
            validate_assembly,
            write_assembly_output_path,
            check_resume_status,
            save_assembly_checkpoint,
        ] + _make_memory_tools(ASSEMBLY),
        model=model,
    )


# ---------------------------------------------------------------------------
# Backward edge conditions
# ---------------------------------------------------------------------------


def _needs_scenario_retry(prev_output: Any, *_args, **_kwargs) -> bool:
    """Check if OTIO gate requested scenario recovery."""
    try:
        data = json.loads(prev_output) if isinstance(prev_output, str) else prev_output
        return data.get("recovery_target") == SCENARIO
    except Exception:
        return False


def _needs_audio_retry(prev_output: Any, *_args, **_kwargs) -> bool:
    """Check if OTIO gate requested audio recovery."""
    try:
        data = json.loads(prev_output) if isinstance(prev_output, str) else prev_output
        return data.get("recovery_target") == AUDIO
    except Exception:
        return False


def _needs_video_retry(prev_output: Any, *_args, **_kwargs) -> bool:
    """Check if OTIO gate requested video recovery."""
    try:
        data = json.loads(prev_output) if isinstance(prev_output, str) else prev_output
        return data.get("recovery_target") == VIDEO
    except Exception:
        return False


def _needs_assembly_retry(prev_output: Any, *_args, **_kwargs) -> bool:
    """Check if OTIO gate requested assembly recovery."""
    try:
        data = json.loads(prev_output) if isinstance(prev_output, str) else prev_output
        return data.get("recovery_target") == ASSEMBLY
    except Exception:
        return False


def _scenario_not_completed(_prev_output: Any, *_args, **_kwargs) -> bool:
    """Run the Scenario stage only if it was NOT already completed."""
    shell = get_recovery_shell()
    if shell and SCENARIO in shell.completed_stages:
        return False
    # FIX: Also check OTIO for scenes metadata
    from tools.otio_file_ops import resolve_timeline_path
    from tools.otio_metadata import metadata_key_exists
    try:
        tp = resolve_timeline_path()
        if metadata_key_exists(tp, MetadataSchema.SCENES):
            return False
    except Exception:
        pass
    return True


def _audio_not_completed(_prev_output: Any, *_args, **_kwargs) -> bool:
    """Run the Audio stage only if it was NOT already completed."""
    shell = get_recovery_shell()
    if shell and AUDIO in shell.completed_stages:
        return False
    # FIX: Check OTIO for A1_Narration clips (works even after graph reset)
    from tools.otio_file_ops import resolve_timeline_path, otio_read
    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)
        for track in timeline.tracks:
            if track.name == "A1_Narration" and len(list(track)) > 0:
                return False
    except Exception:
        pass
    return True


def _video_not_completed(_prev_output: Any, *_args, **_kwargs) -> bool:
    """Run the Video stage only if it was NOT already completed."""
    shell = get_recovery_shell()
    if shell and VIDEO in shell.completed_stages:
        return False
    # FIX: Check OTIO for V1_Video clips (works even after graph reset)
    from tools.otio_file_ops import resolve_timeline_path, otio_read
    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)
        for track in timeline.tracks:
            if track.name == "V1_Video" and len(list(track)) > 0:
                return False
    except Exception:
        pass
    return True


def _assembly_not_completed(_prev_output: Any, *_args, **_kwargs) -> bool:
    """Run the Assembly stage only if it was NOT already completed."""
    shell = get_recovery_shell()
    if shell and ASSEMBLY in shell.completed_stages:
        return False
    # FIX: Check OTIO for assembly_output_path AND V1_Video clips
    from tools.otio_file_ops import resolve_timeline_path, otio_read
    from tools.otio_metadata import read_pipeline_metadata
    try:
        tp = resolve_timeline_path()
        # Already assembled?
        output_path = read_pipeline_metadata(tp, MetadataSchema.ASSEMBLY_OUTPUT_PATH)
        if output_path and os.path.exists(output_path):
            return False
        # Video must be done before assembly
        timeline = otio_read(tp)
        has_video = False
        for track in timeline.tracks:
            if track.name == "V1_Video" and len(list(track)) > 0:
                has_video = True
                break
        if not has_video:
            return False  # Can't assemble without video
    except Exception:
        # Fallback: if OTIO check fails, use completed_stages logic
        if shell and VIDEO not in shell.completed_stages:
            return False
    return True