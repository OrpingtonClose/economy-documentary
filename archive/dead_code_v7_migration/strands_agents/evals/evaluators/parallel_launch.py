"""ParallelLaunchEvaluator — deterministic concurrent-tool-launch check.

Validates that the orchestrator launches the expected ``launch_*`` tool
as a parallel batch in every iteration (i.e. all calls in one iteration
emitted on the same agent turn) rather than serialising them across
multiple round-trips. The orchestration transcript is expected to carry
an ``at_turn`` marker on each tool call
(see ``EVAL_ARCHITECTURE.md`` §7.2).

Per-batch semantics
-------------------
``expected_count`` is the number of launches expected **per batch** (e.g.
scenes per timing-loop iteration). Trajectories with multiple iterations
yield multiple batches; each one is validated independently. This keeps
the evaluator usable for single-shot launches (1 batch) and for loops
like the timing loop (N iterations × M scenes).

Input shape
-----------
``EvaluationData`` with:

* ``actual_trajectory``: ``list[dict]`` of tool-call records. Each
  record must carry a ``"name"`` key; an ``"at_turn"`` (int) is used
  to group calls into batches.
* ``metadata[`tool_name`]`` (required): the ``launch_*`` tool whose
  parallel dispatch is under test.
* ``metadata[`expected_count`]`` (required): number of concurrent
  launches expected **per batch**.
* ``metadata[`completion_tool`]`` (optional): name of the awaiter
  tool (e.g. ``await_tasks``). When supplied, a completion call at a
  turn strictly greater than every launch batch's turn is required.

Output
------
Three :class:`EvaluationOutput` entries:

* ``parallel.count`` — at least one batch detected and every batch has
  exactly ``expected_count`` launches.
* ``parallel.batched`` — every launch carries an ``at_turn`` marker and
  at least one batch was detected.
* ``parallel.awaited`` — the completion tool was invoked after every
  launch batch. Skipped when ``completion_tool`` is not supplied.

Hard gate: all supplied checks must pass.
"""

from __future__ import annotations

from typing import Any, cast

from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput  # type: ignore[import-not-found]


class ParallelLaunchEvaluator(Evaluator[Any, Any]):
    """Check that every batch of ``launch_*`` tools fires in one turn."""

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
                        "'expected_count' (launches per batch)"
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

        batches_by_turn: dict[Any, list[dict[str, Any]]] = {}
        missing_turn = 0
        for call in launches:
            at = call.get("at_turn")
            if at is None:
                missing_turn += 1
                continue
            batches_by_turn.setdefault(at, []).append(call)

        outputs: list[EvaluationOutput] = []

        wrong_size_batches = {
            turn: len(batch)
            for turn, batch in batches_by_turn.items()
            if len(batch) != expected_count
        }
        count_ok = (
            len(launches) > 0
            and missing_turn == 0
            and len(batches_by_turn) >= 1
            and not wrong_size_batches
        )
        if not launches:
            count_reason = f"FAIL {tool_name} was never dispatched"
        elif missing_turn:
            count_reason = (
                f"FAIL {missing_turn}/{len(launches)} {tool_name} calls are "
                f"missing the 'at_turn' marker"
            )
        elif wrong_size_batches:
            offenders = ", ".join(
                f"turn {turn}: {size}"
                for turn, size in sorted(wrong_size_batches.items(), key=lambda kv: str(kv[0]))
            )
            count_reason = (
                f"FAIL {tool_name} expected {expected_count} launch(es) per "
                f"batch, got mismatched batches [{offenders}]"
            )
        else:
            count_reason = (
                f"PASS {tool_name} dispatched {expected_count} launch(es) per "
                f"batch across {len(batches_by_turn)} batch(es)"
            )
        outputs.append(
            EvaluationOutput(
                score=1.0 if count_ok else 0.0,
                test_pass=count_ok,
                reason=count_reason,
                label="parallel.count",
            )
        )

        batched_ok = (
            len(launches) > 0
            and missing_turn == 0
            and len(batches_by_turn) >= 1
        )
        if not launches:
            batched_reason = (
                f"FAIL cannot verify batching — {tool_name} was never dispatched"
            )
        elif missing_turn:
            batched_reason = (
                f"FAIL {missing_turn}/{len(launches)} launch calls missing "
                f"'at_turn' marker"
            )
        else:
            batched_reason = (
                f"PASS all {len(launches)} launches carry 'at_turn' across "
                f"{len(batches_by_turn)} batch(es)"
            )
        outputs.append(
            EvaluationOutput(
                score=1.0 if batched_ok else 0.0,
                test_pass=batched_ok,
                reason=batched_reason,
                label="parallel.batched",
            )
        )

        if completion_tool:
            if not batched_ok:
                awaited_ok = False
                awaited_reason = (
                    "FAIL cannot verify completion ordering when batching "
                    "check failed"
                )
            else:
                missing_after = _batches_without_completion(
                    trajectory,
                    completion_tool=completion_tool,
                    batch_turns=sorted(batches_by_turn.keys()),
                )
                awaited_ok = not missing_after
                if awaited_ok:
                    awaited_reason = (
                        f"PASS {completion_tool} invoked after every launch batch"
                    )
                else:
                    awaited_reason = (
                        f"FAIL {completion_tool} not invoked after launch batches "
                        f"{missing_after}"
                    )
            outputs.append(
                EvaluationOutput(
                    score=1.0 if awaited_ok else 0.0,
                    test_pass=awaited_ok,
                    reason=awaited_reason,
                    label="parallel.awaited",
                )
            )

        return outputs


def _batches_without_completion(
    trajectory: list[Any],
    *,
    completion_tool: str,
    batch_turns: list[Any],
) -> list[Any]:
    """Return batch turns that lack a completion call at a strictly later turn.

    ``at_turn`` values are compared directly; callers are expected to use
    comparable (typically integer) markers. Completion calls missing an
    ``at_turn`` are ignored so a completion cannot be masked by omitting
    the marker.
    """
    completion_turns = sorted(
        cast(list[Any], [
            call.get("at_turn")
            for call in trajectory
            if isinstance(call, dict)
            and call.get("name") == completion_tool
            and call.get("at_turn") is not None
        ])
    )
    missing: list[Any] = []
    for batch_turn in batch_turns:
        if not any(ct > batch_turn for ct in completion_turns):
            missing.append(batch_turn)
    return missing
