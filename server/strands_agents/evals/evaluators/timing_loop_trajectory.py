"""TimingLoopTrajectoryEvaluator — iteration-aware trajectory check.

Validates the tool-call trajectory emitted by the DeepAgent orchestrator
when running the timing loop described in
``docs/strands-migration/components/05-timing-loop.md``.

The timing loop has a deterministic shape per iteration:

1. One-or-more ``launch_audio_render`` calls.
2. Exactly one ``await_tasks`` call awaiting those launches.
3. Exactly one ``evaluate_timing`` call on the resulting alignment.

If the iteration ended with ``timing_passed=False`` the orchestrator
may follow with at most one ``refine_scenario`` (optionally paired with
a ``write_file``) before starting the next iteration.

Hard ceiling: 10 iterations. When the cap is hit the orchestrator must
delegate to the escalation SubAgent — the evaluator accepts either
``task`` with ``{"subagent_type": "escalation"}`` or a direct
``delegate_to_escalation`` tool call as a valid delegation marker.

Input shape
-----------
``EvaluationData`` with:

* ``actual_trajectory``: ``list[dict]`` of tool-call records. Each
  record must carry a ``"name"`` key; ``"args"`` is inspected for
  ``refine_scenario`` (to check a ``timing_report`` argument is
  present) and ``task`` (to detect escalation delegation).

* ``metadata[`expected_iterations`]`` (required): total number of
  timing iterations expected (counted by ``evaluate_timing`` calls).
* ``metadata[`expected_refines`]`` (optional): number of
  ``refine_scenario`` calls expected. Defaults to
  ``expected_iterations - 1`` when ``expects_pass=True``, else
  ``expected_iterations``.
* ``metadata[`expects_pass`]`` (optional, default ``True``): whether
  the final iteration should end with ``timing_passed=True``.
* ``metadata[`expects_delegation`]`` (optional, default ``False``):
  whether the trajectory must end with a delegation to the escalation
  SubAgent (i.e. iteration cap reached).
* ``metadata[`max_iterations`]`` (optional, default ``10``).

Output
------
Up to five :class:`EvaluationOutput` entries:

* ``timing_loop.iteration_count``
* ``timing_loop.shape``      — each iteration matches the expected
  ``launch_audio_render+ → await_tasks → evaluate_timing`` shape.
* ``timing_loop.refine_count``
* ``timing_loop.refine_inputs`` — every ``refine_scenario`` call
  received a non-empty ``timing_report`` argument.
* ``timing_loop.delegation``  — escalation delegation present/absent
  as expected.

All outputs are hard gates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

_DEFAULT_MAX_ITERATIONS = 10

_ESCALATION_DELEGATION_NAMES = frozenset({"delegate_to_escalation"})
_TASK_TOOL_NAME = "task"


@dataclass
class _Iteration:
    launches: int = 0
    await_calls: int = 0
    evaluate_calls: int = 0
    refines: int = 0
    refine_has_report: list[bool] = field(default_factory=list)
    order_ok: bool = True


def _extract_calls(trajectory: Any) -> list[dict[str, Any]] | None:
    if not isinstance(trajectory, list):
        return None
    result: list[dict[str, Any]] = []
    for call in trajectory:
        if isinstance(call, dict) and isinstance(call.get("name"), str):
            result.append(call)
        else:
            return None
    return result


def _is_escalation_delegation(call: dict[str, Any]) -> bool:
    name = call.get("name")
    if name in _ESCALATION_DELEGATION_NAMES:
        return True
    if name == _TASK_TOOL_NAME:
        args = call.get("args") or {}
        return args.get("subagent_type") == "escalation"
    return False


def _split_iterations(calls: list[dict[str, Any]]) -> tuple[list[_Iteration], bool]:
    """Split the trajectory into iterations bounded by ``evaluate_timing``.

    Returns the list of iteration records and a flag indicating whether
    any call sequence violates the expected per-iteration shape.
    """
    iterations: list[_Iteration] = []
    current = _Iteration()
    # States inside an iteration: "launching" → "awaiting" → "evaluated"
    state = "launching"
    started = False  # have we seen any launch for the current iteration yet?

    def _close_iteration() -> None:
        nonlocal current, state, started
        iterations.append(current)
        current = _Iteration()
        state = "launching"
        started = False

    for call in calls:
        name = call.get("name")

        if _is_escalation_delegation(call):
            # Delegation is handled outside the iteration bookkeeping.
            continue

        if name == "launch_audio_render":
            if state == "launching":
                current.launches += 1
                started = True
            elif state == "awaiting" or state == "evaluated":
                # A launch after await_tasks without going through
                # evaluate_timing (or before it finished) — mark shape
                # broken and treat it as the first launch of a new
                # iteration.
                if state == "awaiting":
                    current.order_ok = False
                if state == "evaluated":
                    # implicit next iteration — refiner may have been
                    # skipped. That's handled by refine_count eval.
                    _close_iteration()
                current.launches += 1
                state = "launching"
                started = True
            continue

        if name == "await_tasks":
            if state == "launching" and started:
                current.await_calls += 1
                state = "awaiting"
            else:
                current.await_calls += 1
                current.order_ok = False
            continue

        if name == "evaluate_timing":
            if state == "awaiting" and current.await_calls == 1:
                current.evaluate_calls += 1
                state = "evaluated"
            else:
                current.evaluate_calls += 1
                current.order_ok = False
                if state == "launching":
                    # evaluate without any await — still close the
                    # iteration so we don't lose track.
                    state = "evaluated"
            continue

        if name == "refine_scenario":
            if state == "evaluated":
                current.refines += 1
                args = call.get("args") or {}
                report = args.get("timing_report")
                current.refine_has_report.append(bool(report))
            else:
                current.refines += 1
                current.refine_has_report.append(False)
                current.order_ok = False
            continue

        if name == "write_file":
            # write_file is allowed anywhere; does not affect shape.
            continue

        # Any other tool name inside an iteration is permitted (e.g.
        # write_todos). Do not fail the shape check on it.

    # Close the trailing iteration if it reached at least the evaluate
    # step (otherwise it represents an in-flight, unfinished iteration
    # which is a shape violation).
    if current.evaluate_calls > 0 or current.launches > 0 or current.refines > 0:
        if current.evaluate_calls == 0:
            current.order_ok = False
        iterations.append(current)

    shape_ok = all(
        it.order_ok
        and it.launches >= 1
        and it.await_calls == 1
        and it.evaluate_calls == 1
        for it in iterations
    )
    return iterations, shape_ok


class TimingLoopTrajectoryEvaluator(Evaluator[Any, Any]):
    """Check timing-loop trajectory shape and iteration counts."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[Any, Any],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}

        expected_iterations = metadata.get("expected_iterations")
        if not isinstance(expected_iterations, int) or expected_iterations <= 0:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        "metadata['expected_iterations'] must be a positive int"
                    ),
                    label="timing_loop.missing_config",
                )
            ]

        expects_pass = bool(metadata.get("expects_pass", True))
        expects_delegation = bool(metadata.get("expects_delegation", False))
        max_iterations = int(metadata.get("max_iterations", _DEFAULT_MAX_ITERATIONS))

        # Default refine count: one refine per failed iteration.
        default_refines = (
            expected_iterations - 1 if expects_pass else expected_iterations
        )
        expected_refines = int(metadata.get("expected_refines", default_refines))

        calls = _extract_calls(evaluation_case.actual_trajectory)
        if calls is None:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="actual_trajectory must be list[dict] of tool calls",
                    label="timing_loop.missing_actual",
                )
            ]

        iterations, shape_ok = _split_iterations(calls)
        iteration_count = len(iterations)
        total_refines = sum(it.refines for it in iterations)
        refine_inputs_ok = all(
            all(it.refine_has_report) for it in iterations if it.refines
        )

        outputs: list[EvaluationOutput] = []

        count_ok = iteration_count == expected_iterations and iteration_count <= max_iterations
        outputs.append(
            EvaluationOutput(
                score=1.0 if count_ok else 0.0,
                test_pass=count_ok,
                reason=(
                    f"PASS {iteration_count} iteration(s), expected {expected_iterations}"
                    if count_ok
                    else (
                        f"FAIL {iteration_count} iteration(s), expected "
                        f"{expected_iterations} (cap {max_iterations})"
                    )
                ),
                label="timing_loop.iteration_count",
            )
        )

        outputs.append(
            EvaluationOutput(
                score=1.0 if shape_ok else 0.0,
                test_pass=shape_ok,
                reason=(
                    "PASS every iteration matches "
                    "launch_audio_render+ → await_tasks → evaluate_timing"
                    if shape_ok
                    else "FAIL iteration shape violated (see iteration records)"
                ),
                label="timing_loop.shape",
            )
        )

        refine_count_ok = total_refines == expected_refines
        outputs.append(
            EvaluationOutput(
                score=1.0 if refine_count_ok else 0.0,
                test_pass=refine_count_ok,
                reason=(
                    f"PASS {total_refines} refine_scenario call(s), expected {expected_refines}"
                    if refine_count_ok
                    else (
                        f"FAIL {total_refines} refine_scenario call(s), "
                        f"expected {expected_refines}"
                    )
                ),
                label="timing_loop.refine_count",
            )
        )

        if total_refines:
            outputs.append(
                EvaluationOutput(
                    score=1.0 if refine_inputs_ok else 0.0,
                    test_pass=refine_inputs_ok,
                    reason=(
                        "PASS every refine_scenario carried a non-empty timing_report"
                        if refine_inputs_ok
                        else "FAIL at least one refine_scenario missing timing_report"
                    ),
                    label="timing_loop.refine_inputs",
                )
            )

        delegation_present = any(_is_escalation_delegation(call) for call in calls)
        delegation_ok = delegation_present == expects_delegation
        outputs.append(
            EvaluationOutput(
                score=1.0 if delegation_ok else 0.0,
                test_pass=delegation_ok,
                reason=(
                    "PASS escalation delegation "
                    + ("present as expected" if delegation_present else "absent as expected")
                    if delegation_ok
                    else (
                        "FAIL escalation delegation "
                        + (
                            "present but not expected"
                            if delegation_present
                            else "expected but absent"
                        )
                    )
                ),
                label="timing_loop.delegation",
            )
        )

        return outputs


__all__ = ["TimingLoopTrajectoryEvaluator"]
