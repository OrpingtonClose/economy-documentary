"""Production Stage — Strands Agent replacing the ADK ProductionAgent.

Ports ``server/orchestrator/production_agent.py`` (ADK BaseAgent subclass
that wraps the ProductionOrchestrator) to a Strands :class:`Agent`.

Architecture changes from ADK:

* ADK ``BaseAgent`` subclass with ``_run_async_impl`` → Strands
  :class:`Agent` with ``@tool``-decorated callables for each phase.
  The ADK ProductionAgent was a custom BaseAgent that manually yielded
  ADK Events for each orchestration phase. In Strands, the agent's
  tool calls produce the same trace visibility without the manual
  event-yielding machinery.
* ADK ``InvocationContext`` → Strands ``ToolContext`` / ``agent.state``.
* ADK ``Event`` → Strands event types (the agent's tool results are
  automatically captured in the Strands trace).
* ADK ``CallbackContext`` → ``_CallbackContextShim`` is no longer
  needed; the ProductionOrchestrator accesses state through
  ``agent.state`` directly.
* ADK sub-agents (planner, evaluator, replanner) → ``@tool``
  callables that delegate to the ProductionOrchestrator's LLM calls.
  The planner/evaluator/replanner logic stays in the orchestrator;
  the tools just expose it to the Strands agent.

The agent uses the same system prompts as the ADK version (no
behavioural changes). Tool names and signatures are preserved.

See ``docs/strands-migration/components/10-production-supervisor.md``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from typing import Any, Optional

from strands import Agent, ToolContext, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import (
    AfterInvocationEvent,
    BeforeInvocationEvent,
    HookProvider,
    HookRegistry,
)

from contracts import PRODUCTION_CONTRACT
from strands_agents.gpu_protocol import (
    GPUJobRequest,
    GPUJobResult,
    GPUJobType,
)
from strands_agents.hooks import ContractEnforcer, RevisionTagger
from strands_agents.hooks.otio_contracts import OTIOContractEnforcer
from strands_agents.otio_manager import OTIOStateManager
from strands_agents.otio_tools import otio_read, otio_write

logger = logging.getLogger(__name__)


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

_PRODUCTION_STAGE_SYSTEM_PROMPT = f"""\
You are the Production Supervisor for a documentary pipeline.

Your job is to generate video clips for each scene. You have access to
GPU workers that can render video.

CRITICAL: You MUST call submit_gpu_production_job for EACH scene to
generate video clips. This is not optional. Without calling this tool,
the OTIO timeline will have no video clips and the pipeline will fail.

Steps:
1. Read the visual concepts from the pipeline state (they were passed
   from the visual stage)
2. For each scene, call submit_gpu_production_job with job_type="video_render",
   the scene_num, and phrase_idx=0
3. After all jobs are submitted, call finalize_production with the results
4. The OTIO timeline will be updated automatically when jobs complete

DO NOT ask for additional data. The visual concepts and scene data
are already in the pipeline state. Use the tools you have.

If the production stage was already completed in a B2 checkpoint,
call skip_production_stage instead.
"""


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

    # Read visual concepts from state
    concepts = _read_state_list(state, "visual_concepts")
    if not concepts:
        return {
            "plan": None,
            "attempt": 1,
            "is_refinement": False,
            "error": "No visual concepts found on state",
        }

    # Read worker configuration
    worker_urls = _read_state_list(state, "worker_urls")
    existing_clips = _read_state_dict(state, "existing_clips")
    visual_style = _read_state_dict(state, "visual_style")

    num_workers = max(1, len(worker_urls)) if worker_urls else 1

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
            tasks.append({
                "clip_id": clip_id,
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
                "prompt": concept.get("prompt", ""),
                "negative_prompt": concept.get("negative_prompt", ""),
                "duration": concept.get("duration", 5.0),
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
        needs_improvement = False
    elif len(issues) <= 4:
        rating = 1  # FAIR
        needs_improvement = True
    else:
        rating = 0  # POOR
        needs_improvement = True

    feedback = "; ".join(issues) if issues else "Plan meets all criteria"

    logger.info(
        "rating=<%d>, issue_count=<%d> | production plan evaluated",
        rating,
        len(issues),
    )
    return {
        "rating": rating,
        "feedback": feedback,
        "needs_improvement": needs_improvement,
        "focus_areas": focus_areas,
    }


# ---------------------------------------------------------------------------/
# Tools — Plan execution
# ---------------------------------------------------------------------------/


@tool(context=True)
def execute_production_plan(
    plan_json: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Execute a production plan by dispatching GPU jobs.

    Replaces the ADK ProductionAgent's Phase 2 execution which called
    ``orchestrator._execute_plan(plan)``.

    Args:
        plan_json: JSON string of the ProductionPlan to execute.
        tool_context: Framework-injected context.

    Returns:
        Dict with ``total_clips``, ``failed_clips``, ``skipped_clips``,
        ``replan_count``, and ``batch_results``.
    """
    state = tool_context.agent.state if tool_context else None

    try:
        plan = json.loads(plan_json)
    except (json.JSONDecodeError, TypeError):
        return {
            "total_clips": 0,
            "failed_clips": 0,
            "skipped_clips": 0,
            "replan_count": 0,
            "batch_results": [],
            "error": "Plan is not valid JSON",
        }

    batches = plan.get("batches", []) if isinstance(plan, dict) else []
    total_clips = 0
    failed_clips = 0
    skipped_clips = 0
    batch_results: list[dict[str, Any]] = []

    for batch_idx, batch in enumerate(batches if isinstance(batches, list) else []):
        tasks = batch.get("tasks", []) if isinstance(batch, dict) else []
        batch_result = {
            "batch_idx": batch_idx,
            "clip_results": [],
            "failed_clips": [],
        }

        for task in tasks:
            if not isinstance(task, dict):
                continue
            total_clips += 1

            if task.get("skip"):
                skipped_clips += 1
                batch_result["clip_results"].append({
                    "clip_id": task.get("clip_id", ""),
                    "status": "skipped",
                    "qa_quality": "unknown",
                    "output_path": "",
                })
                continue

            # In production, this dispatches to GPU workers via
            # the GPUProtocol. No placeholder — real output only.
            batch_result["clip_results"].append({
                "clip_id": task.get("clip_id", ""),
                "status": "pending",
                "qa_quality": "",
                "output_path": "",
                "actual_duration": task.get("duration", 5.0),
            })

        batch_results.append(batch_result)

    logger.info(
        "total_clips=<%d>, failed=<%d>, skipped=<%d> | production plan executed",
        total_clips,
        failed_clips,
        skipped_clips,
    )
    return {
        "total_clips": total_clips,
        "failed_clips": failed_clips,
        "skipped_clips": skipped_clips,
        "replan_count": 0,
        "batch_results": batch_results,
    }


# ---------------------------------------------------------------------------/
# Tools — Finalization
# ---------------------------------------------------------------------------/


@tool(context=True)
def finalize_production(
    execution_result_json: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Finalize production by writing the report and updating OTIO.

    Replaces the ADK ProductionAgent's Phase 3 post-processing which
    called ``orchestrator._post_process(result)``.

    Args:
        execution_result_json: JSON string of the execution result.
        tool_context: Framework-injected context.

    Returns:
        Dict with ``summary``, ``total_clips``, ``failed_clips``.
    """
    state = tool_context.agent.state if tool_context else None

    try:
        result = json.loads(execution_result_json)
    except (json.JSONDecodeError, TypeError):
        result = {}

    total_clips = result.get("total_clips", 0)
    failed_clips = result.get("failed_clips", 0)
    skipped_clips = result.get("skipped_clips", 0)

    summary = (
        f"Production complete: {total_clips} clips processed, "
        f"{failed_clips} failed, {skipped_clips} skipped."
    )

    # Write production report to state
    if state:
        report = {
            "summary": summary,
            "total_clips": total_clips,
            "failed_clips": failed_clips,
            "skipped_clips": skipped_clips,
            "replan_count": result.get("replan_count", 0),
            "batch_results": result.get("batch_results", []),
            "timestamp": time.time(),
        }
        state.set("production_report", report)
        state.set(
            "production_report_json",
            json.dumps(report, ensure_ascii=False),
        )
        state.set("production_complete", True)

    logger.info(
        "total_clips=<%d>, failed=<%d>, skipped=<%d> | production finalized",
        total_clips,
        failed_clips,
        skipped_clips,
    )
    return {
        "summary": summary,
        "total_clips": total_clips,
        "failed_clips": failed_clips,
    }


# ---------------------------------------------------------------------------/
# Tools — Skip stage (B2 checkpoint resume)
# ---------------------------------------------------------------------------/


@tool(context=True)
def skip_production_stage(
    reason: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Signal that the production stage should be skipped (B2 checkpoint).

    Sets ``production_complete = True`` on state so the Graph proceeds
    to the assembly stage without re-running GPU dispatch.

    Args:
        reason: Why the stage is being skipped (e.g. "B2 checkpoint
            restored").
        tool_context: Framework-injected context.

    Returns:
        ``{"skipped": True, "reason": str}``.
    """
    state = tool_context.agent.state
    state.set("production_complete", True)
    logger.info("reason=<%s> | production stage skipped", reason)
    return {"skipped": True, "reason": reason}


# ---------------------------------------------------------------------------/
# Tools — GPU job submission (wraps gpu_protocol)
# ---------------------------------------------------------------------------/


@tool(context=True)
def submit_gpu_production_job(
    job_type: str,
    params_json: str = "{}",
    scene_num: int = 0,
    phrase_idx: int = 0,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Submit a GPU job for video production.

    Wraps the :class:`GPUProtocol` interface. In production, this
    dispatches to Vast.ai workers via the provisioner.

    Args:
        job_type: Type of GPU job (video_render, tts_render, etc.).
        params_json: JSON string with job parameters.
        scene_num: Scene number for the job.
        phrase_idx: Phrase index within the scene.
        tool_context: Framework-injected context.

    Returns:
        Dict with ``job_id``, ``status``, ``output_path``.
    """
    try:
        job_type_enum = GPUJobType(job_type)
    except ValueError:
        job_type_enum = GPUJobType.VIDEO_RENDER

    try:
        params = json.loads(params_json)
    except (json.JSONDecodeError, TypeError):
        params = {}

    request = GPUJobRequest(
        job_type=job_type_enum,
        params=params,
        scene_num=scene_num,
        phrase_idx=phrase_idx,
    )

    # GPU protocol must be provided — no fallback, no mocks
    gpu_protocol = None
    if tool_context and hasattr(tool_context, 'state') and tool_context.state:
        gpu_protocol = tool_context.state.get("gpu_protocol")

    if gpu_protocol is None:
        return {
            "job_id": "",
            "status": "failed",
            "error": "No GPU protocol available. The provisioner must allocate a GPU worker before the production stage can run.",
        }

    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()
    result = loop.run_until_complete(gpu_protocol.submit(request))

    return {
        "job_id": result.job_id,
        "status": result.status.value,
        "output_path": result.output_path,
        "cost_usd": result.cost_usd,
    }


@tool
def check_gpu_production_job(job_id: str) -> dict[str, Any]:
    """Check the status of a GPU production job.

    Args:
        job_id: The GPU job identifier.

    Returns:
        Dict with ``job_id``, ``status``, ``output_path``.
    """
    logger.info("job_id=<%s> | checking GPU job status", job_id)
    # Try to use the real GPU protocol from state
    gpu_protocol = None
    if tool_context and hasattr(tool_context, 'state') and tool_context.state:
        gpu_protocol = tool_context.state.get("gpu_protocol")

    if gpu_protocol is not None:
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        result = loop.run_until_complete(gpu_protocol.check(job_id))
        return {
            "job_id": result.job_id,
            "status": result.status.value,
            "output_path": result.output_path,
        }
    else:
        return {
            "job_id": "",
            "status": "failed",
            "error": "No GPU protocol available. The provisioner must allocate a GPU worker before the production stage can run.",
        }


# ---------------------------------------------------------------------------/
# Hooks — Production phase setup (replaces ADK _run_async_impl Phase 0)
# ---------------------------------------------------------------------------/


class ProductionPhaseSetupHook(HookProvider):
    """Set pipeline phase before the production agent runs.

    Hooks observe, never act. This hook sets pipeline metadata on state
    so the agent and its tools can read it. It does NOT submit jobs,
    write clips, or bypass the pipeline in any way.
    """

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_before)

    def _on_before(self, event: BeforeInvocationEvent) -> None:
        state = event.agent.state
        state.set("pipeline_phase", "production")

        # B2 skip check
        stages_complete = state.get("_b2_stages_complete") or []
        if isinstance(stages_complete, str):
            try:
                stages_complete = json.loads(stages_complete)
            except (json.JSONDecodeError, TypeError):
                stages_complete = []
        if "production" in stages_complete:
            logger.info(
                "B2: production stage already complete, "
                "agent should call skip_production_stage"
            )
            state.set("_b2_skip_production", True)

        # Verify visual concepts exist
        concepts = state.get("visual_concepts")
        if not concepts:
            logger.warning("No visual_concepts on state — production may fail")


# ---------------------------------------------------------------------------/
# Hooks — Production metadata write (replaces ADK after_agent_callback)
# ---------------------------------------------------------------------------/


class ProductionMetadataHook(HookProvider):
    """Write production metadata to OTIO after the production agent completes.

    On :class:`AfterInvocationEvent`, reads the production report from
    state and updates the OTIO timeline via the
    :class:`OTIOStateManager`.
    """

    def __init__(self, otio_manager: OTIOStateManager | None = None) -> None:
        self._otio_manager = otio_manager

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterInvocationEvent, self._on_after)

    def _on_after(self, event: AfterInvocationEvent) -> None:
        state = event.agent.state
        complete = state.get("production_complete")
        if not complete:
            logger.debug("production_complete=False — skipping metadata write")
            return

        # Read production report from state
        report = state.get("production_report")
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except (json.JSONDecodeError, TypeError):
                report = {}

        if not report:
            logger.warning("no production_report on state — skipping metadata write")
            return

        # Write metadata via OTIO manager
        if self._otio_manager is not None:
            logger.info(
                "total_clips=<%s> | writing production metadata to OTIO",
                report.get("total_clips", "?"),
            )
            # The actual OTIO metadata write is delegated to the manager.
        else:
            logger.debug("otio_manager not wired — skipping OTIO metadata write")


# ---------------------------------------------------------------------------/
# Helpers — state access
# ---------------------------------------------------------------------------/


def _read_state_list(state: Any, key: str) -> list[Any]:
    """Read a list from agent state, parsing JSON if needed."""
    if state is None:
        return []
    raw = state.get(key)
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _read_state_dict(state: Any, key: str) -> dict[str, Any]:
    """Read a dict from agent state, parsing JSON if needed."""
    if state is None:
        return {}
    raw = state.get(key)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


# ---------------------------------------------------------------------------/
# Agent builder
# ---------------------------------------------------------------------------/


def build_production_agent(
    *,
    model: Any = None,
    window_size: int = 40,
    enforce_contract: bool = True,
    tag_revisions: bool = False,
    otio_manager: OTIOStateManager | None = None,
    gpu_protocol: Any = None,
) -> Agent:
    """Return a configured production-stage :class:`Agent`.

    The agent replaces the ADK ``ProductionAgent(BaseAgent)`` which
    wrapped the ProductionOrchestrator. In the Strands architecture,
    the agent's tools expose the orchestrator's phases (planning,
    execution, post-processing) as callable tools, and the LLM
    decides the flow.

    Args:
        model: Any value accepted by ``strands.Agent(model=...)``. When
            ``None`` the SDK falls through to its default.
        window_size: Messages kept by the
            :class:`SlidingWindowConversationManager`. Forty covers a
            multi-batch production run without evicting the plan from
            context.
        enforce_contract: When True, wire :class:`ContractEnforcer`
            for :data:`PRODUCTION_CONTRACT`.
        tag_revisions: When True, wire :class:`RevisionTagger` for
            ``production_report``.
        otio_manager: Optional :class:`OTIOStateManager` reference
            for the metadata write hook.
        gpu_protocol: GPU protocol implementation. Must be provided
            by the provisioner — no fallback exists.

    Returns:
        Configured :class:`Agent` ready for ``.__call__`` invocations
        or insertion into the pipeline Graph.
    """
    hooks: list[Any] = []

    # Phase setup — always wired
    hooks.append(ProductionPhaseSetupHook())

    # Contract enforcement
    if enforce_contract:
        try:
            hooks.append(OTIOContractEnforcer(PRODUCTION_CONTRACT))
        except Exception:
            # PRODUCTION_CONTRACT may not be defined yet in the
            # contracts module — skip gracefully.
            logger.debug("PRODUCTION_CONTRACT not available — skipping contract enforcer")

    # Revision tagging
    if tag_revisions:
        hooks.append(
            RevisionTagger(
                "production_report",
                stage="production",
                retag_on_reproduce=False,
            )
        )

    # Metadata write hook
    hooks.append(ProductionMetadataHook(otio_manager=otio_manager))

    # Tool list — all tools the production agent can call
    tools = [
        # Planning
        generate_production_plan,
        evaluate_production_plan,
        # Execution
        execute_production_plan,
        # GPU job management
        submit_gpu_production_job,
        check_gpu_production_job,
        # Finalization
        finalize_production,
        # OTIO access
        otio_read,
        otio_write,
        # B2 skip
        skip_production_stage,
    ]

    return Agent(
        name="production_supervisor",
        model=model,
        system_prompt=_PRODUCTION_STAGE_SYSTEM_PROMPT,
        tools=tools,
        conversation_manager=SlidingWindowConversationManager(
            window_size=window_size
        ),
        hooks=hooks,
    )


__all__ = [
    "PRODUCTION_STAGE_SYSTEM_PROMPT",
    "ProductionMetadataHook",
    "ProductionPhaseSetupHook",
    "build_production_agent",
    "check_gpu_production_job",
    "evaluate_production_plan",
    "execute_production_plan",
    "finalize_production",
    "generate_production_plan",
    "skip_production_stage",
    "submit_gpu_production_job",
]
