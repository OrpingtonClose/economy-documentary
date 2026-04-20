"""PipelineTrajectoryEvaluator — deterministic tool-sequence matching.

Validates that the DeepAgent orchestration transcript contains a given
ordered subsequence of tool calls. Orchestration evals read the
LangGraph ``AgentState`` transcript (see
``EVAL_ARCHITECTURE.md`` §7.2), not a Strands :class:`Session`, so this
evaluator operates on a simpler, pre-extracted list of tool-call names.

Input shape
-----------
``EvaluationData`` with:

* ``actual_trajectory``: one of

    * ``list[str]`` — tool call names in the order they were emitted.
    * ``list[dict]`` — tool-call records with a ``"name"`` key.
    * ``strands_evals.types.trace.Session`` — a Strands session
      (tool-execution spans are flattened in order).

* ``metadata[`expected_tool_sequence`]`` (required): the ordered list
  of tool names that must appear as a subsequence. Missing tools fail
  the case with ``test_pass=False``.
* ``metadata[`strict_order`]`` (optional, default ``True``): when
  ``False``, the expected tools may appear in any order.

Output
------
Two :class:`EvaluationOutput` entries:

* ``trajectory.coverage`` — fraction of expected tools present.
* ``trajectory.order`` — ``1.0`` if the expected sequence appears in
  order, ``0.0`` otherwise. Skipped when ``strict_order`` is ``False``.

Hard gate: both must pass when ``strict_order`` is ``True``.
"""

from __future__ import annotations

from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import Session, ToolExecutionSpan


class PipelineTrajectoryEvaluator(Evaluator[Any, Any]):
    """Check a tool-call trajectory against an expected subsequence."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[Any, Any],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}
        expected = list(metadata.get("expected_tool_sequence") or [])
        strict_order = bool(metadata.get("strict_order", True))

        if not expected:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="metadata['expected_tool_sequence'] missing or empty",
                    label="trajectory.missing_expected",
                )
            ]

        actual_names = _extract_tool_names(evaluation_case.actual_trajectory)
        if actual_names is None:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="actual_trajectory missing or unsupported type",
                    label="trajectory.missing_actual",
                )
            ]

        # Count-aware coverage: duplicates in ``expected`` must be matched by
        # duplicates in ``actual_names``. A plain ``name not in actual_names``
        # membership test reports ["a","a","b"] vs ["a","b"] as full coverage,
        # silently passing cases where a tool was expected to run twice.
        remaining = list(actual_names)
        missing: list[str] = []
        for name in expected:
            try:
                remaining.remove(name)
            except ValueError:
                missing.append(name)
        coverage = (len(expected) - len(missing)) / len(expected)
        coverage_output = EvaluationOutput(
            score=coverage,
            test_pass=not missing,
            reason=(
                f"PASS all {len(expected)} expected tool(s) present"
                if not missing
                else f"FAIL missing tool(s): {missing}"
            ),
            label="trajectory.coverage",
        )

        if not strict_order:
            return [coverage_output]

        order_ok = _is_subsequence(expected, actual_names)
        order_output = EvaluationOutput(
            score=1.0 if order_ok else 0.0,
            test_pass=order_ok,
            reason=(
                f"PASS expected subsequence {expected} found in order"
                if order_ok
                else f"FAIL expected {expected} not a subsequence of {actual_names}"
            ),
            label="trajectory.order",
        )
        return [coverage_output, order_output]


def _extract_tool_names(trajectory: Any) -> list[str] | None:
    if trajectory is None:
        return None
    if isinstance(trajectory, Session):
        names: list[str] = []
        for trace in trajectory.traces:
            for span in trace.spans:
                if isinstance(span, ToolExecutionSpan):
                    names.append(span.tool_call.name)
        return names
    if isinstance(trajectory, list):
        result: list[str] = []
        for item in trajectory:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict) and "name" in item:
                result.append(str(item["name"]))
            else:
                return None
        return result
    return None


def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
    it = iter(actual)
    return all(name in it for name in expected)
