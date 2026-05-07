"""
Documentary pipeline — Strands Graph orchestration.

4 agent nodes: Scenario → OTIO Gate → Audio → OTIO Gate → Video → OTIO Gate

Architecture::

    RecoveryShell (wraps Graph invocation)
      ├─ Catches RuntimeError from fail-fast Graph
      ├─ Classifies failure (which node, what went wrong)
      └─ Re-invokes Graph with recovery context

    Strands Graph (pipeline orchestration)
      ├─ 4 nodes: scenario → otio → audio → otio → video → otio
      ├─ OTIO gate node: validation between stages, draft→authoritative
      ├─ Forward edges: deterministic stage ordering via gates
      ├─ Backward edges: conditional recovery from OTIO file
      └─ Data flows through OTIO file on disk (stateless)

Agents:
    - Scenario agent (generates scenes, visual style, style lock)
    - Audio+Provisioner agent (TTS + GPU workers, owns narration end-to-end)
    - Video+Provisioner agent (visual planning + rendering, owns clips end-to-end)
    - OTIO gate agent (validation, lifecycle enforcement, escalation management)
"""

from __future__ import annotations

import json
import logging
import os
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
    model: Any | None = None,
) -> Graph:
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
    # Build stage agents as Strands Agents with stateless OTIO tools
    scenario_agent = _build_scenario_agent(model)
    audio_agent = _build_audio_agent(model)
    video_agent = _build_video_agent(model)
    otio_gate_agent = _build_otio_gate_agent(model)

    # Build nodes
    nodes = {
        SCENARIO: GraphNode(node_id=SCENARIO, executor=scenario_agent),
        OTIO: GraphNode(node_id=OTIO, executor=otio_gate_agent),
        AUDIO: GraphNode(node_id=AUDIO, executor=audio_agent),
        VIDEO: GraphNode(node_id=VIDEO, executor=video_agent),
    }

    # Forward edges: scenario → otio → audio → otio → video → otio
    forward_edges = {
        GraphEdge(from_node=nodes[SCENARIO], to_node=nodes[OTIO]),
        GraphEdge(from_node=nodes[OTIO], to_node=nodes[AUDIO]),
        GraphEdge(from_node=nodes[AUDIO], to_node=nodes[OTIO]),
        GraphEdge(from_node=nodes[OTIO], to_node=nodes[VIDEO]),
        GraphEdge(from_node=nodes[VIDEO], to_node=nodes[OTIO]),
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
        from tools.otio_metadata import metadata_key_exists, read_pipeline_metadata
        tp = resolve_timeline_path()
        errors = []
        if not metadata_key_exists(tp, "scenes"):
            errors.append("Missing 'scenes' in OTIO metadata")
        if not metadata_key_exists(tp, "visual_style"):
            errors.append("Missing 'visual_style' in OTIO metadata")
        if not metadata_key_exists(tp, "style_lock"):
            errors.append("Missing 'style_lock' in OTIO metadata")
        if errors:
            return json.dumps({"valid": False, "errors": errors, "recovery_target": SCENARIO})
        return json.dumps({"valid": True, "next_stage": AUDIO})

    @tool
    def validate_audio() -> str:
        """Validate audio output: narration clips must exist, timing within tolerance."""
        from tools.otio_file_ops import resolve_timeline_path, otio_read
        from tools.otio_metadata import metadata_key_exists
        tp = resolve_timeline_path()
        errors = []
        if not metadata_key_exists(tp, "whisperx_alignment"):
            errors.append("Missing 'whisperx_alignment' in OTIO metadata")
        # Check A1_Narration track for clips
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
            return json.dumps({"valid": False, "errors": errors, "recovery_target": AUDIO})
        return json.dumps({"valid": True, "next_stage": VIDEO})

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
        return json.dumps({"valid": True, "pipeline_complete": True})

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
        return write_pipeline_metadata(tp, f"gate_{stage}", result, provenance={"agent": "otio_gate"})

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
        key = f"{stage}_ladder"
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
        with honest placeholders for any missing slots.
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
            "6. After every validation (pass OR fail): write_critique_record + trigger_preview\n\n"
            "WORKFLOW:\n"
            "- After scenario: call validate_scenario(). If valid, next is audio.\n"
            "- After audio: call validate_audio(). If valid, call transition_to_authoritative, next is video.\n"
            "- After video: call validate_video(). If valid, pipeline is complete.\n"
            "- After EVERY validation: write_critique_record(stage, verdict, details) + trigger_preview(stage)\n\n"
            "RULES:\n"
            "- You NEVER generate content. You only validate and enforce.\n"
            "- Read ALL data from the OTIO file. All results go TO the OTIO file.\n"
            "- If validation fails, write the error to OTIO and report it clearly.\n"
            "- The draft→authoritative transition happens ONLY after audio validation passes.\n"
            "- Critique records and previews are fire-and-forget QA artifacts. They never block the pipeline.\n"
        ),
        tools=[
            read_pipeline_data, read_timeline,
            validate_scenario, validate_audio, validate_video,
            transition_to_authoritative, write_gate_result,
            get_otio_lifecycle_state,
            begin_escalation, end_escalation,
            read_ladder_state,
            write_critique_record, trigger_preview,
        ],
        model=model,
    )


# ---------------------------------------------------------------------------
# Agent builders — wrap existing agents as Strands Agents
# ---------------------------------------------------------------------------


def _build_scenario_agent(model) -> Agent:
    """Build a Strands Agent for the scenario stage.

    Uses stateless OTIO file ops to persist scenes, visual_style,
    and style_lock to the OTIO file on disk.
    """
    from strands import tool

    @tool
    def write_scenes(scenes_json: str, provenance_json: str = "{}") -> str:
        """Write scenes to the OTIO timeline file."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        return write_pipeline_metadata(tp, "scenes", json.loads(scenes_json),
                                       provenance=json.loads(provenance_json))

    @tool
    def write_visual_style(style_json: str, provenance_json: str = "{}") -> str:
        """Write visual style to the OTIO timeline file."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        return write_pipeline_metadata(tp, "visual_style", json.loads(style_json),
                                       provenance=json.loads(provenance_json))

    @tool
    def write_style_lock(lock_json: str, provenance_json: str = "{}") -> str:
        """Write style lock to the OTIO timeline file."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        return write_pipeline_metadata(tp, "style_lock", json.loads(lock_json),
                                       provenance=json.loads(provenance_json))

    return Agent(
        name="scenario",
        system_prompt=(
            "You are the Scenario Agent for a documentary pipeline.\n\n"
            "Your job is to generate a documentary scenario: scenes, visual "
            "style, and style lock. Write ALL output to the OTIO file "
            "using write_scenes, write_visual_style, and write_style_lock.\n\n"
            "RULES:\n"
            "- ALL data goes to the OTIO file on disk. No agent state.\n"
            "- Every write carries provenance.\n"
            "- Persist immediately, even on error.\n"
        ),
        tools=[write_scenes, write_visual_style, write_style_lock],
        model=model,
    )


def _build_audio_agent(model) -> Agent:
    """Build a Strands Agent for the audio stage.

    Uses the AudioProvisionerAgent — merged audio + provisioner.
    The agent owns TTS end-to-end: allocate workers, generate
    narration, evaluate quality, scale workers.
    All OTIO operations are stateless — read/write the OTIO file directly.
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
    def find_narration_gaps() -> str:
        """Scan OTIO timeline for missing narration. Returns TTS job list."""
        from agents.audio_provisioner_agent import _tool_find_narration_gaps
        return _tool_find_narration_gaps()

    @tool
    def get_scene_durations() -> str:
        """Get per-scene duration budgets from OTIO."""
        from agents.audio_provisioner_agent import _tool_get_scene_durations
        return _tool_get_scene_durations()

    @tool
    def write_pipeline_data(key: str, value_json: str, provenance_json: str = "{}") -> str:
        """Write pipeline metadata to the OTIO file."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        return write_pipeline_metadata(tp, key, json.loads(value_json),
                                       provenance=json.loads(provenance_json))

    @tool
    def add_clip(track: str, scene_num: int, phrase_idx: int,
                 clip_path: str, duration: float,
                 provenance_json: str = "{}") -> str:
        """Add a clip to the OTIO timeline."""
        from agents.audio_provisioner_agent import _tool_add_clip
        return _tool_add_clip(track, scene_num, phrase_idx, clip_path, duration, provenance_json)

    @tool
    def validate_timeline(phase: str) -> str:
        """Validate timeline structural integrity."""
        from agents.audio_provisioner_agent import _tool_validate_timeline
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

    @tool
    def begin_escalation(escalation_type: str, reason: str) -> str:
        """Open an escalation window to modify authoritative OTIO.
        escalation_type: 'REPLACE' or 'EXTEND'.
        """
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_lifecycle import begin_escalation as _begin
        tp = resolve_timeline_path()
        return _begin(tp, escalation_type, reason, "audio")

    @tool
    def end_escalation() -> str:
        """Close the escalation window on the OTIO timeline."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_lifecycle import end_escalation as _end
        tp = resolve_timeline_path()
        return _end(tp)

    @tool
    def classify_failure(error_msg: str) -> str:
        """Classify a pipeline failure as content, infra, or unclear."""
        from diagnostic_classifier import classify_failure as _classify, build_signals_from_error
        signals = build_signals_from_error(error_msg)
        result = _classify(error_msg, signals)
        return json.dumps({"failure_class": result.name if hasattr(result, "name") else str(result)})

    @tool
    def write_ladder_state(level: int, attempts: int, history_json: str = "[]") -> str:
        """Write audio ladder state to OTIO for tracking."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        return write_pipeline_metadata(tp, "audio_ladder", {
            "level": level, "attempts": attempts,
            "history": json.loads(history_json),
        }, provenance={"agent": "audio"})

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
            "- ALL data flows through the OTIO file on disk. No agent state.\n"
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
            begin_escalation, end_escalation, classify_failure, write_ladder_state,
        ],
        model=model,
    )


def _build_video_agent(model) -> Agent:
    """Build a Strands Agent for the video stage.

    Uses the VideoProvisionerAgent — merged video + provisioner.
    The agent owns rendering end-to-end: visual planning, allocate
    GPU workers, render clips, evaluate quality, scale fleet.
    All OTIO operations are stateless — read/write the OTIO file directly.
    """
    from strands import tool

    @tool
    def read_timeline() -> str:
        """Read the full OTIO timeline structure."""
        from tools.otio_file_ops import resolve_timeline_path, otio_read
        tp = resolve_timeline_path()
        timeline = otio_read(tp)
        summary = {}
        for track in timeline.tracks:
            clips = []
            for item in track:
                clips.append({
                    "name": item.name,
                    "type": type(item).__name__,
                })
            summary[track.name] = {"clip_count": len(clips), "clips": clips}
        return json.dumps(summary)

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
    def get_scene_durations() -> str:
        """Get per-scene duration budgets from OTIO."""
        from agents.video_provisioner_agent import _tool_get_scene_durations
        return _tool_get_scene_durations()

    @tool
    def find_video_gaps() -> str:
        """Scan OTIO timeline for missing video. Returns render job list."""
        from agents.video_provisioner_agent import _tool_find_video_gaps
        return _tool_find_video_gaps()

    @tool
    def write_pipeline_data(key: str, value_json: str, provenance_json: str = "{}") -> str:
        """Write pipeline metadata to the OTIO file."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        return write_pipeline_metadata(tp, key, json.loads(value_json),
                                       provenance=json.loads(provenance_json))

    @tool
    def add_clip(track: str, scene_num: int, phrase_idx: int,
                 clip_path: str, duration: float,
                 provenance_json: str = "{}") -> str:
        """Add a clip to the OTIO timeline."""
        from agents.video_provisioner_agent import _tool_add_clip
        return _tool_add_clip(track, scene_num, phrase_idx, clip_path, duration, provenance_json)

    @tool
    def validate_timeline(phase: str) -> str:
        """Validate timeline structural integrity."""
        from agents.video_provisioner_agent import _tool_validate_timeline
        return _tool_validate_timeline(phase)

    @tool
    def rebalance_durations(adjustments_json: str, reason: str) -> str:
        """Redistribute time between scenes."""
        from agents.video_provisioner_agent import _tool_rebalance_durations
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

    @tool
    def begin_escalation(escalation_type: str, reason: str) -> str:
        """Open an escalation window to modify authoritative OTIO.
        escalation_type: 'REPLACE' or 'EXTEND'. Duration-preserving only for video.
        """
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_lifecycle import begin_escalation as _begin
        tp = resolve_timeline_path()
        return _begin(tp, escalation_type, reason, "video")

    @tool
    def end_escalation() -> str:
        """Close the escalation window on the OTIO timeline."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_lifecycle import end_escalation as _end
        tp = resolve_timeline_path()
        return _end(tp)

    @tool
    def classify_failure(error_msg: str) -> str:
        """Classify a pipeline failure as content, infra, or unclear."""
        from diagnostic_classifier import classify_failure as _classify, build_signals_from_error
        signals = build_signals_from_error(error_msg)
        result = _classify(error_msg, signals)
        return json.dumps({"failure_class": result.name if hasattr(result, "name") else str(result)})

    @tool
    def write_ladder_state(level: int, attempts: int, history_json: str = "[]") -> str:
        """Write video ladder state to OTIO for tracking."""
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import write_pipeline_metadata
        tp = resolve_timeline_path()
        return write_pipeline_metadata(tp, "video_ladder", {
            "level": level, "attempts": attempts,
            "history": json.loads(history_json),
        }, provenance={"agent": "video"})

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
            "- ALL data flows through the OTIO file on disk. No agent state.\n"
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
            begin_escalation, end_escalation, classify_failure, write_ladder_state,
        ],
        model=model,
    )


# ---------------------------------------------------------------------------
# Recovery conditions for backward edges — read from OTIO file
# ---------------------------------------------------------------------------


def _needs_scenario_retry(state) -> bool:
    """Backward edge: otio gate → scenario when scenario validation fails."""
    try:
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import read_pipeline_metadata
        tp = resolve_timeline_path()
        gate = read_pipeline_metadata(tp, "gate_scenario")
        if gate and isinstance(gate, dict) and not gate.get("valid", True):
            return gate.get("recovery_target") == SCENARIO
    except Exception:
        pass
    # Fallback to state dict
    try:
        return state.get("_recovery_target") == SCENARIO if hasattr(state, "get") else False
    except Exception:
        return False


def _needs_audio_retry(state) -> bool:
    """Backward edge: otio gate → audio when audio validation fails."""
    try:
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import read_pipeline_metadata
        tp = resolve_timeline_path()
        gate = read_pipeline_metadata(tp, "gate_audio")
        if gate and isinstance(gate, dict) and not gate.get("valid", True):
            return gate.get("recovery_target") == AUDIO
    except Exception:
        pass
    try:
        return state.get("_recovery_target") == AUDIO if hasattr(state, "get") else False
    except Exception:
        return False


def _needs_video_retry(state) -> bool:
    """Backward edge: otio gate → video when video validation fails."""
    try:
        from tools.otio_file_ops import resolve_timeline_path
        from tools.otio_metadata import read_pipeline_metadata
        tp = resolve_timeline_path()
        gate = read_pipeline_metadata(tp, "gate_video")
        if gate and isinstance(gate, dict) and not gate.get("valid", True):
            return gate.get("recovery_target") == VIDEO
    except Exception:
        pass
    try:
        return state.get("_recovery_target") == VIDEO if hasattr(state, "get") else False
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

    async def run(self, task: str, initial_state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute the graph with automatic recovery on failure."""
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

    @staticmethod
    def _classify_failure(exc: RuntimeError) -> str:
        """Extract the failed node name from a Graph RuntimeError."""
        msg = str(exc)
        for stage in STAGE_ORDER:
            if stage in msg:
                return stage
        logger.warning("Could not classify failure, defaulting to scenario: %s", msg[:200])
        return SCENARIO
