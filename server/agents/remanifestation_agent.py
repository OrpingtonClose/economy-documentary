"""
Re-manifestation Agent (ARCH-A6, issue #136) -- ADK wrapper.

This is a thin composition layer over the pure-Python planner /
validator / executor in :mod:`callbacks.remanifestation`.  The core
logic lives there so it can be unit-tested (and called from callbacks)
without requiring the google-adk runtime; this module is what the
outer pipeline composes to run A6 on every stage boundary.

The agent itself performs no LLM work.  It exposes the four A6 tools
as plain callables so an operator / dashboard can inspect each stage
(analyse, plan, validate, execute) independently, and wires
:func:`after_agent_remanifestation_callback` as the stage-boundary
guardian that drains any pending drift signals and re-manifests the
affected artifacts.

The tool surface on the agent:

* ``analyse_pending_drift(state) -> list[dict]`` -- non-destructive
  inspection of what A5 has queued for A6.
* ``plan_next_drift(state) -> dict`` -- peek at the plan for the
  next queued drift without consuming it.
* ``validate_next_plan(state) -> dict`` -- run the validator on the
  next plan; returns ``{"ok": bool, "error": str | None}``.
* ``execute_pending_drift(state) -> list[dict]`` -- drain the queue
  and run analyse/plan/validate/execute for every signal.

Each tool is a pure callable so it also works from the REPL / tests
without an ADK runtime.
"""

from __future__ import annotations

import logging
from typing import Any, MutableMapping

from callbacks.consistency_checker import pending_drift_signals
from callbacks.remanifestation import (
    after_agent_remanifestation_callback,
    analyse_impact,
    handle_drift,
    plan_remanifestation,
    validate_plan,
    InvalidPlanError,
)

logger = logging.getLogger(__name__)


REMANIFESTATION_AGENT_NAME = "remanifestation_agent"
REMANIFESTATION_OUTPUT_KEY = "remanifestation_summary"


_REMANIFESTATION_INSTRUCTION = """\
You are the Re-manifestation agent (ARCH-A6).  You do not generate
media.  You watch for Preference Ledger drift (queued by ARCH-A5) and
call the appropriate tool to analyse impact, plan a minimal
re-manifestation, validate it, and execute it via the existing
escalation menu actions (REPLACE / EXTEND / rewrite_scene only).

NEVER bypass the plan validator.  NEVER emit actions outside the
permitted subset.  If the validator rejects a plan, surface the error
and stop -- a human reviewer must intervene.
"""


# ---------------------------------------------------------------------------
# Plain-callable tools
# ---------------------------------------------------------------------------


def analyse_pending_drift_tool(
    state: MutableMapping[str, Any],
) -> list[dict[str, Any]]:
    """Return one impact summary per queued drift signal (non-destructive)."""
    out: list[dict[str, Any]] = []
    for drift in pending_drift_signals(state):
        impacted = analyse_impact(state, drift)
        out.append({
            "stage_name": drift.stage_name,
            "from_rev": drift.from_rev,
            "to_rev": drift.to_rev,
            "impacted_artifacts": [
                {
                    "artifact_key": i.artifact_key,
                    "stage": i.stage,
                    "triggering_revisions": [
                        r.revision for r in i.triggering_records
                    ],
                    "has_hard_record": i.has_hard_record,
                }
                for i in impacted
            ],
        })
    return out


def plan_next_drift_tool(
    state: MutableMapping[str, Any],
) -> dict[str, Any]:
    """Peek at the plan for the head-of-queue drift without consuming it."""
    signals = pending_drift_signals(state)
    if not signals:
        return {"plan": None, "reason": "no pending drift signals"}
    plan = plan_remanifestation(state, signals[0])
    return {"plan": plan.to_dict(), "reason": plan.reason}


def validate_next_plan_tool(
    state: MutableMapping[str, Any],
) -> dict[str, Any]:
    """Validate the head-of-queue drift's plan.  Does not consume the signal."""
    signals = pending_drift_signals(state)
    if not signals:
        return {"ok": True, "error": None, "plan": None}
    plan = plan_remanifestation(state, signals[0])
    try:
        validate_plan(state, plan)
    except InvalidPlanError as exc:
        return {"ok": False, "error": str(exc), "plan": plan.to_dict()}
    return {"ok": True, "error": None, "plan": plan.to_dict()}


def execute_pending_drift_tool(
    state: MutableMapping[str, Any],
) -> list[dict[str, Any]]:
    """Drain every queued drift and run analyse/plan/validate/execute."""
    receipts = handle_drift(state)
    return [
        {
            "stage_name": r.drift.stage_name,
            "from_rev": r.drift.from_rev,
            "to_rev": r.drift.to_rev,
            "plan_step_count": len(r.plan.steps),
            "plan_reason": r.plan.reason,
            "step_receipts": [dict(sr) for sr in r.step_receipts],
            "error": r.error,
        }
        for r in receipts
    ]


# ---------------------------------------------------------------------------
# ADK Agent wrapper
# ---------------------------------------------------------------------------


def _build_remanifestation_agent() -> Any:
    """Build the ADK ``Agent`` wrapper.

    Returns ``None`` when ADK / model-config cannot be imported, so
    CI / offline test paths still exercise the pure-Python tools.
    Matches the :mod:`agents.preference_interpreter` composition
    pattern.
    """
    try:
        from google.adk.agents import Agent

        from agents.model_config import build_model
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.warning(
            "ADK unavailable (%s) -- remanifestation_agent will be "
            "None; the analyse/plan/validate/execute tools still work "
            "as plain Python entry points.",
            exc,
        )
        return None

    return Agent(
        name=REMANIFESTATION_AGENT_NAME,
        model=build_model(synthesis=True),
        instruction=_REMANIFESTATION_INSTRUCTION,
        tools=[
            analyse_pending_drift_tool,
            plan_next_drift_tool,
            validate_next_plan_tool,
            execute_pending_drift_tool,
        ],
        output_key=REMANIFESTATION_OUTPUT_KEY,
        after_agent_callback=after_agent_remanifestation_callback,
    )


remanifestation_agent = _build_remanifestation_agent()


__all__ = [
    "REMANIFESTATION_AGENT_NAME",
    "REMANIFESTATION_OUTPUT_KEY",
    "analyse_pending_drift_tool",
    "execute_pending_drift_tool",
    "plan_next_drift_tool",
    "remanifestation_agent",
    "validate_next_plan_tool",
]
