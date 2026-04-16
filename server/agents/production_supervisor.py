"""
Production Supervisor -- orchestrates actual video generation on GPU.

Reads visual concepts from state["visual_concepts"], provisions GPU VMs
via Vast.ai, generates video clips using LTX-2.3, probes results, and
adds clips to the OTIO timeline.

Uses the ADK ProductionAgent (CustomAgent) when available — this wraps
the mcp-agent-pattern orchestrator so every phase (planning, execution,
synthesis) yields ADK events, and the planner/evaluator/replanner are
ADK sub-agents whose instructions can be rewritten by ``adk optimize``.

Falls back to the plain Agent + orchestrated_production_callback when
ProductionAgent cannot be initialised.
"""

from __future__ import annotations

import logging

from google.adk.agents import Agent

from agents.model_config import build_model
from callbacks.timeline_guardian import timeline_guardian_callback
from orchestrator.production_orchestrator import orchestrated_production_callback

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the Production Supervisor for a documentary pipeline.
Video generation is handled automatically. Report completion.
"""


def _build_production_supervisor():
    """Build the production supervisor agent.

    Tries to use the ADK ProductionAgent (CustomAgent) which provides
    traceable events and optimizable sub-agents. Falls back to the
    plain Agent + orchestrated_production_callback if unavailable.
    """
    try:
        from orchestrator.production_agent import ProductionAgent

        agent = ProductionAgent(
            name="production_supervisor",
            description=(
                "ADK CustomAgent that wraps the ProductionOrchestrator. "
                "Every orchestration phase (planning, evaluation, execution, "
                "synthesis) yields ADK events visible to adk eval/optimize. "
                "Sub-agents: production_planner, production_evaluator, "
                "production_replanner."
            ),
            # before/after callbacks for approval gates are wired in pipeline.py
            after_agent_callback=timeline_guardian_callback,
        )
        logger.info(
            "Production supervisor: using ADK ProductionAgent (CustomAgent) "
            "with traceable sub-agents"
        )
        return agent

    except Exception as e:
        logger.warning(
            "ProductionAgent unavailable (%s) — falling back to plain Agent + "
            "orchestrated_production_callback",
            e,
        )
        return Agent(
            name="production_supervisor",
            model=build_model(),
            instruction=_INSTRUCTION,
            tools=[],
            before_agent_callback=orchestrated_production_callback,
            after_agent_callback=timeline_guardian_callback,
        )


production_supervisor = _build_production_supervisor()
