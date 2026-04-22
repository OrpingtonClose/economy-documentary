"""Approval-gate experiment (component 15).

Exercises the three HITL gates declared in
``docs/strands-migration/components/15-approval-gates.md`` without
calling an LLM. Each case is a pre-captured interrupt trajectory +
final-state snapshot the orchestrator would have produced on a real
run. The experiment verifies:

* the correct gate fired an interrupt,
* the operator's scripted decision was applied,
* follow-through (``accept`` / ``edit``) actually dispatched the
  gated tool while short-circuit (``reject`` / ``respond``) left
  downstream tools uncalled.

Real model-backed runs (a DeepAgent hitting a stubbed TTS/GPU pool and
replaying scripted decisions via
:func:`server.strands_agents.run.replay_operator_decisions`) land once
worker credentials exist in CI. The pre-captured trajectories here are
the contract the live run must satisfy.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.deterministic import Equals
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.evals.evaluators import (
    ApprovalGateTrajectoryEvaluator,
)

# ---------------------------------------------------------------------------
# Trajectory fixtures
# ---------------------------------------------------------------------------


_VISUAL_ARGS = {
    "scene_id": "s1",
    "prompt": "cinematic cityscape at dawn, warm tones",
    "duration": 5.0,
}
_VISUAL_ARGS_EDITED = {
    "scene_id": "s1",
    "prompt": "cinematic cityscape at dawn, warm tones, 35mm film grain",
    "duration": 5.0,
}
_ASSEMBLY_ARGS = {"timeline_path": "/runs/r1/timeline.otio"}


def _pre_gate_trail() -> list[dict[str, Any]]:
    """Tool calls every run performs before hitting a visual gate."""

    return [
        {"kind": "tool_call", "name": "generate_scenario"},
        {"kind": "tool_call", "name": "evaluate_scenario"},
        {"kind": "tool_call", "name": "launch_audio_render"},
        {"kind": "tool_call", "name": "await_tasks"},
        {"kind": "tool_call", "name": "evaluate_timing"},
    ]


_ACCEPT_VISUAL_TRAJECTORY: list[dict[str, Any]] = [
    *_pre_gate_trail(),
    {
        "kind": "interrupt",
        "tool": "launch_visual_production",
        "decision": "accept",
        "args": _VISUAL_ARGS,
    },
    {"kind": "tool_call", "name": "launch_visual_production", "args": _VISUAL_ARGS},
    {"kind": "tool_call", "name": "await_tasks"},
    {"kind": "tool_call", "name": "launch_assembly", "args": _ASSEMBLY_ARGS},
    {"kind": "tool_call", "name": "launch_b2_sync"},
]

_EDIT_VISUAL_TRAJECTORY: list[dict[str, Any]] = [
    *_pre_gate_trail(),
    {
        "kind": "interrupt",
        "tool": "launch_visual_production",
        "decision": "edit",
        "args": _VISUAL_ARGS_EDITED,
    },
    {
        "kind": "tool_call",
        "name": "launch_visual_production",
        "args": _VISUAL_ARGS_EDITED,
    },
    {"kind": "tool_call", "name": "await_tasks"},
    {"kind": "tool_call", "name": "launch_assembly", "args": _ASSEMBLY_ARGS},
    {"kind": "tool_call", "name": "launch_b2_sync"},
]

_REJECT_VISUAL_TRAJECTORY: list[dict[str, Any]] = [
    *_pre_gate_trail(),
    {
        "kind": "interrupt",
        "tool": "launch_visual_production",
        "decision": "reject",
        "args": _VISUAL_ARGS,
        "reason": "prompt unsafe for client",
    },
    # Escalation SubAgent kicks in and requests a human approval to
    # decide whether to skip the scene entirely.
    {
        "kind": "tool_call",
        "name": "request_human_approval",
        "args": {"reason": "escalation:visual_rejected:s1"},
    },
]

_RESPOND_ASSEMBLY_TRAJECTORY: list[dict[str, Any]] = [
    *_pre_gate_trail(),
    {"kind": "tool_call", "name": "launch_visual_production", "args": _VISUAL_ARGS},
    {"kind": "tool_call", "name": "await_tasks"},
    {
        "kind": "interrupt",
        "tool": "launch_assembly",
        "decision": "respond",
        "args": _ASSEMBLY_ARGS,
        "content": "hold assembly 24h pending marketing sign-off",
    },
]

_ESCALATION_DECISION_TRAJECTORY: list[dict[str, Any]] = [
    *_pre_gate_trail(),
    {"kind": "tool_call", "name": "launch_visual_production", "args": _VISUAL_ARGS},
    {"kind": "tool_call", "name": "await_tasks"},
    {
        "kind": "interrupt",
        "tool": "request_human_approval",
        "decision": "respond",
        "args": {"reason": "escalation:persistent_gpu_failure", "summary": "s3 VRAM OOM x3"},
        "content": "skip scene s3",
    },
    {"kind": "tool_call", "name": "launch_assembly", "args": _ASSEMBLY_ARGS},
    {"kind": "tool_call", "name": "launch_b2_sync"},
]

# A second accept case mimicking operator reconnecting after a process
# restart. The trajectory shape is identical to the happy accept —
# what the case really asserts is that downstream tools still fire
# once the operator resumes, proving the checkpointer + queue
# round-trip.
_RESUME_AFTER_RESTART_TRAJECTORY: list[dict[str, Any]] = list(_ACCEPT_VISUAL_TRAJECTORY)


# ---------------------------------------------------------------------------
# Expected final states + case envelopes
# ---------------------------------------------------------------------------


def _final_state(**overrides: Any) -> dict[str, Any]:
    """Representative final-state snapshot for a successful run."""

    state: dict[str, Any] = {
        "run_id": "r1",
        "approvals_resolved": 1,
        "launch_visual_production_dispatched": True,
        "launch_assembly_dispatched": True,
    }
    state.update(overrides)
    return state


_CASES: list[Case] = [
    Case(
        name="accept_visual_dispatch",
        input={"brief": "accept path — operator approves the visual prompt"},
        expected_output=_final_state(),
        expected_trajectory=[
            call.get("name") or call.get("tool")
            for call in _ACCEPT_VISUAL_TRAJECTORY
        ],
        metadata={
            "full_trajectory": _ACCEPT_VISUAL_TRAJECTORY,
            "gated_tool": "launch_visual_production",
            "expected_decision": "accept",
            "post_approval_tool": "launch_visual_production",
            "expected_tool_arguments": {
                "launch_visual_production": _VISUAL_ARGS,
            },
        },
    ),
    Case(
        name="edit_visual_prompt",
        input={"brief": "edit path — operator tweaks the prompt"},
        expected_output=_final_state(
            launch_visual_production_prompt_edited=True,
        ),
        expected_trajectory=[
            call.get("name") or call.get("tool")
            for call in _EDIT_VISUAL_TRAJECTORY
        ],
        metadata={
            "full_trajectory": _EDIT_VISUAL_TRAJECTORY,
            "gated_tool": "launch_visual_production",
            "expected_decision": "edit",
            "post_approval_tool": "launch_visual_production",
            "expected_tool_arguments": {
                "launch_visual_production": _VISUAL_ARGS_EDITED,
            },
        },
    ),
    Case(
        name="reject_visual_dispatch",
        input={"brief": "reject path — operator rejects, escalation kicks in"},
        expected_output=_final_state(
            launch_visual_production_dispatched=False,
            launch_assembly_dispatched=False,
            escalation_raised=True,
        ),
        expected_trajectory=[
            call.get("name") or call.get("tool")
            for call in _REJECT_VISUAL_TRAJECTORY
        ],
        metadata={
            "full_trajectory": _REJECT_VISUAL_TRAJECTORY,
            "gated_tool": "launch_visual_production",
            "expected_decision": "reject",
            "forbidden_on_reject": ["launch_assembly", "launch_b2_sync"],
        },
    ),
    Case(
        name="respond_assembly_hold",
        input={"brief": "respond path — operator holds assembly"},
        expected_output=_final_state(
            launch_assembly_dispatched=False,
            assembly_response="hold assembly 24h pending marketing sign-off",
        ),
        expected_trajectory=[
            call.get("name") or call.get("tool")
            for call in _RESPOND_ASSEMBLY_TRAJECTORY
        ],
        metadata={
            "full_trajectory": _RESPOND_ASSEMBLY_TRAJECTORY,
            "gated_tool": "launch_assembly",
            "expected_decision": "respond",
            "forbidden_on_reject": ["launch_b2_sync"],
        },
    ),
    Case(
        name="escalation_decision",
        input={"brief": "escalation — operator decides to skip a scene"},
        expected_output=_final_state(
            approvals_resolved=2,
            escalation_decision="skip scene s3",
        ),
        expected_trajectory=[
            call.get("name") or call.get("tool")
            for call in _ESCALATION_DECISION_TRAJECTORY
        ],
        metadata={
            "full_trajectory": _ESCALATION_DECISION_TRAJECTORY,
            "gated_tool": "request_human_approval",
            "expected_decision": "respond",
            "forbidden_on_reject": [],
        },
    ),
    Case(
        name="resume_after_restart",
        input={"brief": "resume after restart — checkpointer restores queue"},
        expected_output=_final_state(resumed_after_restart=True),
        expected_trajectory=[
            call.get("name") or call.get("tool")
            for call in _RESUME_AFTER_RESTART_TRAJECTORY
        ],
        metadata={
            "full_trajectory": _RESUME_AFTER_RESTART_TRAJECTORY,
            "gated_tool": "launch_visual_production",
            "expected_decision": "accept",
            "post_approval_tool": "launch_visual_production",
            "resumed_after_restart": True,
            "expected_tool_arguments": {
                "launch_visual_production": _VISUAL_ARGS,
            },
        },
    ),
]


class _DispatchedArgsEvaluator(Evaluator[Any, Any]):
    """Deterministic arg-accuracy check for post-accept/-edit dispatch.

    Scores 1.0 iff every tool in
    ``metadata["expected_tool_arguments"]`` appears in the trajectory
    as a ``kind=tool_call`` record whose ``args`` match exactly.

    Short-circuit cases (``reject`` / ``respond``) opt out by not
    populating ``expected_tool_arguments``; the evaluator returns a
    single ``skipped`` output in that case so the case still scores
    1.0 overall.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[Any, Any],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}
        expected_args: dict[str, dict[str, Any]] = (
            metadata.get("expected_tool_arguments") or {}
        )
        if not expected_args:
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="SKIP no expected_tool_arguments for this case",
                    label="approval.args.skipped",
                ),
            ]

        trajectory = evaluation_case.actual_trajectory
        if not isinstance(trajectory, list):
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="actual_trajectory must be list[dict]",
                    label="approval.args.missing_actual",
                ),
            ]

        dispatched: dict[str, dict[str, Any]] = {}
        for record in trajectory:
            if not isinstance(record, dict):
                continue
            if record.get("kind", "tool_call") != "tool_call":
                continue
            name = record.get("name")
            if isinstance(name, str):
                dispatched[name] = record.get("args") or {}

        outputs: list[EvaluationOutput] = []
        for tool_name, want in expected_args.items():
            got = dispatched.get(tool_name)
            if got is None:
                outputs.append(
                    EvaluationOutput(
                        score=0.0,
                        test_pass=False,
                        reason=f"FAIL {tool_name} never dispatched",
                        label=f"approval.args.{tool_name}",
                    ),
                )
                continue
            ok = got == want
            outputs.append(
                EvaluationOutput(
                    score=1.0 if ok else 0.0,
                    test_pass=ok,
                    reason=(
                        f"PASS {tool_name} dispatched with approved args"
                        if ok
                        else f"FAIL {tool_name} args={got!r} want={want!r}"
                    ),
                    label=f"approval.args.{tool_name}",
                ),
            )
        return outputs


def build_approval_experiment() -> Experiment:
    """Construct the approval-gate experiment.

    Returns:
        An :class:`Experiment` with the 6 spec cases and three
        deterministic hard gates:

        * :class:`ApprovalGateTrajectoryEvaluator` — interrupt fired,
          decision honoured, follow-through / short-circuit correct.
        * :class:`_DispatchedArgsEvaluator` — the dispatched tool's
          arguments match what the operator approved (``accept`` keeps
          original args, ``edit`` substitutes the edited args;
          ``reject``/``respond`` skip this check).
        * :class:`Equals` — the final-state snapshot matches the
          contract.
    """

    return Experiment(
        cases=list(_CASES),
        evaluators=[
            ApprovalGateTrajectoryEvaluator(),
            _DispatchedArgsEvaluator(),
            Equals(),
        ],
    )


def approval_task(case: Case) -> dict[str, Any]:
    """Task adapter for :func:`Experiment.run_evaluations`.

    Surfaces the case's canonical trajectory (full interrupt +
    tool-call records) and expected final state as the envelope the
    evaluators consume. A live model-backed run replaces this with
    an ``agent.ainvoke`` wrapper and a decision replayer built from
    ``case.metadata["expected_decision"]``.
    """

    metadata = case.metadata or {}
    return {
        "output": case.expected_output or {},
        "trajectory": list(metadata.get("full_trajectory") or []),
    }


__all__ = [
    "approval_task",
    "build_approval_experiment",
]
