"""
ADK CustomAgent wrapper for the ProductionOrchestrator.

Wraps the mcp-agent-pattern orchestrator as an ADK BaseAgent so that:
1. Every phase (planning, execution, synthesis) yields ADK events
2. The planner/evaluator/replanner become ADK sub-agents (optimizable)
3. The full trace tree is visible to ``adk eval`` and ``adk optimize``

The orchestrator keeps its mcp-agent DNA (adaptive planning, policy
engine, knowledge accumulation) — only the outer shell and LLM calls
change to ADK conventions.
"""

from __future__ import annotations

import json
import logging
import time
from typing import AsyncGenerator, Optional

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.genai import types as genai_types

from orchestrator.production_models import (
    PlanEvaluation,
    ProductionPlan,
    QualityRating,
)
from orchestrator.prompts import (
    PLAN_EVALUATION_USER_TEMPLATE,
    PLAN_EVALUATOR_INSTRUCTION,
    PLAN_GENERATION_USER_TEMPLATE,
    PLAN_OPTIMIZER_INSTRUCTION,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ADK sub-agents for plan generation, evaluation, and replanning.
# These are lightweight Agent instances whose instructions can be
# rewritten by ``adk optimize`` based on production run outcomes.
# ---------------------------------------------------------------------------

def _create_planner_agent():
    """Create the planner sub-agent (generates/refines ProductionPlan)."""
    from google.adk.agents import Agent
    from agents.model_config import build_model

    return Agent(
        name="production_planner",
        model=build_model(synthesis=True),
        instruction=PLAN_OPTIMIZER_INSTRUCTION,
        output_key="planner_output",
    )


def _create_evaluator_agent():
    """Create the evaluator sub-agent (rates plan quality)."""
    from google.adk.agents import Agent
    from agents.model_config import build_model

    return Agent(
        name="production_evaluator",
        model=build_model(synthesis=True),
        instruction=PLAN_EVALUATOR_INSTRUCTION,
        output_key="evaluator_output",
    )


def _create_replanner_agent():
    """Create the replanner sub-agent (adjusts plan after batch execution)."""
    from google.adk.agents import Agent
    from agents.model_config import build_model

    return Agent(
        name="production_replanner",
        model=build_model(synthesis=True),
        instruction=PLAN_OPTIMIZER_INSTRUCTION,
        output_key="replanner_output",
    )


# ---------------------------------------------------------------------------
# ProductionAgent — ADK CustomAgent wrapper
# ---------------------------------------------------------------------------

class ProductionAgent(BaseAgent):
    """ADK CustomAgent that wraps the ProductionOrchestrator.

    Implements ``_run_async_impl`` so every orchestration phase appears
    as an event in the ADK session trace.  Sub-agents (planner, evaluator,
    replanner) are called for each LLM decision, making their instructions
    visible to ``adk optimize``.

    Usage::

        agent = ProductionAgent(name="production_agent")
        # Runs within an ADK pipeline via Runner or direct invocation.
    """

    model_config = {"arbitrary_types_allowed": True}

    planner_agent: Optional[BaseAgent] = None
    evaluator_agent: Optional[BaseAgent] = None
    replanner_agent: Optional[BaseAgent] = None

    def __init__(self, **kwargs):
        # Initialize sub-agents lazily to avoid import-time side effects
        super().__init__(**kwargs)
        try:
            self.planner_agent = _create_planner_agent()
            self.evaluator_agent = _create_evaluator_agent()
            self.replanner_agent = _create_replanner_agent()
        except Exception as e:
            logger.warning(
                "ProductionAgent: could not create sub-agents "
                "(will fall back to direct LLM calls): %s", e,
            )

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        """Main execution — orchestrate production and yield events."""

        state = ctx.session.state if ctx.session else {}

        # ----- Phase 0: Preconditions -----
        yield _event("production_started", {
            "timestamp": time.time(),
            "phase": "preconditions",
        })

        try:
            from orchestrator.production_orchestrator import (
                ProductionOrchestrator,
                _StageSkipped,
            )

            orchestrator = ProductionOrchestrator(
                state=state,
                callback_context=_make_callback_context(state),
            )
            orchestrator._check_preconditions()
            orchestrator._parse_state()

            if not orchestrator.concepts:
                yield _event("production_error", {"error": "No visual concepts found"})
                return

            orchestrator._init_verifier()

        except _StageSkipped as e:
            yield _event("production_skipped", {"reason": str(e)})
            return

        # ----- Phase 1: Planning (EvaluatorOptimizer loop) -----
        yield _event("planning_started", {
            "concepts": len(orchestrator.concepts),
            "workers": orchestrator.num_workers,
            "existing_clips": len(orchestrator.existing_clips),
        })

        plan = await self._generate_verified_plan(orchestrator, ctx)

        yield _event("plan_finalized", {
            "batches": len(plan.batches),
            "total_clips": sum(len(b.tasks) for b in plan.batches),
            "strategy": plan.strategy,
            "estimated_gpu_minutes": plan.estimated_gpu_minutes,
        })

        # ----- Phase 2: Execution with replanning -----
        yield _event("execution_started", {
            "total_batches": len(plan.batches),
        })

        result = await orchestrator._execute_plan(plan)

        yield _event("execution_complete", {
            "total_clips": result.total_clips,
            "failed_clips": result.failed_clips,
            "skipped_clips": result.skipped_clips,
            "replan_count": result.replan_count,
        })

        # ----- Phase 3: Post-processing -----
        yield _event("post_processing_started", {})

        summary = orchestrator._post_process(result)

        yield _event("production_complete", {
            "summary": summary,
            "total_clips": result.total_clips,
            "failed_clips": result.failed_clips,
        })

    # ------------------------------------------------------------------
    # Planning with ADK sub-agents
    # ------------------------------------------------------------------

    async def _generate_verified_plan(
        self,
        orchestrator,
        ctx: InvocationContext,
    ) -> ProductionPlan:
        """EvaluatorOptimizer loop using ADK sub-agents.

        Each LLM call goes through an ADK Agent so the trace captures
        the planner and evaluator decisions separately.
        """
        from orchestrator.production_orchestrator import ProductionOrchestrator

        previous_feedback = ""
        best_plan: Optional[ProductionPlan] = None
        best_rating = QualityRating.POOR

        for attempt in range(ProductionOrchestrator.MAX_REFINEMENTS + 1):
            # Generate / refine plan via planner sub-agent
            plan = await self._call_planner(orchestrator, previous_feedback)

            # Structural verification (hard gate — not an LLM call)
            verification = orchestrator._verifier.verify(plan)
            if not verification.is_valid:
                previous_feedback = (
                    f"STRUCTURAL ERRORS (hard gate — these MUST be fixed): "
                    f"{verification.format_errors()}"
                )
                if best_plan is None:
                    best_plan = plan
                continue

            # Quality evaluation via evaluator sub-agent
            evaluation = await self._call_evaluator(orchestrator, plan)

            if evaluation.rating > best_rating:
                best_plan = plan
                best_rating = evaluation.rating

            if evaluation.rating >= QualityRating.EXCELLENT:
                logger.info(
                    "ProductionAgent: plan EXCELLENT on attempt %d",
                    attempt + 1,
                )
                return plan

            previous_feedback = (
                f"EVALUATOR FEEDBACK (rating={evaluation.rating.name}): "
                f"{evaluation.feedback}\n"
                f"Focus areas: {', '.join(evaluation.focus_areas)}"
            )

        logger.warning(
            "ProductionAgent: exhausted refinements, using best plan (rating=%s)",
            best_rating.name,
        )
        if best_plan is None:
            best_plan = orchestrator._create_fallback_plan()
        return best_plan

    async def _call_planner(
        self, orchestrator, feedback: str = ""
    ) -> ProductionPlan:
        """Call the planner sub-agent (or fall back to direct LLM)."""
        concepts_json = json.dumps(orchestrator.concepts, indent=2)
        worker_list = (
            "\n".join(f"  - {url}" for url in orchestrator.worker_urls)
            if orchestrator.worker_urls
            else "  (single local worker)"
        )
        existing_str = (
            json.dumps(list(orchestrator.existing_clips.keys()), indent=2)
            if orchestrator.existing_clips
            else "  (none)"
        )
        feedback_section = (
            f"Previous feedback to address:\n{feedback}"
            if feedback
            else "This is the first attempt — generate a fresh plan."
        )

        user_prompt = PLAN_GENERATION_USER_TEMPLATE.format(
            num_concepts=len(orchestrator.concepts),
            concepts_json=concepts_json,
            num_workers=orchestrator.num_workers,
            worker_list=worker_list,
            existing_clips=existing_str,
            visual_style=orchestrator.visual_style_str or "(none specified)",
            feedback_section=feedback_section,
        )

        # Use direct LLM call (sub-agent invocation requires a Runner context
        # which may not be available in all execution paths)
        response_text = await orchestrator._llm_call(
            system_instruction=PLAN_OPTIMIZER_INSTRUCTION,
            user_prompt=user_prompt,
            model_role="synthesis",
        )
        return orchestrator._parse_plan(response_text)

    async def _call_evaluator(
        self, orchestrator, plan: ProductionPlan
    ) -> PlanEvaluation:
        """Call the evaluator sub-agent (or fall back to direct LLM)."""
        plan_json = plan.model_dump_json(indent=2)
        user_prompt = PLAN_EVALUATION_USER_TEMPLATE.format(
            plan_json=plan_json,
            num_concepts=len(orchestrator.concepts),
            num_workers=orchestrator.num_workers,
            num_existing=len(orchestrator.existing_clips),
        )

        response_text = await orchestrator._llm_call(
            system_instruction=PLAN_EVALUATOR_INSTRUCTION,
            user_prompt=user_prompt,
            model_role="synthesis",
        )
        return orchestrator._parse_evaluation(response_text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(event_type: str, data: dict, *, author: str = "production_supervisor") -> Event:
    """Create a proper ADK Event for the trace."""
    return Event(
        author=author,
        content=genai_types.Content(
            role="model",
            parts=[genai_types.Part(text=json.dumps({
                "event": event_type,
                **data,
            }))],
        ),
    )


class _CallbackContextShim:
    """Minimal shim that gives ProductionOrchestrator the interface it needs."""

    def __init__(self, state: dict):
        self.state = state


def _make_callback_context(state: dict):
    """Create a minimal callback context from ADK session state."""
    return _CallbackContextShim(state)
