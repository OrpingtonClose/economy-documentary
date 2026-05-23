"""EscalationTaxonomyEvaluator — trajectory check for escalation triggers.

Enforces three AGENTS.md rules on the orchestrator's tool-call
trajectory:

1. **Two failures on the same scene/tool combo must escalate.**  When
   the same ``scene_id`` records two failures, the orchestrator must
   issue a ``request_escalation`` or delegate to the escalation
   SubAgent (``task(subagent_type="escalation", ...)``) before
   attempting any further ``launch_visual_production`` or
   ``launch_audio_render`` for that scene.  AGENTS.md §"Retry policy":
   "If the same scene fails twice, delegate to escalation."

2. **Two consecutive byte-identical refiner outputs must escalate.**
   A refiner that cannot change the scenes cannot converge the timing
   loop.  AGENTS.md §"Timing stage":  "If two consecutive
   ``refine_scenario`` outputs are byte-identical to the previous
   revision, stop iterating and delegate to ``escalation``."

3. **An approval rejection must re-plan, not retry with identical
   arguments.**  AGENTS.md §"Hard invariant #9":  "A ``reject`` means
   re-plan, not retry-with-same-args."

Failures, refiner no-ops, and approval rejects are marked in the
trajectory via dedicated stub tools the trajectory harness emits:

* ``record_scene_failure(scene_id, tool)``
* ``record_refiner_noop()``
* ``record_approval_reject(tool, args)``

These are orchestrator-visible hints that the corresponding event has
just occurred — an orchestrator running against real tools would see
the same signals through tool return values.  In the simulator we
inject them explicitly so the evaluator can grade the sequence.

Input shape
-----------
``EvaluationData`` with:

* ``actual_trajectory``: ``list[dict]`` of tool-call records in the
  shape :func:`strands_agents.sim.tool_call_trajectory` returns
  (``{"name": str, "args": dict, "at_turn": int}``).
* ``metadata[`expect_escalation`]`` (optional, default ``True``):
  when ``False``, the gate flips — a scripted scenario that explicitly
  does NOT require escalation passes when no escalation is found.

Output
------
One :class:`EvaluationOutput` per gate that had at least one relevant
event in the trajectory, plus always-on presence checks:

* ``escalation.two_failures_trigger`` — every scene that recorded two
  failures has a later escalation call (with no intervening retry of
  the same tool) before any further launch.  Absent if the trajectory
  has no ``record_scene_failure`` entries at all.
* ``escalation.refiner_noop_trigger`` — every ``record_refiner_noop``
  marker is followed by an escalation call before any further
  ``refine_scenario``.  Absent if no noop markers in trajectory.
* ``escalation.approval_reject_retry_with_different_args`` — every
  ``record_approval_reject`` marker is followed either by no further
  call of the rejected tool, or by a call with *different* args than
  the rejected invocation.  Absent if no reject markers in trajectory.

Gates always emit ``missing_trajectory`` if ``actual_trajectory`` is
not a list (mirrors :class:`AssemblyOrderingEvaluator` behaviour).
"""

from __future__ import annotations

import json
from typing import Any

from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput  # type: ignore[import-not-found]

_FAILURE_MARKER = "record_scene_failure"
_REFINER_NOOP_MARKER = "record_refiner_noop"
_APPROVAL_REJECT_MARKER = "record_approval_reject"

_ESCALATION_TOOL = "request_escalation"
_TASK_TOOL = "task"
_ESCALATION_SUBAGENT = "escalation"

_SCENE_RETRY_TOOLS = frozenset(
    {"launch_visual_production", "launch_audio_render"}
)
_REFINER_TOOL = "refine_scenario"


def _extract_calls(trajectory: Any) -> list[dict[str, Any]] | None:
    """Return the tool-call list, or ``None`` if trajectory is not a list.

    Entries that are not tool-call dicts (wrong type or missing
    ``name``) are skipped — the trajectory may interleave tool calls
    with other event shapes.  Matches
    :class:`AssemblyOrderingEvaluator._extract_calls`.
    """
    if not isinstance(trajectory, list):
        return None
    return [
        call
        for call in trajectory
        if isinstance(call, dict) and isinstance(call.get("name"), str)
    ]


def _is_escalation_call(call: dict[str, Any]) -> bool:
    """Does this call hand off to the escalation path?"""
    if call["name"] == _ESCALATION_TOOL:
        return True
    if call["name"] == _TASK_TOOL:
        args = call.get("args") or {}
        if isinstance(args, dict):
            subagent = args.get("subagent_type")
            return subagent == _ESCALATION_SUBAGENT
    return False


def _scene_id(call: dict[str, Any]) -> str | None:
    args = call.get("args") or {}
    if not isinstance(args, dict):
        return None
    sid = args.get("scene_id")
    return sid if isinstance(sid, str) else None


def _args_key(args: Any) -> str:
    """Stable serialisation for args equality comparisons.

    Uses ``sort_keys=True`` so dict ordering noise doesn't make two
    semantically-identical arg payloads compare unequal.  Falls back to
    ``repr`` for non-JSON-serialisable values so we never raise.
    """
    try:
        return json.dumps(args, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return repr(args)


def _grade_two_failures_trigger(
    calls: list[dict[str, Any]],
) -> EvaluationOutput | None:
    """Rule: 2+ failures on the same scene ⇒ escalate before retrying."""
    # Track per-scene counters.  When a scene hits its second failure
    # we record the index; the scene must see an escalation call before
    # any further retry tool call for that scene.
    failure_counts: dict[str, int] = {}
    second_failure_idx: dict[str, int] = {}
    escalated_scenes: set[str] = set()
    violations: list[str] = []

    for idx, call in enumerate(calls):
        name = call["name"]
        if name == _FAILURE_MARKER:
            sid = _scene_id(call)
            if sid is None:
                continue
            failure_counts[sid] = failure_counts.get(sid, 0) + 1
            if failure_counts[sid] == 2:
                second_failure_idx[sid] = idx
            continue
        if _is_escalation_call(call):
            esc_sid = _scene_id(call)
            if esc_sid is not None:
                escalated_scenes.add(esc_sid)
            else:
                # ``task(subagent_type="escalation")`` without a scene_id
                # is treated as a global escalation that covers every
                # currently-pending scene failure — same semantics as
                # AGENTS.md's "delegate to escalation" hand-off.
                escalated_scenes.update(second_failure_idx.keys())
            continue
        if name in _SCENE_RETRY_TOOLS:
            sid = _scene_id(call)
            if sid is None:
                continue
            trigger_idx = second_failure_idx.get(sid)
            if trigger_idx is None:
                continue
            if sid in escalated_scenes:
                continue
            violations.append(
                f"scene {sid!r} attempted retry via {name!r} at index {idx} "
                f"after recording two failures (second at index {trigger_idx}) "
                f"without an intervening escalation"
            )

    if not failure_counts:
        # No failure markers in this trajectory — gate not applicable.
        return None

    if violations:
        return EvaluationOutput(
            score=0.0,
            test_pass=False,
            reason="FAIL " + "; ".join(violations),
            label="escalation.two_failures_trigger",
        )

    # Presence check: every scene that hit two failures must be in the
    # escalated set by end of trajectory.  Otherwise the orchestrator
    # simply dropped the scene — also a violation.
    missing = sorted(set(second_failure_idx) - escalated_scenes)
    if missing:
        return EvaluationOutput(
            score=0.0,
            test_pass=False,
            reason=(
                f"FAIL scenes hit two failures but never escalated: {missing}"
            ),
            label="escalation.two_failures_trigger",
        )

    return EvaluationOutput(
        score=1.0,
        test_pass=True,
        reason=(
            f"PASS {len(second_failure_idx)} scene(s) hit two failures and "
            "escalated before any third retry"
        ),
        label="escalation.two_failures_trigger",
    )


def _grade_refiner_noop_trigger(
    calls: list[dict[str, Any]],
) -> EvaluationOutput | None:
    """Rule: refiner-noop marker ⇒ escalate before another refine_scenario."""
    violations: list[str] = []
    expecting_escalation = False
    noop_index: int | None = None
    pending_count = 0

    for idx, call in enumerate(calls):
        name = call["name"]
        if name == _REFINER_NOOP_MARKER:
            expecting_escalation = True
            noop_index = idx
            pending_count += 1
            continue
        if expecting_escalation:
            if _is_escalation_call(call):
                expecting_escalation = False
                continue
            if name == _REFINER_TOOL:
                violations.append(
                    f"refine_scenario at index {idx} fired after "
                    f"record_refiner_noop at index {noop_index} without an "
                    "intervening escalation"
                )
                # Reset: the violation has been recorded, keep scanning
                # for any subsequent noops in the same trajectory.
                expecting_escalation = False

    if pending_count == 0:
        return None

    if violations:
        return EvaluationOutput(
            score=0.0,
            test_pass=False,
            reason="FAIL " + "; ".join(violations),
            label="escalation.refiner_noop_trigger",
        )

    if expecting_escalation:
        return EvaluationOutput(
            score=0.0,
            test_pass=False,
            reason=(
                f"FAIL record_refiner_noop at index {noop_index} was never "
                "followed by an escalation call"
            ),
            label="escalation.refiner_noop_trigger",
        )

    return EvaluationOutput(
        score=1.0,
        test_pass=True,
        reason=(
            f"PASS {pending_count} refiner-noop marker(s) followed by "
            "escalation before any further refine_scenario"
        ),
        label="escalation.refiner_noop_trigger",
    )


def _grade_approval_reject_retry(
    calls: list[dict[str, Any]],
) -> EvaluationOutput | None:
    """Rule: reject ⇒ re-plan, not retry with identical args."""
    violations: list[str] = []
    # Track open rejects: {tool_name: {rejected_args_key: reject_idx}}.
    # A subsequent call of the same tool with the same args key is a
    # violation.  Different args close the reject.  An escalation also
    # closes the reject (re-plan delegated).
    open_rejects: dict[str, dict[str, int]] = {}
    reject_count = 0

    for idx, call in enumerate(calls):
        name = call["name"]
        if name == _APPROVAL_REJECT_MARKER:
            args = call.get("args") or {}
            if not isinstance(args, dict):
                continue
            tool = args.get("tool")
            if not isinstance(tool, str):
                continue
            # The marker may carry the rejected args either as a JSON-
            # encoded string (``rejected_args_json``) — used by
            # LangChain-backed stub tools where ``dict[str, Any]``
            # parameters would be spread as kwargs by the structured-
            # tool schema — or as a plain ``args`` dict for evaluators
            # driven directly by unit-test code. Accept both.
            rejected_args: Any
            if "rejected_args_json" in args:
                raw = args.get("rejected_args_json")
                if isinstance(raw, str):
                    try:
                        rejected_args = json.loads(raw)
                    except (TypeError, ValueError):
                        rejected_args = raw
                else:
                    rejected_args = raw
            else:
                rejected_args = args.get("args")
            key = _args_key(rejected_args)
            open_rejects.setdefault(tool, {})[key] = idx
            reject_count += 1
            continue
        if _is_escalation_call(call):
            # Escalation covers every open reject — the orchestrator
            # has handed off the re-plan.
            open_rejects.clear()
            continue
        tool_rejects = open_rejects.get(name)
        if not tool_rejects:
            continue
        call_key = _args_key(call.get("args"))
        if call_key in tool_rejects:
            reject_idx = tool_rejects[call_key]
            violations.append(
                f"{name!r} at index {idx} retried with identical args after "
                f"rejection at index {reject_idx}"
            )
            # Consume the reject so a second identical-retry is reported
            # as its own violation (it would open a fresh reject only
            # if the orchestrator recorded one).
            del tool_rejects[call_key]
        else:
            # Same tool, different args — re-plan honoured.  Clear the
            # whole tool's reject bucket because the orchestrator has
            # demonstrated it is varying its args.
            open_rejects.pop(name, None)

    if reject_count == 0:
        return None

    if violations:
        return EvaluationOutput(
            score=0.0,
            test_pass=False,
            reason="FAIL " + "; ".join(violations),
            label="escalation.approval_reject_retry_with_different_args",
        )

    return EvaluationOutput(
        score=1.0,
        test_pass=True,
        reason=(
            f"PASS {reject_count} approval-reject marker(s) were followed "
            "either by no retry, an escalation, or a retry with different args"
        ),
        label="escalation.approval_reject_retry_with_different_args",
    )


class EscalationTaxonomyEvaluator(Evaluator[Any, Any]):
    """Check escalation-taxonomy invariants on a tool-call trajectory."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[Any, Any],
    ) -> list[EvaluationOutput]:
        calls = _extract_calls(evaluation_case.actual_trajectory)
        if calls is None:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="no actual_trajectory (expected list of tool calls)",
                    label="escalation.missing_trajectory",
                )
            ]

        outputs: list[EvaluationOutput] = []
        for grader in (
            _grade_two_failures_trigger,
            _grade_refiner_noop_trigger,
            _grade_approval_reject_retry,
        ):
            result = grader(calls)
            if result is not None:
                outputs.append(result)

        if not outputs:
            # Trajectory had no escalation-relevant events at all.  This
            # is always "no violation" — we return a single presence
            # output so callers always have something to assert against.
            outputs.append(
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="PASS no escalation-trigger events in trajectory",
                    label="escalation.no_events",
                )
            )
        return outputs


__all__ = ["EscalationTaxonomyEvaluator"]
