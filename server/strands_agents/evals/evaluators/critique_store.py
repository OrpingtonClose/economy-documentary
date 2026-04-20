"""CritiqueStoreEvaluator — surface QA verdicts from the critique store.

Reads :class:`ArtifactCritiqueRecord` entries written by the critic +
QA jury (``server/critique/store.py``) and maps the worst verdict on
each record to a Strands :class:`EvaluationOutput`.

Input shape
-----------
``EvaluationData`` with:

* ``input``: the ``artifact_id`` (``str``).
* ``metadata[`artifact_type`]``: required — one of ``ArtifactType``.

Output
------
One :class:`EvaluationOutput` per record read, scored by
``QaVerdictStatus``: ``pass`` → 1.0, ``warn`` → 0.75,
``escalate`` → 0.5, ``fail`` → 0.0. ``test_pass=True`` for
``pass``/``warn`` only (``CUSTOM_EVALUATORS.md`` §7).
"""

from __future__ import annotations

from typing import Any, Optional

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from critique.record import ArtifactCritiqueRecord, QaVerdictStatus
from critique.store import ArtifactCritiqueStore, get_critique_store

_VERDICT_SCORES: dict[str, float] = {
    "pass": 1.0,
    "warn": 0.75,
    "escalate": 0.5,
    "fail": 0.0,
}

_PASS_VERDICTS = frozenset({"pass", "warn"})


class CritiqueStoreEvaluator(Evaluator[str, dict[str, Any]]):
    """Bridge the persisted critique store into the Evaluator protocol."""

    def __init__(self, store: Optional[ArtifactCritiqueStore] = None) -> None:
        super().__init__()
        self._store = store

    def evaluate(
        self,
        evaluation_case: EvaluationData[str, dict[str, Any]],
    ) -> list[EvaluationOutput]:
        artifact_id = evaluation_case.input
        metadata = evaluation_case.metadata or {}
        artifact_type = metadata.get("artifact_type")

        if not artifact_id:
            return [_fail("missing artifact_id")]
        if not artifact_type:
            return [_fail("missing metadata['artifact_type']")]

        store = self._store or get_critique_store()
        record = store.read(artifact_type=artifact_type, artifact_id=artifact_id)
        if record is None:
            return [
                _fail(f"no critique record for {artifact_type}:{artifact_id}")
            ]

        return [_record_to_output(record)]


def _record_to_output(record: ArtifactCritiqueRecord) -> EvaluationOutput:
    verdict: QaVerdictStatus = record.worst_qa()
    score = _VERDICT_SCORES.get(verdict, 0.0)
    test_pass = verdict in _PASS_VERDICTS
    reason = (
        f"{verdict.upper()} "
        f"{record.artifact_type}:{record.artifact_id}@{record.iteration} "
        f"({len(record.qa_results)} QA verdicts)"
    )
    return EvaluationOutput(
        score=score,
        test_pass=test_pass,
        reason=reason,
        label=f"critique.{record.artifact_type}.{verdict}",
    )


def _fail(reason: str) -> EvaluationOutput:
    return EvaluationOutput(
        score=0.0,
        test_pass=False,
        reason=f"FAIL critique_store: {reason}",
        label="critique.missing",
    )
