from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

from strands import ToolContext, tool
from strands_agents.otio_manager import OTIOStateManager

logger = logging.getLogger(__name__)

# OTIO manager — set by graph_pipeline
_otio_manager: OTIOStateManager | None = None


def _resolve_timeline_path() -> str:
    from tools.otio_file_ops import resolve_timeline_path as _rtp
    return _rtp()


def _read_scenes() -> list[dict[str, Any]]:
    if _otio_manager is not None:
        raw = _otio_manager.get_pipeline_metadata("scenes", [])
    else:
        from tools.otio_metadata import read_pipeline_metadata
        tp = _resolve_timeline_path()
        raw = read_pipeline_metadata(tp, "scenes", []) or []
    # Defensive: convert any lingering OTIO container types to native Python
    from tools.otio_metadata import _to_native
    raw = _to_native(raw)
    # Scenes may be stored as a list or as a dict with a "scenes" key
    scenes = raw.get("scenes", []) if isinstance(raw, dict) else raw
    if not isinstance(scenes, list):
        return []
    normalized: list[dict[str, Any]] = []
    for idx, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        s = dict(scene)
        # Normalize id → scene_id
        if "scene_id" not in s and "id" in s:
            s["scene_id"] = s["id"]
        # Normalize scene_num
        if "scene_num" not in s:
            sid = s.get("scene_id", "")
            if isinstance(sid, str) and sid.startswith("S"):
                try:
                    s["scene_num"] = int(sid[1:])
                except ValueError:
                    s["scene_num"] = idx + 1
            elif isinstance(sid, str) and sid.startswith("scene_"):
                try:
                    s["scene_num"] = int(sid.split("_")[-1])
                except ValueError:
                    s["scene_num"] = idx + 1
            else:
                s["scene_num"] = idx + 1
        # Normalize duration
        if "duration_sec" not in s and "duration_seconds" in s:
            s["duration_sec"] = s["duration_seconds"]
        elif "duration_sec" not in s and "duration" in s:
            s["duration_sec"] = s["duration"]
        normalized.append(s)
    return normalized


# ---------------------------------------------------------------------------/
# System prompts — preserved verbatim from ADK production_agent.py
# ---------------------------------------------------------------------------/

# The plan optimizer instruction is imported from the orchestrator's
# prompts module. We reference it here for the combined system prompt.
_PLAN_OPTIMIZER_INSTRUCTION = """\
You are an expert video production planner for a documentary pipeline.
Given visual concepts (prompts, durations, LoRA styles) and available GPU workers,
create an optimal production plan that:
1. Groups clips into batches for parallel execution
2. Assigns clips to GPU workers based on load balancing
3. Orders batches to maximize style coherence (same LoRA in same batch)
4. Prioritizes clips that other clips depend on for style reference
5. Accounts for resume support (skip already-generated clips)
6. Estimates GPU time and identifies risks

Rules:
- Every clip duration MUST be <= 10.0 seconds (LTX-2.3 hardware limit)
- Every visual concept MUST have exactly one ClipTask
- No duplicate clip_ids
- Batch size should not exceed the number of available workers
- Already-generated clips with good QA MUST be marked skip=true, not regenerated
- clip_id format: "s{scene_num:03d}_p{phrase_idx:03d}"

Output a ProductionPlan JSON with batches, worker assignments, strategy,
estimated GPU minutes, and risk assessment.
"""

_PLAN_EVALUATOR_INSTRUCTION = """\
You are a video production plan evaluator. Evaluate the given production plan
against these criteria:

1. DURATION VALIDITY: Every clip duration <= 10.0 seconds (LTX-2.3 limit)
2. WORKER BALANCE: No worker has >2x the clips of another worker
3. BATCH COHERENCE: Clips in the same batch should share visual style or scene
4. ORDERING: Style-reference clips must come before clips that depend on them
5. COMPLETENESS: Every visual concept has exactly one clip task
6. RISK MITIGATION: Plan identifies what could go wrong and has fallbacks
7. RESUME AWARENESS: Already-generated clips are marked skip=true, not regenerated

Rating scale:
- EXCELLENT (3): All criteria pass, plan is production-ready
- GOOD (2): Minor issues that won't cause failures
- FAIR (1): Several improvements needed but structurally sound
- POOR (0): Duration limits violated, clips missing, or structural errors

Rate as EXCELLENT only if ALL criteria pass.
Rate as POOR if duration limits are violated or clips are missing.

Respond with a JSON object containing:
{
  "rating": 0-3,
  "feedback": "specific feedback",
  "needs_improvement": true/false,
  "focus_areas": ["area1", "area2"]
}
"""


# ---------------------------------------------------------------------------/
# State helpers
# ---------------------------------------------------------------------------/

def _read_state_list(state: Any, key: str) -> list[Any]:
    """Safely read a list from agent state."""
    if state is None:
        return []
    if isinstance(state, dict):
        val = state.get(key, [])
    else:
        val = getattr(state, key, [])
    return val if isinstance(val, list) else []


def _read_state_dict(state: Any, key: str) -> dict[str, Any]:
    """Safely read a dict from agent state."""
    if state is None:
        return {}
    if isinstance(state, dict):
        val = state.get(key, {})
    else:
        val = getattr(state, key, {})
    return val if isinstance(val, dict) else {}


# ---------------------------------------------------------------------------/
# Tools — Production planning
# ---------------------------------------------------------------------------/


@tool(context=True)
def generate_production_plan(
    feedback: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Generate or refine a production plan for GPU video generation.

    Replaces the ADK ProductionAgent's ``_call_planner`` method which
    invoked the planner sub-agent. In Strands, the LLM agent calls
    this tool directly.

    The tool reads visual concepts, worker URLs, and existing clips
    from state, then delegates to the ProductionOrchestrator's plan
    generation logic.

    Args:
        feedback: Previous evaluator feedback to address. When empty,
            this is the first attempt — generate a fresh plan.
        tool_context: Framework-injected context.

    Returns:
        Dict with ``plan`` (ProductionPlan JSON), ``attempt`` (int),
        and ``is_refinement`` (bool).
    """
    state = tool_context.agent.state if tool_context else None

    # Read visual concepts from OTIO — not from agent state
    concepts: list[dict[str, Any]] = []
    if _otio_manager is not None:
        concepts = _otio_manager.get_pipeline_metadata("visual_concepts", []) or []
    else:
        from tools.otio_metadata import read_pipeline_metadata
        tp = _resolve_timeline_path()
        concepts = read_pipeline_metadata(tp, "visual_concepts", []) or []

    # Derive visual concepts from scenes if none were explicitly set
    if not concepts:
        scenes = _read_scenes()
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            scene_num = scene.get("scene_num")
            if scene_num is None:
                sid = scene.get("scene_id", "")
                if isinstance(sid, str) and sid.startswith("S"):
                    try:
                        scene_num = int(sid[1:])
                    except ValueError:
                        scene_num = 0
                else:
                    scene_num = 0
            prompt = (
                scene.get("visual_prompt", "")
                or scene.get("prompt", "")
                or scene.get("visual_notes", "")
                or scene.get("description", "")
            )
            if prompt:
                concepts.append({
                    "scene_num": scene_num,
                    "phrase_idx": 0,
                    "prompt": prompt,
                    "negative_prompt": scene.get("negative_prompt", ""),
                    "duration": min(float(scene.get("duration_sec", scene.get("duration", 5.0))), 10.0),
                    "lora_id": scene.get("lora_id", "documentary-realism"),
                    "lora_weight": float(scene.get("lora_weight", 0.75)),
                })

    if not concepts:
        return {
            "plan": None,
            "attempt": 1,
            "is_refinement": False,
            "error": "No visual concepts found on state and could not derive from scenes",
        }

    existing_clips = _read_state_dict(state, "existing_clips")

    # With lazy provisioning, workers are provisioned on-demand by the
    # provisioner agent — there is no pre-known worker pool.  Batch size
    # is set to a reasonable default (2 parallel queue jobs).
    num_workers = 2

    # Build plan structure
    batches = _build_batches(concepts, num_workers, existing_clips)
    total_clips = sum(len(b.get("tasks", [])) for b in batches)
    estimated_gpu_minutes = total_clips * 0.5  # rough estimate

    plan = {
        "batches": batches,
        "is_complete": True,
        "strategy": "style-coherent batching" if not feedback else "refined plan addressing feedback",
        "worker_assignment": {},
        "estimated_gpu_minutes": estimated_gpu_minutes,
        "risk_assessment": "Standard GPU execution risk",
    }

    logger.info(
        "batches=<%d>, total_clips=<%d>, is_refinement=<%s> | "
        "production plan generated",
        len(batches),
        total_clips,
        bool(feedback),
    )
    return {
        "plan": plan,
        "attempt": 1,
        "is_refinement": bool(feedback),
    }


def _get_video_duration(path: str) -> float | None:
    """Return video duration in seconds via ffprobe, or None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def _build_batches(
    concepts: list[dict[str, Any]],
    num_workers: int,
    existing_clips: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build clip batches from visual concepts.

    Groups concepts into batches of size ``num_workers``, skipping
    clips that already exist with good QA.
    """
    batch_size = max(1, num_workers)
    tasks: list[dict[str, Any]] = []

    for concept in concepts:
        scene_num = concept.get("scene_num", 0)
        phrase_idx = concept.get("phrase_idx", 0)
        clip_id = f"s{int(scene_num):03d}_p{int(phrase_idx):03d}"

        # Skip already-generated clips
        if clip_id in existing_clips:
            duration = float(concept.get("duration", 5.0))
            existing_entry = existing_clips[clip_id]
            existing_path: str | None = None
            if isinstance(existing_entry, str) and os.path.isfile(existing_entry):
                existing_path = existing_entry
            elif isinstance(existing_entry, dict):
                for key in ("path", "file_path", "output_path", "video_path"):
                    candidate = existing_entry.get(key)
                    if isinstance(candidate, str) and os.path.isfile(candidate):
                        existing_path = candidate
                        break
            if existing_path:
                actual_duration = _get_video_duration(existing_path)
                if actual_duration is not None:
                    duration = actual_duration

            tasks.append({
                "clip_id": clip_id,
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
                "prompt": concept.get("prompt", ""),
                "negative_prompt": concept.get("negative_prompt", ""),
                "duration": duration,
                "lora_id": concept.get("lora_id", "documentary-realism"),
                "lora_weight": concept.get("lora_weight", 0.75),
                "assigned_worker": "auto",
                "priority": 0,
                "style_group": "default",
                "skip": True,
            })
            continue

        tasks.append({
            "clip_id": clip_id,
            "scene_num": scene_num,
            "phrase_idx": phrase_idx,
            "prompt": concept.get("prompt", ""),
            "negative_prompt": concept.get("negative_prompt", ""),
            "duration": min(float(concept.get("duration", 5.0)), 10.0),
            "lora_id": concept.get("lora_id", "documentary-realism"),
            "lora_weight": concept.get("lora_weight", 0.75),
            "assigned_worker": "auto",
            "priority": 0,
            "style_group": concept.get("lora_id", "default"),
            "skip": False,
        })

    # Split into batches
    batches: list[dict[str, Any]] = []
    for i in range(0, len(tasks), batch_size):
        batch_tasks = tasks[i:i + batch_size]
        batches.append({
            "description": f"Batch {len(batches) + 1}: clips {i+1}-{i+len(batch_tasks)}",
            "tasks": batch_tasks,
            "rationale": "Sequential batch for parallel execution",
        })

    return batches


# ---------------------------------------------------------------------------/
# Tools — Plan evaluation
# ---------------------------------------------------------------------------/


@tool
def evaluate_production_plan(
    plan_json: str,
    num_concepts: int = 0,
    num_workers: int = 1,
) -> dict[str, Any]:
    """Evaluate a production plan's quality.

    Replaces the ADK ProductionAgent's ``_call_evaluator`` method
    which invoked the evaluator sub-agent.

    Args:
        plan_json: JSON string of the ProductionPlan to evaluate.
        num_concepts: Number of visual concepts the plan should cover.
        num_workers: Number of available GPU workers.

    Returns:
        Dict with ``rating`` (0-3), ``feedback`` (str),
        ``needs_improvement`` (bool), and ``focus_areas`` (list).
    """
    try:
        plan = json.loads(plan_json)
    except (json.JSONDecodeError, TypeError):
        return {
            "rating": 0,
            "feedback": "Plan is not valid JSON",
            "needs_improvement": True,
            "focus_areas": ["plan_format"],
        }

    if not isinstance(plan, dict):
        return {
            "rating": 0,
            "feedback": "Plan is not a valid object",
            "needs_improvement": True,
            "focus_areas": ["plan_structure"],
        }

    # Structural checks
    issues: list[str] = []
    focus_areas: list[str] = []

    batches = plan.get("batches", [])
    if not isinstance(batches, list) or not batches:
        issues.append("Plan has no batches")
        focus_areas.append("completeness")

    # Check duration limits
    for batch_idx, batch in enumerate(batches if isinstance(batches, list) else []):
        tasks = batch.get("tasks", []) if isinstance(batch, dict) else []
        for task in tasks:
            if isinstance(task, dict):
                dur = task.get("duration", 0)
                if isinstance(dur, (int, float)) and dur > 10.0:
                    issues.append(
                        f"Clip {task.get('clip_id', '?')} duration {dur}s > 10.0s limit"
                    )
                    if "duration_validity" not in focus_areas:
                        focus_areas.append("duration_validity")

    # Check completeness
    total_tasks = 0
    for batch in (batches if isinstance(batches, list) else []):
        tasks = batch.get("tasks", []) if isinstance(batch, dict) else []
        total_tasks += len(tasks)
    if num_concepts > 0 and total_tasks < num_concepts:
        issues.append(
            f"Plan covers {total_tasks} clips but {num_concepts} concepts exist"
        )
        focus_areas.append("completeness")

    # Determine rating
    if not issues:
        rating = 3  # EXCELLENT
        needs_improvement = False
    elif len(issues) <= 2:
        rating = 2  # GOOD
        needs_improvement = True
    elif len(issues) <= 4:
        rating = 1  # FAIR
        needs_improvement = True
    else:
        rating = 0  # POOR
        needs_improvement = True

    if not focus_areas:
        focus_areas = ["none"]

    return {
        "rating": rating,
        "feedback": "; ".join(issues) if issues else "Plan looks good",
        "needs_improvement": needs_improvement,
        "focus_areas": focus_areas,
    }


# ---------------------------------------------------------------------------/
# Tools — Finalization
# ---------------------------------------------------------------------------/


@tool
def finalize_production(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Finalize the production stage with collected job results.

    Args:
        results: List of result dicts.

    Returns:
        Finalization summary.
    """
    reused = [r for r in results if r.get("status") == "reused"]
    submitted = [r for r in results if r.get("status") == "submitted"]

    logger.info(
        "production finalized | total=<%d> reused=<%d> submitted=<%d>",
        len(results),
        len(reused),
        len(submitted),
    )

    return {
        "status": "finalized",
        "total_clips": len(results),
        "reused_clips": len(reused),
        "submitted_clips": len(submitted),
        "results": results,
    }
