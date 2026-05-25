"""ContractComplianceEvaluator — stage contract validator.

Checks a :class:`StageContract` (``server/contracts.py``) against a
post-run state dict and artifact root. Service health is NOT checked
here — that belongs to a live runtime precondition hook; this
evaluator runs in CI and is deterministic.

Input shape
-----------
``EvaluationData`` with:

* ``actual_output``: a dict representing the pipeline state after the
  stage ran. Keys named in ``contract.required_state`` and
  ``contract.produced_state`` are expected to hold real values
  (non-empty, not placeholder strings).
* ``metadata[`artifact_root`]`` (optional): filesystem root against
  which ``contract.produced_artifacts`` globs are resolved. Defaults
  to ``/tmp/documentary-pipeline``.

Output
------
One :class:`EvaluationOutput` per contract clause
(``required_state.<key>``, ``produced_state.<key>``,
``produced_artifacts.<glob>``). Hard gate: any failure fails the
case per ``CUSTOM_EVALUATORS.md`` §5.
"""

from __future__ import annotations

import glob
import os
from typing import Any

from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput  # type: ignore[import-not-found]

from contracts import StageContract

_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "[]",
        "{}",
        "(not yet analyzed)",
        "(not yet generated)",
        "(not yet evaluated)",
    }
)

_DEFAULT_ARTIFACT_ROOT = "/tmp/documentary-pipeline"


class ContractComplianceEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Validate a ``StageContract`` against a state+artifact snapshot.

    Args:
        contract: The :class:`StageContract` to validate. Cloned into
            the evaluator at construction so one evaluator instance
            corresponds to one stage.
    """

    def __init__(self, contract: StageContract) -> None:
        super().__init__()
        self._contract = contract

    @property
    def contract(self) -> StageContract:
        return self._contract

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        state = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        artifact_root = str(metadata.get("artifact_root", _DEFAULT_ARTIFACT_ROOT))

        outputs: list[EvaluationOutput] = []

        for key in self._contract.required_state:
            outputs.append(
                _state_check(state, key, clause=f"required_state.{key}")
            )

        for key in self._contract.produced_state:
            outputs.append(
                _state_check(state, key, clause=f"produced_state.{key}")
            )

        for pattern in self._contract.produced_artifacts:
            outputs.append(_artifact_check(artifact_root, pattern))

        if not outputs:
            outputs.append(
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason=f"contract '{self._contract.name}' has no clauses",
                    label=f"contract.{self._contract.name}.empty",
                )
            )

        return outputs


def _state_check(state: dict[str, Any], key: str, *, clause: str) -> EvaluationOutput:
    val = state.get(key, "")
    val_str = str(val).strip() if val is not None else ""
    passed = val_str not in _PLACEHOLDER_VALUES
    reason = (
        f"'{key}' populated"
        if passed
        else f"'{key}' empty or placeholder: '{val_str[:80]}'"
    )
    return EvaluationOutput(
        score=1.0 if passed else 0.0,
        test_pass=passed,
        reason=f"{'PASS' if passed else 'FAIL'} {clause}: {reason}",
        label=clause,
    )


def _artifact_check(root: str, pattern: str) -> EvaluationOutput:
    label = f"produced_artifacts.{pattern}"
    full = os.path.join(root, pattern)
    matches = glob.glob(full)
    if not matches:
        return EvaluationOutput(
            score=0.0,
            test_pass=False,
            reason=f"FAIL {label}: no artifacts matched {full}",
            label=label,
        )
    # Mirror contracts.validate_postconditions: 0-byte artifacts are
    # treated as a failure (truncated writes, crashed workers).
    empty = [m for m in matches if os.path.getsize(m) == 0]
    if empty:
        return EvaluationOutput(
            score=0.0,
            test_pass=False,
            reason=f"FAIL {label}: {len(empty)} empty file(s): {empty[:3]}",
            label=label,
        )
    return EvaluationOutput(
        score=1.0,
        test_pass=True,
        reason=f"PASS {label}: {len(matches)} artifact(s) matched",
        label=label,
    )
