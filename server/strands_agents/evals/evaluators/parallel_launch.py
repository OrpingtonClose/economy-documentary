"""ParallelLaunchEvaluator — deterministic concurrent-tool-launch check.

Validates that the orchestrator launched the expected ``launch_*`` tool
in a single tool-call batch (i.e. all calls emitted on the same agent
turn) rather than serialising them across multiple round-trips. The
orchestration transcript is expected to carry an ``at_turn`` marker on
each tool call (see ``EVAL_ARCHITECTURE.md`` §7.2).

Input shape
-----------
``EvaluationData`` with:

* ``actual_trajectory``: ``list[dict]`` of tool-call records. Each
  record must carry a ``"name"`` key; an ``"at_turn"`` (int) is used
  to verify batching.
* ``metadata[`tool_name`]`` (required): the ``launch_*`` tool whose
  parallel dispatch is under test.
* ``metadata[`expected_count`]`` (required): number of concurrent
  launches expected.
* ``metadata[`completion_tool`]`` (optional): name of the awaiter
  tool (e.g. ``await_tasks``). When supplied, its presence after the
  launch batch is checked.

Output
------
Three :class:`EvaluationOutput` entries:

* ``parallel.count`` — expected number of launches actually occurred.
* ``parallel.batched`` — all launches share the same ``at_turn`` marker.
* ``parallel.awaited`` — the completion tool was invoked after the
  batch. Skipped when ``completion_tool`` is not supplied.

Hard gate: all supplied checks must pass.
"""

from __future__ import annotations

from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput


class ParallelLaunchEvaluator(Evaluator[Any, Any]):
    """Check that N ``launch_*`` tools were emitted in one turn."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[Any, Any],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}
        tool_name = metadata.get("tool_name")
        expected_count = metadata.get("expected_count")
        completion_tool = metadata.get("completion_tool")

        if not tool_name or not isinstance(expected_count, int) or expected_count <= 0:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        "metadata requires 'tool_name' and positive int "
                        "'expected_count'"
                    ),
                    label="parallel.missing_config",
                )
            ]

        trajectory = evaluation_case.actual_trajectory
        if not isinstance(trajectory, list):
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="actual_trajectory must be list[dict] of tool calls",
                    label="parallel.missing_actual",
                )
            ]

        launches = [
            call
            for call in trajectory
            if isinstance(call, dict) and call.get("name") == tool_name
        ]
        outputs: list[EvaluationOutput] = []

        count_ok = len(launches) == expected_count
        outputs.append(
            EvaluationOutput(
                score=1.0 if count_ok else 0.0,
                test_pass=count_ok,
                reason=(
                    f"PASS {tool_name} dispatched {len(launches)} time(s)"
                    if count_ok
                    else (
                        f"FAIL {tool_name} dispatched {len(launches)} time(s), "
                        f"expected {expected_count}"
                    )
                ),
                label="parallel.count",
            )
        )

        turns = {call.get("at_turn") for call in launches if call.get("at_turn") is not None}
        missing_turn = sum(1 for call in launches if call.get("at_turn") is None)
        batched_ok = count_ok and missing_turn == 0 and len(turns) == 1
        if not count_ok:
            batched_reason = (
                "FAIL cannot verify batching when expected launch count is wrong"
            )
        elif missing_turn:
            batched_reason = (
                f"FAIL {missing_turn}/{len(launches)} launch calls missing 'at_turn' marker"
            )
        elif not turns:
            batched_reason = "FAIL no 'at_turn' markers on launch calls"
        elif len(turns) == 1:
            batched_reason = f"PASS all {len(launches)} launches on turn {next(iter(turns))}"
        else:
            batched_reason = f"FAIL launches spread across turns {sorted(turns)}"
        outputs.append(
            EvaluationOutput(
                score=1.0 if batched_ok else 0.0,
                test_pass=batched_ok,
                reason=batched_reason,
                label="parallel.batched",
            )
        )

        if completion_tool:
            launch_turn = next(iter(turns)) if batched_ok else None
            awaited_ok = _completion_after_batch(
                trajectory,
                completion_tool=completion_tool,
                launch_turn=launch_turn,
            )
            outputs.append(
                EvaluationOutput(
                    score=1.0 if awaited_ok else 0.0,
                    test_pass=awaited_ok,
                    reason=(
                        f"PASS {completion_tool} invoked after launch batch"
                        if awaited_ok
                        else f"FAIL {completion_tool} not invoked after launch batch"
                    ),
                    label="parallel.awaited",
                )
            )

        return outputs


def _completion_after_batch(
    trajectory: list[Any],
    *,
    completion_tool: str,
    launch_turn: int | None,
) -> bool:
    for call in trajectory:
        if not isinstance(call, dict):
            continue
        if call.get("name") != completion_tool:
            continue
        at_turn = call.get("at_turn")
        if launch_turn is None or at_turn is None:
            return True
        if at_turn > launch_turn:
            return True
    return False
