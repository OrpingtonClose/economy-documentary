"""Scenario-agent experiment factory.

Assembles the :class:`Experiment` the strands-evals runner consumes for
``docs/strands-migration/components/01-scenario-agent.md``. Five cases
(happy path, long-form, edge-short, edge-long, failure) plus the
five-evaluator stack from ``eval-framework/CUSTOM_EVALUATORS.md``.

The ``task`` callable passed to :meth:`Experiment.run_evaluations` is
supplied by whoever drives the run (CI, a shadow runner, a notebook)
so this module stays free of LLM calls and can build the experiment
definition deterministically in pytest.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.coherence_evaluator import CoherenceEvaluator
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.evaluators.faithfulness_evaluator import FaithfulnessEvaluator
from strands_evals.evaluators.trajectory_evaluator import TrajectoryEvaluator
from strands_evals.experiment import Experiment

from contracts import SCENARIO_CONTRACT
from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    ScenarioQualityEvaluator,
)


#: Minimum score per evaluator — mirrors ``eval-framework/THRESHOLDS.md``.
#: ``True`` in the second element means the threshold is a hard gate; a
#: soft gate (``False``) logs a regression without failing the run.
SCENARIO_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "ContractComplianceEvaluator": (1.0, True),
    "ScenarioQualityEvaluator": (0.7, True),
    "TrajectoryEvaluator": (0.8, True),
    "CoherenceEvaluator": (0.75, False),
    "FaithfulnessEvaluator": (0.7, False),
}


#: Rubric handed to :class:`TrajectoryEvaluator`. The judge prompt
#: consumes this as the success criterion for the tool-call sequence.
SCENARIO_TRAJECTORY_RUBRIC = (
    "The scenario agent must always call generate_scenario first, then "
    "evaluate_scenario, optionally alternating with refine_scenario+"
    "evaluate_scenario until the rating is GOOD or EXCELLENT, and "
    "finally call create_timeline exactly once. Reject trajectories "
    "that skip evaluation, call create_timeline before a passing "
    "evaluation, or invoke tools outside this set."
)

#: Trajectory descriptions shown to the judge LLM. Keyed by tool name.
SCENARIO_TRAJECTORY_DESCRIPTION = {
    "generate_scenario": "Produce the initial scenes list plus visual_style and style_lock.",
    "evaluate_scenario": "Run structural checks and return rating + issues.",
    "refine_scenario": "Adjust scenes based on evaluator feedback.",
    "create_timeline": "Emit the OTIO timeline once scenes are approved.",
}


def scenario_cases() -> list[Case[str, dict[str, Any]]]:
    """Return the five canonical scenario-agent test cases.

    Every case's ``metadata`` carries ``target_duration_sec`` plus the
    knobs :class:`ScenarioQualityEvaluator` forwards to
    :func:`run_all_structural_checks`.
    """
    return [
        Case[str, dict[str, Any]](
            name="economics_basics",
            session_id="scenario-case-001",
            input=(
                "Produce a 5-scene, 5-minute explainer documentary "
                "about inflation suitable for a curious non-economist."
            ),
            expected_trajectory=[
                "generate_scenario",
                "evaluate_scenario",
                "refine_scenario",
                "evaluate_scenario",
                "create_timeline",
            ],
            metadata={
                "target_duration_sec": 300.0,
                "minimum_rating": "GOOD",
            },
        ),
        Case[str, dict[str, Any]](
            name="complex_monetary_policy",
            session_id="scenario-case-002",
            input=(
                "Produce a 10-scene, 10-minute deep dive on the transmission "
                "mechanism of monetary policy across deposit rates, credit "
                "supply, exchange rates, and household expectations."
            ),
            expected_trajectory=[
                "generate_scenario",
                "evaluate_scenario",
                "refine_scenario",
                "evaluate_scenario",
                "create_timeline",
            ],
            metadata={
                "target_duration_sec": 600.0,
                "minimum_rating": "GOOD",
                "per_scene_duration_tolerance": 0.15,
            },
        ),
        Case[str, dict[str, Any]](
            name="edge_single_scene",
            session_id="scenario-case-003",
            input=(
                "Produce a 1-scene, 1-minute micro-documentary on the "
                "gold standard."
            ),
            expected_trajectory=[
                "generate_scenario",
                "evaluate_scenario",
                "create_timeline",
            ],
            metadata={
                "target_duration_sec": 60.0,
                "minimum_rating": "FAIR",
            },
        ),
        Case[str, dict[str, Any]](
            name="edge_max_scenes",
            session_id="scenario-case-004",
            input=(
                "Produce a 15-scene, 15-minute historical survey of inflation "
                "episodes across the 20th century."
            ),
            expected_trajectory=[
                "generate_scenario",
                "evaluate_scenario",
                "refine_scenario",
                "evaluate_scenario",
                "create_timeline",
            ],
            metadata={
                "target_duration_sec": 900.0,
                "minimum_rating": "GOOD",
            },
        ),
        Case[str, dict[str, Any]](
            name="failure_empty_topic",
            session_id="scenario-case-005",
            input="",
            expected_trajectory=["generate_scenario"],
            metadata={
                "target_duration_sec": 300.0,
                "expect_contract_violation": True,
            },
        ),
    ]


def scenario_evaluators() -> list[Evaluator[str, dict[str, Any]]]:
    """Return the evaluator stack applied to every scenario case.

    Order matters only for readability — all evaluators run and every
    returned :class:`EvaluationOutput` contributes to the aggregate
    report. The hard gates (contract, structural quality, trajectory)
    come first.
    """
    return [
        ContractComplianceEvaluator(SCENARIO_CONTRACT),
        ScenarioQualityEvaluator(),
        TrajectoryEvaluator(
            rubric=SCENARIO_TRAJECTORY_RUBRIC,
            trajectory_description=SCENARIO_TRAJECTORY_DESCRIPTION,
        ),
        CoherenceEvaluator(),
        FaithfulnessEvaluator(),
    ]


def build_scenario_experiment() -> Experiment[str, dict[str, Any]]:
    """Construct the :class:`Experiment` for Component 01."""
    return Experiment(cases=scenario_cases(), evaluators=scenario_evaluators())
