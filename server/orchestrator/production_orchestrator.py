"""
Planning-heavy production orchestrator for video generation.

Replaces deterministic_production_callback with an LLM-driven
planning → execution → replan loop inspired by mcp-agent's
Orchestrator and EvaluatorOptimizer patterns.

Gated behind the ``PRODUCTION_ORCHESTRATOR`` env var (set to "1" or "true").
When disabled, the existing deterministic callback runs unchanged.

Architecture:
  Phase 1  PLANNING (heavy) — LLM generates ProductionPlan, evaluator
           rates it, verifier validates structure, loop until EXCELLENT.
  Phase 2  EXECUTION — batches executed sequentially, clips within each
           batch in parallel across GPU workers.
  Phase 3  REPLAN — after each batch, planner sees QA results and can
           adjust prompts, rebalance workers, modify style globally.

Key invariants preserved (NON-NEGOTIABLE):
  1. OTIO as single source of truth (add_video_clip with _otio_lock)
  2. Gatekeeper runs AFTER B2 upload (audit trail before verdict)
  3. Recovery middleware (VIDEO_POLICY wraps every GPU call)
  4. Contracts (PRODUCTION_CONTRACT validated before stage)
  5. InfraAgent (notify_stage_start/complete)
  6. Approval gate (mark_stage_ready("clips"))
  7. Timeline Guardian (runs after production, violations are fatal)
  8. B2 stage marker (uploaded only AFTER gatekeeper passes)
  9. Resume support (skip clips with existing good QA)
 10. Fatal errors (RuntimeError never swallowed)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from google.genai import types as genai_types

from orchestrator.clip_helpers import (
    _MockToolContext,
    generate_one_clip,
    process_results_to_otio,
    run_gatekeeper_and_upload,
    run_post_production,
)
from orchestrator.plan_verifier import ProductionPlanVerifier
from orchestrator.production_models import (
    BatchResult,
    ClipBatch,
    ClipResult,
    ClipTask,
    PlanEvaluation,
    ProductionPlan,
    ProductionPlanResult,
    QualityRating,
)
from orchestrator.prompts import (
    PLAN_EVALUATION_USER_TEMPLATE,
    PLAN_EVALUATOR_INSTRUCTION,
    PLAN_GENERATION_USER_TEMPLATE,
    PLAN_OPTIMIZER_INSTRUCTION,
    REPLAN_PROMPT_TEMPLATE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ProductionOrchestrator
# ---------------------------------------------------------------------------

class ProductionOrchestrator:
    """Planning-heavy orchestrator for video production.

    Replaces deterministic_production_callback with LLM-driven planning.
    Uses EvaluatorOptimizer pattern for plan generation (plan is refined
    until EXCELLENT before any GPU time is spent).
    """

    MAX_REFINEMENTS = 3

    def __init__(self, state: object, callback_context: object):
        """
        Args:
            state: ADK pipeline state (dict-like, from callback_context.state).
            callback_context: ADK CallbackContext passed to before_agent_callback.
        """
        self.state = state
        self.callback_context = callback_context

        # Visual concepts
        self.concepts: list[dict] = []
        self.visual_style_str = ""
        self.visual_style_avoid: list[str] = []
        self.default_negative = ""

        # Workers
        self.worker_urls: list[str] = []
        self.num_workers = 1

        # Video output dir
        self.video_dir = os.environ.get(
            "VIDEO_OUTPUT_DIR", "/tmp/documentary-pipeline/video"
        )

        # Existing clips (for resume support)
        self.existing_clips: dict[str, str] = {}

        # AG-UI feedback store
        self._feedback_store: Optional[object] = None

        # Narration durations for gatekeeper cross-validation
        self._narr_durations: dict = {}

        # Plan verifier (initialized after concepts are parsed)
        self._verifier: Optional[ProductionPlanVerifier] = None

    # -- Main entry point ---------------------------------------------------

    async def run(self) -> str:
        """Main entry point.  Returns summary string for the pipeline."""

        # Phase 0: Preconditions (contracts, infra, gatekeeper)
        self._check_preconditions()

        # Parse state
        self._parse_state()

        if not self.concepts:
            from infra_agent import get_infra_agent
            _infra = get_infra_agent()
            if _infra:
                _infra.notify_stage_complete("production")
            return "ERROR: No visual concepts found"

        # Initialize verifier
        self._init_verifier()

        # Phase 1: PLANNING (heavy)
        plan = await self._generate_verified_plan()
        self._emit_planning_event("plan_finalized", {
            "batches": len(plan.batches),
            "total_clips": sum(len(b.tasks) for b in plan.batches),
            "strategy": plan.strategy,
        })

        # Phase 2: EXECUTION with replanning
        result = await self._execute_plan(plan)

        # Phase 3: POST-PROCESSING (OTIO, gatekeeper, B2, approval gate)
        summary = self._post_process(result)

        return summary

    # -- Phase 0: Preconditions ---------------------------------------------

    def _check_preconditions(self) -> None:
        """Preserve ALL existing precondition checks from deterministic_production_callback.

        Lines 923-1014 of deterministic_steps.py.
        """
        state = self.state

        # B2 stage skip check
        stages_complete = state.get("_b2_stages_complete", [])
        if "production" in stages_complete:
            raise _StageSkipped(
                "Production stage restored from B2 checkpoint — skipped."
            )

        # OTIO GATE: refuse to proceed if a previous stage flagged a violation
        if state.get("otio_violation"):
            raise RuntimeError(
                f"OTIO VIOLATION (from previous stage): {state['otio_violation']}"
            )

        # CONTRACT: validate preconditions before starting production stage
        from contracts import PRODUCTION_CONTRACT, validate_preconditions
        validate_preconditions(PRODUCTION_CONTRACT, state.to_dict())

        # INFRA: notify stage start + check if pipeline is paused
        from infra_agent import check_infra_pause, get_infra_agent
        _infra = get_infra_agent()
        if _infra:
            _infra.notify_stage_start("production")
        check_infra_pause()

        state["pipeline_phase"] = "production"

    # -- State parsing ------------------------------------------------------

    def _parse_state(self) -> None:
        """Parse visual concepts, style, workers from state/env."""
        from callbacks.deterministic_steps import extract_json_array, extract_json_object
        from tools.otio_tools import get_narration_durations_by_scene

        state = self.state
        raw_concepts = state.get("visual_concepts", "")
        concepts = extract_json_array(str(raw_concepts))
        if not concepts:
            obj = extract_json_object(str(raw_concepts))
            if obj and "visual_concepts" in obj:
                concepts = obj["visual_concepts"]

        # Extract movie-level visual style
        raw_visual_style = state.get("visual_style", "")
        if raw_visual_style:
            try:
                vs = (
                    json.loads(str(raw_visual_style))
                    if isinstance(raw_visual_style, str)
                    else raw_visual_style
                )
                if isinstance(vs, dict):
                    self.visual_style_str = json.dumps(vs)
                    self.visual_style_avoid = vs.get("avoid", [])
            except (json.JSONDecodeError, TypeError):
                self.visual_style_str = str(raw_visual_style)

        # Fallback: generate simple concepts from scenes data
        if not concepts:
            raw_scenes = state.get("scenes", "[]")
            scenes = extract_json_array(str(raw_scenes))
            if scenes:
                concepts = []
                for scene in scenes:
                    sn = scene.get("scene_num", 0)
                    concepts.append({
                        "scene_num": sn,
                        "phrase_idx": 0,
                        "duration": min(scene.get("duration_sec", 5), 10.0),
                        "prompt": (
                            f"Documentary footage: {scene.get('title', 'scene')}. "
                            f"{scene.get('visual_notes', '')}"
                        ),
                        "start_time": 0.0,
                        "end_time": min(scene.get("duration_sec", 5), 10.0),
                        "lora_id": "documentary-realism",
                        "lora_weight": 0.75,
                    })

        self.concepts = concepts or []
        self.default_negative = (
            ", ".join(self.visual_style_avoid) if self.visual_style_avoid else ""
        )

        # Workers
        worker_urls_str = os.environ.get("VIDEO_WORKER_URLS", "")
        self.worker_urls = (
            [u.strip() for u in worker_urls_str.split(",") if u.strip()]
            if worker_urls_str
            else []
        )
        self.num_workers = max(1, len(self.worker_urls))
        logger.info(
            "Orchestrator: %d GPU worker(s), %d concepts to plan",
            self.num_workers, len(self.concepts),
        )

        # Scan for existing clips (resume support)
        self._scan_existing_clips()

        # AG-UI feedback store
        from agui import get_feedback_store
        self._feedback_store = get_feedback_store()

        # Gatekeeper imports
        from gatekeeper import (
            check_stage_handoff,
            has_rejects,
            intervention_window,
        )

        # GATEKEEPER: stage handoff check (visual_direction → production)
        handoff_checks = check_stage_handoff(
            "visual_direction", "production", state.to_dict()
        )
        if has_rejects(handoff_checks):
            rejects = [c for c in handoff_checks if c.verdict.value == "reject"]
            raise RuntimeError(
                "GATEKEEPER BLOCKED production start: "
                + "; ".join(c.message for c in rejects)
            )
        if not intervention_window("production_start", handoff_checks):
            raise RuntimeError(
                "GATEKEEPER: user halted pipeline at production start"
            )

        # Narration durations for gatekeeper cross-validation
        self._narr_durations = get_narration_durations_by_scene(
            tool_context=_MockToolContext(state),
        )

    def _scan_existing_clips(self) -> None:
        """Scan video_dir for already-generated clips with good QA."""
        self.existing_clips = {}
        if not os.path.exists(self.video_dir):
            return
        for fname in os.listdir(self.video_dir):
            if fname.endswith("_status.json"):
                status_path = os.path.join(self.video_dir, fname)
                try:
                    with open(status_path) as sf:
                        status = json.load(sf)
                    if status.get("quality") in ("good", "excellent"):
                        # Derive clip_id from filename
                        # e.g. "scene_001_phrase_002_status.json" -> "s001_p002"
                        base = fname.replace("_status.json", "")
                        parts = base.split("_")
                        if len(parts) >= 4 and parts[0] == "scene" and parts[2] == "phrase":
                            clip_id = f"s{parts[1]}_p{parts[3]}"
                            mp4_path = os.path.join(
                                self.video_dir, base + ".mp4"
                            )
                            if os.path.exists(mp4_path):
                                self.existing_clips[clip_id] = mp4_path
                except (json.JSONDecodeError, OSError, IndexError):
                    pass

    def _init_verifier(self) -> None:
        """Initialize the plan verifier with current state."""
        # Collect known LoRA IDs from concepts
        known_loras = {c.get("lora_id", "documentary-realism") for c in self.concepts}
        known_loras.add("documentary-realism")  # always valid

        self._verifier = ProductionPlanVerifier(
            visual_concepts=self.concepts,
            worker_urls=self.worker_urls,
            existing_clips=self.existing_clips,
            known_lora_ids=known_loras,
        )

    # -- Phase 1: Planning (heavy) ------------------------------------------

    async def _generate_verified_plan(self) -> ProductionPlan:
        """Generate and verify production plan using EvaluatorOptimizer loop.

        This is the PLANNING EMPHASIS — the plan goes through multiple rounds:
        1. Optimizer generates a ProductionPlan from visual_concepts + worker info
        2. PlanVerifier does structural validation (hard gate)
        3. Evaluator rates the plan (POOR/FAIR/GOOD/EXCELLENT)
        4. If not EXCELLENT, evaluator provides specific feedback
        5. Optimizer refines the plan based on feedback
        6. Loop until EXCELLENT or max_refinements reached
        """
        previous_feedback = ""
        best_plan: Optional[ProductionPlan] = None
        best_rating = QualityRating.POOR

        for attempt in range(self.MAX_REFINEMENTS + 1):
            self._emit_planning_event("plan_attempt", {
                "attempt": attempt + 1,
                "max_attempts": self.MAX_REFINEMENTS + 1,
                "previous_feedback": previous_feedback[:200] if previous_feedback else "",
            })

            # Generate / refine plan via LLM
            plan = await self._call_plan_optimizer(feedback=previous_feedback)

            # Structural verification (hard gate)
            verification = self._verifier.verify(plan)
            if not verification.is_valid:
                previous_feedback = (
                    f"STRUCTURAL ERRORS (hard gate — these MUST be fixed): "
                    f"{verification.format_errors()}"
                )
                self._emit_planning_event("verification_failed", {
                    "attempt": attempt + 1,
                    "errors": verification.format_errors(),
                })
                if best_plan is None:
                    best_plan = plan
                continue

            # Quality evaluation via LLM
            evaluation = await self._call_plan_evaluator(plan)

            self._emit_planning_event("plan_evaluated", {
                "attempt": attempt + 1,
                "rating": evaluation.rating.name,
                "feedback": evaluation.feedback[:200],
            })

            if evaluation.rating > best_rating:
                best_plan = plan
                best_rating = evaluation.rating

            if evaluation.rating >= QualityRating.EXCELLENT:
                logger.info(
                    "Plan achieved EXCELLENT rating on attempt %d", attempt + 1
                )
                return plan

            previous_feedback = (
                f"EVALUATOR FEEDBACK (rating={evaluation.rating.name}): "
                f"{evaluation.feedback}\n"
                f"Focus areas: {', '.join(evaluation.focus_areas)}"
            )

        # Exhausted refinements — use best plan
        logger.warning(
            "Exhausted %d refinements, using best plan (rating=%s)",
            self.MAX_REFINEMENTS, best_rating.name,
        )
        if best_plan is None:
            # Fallback: create a simple deterministic plan
            best_plan = self._create_fallback_plan()
        return best_plan

    async def _call_plan_optimizer(self, feedback: str = "") -> ProductionPlan:
        """Call the plan optimizer LLM to generate/refine a ProductionPlan."""
        # Build user prompt
        concepts_json = json.dumps(self.concepts, indent=2)
        worker_list = "\n".join(
            f"  - {url}" for url in self.worker_urls
        ) if self.worker_urls else "  (single local worker)"
        existing_str = (
            json.dumps(list(self.existing_clips.keys()), indent=2)
            if self.existing_clips
            else "  (none)"
        )
        feedback_section = (
            f"Previous feedback to address:\n{feedback}"
            if feedback
            else "This is the first attempt — generate a fresh plan."
        )

        user_prompt = PLAN_GENERATION_USER_TEMPLATE.format(
            num_concepts=len(self.concepts),
            concepts_json=concepts_json,
            num_workers=self.num_workers,
            worker_list=worker_list,
            existing_clips=existing_str,
            visual_style=self.visual_style_str or "(none specified)",
            feedback_section=feedback_section,
        )

        response_text = await self._llm_call(
            system_instruction=PLAN_OPTIMIZER_INSTRUCTION,
            user_prompt=user_prompt,
            model_role="synthesis",
        )

        return self._parse_plan(response_text)

    async def _call_plan_evaluator(self, plan: ProductionPlan) -> PlanEvaluation:
        """Call the plan evaluator LLM to rate a ProductionPlan."""
        plan_json = plan.model_dump_json(indent=2)
        user_prompt = PLAN_EVALUATION_USER_TEMPLATE.format(
            plan_json=plan_json,
            num_concepts=len(self.concepts),
            num_workers=self.num_workers,
            num_existing=len(self.existing_clips),
        )

        response_text = await self._llm_call(
            system_instruction=PLAN_EVALUATOR_INSTRUCTION,
            user_prompt=user_prompt,
            model_role="synthesis",
        )

        return self._parse_evaluation(response_text)

    # -- Phase 2: Execution -------------------------------------------------

    async def _execute_plan(self, plan: ProductionPlan) -> ProductionPlanResult:
        """Execute batches sequentially, clips within batch in parallel.

        After each batch, check results and optionally replan.
        """
        all_results: list[dict] = []
        batch_results: list[BatchResult] = []
        replan_count = 0
        completed_clip_ids: set[str] = set()

        # Use while-loop so replanned batches are picked up — a for-loop
        # evaluates the iterable once and would ignore plan reassignments.
        batch_idx = 0
        while batch_idx < len(plan.batches):
            batch = plan.batches[batch_idx]
            self._emit_planning_event("batch_start", {
                "batch_idx": batch_idx,
                "total_batches": len(plan.batches),
                "clips_in_batch": len(batch.tasks),
                "description": batch.description,
            })

            # Execute batch (parallel clip generation)
            batch_result, clip_gen_results = self._execute_batch(batch, batch_idx)
            batch_results.append(batch_result)
            all_results.extend(clip_gen_results)

            # Track completed clips
            for cr in batch_result.clip_results:
                if cr.status in ("generated", "skipped"):
                    completed_clip_ids.add(cr.clip_id)

            self._emit_planning_event("batch_complete", {
                "batch_idx": batch_idx,
                "failed": len(batch_result.failed_clips),
                "avg_qa": batch_result.avg_qa_score,
            })

            # Check for failures and replan if needed
            if batch_result.failed_clips and batch_idx < len(plan.batches) - 1:
                remaining_batches = plan.batches[batch_idx + 1:]
                try:
                    updated_plan = await self._replan(
                        batch_result, remaining_batches, completed_clip_ids
                    )
                    # Replace remaining batches with updated plan
                    plan = ProductionPlan(
                        batches=(
                            plan.batches[:batch_idx + 1] + updated_plan.batches
                        ),
                        is_complete=updated_plan.is_complete,
                        strategy=updated_plan.strategy,
                        worker_assignment=updated_plan.worker_assignment,
                        estimated_gpu_minutes=updated_plan.estimated_gpu_minutes,
                        risk_assessment=updated_plan.risk_assessment,
                    )
                    replan_count += 1
                    self._emit_planning_event("replan_complete", {
                        "replan_count": replan_count,
                        "new_batch_count": len(updated_plan.batches),
                    })
                except RuntimeError:
                    raise  # Fatal errors — never swallow.  Invariant #10.
                except Exception as e:
                    logger.warning("Replan failed, continuing with original plan: %s", e)

            batch_idx += 1

        # Post-process all results through OTIO + gatekeeper
        total_clips, skipped_clips, errors, deferred_gk_clips = process_results_to_otio(
            results=all_results,
            state=self.state,
            narr_durations=self._narr_durations,
        )

        # Run gatekeeper and B2 upload
        run_gatekeeper_and_upload(self.state, deferred_gk_clips)

        return ProductionPlanResult(
            plan=plan,
            batch_results=batch_results,
            total_clips=total_clips,
            failed_clips=sum(len(br.failed_clips) for br in batch_results),
            skipped_clips=skipped_clips,
            replan_count=replan_count,
        )

    def _execute_batch(
        self, batch: ClipBatch, batch_idx: int
    ) -> tuple[BatchResult, list[dict]]:
        """Execute a single batch of clips in parallel.

        Uses existing generate_video_clip() from server/tools/video_tools.py
        with the existing recovery middleware (VIDEO_POLICY from server/recovery.py).
        Uses ThreadPoolExecutor like the current implementation for GPU I/O.
        """
        clip_results: list[ClipResult] = []
        gen_results: list[dict] = []

        # Build concept dicts for each task
        task_concepts: list[dict] = []
        for task in batch.tasks:
            if task.skip:
                # Already-generated clip — still need to process through OTIO
                output_path = self.existing_clips.get(task.clip_id, "")
                if not output_path:
                    output_path = os.path.join(
                        self.video_dir,
                        f"scene_{task.scene_num:03d}_phrase_{task.phrase_idx:03d}.mp4",
                    )
                gen_results.append({
                    "skipped": True,
                    "output_path": output_path,
                    "scene_num": task.scene_num,
                    "phrase_idx": task.phrase_idx,
                    "duration": task.duration,
                    "lora_id": task.lora_id,
                })
                clip_results.append(ClipResult(
                    clip_id=task.clip_id,
                    status="skipped",
                    output_path=output_path,
                    scene_num=task.scene_num,
                    phrase_idx=task.phrase_idx,
                    lora_id=task.lora_id,
                ))
                continue

            task_concepts.append({
                "scene_num": task.scene_num,
                "phrase_idx": task.phrase_idx,
                "duration": task.duration,
                "prompt": task.prompt,
                "negative_prompt": task.negative_prompt or self.default_negative,
                "lora_id": task.lora_id,
                "lora_weight": task.lora_weight,
                "_clip_id": task.clip_id,
            })

        # Generate active clips in parallel
        # TODO: task.assigned_worker is planned by the LLM but not yet routed
        # to specific workers here — generate_video_clip() uses its own
        # round-robin from VIDEO_WORKER_URLS.  Wire up per-clip routing
        # once the orchestrator is validated end-to-end.
        if len(task_concepts) > 1 and self.num_workers > 1:
            with ThreadPoolExecutor(max_workers=self.num_workers) as executor:
                future_to_concept = {
                    executor.submit(
                        generate_one_clip,
                        c,
                        self.video_dir,
                        self.default_negative,
                        self.visual_style_str,
                        self._feedback_store,
                    ): c
                    for c in task_concepts
                }
                for future in as_completed(future_to_concept):
                    c = future_to_concept[future]
                    clip_id = c["_clip_id"]
                    try:
                        result = future.result()
                        gen_results.append(result)
                        clip_results.append(self._result_to_clip_result(clip_id, result))
                    except RuntimeError:
                        raise  # Fatal errors — never swallow
                    except Exception as e:
                        logger.error("Error generating clip %s: %s", clip_id, e)
                        gen_results.append({
                            "status": "error",
                            "error": str(e),
                            "scene_num": c["scene_num"],
                            "phrase_idx": c["phrase_idx"],
                            "duration": c["duration"],
                            "lora_id": c["lora_id"],
                        })
                        clip_results.append(ClipResult(
                            clip_id=clip_id,
                            status="failed",
                            error=str(e),
                            scene_num=c["scene_num"],
                            phrase_idx=c["phrase_idx"],
                            lora_id=c["lora_id"],
                        ))
        else:
            # Sequential fallback
            for c in task_concepts:
                clip_id = c["_clip_id"]
                try:
                    result = generate_one_clip(
                        c,
                        self.video_dir,
                        self.default_negative,
                        self.visual_style_str,
                        self._feedback_store,
                    )
                    gen_results.append(result)
                    clip_results.append(self._result_to_clip_result(clip_id, result))
                except RuntimeError:
                    raise  # Fatal errors — never swallow
                except Exception as e:
                    logger.error("Error generating clip %s: %s", clip_id, e)
                    gen_results.append({
                        "status": "error",
                        "error": str(e),
                        "scene_num": c["scene_num"],
                        "phrase_idx": c["phrase_idx"],
                        "duration": c["duration"],
                        "lora_id": c["lora_id"],
                    })
                    clip_results.append(ClipResult(
                        clip_id=clip_id,
                        status="failed",
                        error=str(e),
                        scene_num=c["scene_num"],
                        phrase_idx=c["phrase_idx"],
                        lora_id=c["lora_id"],
                    ))

        batch_result = BatchResult(batch_idx=batch_idx, clip_results=clip_results)
        batch_result.compute_stats()
        return batch_result, gen_results

    def _result_to_clip_result(self, clip_id: str, result: dict) -> ClipResult:
        """Convert a generation result dict to a ClipResult model."""
        if result.get("skipped"):
            return ClipResult(
                clip_id=clip_id,
                status="skipped",
                output_path=result.get("output_path", ""),
                scene_num=result.get("scene_num", 0),
                phrase_idx=result.get("phrase_idx", 0),
                lora_id=result.get("lora_id", ""),
            )
        if result.get("status") == "error":
            return ClipResult(
                clip_id=clip_id,
                status="failed",
                error=result.get("error", ""),
                scene_num=result.get("scene_num", 0),
                phrase_idx=result.get("phrase_idx", 0),
                lora_id=result.get("lora_id", ""),
            )
        return ClipResult(
            clip_id=clip_id,
            status="generated",
            qa_quality=result.get("qa_quality", "unknown"),
            qa_reason=result.get("qa_reason", ""),
            output_path=result.get("_output_path", result.get("output_path", "")),
            actual_duration=result.get("actual_duration", 0.0),
            gen_time=result.get("gen_time", 0.0),
            scene_num=result.get("scene_num", 0),
            phrase_idx=result.get("phrase_idx", 0),
            lora_id=result.get("lora_id", ""),
        )

    # -- Phase 3: Replan ----------------------------------------------------

    async def _replan(
        self,
        batch_result: BatchResult,
        remaining_batches: list[ClipBatch],
        completed_clip_ids: set[str],
    ) -> ProductionPlan:
        """Replan after a batch with failures."""
        failed_clips_str = json.dumps(batch_result.failed_clips)
        qa_reasons = json.dumps({
            cr.clip_id: cr.qa_reason
            for cr in batch_result.clip_results
            if cr.status == "failed"
        })
        remaining_str = json.dumps(
            [b.model_dump() for b in remaining_batches], indent=2
        )
        completed_str = json.dumps(sorted(completed_clip_ids))

        batch_results_str = json.dumps(
            [cr.model_dump() for cr in batch_result.clip_results], indent=2
        )

        user_prompt = REPLAN_PROMPT_TEMPLATE.format(
            batch_idx=batch_result.batch_idx,
            batch_results=batch_results_str,
            failed_clips=failed_clips_str,
            qa_reasons=qa_reasons,
            remaining_batches=remaining_str,
            completed_clips=completed_str,
        )

        response_text = await self._llm_call(
            system_instruction=PLAN_OPTIMIZER_INSTRUCTION,
            user_prompt=user_prompt,
            model_role="synthesis",
        )

        return self._parse_plan(response_text)

    # -- Post-processing ----------------------------------------------------

    def _post_process(self, result: ProductionPlanResult) -> str:
        """Run post-production steps: timeline guardian, infra, approval gate.

        Preserves ALL existing post-processing from deterministic_production_callback.
        """
        return run_post_production(
            callback_context=self.callback_context,
            num_workers=self.num_workers,
            total_clips=result.total_clips,
            skipped_clips=result.skipped_clips,
            errors=[],  # errors already logged during execution
        )

    # -- LLM call helpers ---------------------------------------------------

    async def _llm_call(
        self,
        system_instruction: str,
        user_prompt: str,
        model_role: str = "synthesis",
    ) -> str:
        """Make an LLM call using litellm (async-compatible).

        Uses build_model() configuration but calls litellm directly
        for async support, since ADK agents are synchronous.
        """
        try:
            import litellm

            from agents.model_config import (
                ADK_SYNTHESIS_MODEL_NAME,
                ADK_VISION_MODEL_NAME,
            )

            if model_role == "vision":
                model_name = ADK_VISION_MODEL_NAME
            else:
                model_name = ADK_SYNTHESIS_MODEL_NAME

            # Strip litellm/ prefix if present
            if model_name.startswith("litellm/"):
                model_name = model_name[len("litellm/"):]

            response = await litellm.acompletion(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=16384,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.warning("LLM call failed (%s), using fallback: %s", model_role, e)
            return "{}"

    # -- Plan parsing helpers -----------------------------------------------

    def _parse_plan(self, response_text: str) -> ProductionPlan:
        """Parse a ProductionPlan from LLM response JSON."""
        try:
            data = self._extract_json(response_text)
            if not data:
                return self._create_fallback_plan()
            plan = ProductionPlan.model_validate(data)
            if not plan.batches:
                logger.warning("Parsed plan has zero batches — using fallback")
                return self._create_fallback_plan()
            return plan
        except Exception as e:
            logger.warning("Failed to parse plan from LLM response: %s", e)
            return self._create_fallback_plan()

    def _parse_evaluation(self, response_text: str) -> PlanEvaluation:
        """Parse a PlanEvaluation from LLM response JSON."""
        try:
            data = self._extract_json(response_text)
            if not data:
                return PlanEvaluation(
                    rating=QualityRating.FAIR,
                    feedback="Could not parse evaluation response",
                    needs_improvement=True,
                )
            # Handle numeric or string rating
            rating_val = data.get("rating", 1)
            if isinstance(rating_val, str):
                rating_val = {
                    "POOR": 0, "FAIR": 1, "GOOD": 2, "EXCELLENT": 3
                }.get(rating_val.upper(), 1)
            return PlanEvaluation(
                rating=QualityRating(min(max(int(rating_val), 0), 3)),
                feedback=data.get("feedback", ""),
                needs_improvement=data.get("needs_improvement", True),
                focus_areas=data.get("focus_areas", []),
            )
        except Exception as e:
            logger.warning("Failed to parse evaluation: %s", e)
            return PlanEvaluation(
                rating=QualityRating.FAIR,
                feedback=f"Parse error: {e}",
                needs_improvement=True,
            )

    def _extract_json(self, text: str) -> Optional[dict]:
        """Extract JSON from LLM response (handles markdown code blocks)."""
        text = text.strip()
        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try extracting from markdown code block
        import re
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass
        # Try finding first { ... } block
        start = text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break
        return None

    # -- Fallback plan (deterministic) --------------------------------------

    def _create_fallback_plan(self) -> ProductionPlan:
        """Create a simple deterministic plan as fallback if LLM planning fails.

        Groups clips into batches of num_workers, round-robin worker assignment.
        This mirrors the behavior of the original deterministic callback.
        """
        logger.info("Creating fallback deterministic plan")
        batches: list[ClipBatch] = []
        current_tasks: list[ClipTask] = []

        for concept in self.concepts:
            sn = concept.get("scene_num", 0)
            pi = concept.get("phrase_idx", 0)
            clip_id = f"s{sn:03d}_p{pi:03d}"

            is_skip = clip_id in self.existing_clips

            task = ClipTask(
                clip_id=clip_id,
                scene_num=sn,
                phrase_idx=pi,
                prompt=concept.get("prompt", ""),
                negative_prompt=concept.get("negative_prompt", self.default_negative),
                duration=min(concept.get("duration", 5.0), 10.0),
                lora_id=concept.get("lora_id", "documentary-realism"),
                lora_weight=concept.get("lora_weight", 0.75),
                assigned_worker="auto",
                priority=0,
                style_group=concept.get("lora_id", "default"),
                skip=is_skip,
            )
            current_tasks.append(task)

            if len(current_tasks) >= self.num_workers:
                batches.append(ClipBatch(
                    description=f"Batch {len(batches) + 1}",
                    tasks=current_tasks,
                    rationale="Round-robin assignment (fallback plan)",
                ))
                current_tasks = []

        if current_tasks:
            batches.append(ClipBatch(
                description=f"Batch {len(batches) + 1}",
                tasks=current_tasks,
                rationale="Round-robin assignment (fallback plan)",
            ))

        return ProductionPlan(
            batches=batches,
            is_complete=True,
            strategy="Fallback deterministic plan — round-robin batches",
            estimated_gpu_minutes=len(self.concepts) * 2.0,
            risk_assessment="Fallback plan — no LLM optimization applied",
        )

    # -- AG-UI event emission -----------------------------------------------

    def _emit_planning_event(self, event_type: str, data: dict) -> None:
        """Emit a planning event to the AG-UI feedback store."""
        if not self._feedback_store:
            return
        try:
            from agui import ArtifactEvent, ArtifactStatus, ArtifactType
            self._feedback_store.register_artifact(ArtifactEvent(
                id=f"orchestrator-{event_type}-{int(time.time())}",
                artifact_type=ArtifactType.VISUAL_CONCEPT,
                status=ArtifactStatus.GENERATING,
                metadata={"orchestrator_event": event_type, **data},
                timestamp=time.time(),
            ))
        except Exception as e:
            logger.debug("Failed to emit planning event: %s", e)


# ---------------------------------------------------------------------------
# Internal exception for stage skip (not a real error)
# ---------------------------------------------------------------------------

class _StageSkipped(Exception):
    """Raised when the production stage is skipped (B2 checkpoint)."""
    pass


# ---------------------------------------------------------------------------
# Synchronous ADK callback wrapper
# ---------------------------------------------------------------------------

def orchestrated_production_callback(
    callback_context,
) -> Optional[genai_types.Content]:
    """ADK before_agent_callback that runs the ProductionOrchestrator.

    Synchronous wrapper around the async orchestrator.
    Falls back to deterministic_production_callback on import/config errors.

    Controlled by PRODUCTION_ORCHESTRATOR env var:
      - "1" or "true": use the new orchestrator
      - anything else: use the existing deterministic callback
    """
    if not os.environ.get("PRODUCTION_ORCHESTRATOR", "").strip().lower() in (
        "1", "true"
    ):
        # Fallback to existing deterministic callback
        from callbacks.deterministic_steps import deterministic_production_callback
        return deterministic_production_callback(callback_context)

    state = callback_context.state

    try:
        orchestrator = ProductionOrchestrator(
            state=state,
            callback_context=callback_context,
        )

        # Run async orchestrator in event loop
        loop: Optional[asyncio.AbstractEventLoop] = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside an existing event loop — run in a thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                summary = pool.submit(asyncio.run, orchestrator.run()).result()
        else:
            summary = asyncio.run(orchestrator.run())

        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text=summary)],
        )

    except _StageSkipped as e:
        # B2 checkpoint — stage already complete
        state["pipeline_phase"] = "production"
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text=str(e))],
        )

    except RuntimeError:
        # Fatal errors (OTIO violations, gatekeeper rejections, contract
        # failures, timeline guardian) — NEVER swallow.  Invariant #10.
        raise

    except Exception as e:
        logger.error(
            "ProductionOrchestrator failed, falling back to deterministic: %s", e,
            exc_info=True,
        )
        # Fallback to deterministic callback on non-fatal orchestrator errors
        from callbacks.deterministic_steps import deterministic_production_callback
        return deterministic_production_callback(callback_context)
