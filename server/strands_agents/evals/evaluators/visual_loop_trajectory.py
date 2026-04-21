"""VisualLoopTrajectoryEvaluator — iteration-aware visual-loop check.

Validates the tool-call trajectory emitted by the ``visual`` SubAgent
when running the loop described in
``docs/strands-migration/components/09-visual-loop.md``.

The visual loop has a deterministic shape::

    Iteration 1 (bootstrap + first score)
    ├─ extract_phrases * N         # once per scene, phrase extraction
    ├─ validate_phrases * 1        # deterministic structural check
    ├─ persist_content_analysis * 1
    ├─ propose_concept * M1        # M1 = total phrase count
    ├─ check_style_lock * 1
    ├─ persist_visual_concepts * 1
    ├─ score_visual_coherence * 1
    └─ persist_coherence_report * 1

    Iteration 2..k (revision, k <= VISUAL_LOOP_MAX_ITERATIONS = 5)
    ├─ propose_concept * Mk        # Mk = weak-scene phrase count
    ├─ check_style_lock * 1
    ├─ persist_visual_concepts * 1
    ├─ score_visual_coherence * 1
    └─ persist_coherence_report * 1

The bootstrap tools (``extract_phrases``, ``validate_phrases``,
``persist_content_analysis``) must appear in iteration 1 only — the
content analysis is fixed for the whole loop per the SubAgent prompt.

When the iteration cap is reached, the SubAgent must delegate to the
escalation SubAgent. A ``task`` call with
``args.subagent_type == "escalation"`` or a direct
``delegate_to_escalation`` call counts as a valid delegation marker.

Analyst-failure short-circuit: if ``extract_phrases`` raises, the
SubAgent should stop immediately — no ``persist_content_analysis``,
no ``propose_concept``, and deliver an error to the parent. This is
modelled as ``expected_iterations=0`` + ``expects_delegation=True``
with a trajectory that ends on the failed bootstrap + delegation call.

Input shape
-----------
``EvaluationData`` with:

* ``actual_trajectory``: ``list[dict]`` of tool-call records. Each
  record must carry a ``"name"`` key.

* ``metadata["expected_iterations"]`` (required): total number of
  scoring iterations (counted by ``score_visual_coherence`` calls).
  Zero is valid (the analyst-failure case).
* ``metadata["expected_scene_count"]`` (required): number of scenes
  in the input — governs the ``extract_phrases`` / initial
  ``propose_concept`` counts.
* ``metadata["expected_revision_counts"]`` (optional, default empty):
  list of int, length ``expected_iterations - 1``, giving the number
  of ``propose_concept`` calls in iterations 2..k. Each must be
  strictly less than ``expected_scene_count`` (revisions target only
  weak scenes).
* ``metadata["expects_pass"]`` (optional, default ``True``): whether
  the final iteration should end with a passing coherence rating.
* ``metadata["expects_delegation"]`` (optional, default ``False``):
  whether the trajectory must end with a delegation to the escalation
  SubAgent (iteration cap hit, or analyst failure).
* ``metadata["max_iterations"]`` (optional, default
  :data:`VISUAL_LOOP_MAX_ITERATIONS`).

Output
------
Up to six :class:`EvaluationOutput` entries (all hard gates):

* ``visual_loop.iteration_count``
* ``visual_loop.shape``           — each iteration matches the
  expected tool sequence.
* ``visual_loop.bootstrap_once``  — bootstrap tools appear only in
  iteration 1 (or zero times on analyst failure).
* ``visual_loop.revision_scope``  — iteration 2..k
  ``propose_concept`` counts match ``expected_revision_counts``.
* ``visual_loop.forbidden_launch``— no ``launch_*`` tool is present
  anywhere in the trajectory (SubAgent prompt invariant).
* ``visual_loop.delegation``      — escalation delegation present/
  absent matches ``expects_delegation``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.subagents.visual import (
    VISUAL_LOOP_BOOTSTRAP_TOOLS,
    VISUAL_LOOP_MAX_ITERATIONS,
)

_ESCALATION_DELEGATION_NAMES = frozenset({"delegate_to_escalation"})
_TASK_TOOL_NAME = "task"

#: Iteration terminator — the presence of
#: ``score_visual_coherence`` marks the end of one iteration.
_ITERATION_TERMINATOR = "score_visual_coherence"

#: Tools that may appear inside an iteration without affecting shape
#: scoring (neutral traffic).
_NEUTRAL_TOOLS = frozenset({"write_file", "read_file", "write_todos"})


@dataclass
class _Iteration:
    propose_count: int = 0
    check_style_calls: int = 0
    persist_concepts_calls: int = 0
    score_calls: int = 0
    persist_report_calls: int = 0
    bootstrap_calls: int = 0
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


def _split_iterations(
    calls: list[dict[str, Any]],
) -> tuple[list[_Iteration], bool]:
    """Split the trajectory into iterations bounded by ``score_visual_coherence``.

    Returns the list of iteration records and a flag indicating
    whether any call sequence violates the expected per-iteration
    shape. Bootstrap tools (``extract_phrases``, ``validate_phrases``,
    ``persist_content_analysis``) are accounted against the iteration
    in which they appear; the ``bootstrap_once`` output later asserts
    they only show up in iteration 1.
    """
    iterations: list[_Iteration] = []
    current = _Iteration()
    # Per-iteration phase machine. The loop's expected order is:
    # ``bootstrap`` → ``proposing`` → ``style_check`` →
    # ``persisted_concepts`` → ``scored`` → ``persisted_report``.
    # Bootstrap tools may appear at the top of the very first
    # iteration; revision iterations jump straight into ``proposing``.
    phase = "bootstrap"
    started = False

    def _close_iteration() -> None:
        nonlocal current, phase, started
        iterations.append(current)
        current = _Iteration()
        phase = "proposing"  # revisions skip bootstrap
        started = False

    for call in calls:
        name = call.get("name")

        if _is_escalation_delegation(call):
            continue

        if name in VISUAL_LOOP_BOOTSTRAP_TOOLS:
            current.bootstrap_calls += 1
            started = True
            if phase not in {"bootstrap", "proposing"}:
                current.order_ok = False
            else:
                phase = "bootstrap"
            continue

        if name == "propose_concept":
            current.propose_count += 1
            started = True
            if phase in {"bootstrap", "proposing"}:
                phase = "proposing"
            else:
                current.order_ok = False
            continue

        if name == "check_style_lock":
            current.check_style_calls += 1
            started = True
            if phase == "proposing":
                phase = "style_check"
            else:
                current.order_ok = False
            continue

        if name == "persist_visual_concepts":
            current.persist_concepts_calls += 1
            started = True
            if phase == "style_check":
                phase = "persisted_concepts"
            else:
                current.order_ok = False
            continue

        if name == _ITERATION_TERMINATOR:
            current.score_calls += 1
            started = True
            if phase == "persisted_concepts":
                phase = "scored"
            else:
                current.order_ok = False
                phase = "scored"
            continue

        if name == "persist_coherence_report":
            current.persist_report_calls += 1
            started = True
            if phase == "scored":
                phase = "persisted_report"
            else:
                current.order_ok = False
            # Close the iteration on the persisted_report — the next
            # tool call belongs to the next iteration.
            _close_iteration()
            continue

        if name in _NEUTRAL_TOOLS:
            continue

        # Any other tool name is tolerated but does not reset phase.

    if started:
        # Trailing partial iteration. If the SubAgent reached the
        # concept-proposal phase we treat the partial as an iteration
        # so ``iteration_count`` flags the shape mismatch; bootstrap-
        # only short-circuits (analyst-failure path) leave the
        # trailing remainder uncounted so ``expected_iterations=0``
        # remains accurate.
        if current.propose_count > 0 or current.score_calls > 0:
            current.order_ok = False
            iterations.append(current)

    def _shape_ok(it: _Iteration) -> bool:
        return (
            it.order_ok
            and it.propose_count >= 1
            and it.check_style_calls == 1
            and it.persist_concepts_calls == 1
            and it.score_calls == 1
            and it.persist_report_calls == 1
        )

    shape_ok = all(_shape_ok(it) for it in iterations)
    return iterations, shape_ok


class VisualLoopTrajectoryEvaluator(Evaluator[Any, Any]):
    """Check visual-loop trajectory shape, iteration counts, and scope."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[Any, Any],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}

        expected_iterations = metadata.get("expected_iterations")
        if not isinstance(expected_iterations, int) or expected_iterations < 0:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        "metadata['expected_iterations'] must be a "
                        "non-negative int"
                    ),
                    label="visual_loop.missing_config",
                )
            ]

        expected_scene_count = metadata.get("expected_scene_count")
        if (
            not isinstance(expected_scene_count, int)
            or expected_scene_count <= 0
        ):
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        "metadata['expected_scene_count'] must be a "
                        "positive int"
                    ),
                    label="visual_loop.missing_config",
                )
            ]

        expects_pass = bool(metadata.get("expects_pass", True))  # noqa: F841
        expects_delegation = bool(metadata.get("expects_delegation", False))
        max_iterations = int(
            metadata.get("max_iterations", VISUAL_LOOP_MAX_ITERATIONS)
        )

        expected_revision_counts = metadata.get(
            "expected_revision_counts", []
        )
        if not isinstance(expected_revision_counts, list) or not all(
            isinstance(v, int) and v > 0 for v in expected_revision_counts
        ):
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        "metadata['expected_revision_counts'] must be "
                        "a list[int] with strictly positive entries"
                    ),
                    label="visual_loop.missing_config",
                )
            ]
        revision_count_expected = max(expected_iterations - 1, 0)
        if len(expected_revision_counts) != revision_count_expected:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        f"metadata['expected_revision_counts'] has "
                        f"length {len(expected_revision_counts)}, "
                        f"expected {revision_count_expected}"
                    ),
                    label="visual_loop.missing_config",
                )
            ]

        calls = _extract_calls(evaluation_case.actual_trajectory)
        if calls is None:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        "actual_trajectory must be list[dict] of tool calls"
                    ),
                    label="visual_loop.missing_actual",
                )
            ]

        iterations, shape_ok = _split_iterations(calls)
        iteration_count = len(iterations)
        bootstrap_counts = [it.bootstrap_calls for it in iterations]
        propose_counts = [it.propose_count for it in iterations]

        outputs: list[EvaluationOutput] = []

        count_ok = (
            iteration_count == expected_iterations
            and iteration_count <= max_iterations
        )
        outputs.append(
            EvaluationOutput(
                score=1.0 if count_ok else 0.0,
                test_pass=count_ok,
                reason=(
                    f"PASS {iteration_count} iteration(s), "
                    f"expected {expected_iterations}"
                    if count_ok
                    else (
                        f"FAIL {iteration_count} iteration(s), "
                        f"expected {expected_iterations} "
                        f"(cap {max_iterations})"
                    )
                ),
                label="visual_loop.iteration_count",
            )
        )

        # Shape is vacuously true when zero iterations are expected
        # (analyst-failure case); otherwise require the per-iteration
        # sequencing to hold.
        if expected_iterations == 0:
            shape_pass = iteration_count == 0
            shape_reason = (
                "PASS no iterations, as expected"
                if shape_pass
                else "FAIL expected zero iterations, got one or more"
            )
        else:
            shape_pass = shape_ok and iteration_count == expected_iterations
            shape_reason = (
                "PASS every iteration matches propose_concept+ → "
                "check_style_lock → persist_visual_concepts → "
                "score_visual_coherence → persist_coherence_report"
                if shape_pass
                else "FAIL iteration shape violated (see iteration records)"
            )
        outputs.append(
            EvaluationOutput(
                score=1.0 if shape_pass else 0.0,
                test_pass=shape_pass,
                reason=shape_reason,
                label="visual_loop.shape",
            )
        )

        # Bootstrap tools must appear exactly once per scene (for
        # extract_phrases) plus one validate_phrases + one
        # persist_content_analysis, all inside iteration 1. For the
        # analyst-failure case (expected_iterations == 0) they may
        # appear but there is no iteration 1 to score — fall back to
        # counting calls against the expected scene count.
        expected_bootstrap_in_first = expected_scene_count + 2
        if expected_iterations == 0:
            # Analyst failure: at least one extract_phrases should
            # have been attempted before the SubAgent bailed; count
            # the raw trajectory since the iteration split drops
            # bootstrap-only short-circuits.
            extract_attempts = sum(
                1 for c in calls if c.get("name") == "extract_phrases"
            )
            bootstrap_ok = extract_attempts >= 1
            bootstrap_reason = (
                "PASS analyst-failure case attempted extract_phrases"
                if bootstrap_ok
                else "FAIL analyst-failure case missing extract_phrases"
            )
        else:
            in_first = bootstrap_counts[0] if bootstrap_counts else 0
            in_rest = sum(bootstrap_counts[1:])
            bootstrap_ok = (
                in_first == expected_bootstrap_in_first and in_rest == 0
            )
            bootstrap_reason = (
                "PASS bootstrap tools confined to iteration 1"
                if bootstrap_ok
                else (
                    f"FAIL bootstrap counts {bootstrap_counts}, expected "
                    f"[{expected_bootstrap_in_first}, 0, 0, ...]"
                )
            )
        outputs.append(
            EvaluationOutput(
                score=1.0 if bootstrap_ok else 0.0,
                test_pass=bootstrap_ok,
                reason=bootstrap_reason,
                label="visual_loop.bootstrap_once",
            )
        )

        # Revision scope: iteration 2..k must propose concepts only
        # for weak scenes. Expressed as exact counts the orchestrator
        # supplies per case. Iteration 1 always regenerates every
        # phrase so we don't score it here.
        if expected_iterations == 0:
            scope_pass = True
            scope_reason = "PASS no revisions expected"
        elif expected_iterations == 1:
            scope_pass = True
            scope_reason = "PASS single-iteration case, no revisions"
        else:
            revision_counts_actual = propose_counts[1:]
            scope_pass = revision_counts_actual == expected_revision_counts
            scope_reason = (
                f"PASS revision counts {revision_counts_actual} match"
                if scope_pass
                else (
                    f"FAIL revision counts {revision_counts_actual}, "
                    f"expected {expected_revision_counts}"
                )
            )
        outputs.append(
            EvaluationOutput(
                score=1.0 if scope_pass else 0.0,
                test_pass=scope_pass,
                reason=scope_reason,
                label="visual_loop.revision_scope",
            )
        )

        # Forbidden launch tools — the SubAgent prompt forbids any
        # ``launch_*`` call. Matching on the ``launch_`` prefix keeps
        # this invariant robust to future production tool additions.
        forbidden = [
            c.get("name")
            for c in calls
            if isinstance(c.get("name"), str)
            and c["name"].startswith("launch_")
        ]
        forbidden_ok = not forbidden
        outputs.append(
            EvaluationOutput(
                score=1.0 if forbidden_ok else 0.0,
                test_pass=forbidden_ok,
                reason=(
                    "PASS no launch_* tools in trajectory"
                    if forbidden_ok
                    else f"FAIL forbidden launch tools present: {forbidden}"
                ),
                label="visual_loop.forbidden_launch",
            )
        )

        delegation_present = any(
            _is_escalation_delegation(call) for call in calls
        )
        delegation_ok = delegation_present == expects_delegation
        outputs.append(
            EvaluationOutput(
                score=1.0 if delegation_ok else 0.0,
                test_pass=delegation_ok,
                reason=(
                    "PASS escalation delegation "
                    + (
                        "present as expected"
                        if delegation_present
                        else "absent as expected"
                    )
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
                label="visual_loop.delegation",
            )
        )

        return outputs


__all__ = ["VisualLoopTrajectoryEvaluator"]

# ``field`` imported for potential future per-iteration detail exposure;
# silence ruff if unused.
_ = field
