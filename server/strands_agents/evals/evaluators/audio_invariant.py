"""AudioInvariantEvaluator — wraps audio stylistic invariants.

Bridges ``server/critique/audio_invariants.py:run_all_invariants`` into
the ``strands-agents-evals`` Evaluator protocol. Deterministic: reads
each block's WAV and reports per-invariant verdicts.

Input shape
-----------
``EvaluationData`` with:

* ``actual_output``: a dict with key ``narration_blocks`` — a list of
  dicts, each matching :class:`NarrationBlock` fields
  (``block_id``, ``wav_path``, ``scene_num``, ``voice_role``,
  ``language``, ``voice_id``).
* ``metadata`` (optional): ``target_lufs`` (float, defaults to
  ``LUFS_TARGET`` from ``critique/audio_invariants.py``),
  ``lufs_tolerance_lu`` (float, defaults to ``LUFS_TOLERANCE_LU``).

Output
------
One :class:`EvaluationOutput` per :class:`InvariantResult`. ``SKIP``
verdicts pass (a scoped ledger override is an explicit waiver).
``FAIL`` verdicts are a hard gate (``test_pass=False``) — any failure
fails the case per ``CUSTOM_EVALUATORS.md`` §2.
"""

from __future__ import annotations

from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from critique.audio_invariants import (
    LUFS_TARGET,
    LUFS_TOLERANCE_LU,
    InvariantResult,
    InvariantVerdict,
    NarrationBlock,
    run_all_invariants,
)


class AudioInvariantEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Deterministic wrapper around audio stylistic invariants."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        output = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}

        raw_blocks = output.get("narration_blocks") or []
        blocks = [_coerce_block(b) for b in raw_blocks]

        if not blocks:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="no narration_blocks supplied",
                    label="audio_invariants.empty",
                )
            ]

        results = run_all_invariants(
            blocks,
            target_lufs=float(metadata.get("target_lufs", LUFS_TARGET)),
            lufs_tolerance_lu=float(
                metadata.get("lufs_tolerance_lu", LUFS_TOLERANCE_LU)
            ),
        )

        return [_invariant_to_output(r) for r in results]


def _coerce_block(block: Any) -> NarrationBlock:
    if isinstance(block, NarrationBlock):
        return block
    if not isinstance(block, dict):
        raise TypeError(f"narration_blocks entry must be dict, got {type(block).__name__}")
    return NarrationBlock(
        block_id=str(block["block_id"]),
        wav_path=str(block["wav_path"]),
        scene_num=int(block["scene_num"]),
        voice_role=str(block["voice_role"]),
        language=str(block.get("language", "")),
        voice_id=str(block.get("voice_id", "")),
    )


def _invariant_to_output(result: InvariantResult) -> EvaluationOutput:
    passed = result.verdict != InvariantVerdict.FAIL
    score = 1.0 if passed else 0.0
    label = f"{result.name}:{result.block_id}"
    reason = f"{result.verdict.value.upper()} {result.name} ({result.block_id})"
    if result.message:
        reason = f"{reason} — {result.message}"
    return EvaluationOutput(
        score=score,
        test_pass=passed,
        reason=reason,
        label=label,
    )
