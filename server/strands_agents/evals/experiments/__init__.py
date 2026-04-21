"""Experiment factories for strands_agents evals.

Each component PR adds a module here exporting a ``build_*`` factory
and a ``*_task`` callable, keeping experiment construction colocated
with the agent it tests.
"""

from __future__ import annotations

from .escalation import (
    ActionEqualsEvaluator,
    HumanSummaryRequiredEvaluator,
    build_escalation_contract_experiment,
    build_escalation_experiment,
    build_escalation_judge_experiment,
    escalation_contract_task,
    escalation_judge_task,
    escalation_task,
)

__all__ = [
    "ActionEqualsEvaluator",
    "HumanSummaryRequiredEvaluator",
    "build_escalation_contract_experiment",
    "build_escalation_experiment",
    "build_escalation_judge_experiment",
    "escalation_contract_task",
    "escalation_judge_task",
    "escalation_task",
]
