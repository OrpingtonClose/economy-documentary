"""Pipeline-orchestrator experiment (component 14).

Exercises the 5-stage orchestration contract declared in
``docs/strands-migration/components/14-pipeline-graph.md`` without
calling an LLM. The experiment operates on pre-captured trajectories
(lists of tool-call names) so CI stays hermetic; the full end-to-end
eval with TTS + GPU + Assembly simulators lands once component 15
(approval gates) supplies an operator mock and model credentials are
wired.

Each case is a ``trajectory + final state`` pair. The evaluator stack
checks that the trajectory covers the expected tool subsequence
(:class:`PipelineTrajectoryEvaluator`) and that the pipeline's final
state satisfies :data:`PIPELINE_CONTRACT`
(:class:`ContractComplianceEvaluator`).

Real model-backed runs (happy_path_5min, timing_refine_once,
visual_revise, escalation_path, operator_approval_edit) will be added
alongside component 15 once the operator simulator and seeded worker
simulators are ready.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.experiment import Experiment

from contracts import PIPELINE_CONTRACT
from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    PipelineTrajectoryEvaluator,
)

# ---------------------------------------------------------------------------
# Fixtures: canonical trajectories for each of the 5 spec cases
# ---------------------------------------------------------------------------
# The per-case trajectories mirror the expected orchestration described
# in the component 14 spec. A production run against real workers will
# emit a superset — these subsequences are what every case MUST contain.
# ---------------------------------------------------------------------------

_HAPPY_PATH_TRAJECTORY: list[str] = [
    "generate_scenario",
    "evaluate_scenario",
    "launch_audio_render",
    "await_tasks",
    "evaluate_timing",
    "launch_visual_production",
    "await_tasks",
    "launch_assembly",
    "launch_b2_sync",
]

_TIMING_REFINE_TRAJECTORY: list[str] = [
    "generate_scenario",
    "evaluate_scenario",
    "launch_audio_render",
    "await_tasks",
    "evaluate_timing",
    "refine_scenario",
    "launch_audio_render",
    "await_tasks",
    "evaluate_timing",
    "launch_visual_production",
    "await_tasks",
    "launch_assembly",
    "launch_b2_sync",
]

_VISUAL_REVISE_TRAJECTORY: list[str] = [
    "generate_scenario",
    "evaluate_scenario",
    "launch_audio_render",
    "await_tasks",
    "evaluate_timing",
    "launch_visual_production",
    "await_tasks",
    "launch_visual_production",
    "await_tasks",
    "launch_assembly",
    "launch_b2_sync",
]

_ESCALATION_TRAJECTORY: list[str] = [
    "generate_scenario",
    "evaluate_scenario",
    "launch_audio_render",
    "await_tasks",
    "evaluate_timing",
    "launch_visual_production",
    "await_tasks",
    "launch_visual_production",
    "await_tasks",
    "request_human_approval",
    "launch_assembly",
    "launch_b2_sync",
]

_OPERATOR_APPROVAL_TRAJECTORY: list[str] = [
    "generate_scenario",
    "evaluate_scenario",
    "launch_audio_render",
    "await_tasks",
    "evaluate_timing",
    "request_human_approval",
    "launch_visual_production",
    "await_tasks",
    "launch_assembly",
    "launch_b2_sync",
]


def _final_state(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a representative final-state dict satisfying ``PIPELINE_CONTRACT``.

    The contract requires ``brief`` / ``target_duration_sec`` in the
    input, and ``scenes`` / ``whisperx_alignment`` / ``visual_concepts``
    / ``final_timeline`` in the output. We fill each with a
    non-placeholder value so :class:`ContractComplianceEvaluator`
    reports a passing case. Real runs produce richer outputs; the
    contract only gates on non-empty presence + artifact existence.
    """

    state: dict[str, Any] = {
        "brief": "5-minute documentary about inflation, 5 scenes",
        "target_duration_sec": 300.0,
        "scenes": [
            {"scene_id": f"scene_{i}", "narration": f"scene {i} text"}
            for i in range(5)
        ],
        "whisperx_alignment": {"words": [{"start": 0.0, "end": 1.0}]},
        "visual_concepts": [
            {"scene_id": f"scene_{i}", "prompt": "cinematic"} for i in range(5)
        ],
        "final_timeline": {"duration": 300.0, "tracks": ["audio", "video"]},
    }
    if extra:
        state.update(extra)
    return state


_CASES: list[Case] = [
    Case(
        name="happy_path_5min",
        input={
            "brief": "5-minute documentary about inflation, 5 scenes",
            "target_duration_sec": 300.0,
        },
        expected_output=_final_state(),
        expected_trajectory=_HAPPY_PATH_TRAJECTORY,
        metadata={
            "expected_tool_sequence": _HAPPY_PATH_TRAJECTORY,
            "strict_order": True,
            "contract_name": PIPELINE_CONTRACT.name,
        },
    ),
    Case(
        name="timing_refine_once",
        input={
            "brief": "3-minute explainer, 3 scenes",
            "target_duration_sec": 180.0,
        },
        expected_output=_final_state(
            {
                "brief": "3-minute explainer, 3 scenes",
                "target_duration_sec": 180.0,
            },
        ),
        expected_trajectory=_TIMING_REFINE_TRAJECTORY,
        metadata={
            "expected_tool_sequence": _TIMING_REFINE_TRAJECTORY,
            "strict_order": True,
            "contract_name": PIPELINE_CONTRACT.name,
        },
    ),
    Case(
        name="visual_revise",
        input={
            "brief": "7-minute documentary, 7 scenes",
            "target_duration_sec": 420.0,
        },
        expected_output=_final_state(
            {
                "brief": "7-minute documentary, 7 scenes",
                "target_duration_sec": 420.0,
            },
        ),
        expected_trajectory=_VISUAL_REVISE_TRAJECTORY,
        metadata={
            "expected_tool_sequence": _VISUAL_REVISE_TRAJECTORY,
            "strict_order": True,
            "contract_name": PIPELINE_CONTRACT.name,
        },
    ),
    Case(
        name="escalation_path",
        input={
            "brief": "5-minute documentary about inflation, 5 scenes",
            "target_duration_sec": 300.0,
        },
        expected_output=_final_state(),
        expected_trajectory=_ESCALATION_TRAJECTORY,
        metadata={
            "expected_tool_sequence": _ESCALATION_TRAJECTORY,
            "strict_order": True,
            "contract_name": PIPELINE_CONTRACT.name,
        },
    ),
    Case(
        name="operator_approval_edit",
        input={
            "brief": "5-minute documentary about inflation, 5 scenes",
            "target_duration_sec": 300.0,
        },
        expected_output=_final_state(),
        expected_trajectory=_OPERATOR_APPROVAL_TRAJECTORY,
        metadata={
            "expected_tool_sequence": _OPERATOR_APPROVAL_TRAJECTORY,
            "strict_order": True,
            "contract_name": PIPELINE_CONTRACT.name,
        },
    ),
]


def build_pipeline_experiment() -> Experiment:
    """Construct the pipeline trajectory + contract-compliance experiment.

    Returns:
        An :class:`Experiment` with the 5 spec cases and the two
        deterministic orchestration hard gates:
        :class:`PipelineTrajectoryEvaluator` and
        :class:`ContractComplianceEvaluator` for
        :data:`PIPELINE_CONTRACT`.
    """

    return Experiment(
        cases=list(_CASES),
        evaluators=[
            PipelineTrajectoryEvaluator(),
            ContractComplianceEvaluator(PIPELINE_CONTRACT),
        ],
    )


def pipeline_task(case: Case) -> dict[str, Any]:
    """Task adapter for :func:`Experiment.run_evaluations`.

    Returns the :class:`Experiment` task-protocol envelope so the
    evaluator's ``EvaluationData`` surfaces the case's expected state
    as ``actual_output`` and the canonical trajectory as
    ``actual_trajectory``.
    """

    return {
        "output": case.expected_output or {},
        "trajectory": list(case.expected_trajectory or []),
    }


__all__ = [
    "build_pipeline_experiment",
    "pipeline_task",
]
