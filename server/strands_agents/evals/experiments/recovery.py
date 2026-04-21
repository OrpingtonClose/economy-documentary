"""Recovery experiment — exercises the 12a classifier + 12b remanifester.

Runs both agents' tools end-to-end through a deterministic ``recovery_task``
so CI can validate behaviour without LLM credentials. The experiment
factory returns the hard-gate :class:`ContractComplianceEvaluator` stack
by default; an optional ``judge_model`` kwarg layers on
:class:`EscalationDecisionEvaluator` for local / staging runs that have
API keys.

See ``docs/strands-migration/components/12-recovery-agents.md`` §evals.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from contracts import (
    RECOVERY_CLASSIFIER_CONTRACT,
    RECOVERY_REMANIFESTER_CONTRACT,
)
from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    EscalationDecisionEvaluator,
)
from strands_agents.hooks.recovery_logger import RecoveryLogger
from strands_agents.subagents.recovery_agents import (
    VALID_CLASSIFICATIONS,
    _PRESERVED_FIELDS,  # reused by RemanifestInvariantEvaluator
)
from strands_agents.subagents.recovery_agents import (
    classify as _classify_tool,
)
from strands_agents.subagents.recovery_agents import (
    diff_concept as _diff_concept_tool,
)
from strands_agents.subagents.recovery_agents import (
    propose_revised_concept as _propose_revised_concept_tool,
)

RECOVERY_EXPERIMENT_NAME = "recovery_agents"


# ---------------------------------------------------------------------------
# Invoke the underlying @tool callables directly (tests & CI)
# ---------------------------------------------------------------------------
#
# Strands wraps @tool functions in DecoratedFunctionTool instances whose
# __call__ does not forward positional args; the test-layer invokes the
# original function object via ``.original_function`` where available.


def _unwrap(tool: Any) -> Any:
    """Return the raw callable for a ``@tool``-decorated function."""
    for attr in ("original_function", "func", "_func"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            return fn
    if callable(tool):
        return tool
    raise TypeError(f"cannot unwrap tool: {tool!r}")


_classify = _unwrap(_classify_tool)
_propose = _unwrap(_propose_revised_concept_tool)
_diff = _unwrap(_diff_concept_tool)


# ---------------------------------------------------------------------------
# Cases — per docs/strands-migration/components/12-recovery-agents.md §evals
# ---------------------------------------------------------------------------


def _base_concept(scene_id: str = "scene_01") -> dict[str, Any]:
    return {
        "phrase_id": f"phr_{scene_id}",
        "scene_id": scene_id,
        "duration_sec": 4.0,
        "style_lock_applied": True,
        "prompt": "wide shot of a city skyline at dusk",
        "negative_prompt": "",
        "camera_movement": "",
        "shot_type": "wide",
    }


def _style_lock() -> dict[str, Any]:
    return {
        "style": "cinematic_documentary",
        "camera_movement": "slow push in",
    }


_CASES: list[Case] = [
    Case(
        name="cuda_oom",
        input={
            "error": "RuntimeError: CUDA out of memory trying to allocate 8GB",
            "recent_history": [],
            "concept": _base_concept(),
        },
        expected_output={"classification": "transient", "remanifested": False},
        expected_trajectory=["classify"],
        metadata={
            "flow": "classify_only",
            "expected_action": "retry",
            "artifact_id": "scene_01",
        },
    ),
    Case(
        name="connection_reset",
        input={
            "error": "urllib3.exceptions.ProtocolError: connection reset by peer",
            "recent_history": [],
            "concept": _base_concept(),
        },
        expected_output={"classification": "transient", "remanifested": False},
        expected_trajectory=["classify"],
        metadata={
            "flow": "classify_only",
            "expected_action": "retry",
            "artifact_id": "scene_01",
        },
    ),
    Case(
        name="wrong_style_output",
        input={
            "error": "output style doesn't match cinematic_documentary",
            "recent_history": [],
            "concept": _base_concept(),
        },
        expected_output={"classification": "fixable", "remanifested": True},
        expected_trajectory=["classify", "propose_revised_concept", "diff_concept"],
        metadata={
            "flow": "full_recovery",
            "expected_action": "fix",
            "artifact_id": "scene_01",
        },
    ),
    Case(
        name="prompt_too_vague",
        input={
            "error": "generation incoherent; prompt too vague",
            "recent_history": [],
            "concept": _base_concept(),
        },
        expected_output={"classification": "fixable", "remanifested": True},
        expected_trajectory=["classify", "propose_revised_concept", "diff_concept"],
        metadata={
            "flow": "full_recovery",
            "expected_action": "fix",
            "artifact_id": "scene_01",
        },
    ),
    Case(
        name="same_error_3x",
        input={
            "error": "output style doesn't match cinematic_documentary",
            "recent_history": [
                {"error": "output style doesn't match cinematic_documentary"},
                {"error": "output style doesn't match cinematic_documentary"},
            ],
            "concept": _base_concept(),
        },
        expected_output={"classification": "persistent", "remanifested": False},
        expected_trajectory=["classify"],
        metadata={
            "flow": "classify_only",
            "expected_action": "escalate",
            "artifact_id": "scene_01",
        },
    ),
    Case(
        name="worker_500_all",
        input={
            "error": "all workers returned 500 from /ltx/generate",
            "recent_history": [],
            "concept": _base_concept(),
        },
        expected_output={"classification": "catastrophic", "remanifested": False},
        expected_trajectory=["classify"],
        metadata={
            "flow": "classify_only",
            "expected_action": "abort",
            "artifact_id": "scene_01",
        },
    ),
    Case(
        name="fixable_then_pass",
        input={
            "error": "generation incoherent; prompt too vague",
            "recent_history": [
                {"error": "generation incoherent; prompt too vague"}
            ],
            "concept": _base_concept(),
        },
        expected_output={"classification": "fixable", "remanifested": True},
        expected_trajectory=["classify", "propose_revised_concept", "diff_concept"],
        metadata={
            "flow": "full_recovery",
            "expected_action": "fix",
            "artifact_id": "scene_01",
        },
    ),
    Case(
        name="recovery_log_integrity",
        input={
            "error": "QA rejected: content_mismatch",
            "recent_history": [],
            "concept": _base_concept("scene_07"),
        },
        expected_output={"classification": "fixable", "remanifested": True},
        expected_trajectory=["classify", "propose_revised_concept", "diff_concept"],
        metadata={
            "flow": "full_recovery",
            "expected_action": "fix",
            "artifact_id": "scene_07",
            "expect_exactly_one_log_per_agent": True,
        },
    ),
]


# ---------------------------------------------------------------------------
# Task — exercises the tools in the order the agents would
# ---------------------------------------------------------------------------


def recovery_task(case: Case) -> dict[str, Any]:
    """Run the recovery tools for ``case`` and return a task envelope.

    The envelope shape mirrors ``assembly_task`` / ``scenario_task``:
    ``{"output": ..., "metadata": ..., "trajectory": [...]}``. The
    ``output`` dict doubles as the ``actual_output`` passed into both
    :class:`ContractComplianceEvaluator` instances (classifier +
    remanifester).

    Args:
        case: Strands-evals :class:`Case` carrying ``error``,
            ``recent_history``, ``concept`` in ``input``.

    Returns:
        Task envelope consumable by ``Experiment.run_evaluations``.
    """
    case_input: dict[str, Any] = case.input  # type: ignore[assignment]
    metadata = dict(case.metadata or {})
    artifact_id = str(metadata.get("artifact_id", "unknown"))
    flow = metadata.get("flow", "classify_only")

    logger = RecoveryLogger()
    trajectory: list[str] = []

    trajectory.append("classify")
    classification = _classify(
        case_input["error"],
        case_input.get("recent_history", []),
        case_input.get("concept", {}),
    )
    logger.record_classification(artifact_id, classification)

    revised_concept: dict[str, Any] | None = None
    diff_payload: dict[str, Any] | None = None
    if classification["class"] == "fixable" and flow == "full_recovery":
        trajectory.append("propose_revised_concept")
        revised_concept = _propose(
            case_input["concept"],
            case_input["error"],
            classification["hint"],
            metadata.get("style_lock") or _style_lock(),
        )
        trajectory.append("diff_concept")
        diff_payload = _diff(case_input["concept"], revised_concept)
        logger.record_remanifestation(artifact_id, diff_payload)

    classifier_state: dict[str, Any] = {
        "recovery_event": {
            "error": case_input["error"],
            "concept_id": case_input.get("concept", {}).get("scene_id"),
        },
        "classification": classification,
    }

    remanifester_state: dict[str, Any] = {
        "classification": classification,
        "original_concept": case_input.get("concept", {}),
        "revised_concept": revised_concept or "(not yet generated)",
    }

    output: dict[str, Any] = {
        "classifier_state": classifier_state,
        "remanifester_state": remanifester_state,
        "classification": classification["class"],
        "remanifested": revised_concept is not None,
        "recovery_log": logger.entries(),
        "revised_concept": revised_concept,
        "diff": diff_payload,
        # Convenience alias used by EscalationDecisionEvaluator. ``action``
        # is derived from ``classification`` and the expected-action rubric
        # in the case metadata.
        "action": _classification_to_action(classification["class"]),
        "reasoning": classification.get("reasoning", ""),
    }

    return {
        "output": output,
        "trajectory": trajectory,
        "metadata": {
            **metadata,
            "diagnostic": {
                "error_class": classification["class"],
                "retry_count": classification.get("repeat_count", 0),
                "signals": classification.get("signals", []),
            },
        },
    }


def _classification_to_action(cls: str) -> str:
    if cls == "transient":
        return "retry"
    if cls == "fixable":
        return "fix"
    if cls == "persistent":
        return "escalate"
    if cls == "catastrophic":
        return "abort"
    return "skip"


# ---------------------------------------------------------------------------
# Deterministic evaluators local to component 12
# ---------------------------------------------------------------------------


class RemanifestInvariantEvaluator(
    Evaluator[dict[str, Any], dict[str, Any]]
):
    """Checks the remanifestation output honors its hard invariants.

    Rubric (all hard):
        * Preserves ``phrase_id``, ``scene_id``, ``duration_sec``,
          ``style_lock_applied`` from the original concept.
        * At least one of ``prompt`` / ``negative_prompt`` /
          ``camera_movement`` changed.
        * ``revised_concept.style_lock_applied`` is ``True``.

    Scoring is deterministic: per-clause 1.0 / 0.0 with a ``test_pass``
    threshold of ``score == 1.0``. Cases that did not remanifest (e.g.
    ``transient`` classifications) return a single skip clause.
    """

    LABEL = "remanifest_invariant"

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        revised = actual.get("revised_concept")
        if not revised:
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    label=f"{self.LABEL}.not_applicable",
                    reason="case did not remanifest; invariant skipped",
                )
            ]
        original = (
            evaluation_case.input.get("concept", {})  # type: ignore[union-attr]
            if isinstance(evaluation_case.input, dict)
            else {}
        )
        outputs: list[EvaluationOutput] = []
        for field in _PRESERVED_FIELDS:
            if field in original:
                passed = revised.get(field) == original.get(field)
                outputs.append(
                    EvaluationOutput(
                        score=1.0 if passed else 0.0,
                        test_pass=passed,
                        label=f"{self.LABEL}.preserved.{field}",
                        reason=(
                            f"'{field}' preserved"
                            if passed
                            else f"'{field}' changed: "
                            f"{original.get(field)!r} -> {revised.get(field)!r}"
                        ),
                    )
                )
        changed_fields = {
            k
            for k in set(original) | set(revised)
            if original.get(k) != revised.get(k)
        }
        meaningful = changed_fields & {
            "prompt",
            "negative_prompt",
            "camera_movement",
        }
        outputs.append(
            EvaluationOutput(
                score=1.0 if meaningful else 0.0,
                test_pass=bool(meaningful),
                label=f"{self.LABEL}.actionable_change",
                reason=(
                    f"changed: {sorted(meaningful)}"
                    if meaningful
                    else "no actionable field changed"
                ),
            )
        )
        outputs.append(
            EvaluationOutput(
                score=1.0 if revised.get("style_lock_applied") else 0.0,
                test_pass=bool(revised.get("style_lock_applied")),
                label=f"{self.LABEL}.style_lock_applied",
                reason=(
                    "style_lock_applied=True"
                    if revised.get("style_lock_applied")
                    else "style_lock_applied missing or False"
                ),
            )
        )
        # Audit: the log should carry exactly one remanifestation entry
        # per remanifested case when the case opted in.
        if metadata.get("expect_exactly_one_log_per_agent"):
            log = list(actual.get("recovery_log", []))
            classifier_entries = [e for e in log if e.get("agent") == "classifier"]
            remanifester_entries = [
                e for e in log if e.get("agent") == "remanifester"
            ]
            ok = len(classifier_entries) == 1 and len(remanifester_entries) == 1
            outputs.append(
                EvaluationOutput(
                    score=1.0 if ok else 0.0,
                    test_pass=ok,
                    label=f"{self.LABEL}.log_integrity",
                    reason=(
                        f"log: {len(classifier_entries)} classifier(s), "
                        f"{len(remanifester_entries)} remanifester(s)"
                    ),
                )
            )
        return outputs


class ClassificationVocabularyEvaluator(
    Evaluator[dict[str, Any], dict[str, Any]]
):
    """Asserts the classifier emitted one of the 4 valid labels.

    Separate from :class:`EscalationDecisionEvaluator` because that
    evaluator requires an LLM; this one is a deterministic sanity check
    that runs in every CI job.
    """

    LABEL = "classification_vocabulary"

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        cls = actual.get("classification")
        ok = cls in VALID_CLASSIFICATIONS
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                label=self.LABEL,
                reason=(
                    f"classification={cls!r}"
                    if ok
                    else f"classification not in vocabulary: {cls!r}"
                ),
            )
        ]


# ---------------------------------------------------------------------------
# Experiment factories
# ---------------------------------------------------------------------------


def _classifier_contract_task_adapter(case: Case) -> dict[str, Any]:
    """Adapter: return the classifier's post-run state as ``output``."""
    env = recovery_task(case)
    return {
        "output": env["output"]["classifier_state"],
        "metadata": env["metadata"],
    }


def _remanifester_contract_task_adapter(case: Case) -> dict[str, Any]:
    """Adapter: return the remanifester's post-run state as ``output``."""
    env = recovery_task(case)
    return {
        "output": env["output"]["remanifester_state"],
        "metadata": env["metadata"],
    }


def build_recovery_experiment(
    *,
    judge_model: Any = None,
) -> Experiment:
    """Construct the deterministic recovery experiment.

    Args:
        judge_model: Optional Strands :class:`Model` (or id) for the
            LLM-as-judge :class:`EscalationDecisionEvaluator`. Omit for
            CI / offline runs.

    Returns:
        :class:`Experiment` wired to :func:`recovery_task`.
    """
    evaluators: list[Evaluator[Any, Any]] = [
        # ContractCompliance on the classifier: the evaluator needs to
        # see ``recovery_event`` + ``classification`` in actual_output.
        # We rely on the outer ``build_recovery_experiment`` caller
        # driving ``recovery_task`` — but the ContractComplianceEvaluator
        # itself reads ``actual_output`` directly. In the CI run we
        # therefore provide a sibling experiment factory below that
        # reshapes the output; here we layer the two contracts on the
        # ``classifier_state`` / ``remanifester_state`` projections.
        ContractComplianceEvaluator(RECOVERY_CLASSIFIER_CONTRACT),
        ContractComplianceEvaluator(RECOVERY_REMANIFESTER_CONTRACT),
        ClassificationVocabularyEvaluator(),
        RemanifestInvariantEvaluator(),
    ]
    if judge_model is not None:
        evaluators.append(EscalationDecisionEvaluator(model=judge_model))
    return Experiment(cases=list(_CASES), evaluators=evaluators)


def build_recovery_classifier_contract_experiment() -> Experiment:
    """Contract-only experiment scoped to the classifier."""
    return Experiment(
        cases=list(_CASES),
        evaluators=[ContractComplianceEvaluator(RECOVERY_CLASSIFIER_CONTRACT)],
    )


def build_recovery_remanifester_contract_experiment() -> Experiment:
    """Contract-only experiment scoped to the remanifester.

    Filters to cases that remanifested (``flow == 'full_recovery'``); the
    remanifester contract is not meaningful on classify-only cases since
    they do not produce a ``revised_concept``.
    """
    filtered = [
        c
        for c in _CASES
        if (c.metadata or {}).get("flow") == "full_recovery"
    ]
    return Experiment(
        cases=filtered,
        evaluators=[ContractComplianceEvaluator(RECOVERY_REMANIFESTER_CONTRACT)],
    )


__all__ = [
    "ClassificationVocabularyEvaluator",
    "RECOVERY_EXPERIMENT_NAME",
    "RemanifestInvariantEvaluator",
    "build_recovery_classifier_contract_experiment",
    "build_recovery_experiment",
    "build_recovery_remanifester_contract_experiment",
    "recovery_task",
]
