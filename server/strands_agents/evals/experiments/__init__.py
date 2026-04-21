"""Experiment factories for strands_agents evals.

Each component PR adds a module here exporting a ``build_*`` factory
and a ``*_task`` callable, keeping experiment construction colocated
with the agent it tests.
"""

from strands_agents.evals.experiments.scenario import (
    SCENARIO_EVALUATOR_THRESHOLDS,
    build_scenario_experiment,
    scenario_cases,
)

__all__ = [
    "SCENARIO_EVALUATOR_THRESHOLDS",
    "build_scenario_experiment",
    "scenario_cases",
]
