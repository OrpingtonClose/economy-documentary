"""FailureModeEvaluator — deterministic QA gate for reject fixtures.

Grades a video fixture's pixel-level signals (see
:mod:`failure_mode_detectors`) against the expected failure mode
declared on the :class:`Case`. Offline, deterministic, no LLM. This is
the QA gate that runs in place of a judge for fixtures whose
``expected_verdict == "reject"``.

Input shape
-----------
:class:`EvaluationData` populated by :func:`failure_mode_video_task`:

* ``input`` — manifest entry dict (same shape as
  :mod:`media_corpus` builds).
* ``actual_output`` — ``{"signals": {...}, "local_path": str}`` where
  ``signals`` is :class:`VideoSignals`-shaped.
* ``metadata.expected_failure_mode`` — one of:

  * ``"frozen"`` — max inter-frame diff must be below
    :data:`DEFAULT_FROZEN_MAX_DIFF`.
  * ``"black"`` — black-frame ratio must be at or above
    :data:`DEFAULT_BLACK_RATIO_THRESHOLD`.
  * ``"white"`` — white-frame ratio must be at or above
    :data:`DEFAULT_WHITE_RATIO_THRESHOLD`.
  * ``"clean"`` — none of the three failure detectors may fire.

Grading
-------
Hard gate: the single relevant clause passes or fails the case. The
evaluator emits one :class:`EvaluationOutput` per grading clause so
per-case diagnostics stay readable.

Design notes
------------
* Thresholds are class-level knobs so an experiment author can widen
  them for a noisier fixture family without touching the detector.
* A ``clean`` case checks *both* black and white thresholds: any
  clean control that accidentally encodes to 90%+ black pixels
  should fail — that is a generator bug worth surfacing.
* ``frozen`` is *not* asserted on clean cases. Plenty of legitimate
  clean fixtures are static (solid colour, held text) and would
  trip a motion-based check; the frozen detector is only meaningful
  when the ``Case`` explicitly marks a clip as supposed-to-be-moving.
"""

from __future__ import annotations

from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

DEFAULT_FROZEN_MAX_DIFF: float = 0.5
DEFAULT_BLACK_RATIO_THRESHOLD: float = 0.9
DEFAULT_WHITE_RATIO_THRESHOLD: float = 0.9

_VALID_MODES = frozenset({"frozen", "black", "white", "clean"})


class FailureModeEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Grade a video fixture against its expected failure mode.

    Args:
        frozen_max_diff: Upper bound on max inter-frame mean absolute
            difference for a ``"frozen"`` case to be accepted. Values
            above this indicate motion — the clip is not frozen and
            the case fails. Defaults to
            :data:`DEFAULT_FROZEN_MAX_DIFF`.
        black_ratio_threshold: Lower bound on the black-frame ratio
            for a ``"black"`` case to be accepted; also the upper
            bound the detector must stay below for a ``"clean"``
            case. Defaults to :data:`DEFAULT_BLACK_RATIO_THRESHOLD`.
        white_ratio_threshold: Mirror of ``black_ratio_threshold``
            for white-out detection. Defaults to
            :data:`DEFAULT_WHITE_RATIO_THRESHOLD`.
    """

    def __init__(
        self,
        *,
        frozen_max_diff: float = DEFAULT_FROZEN_MAX_DIFF,
        black_ratio_threshold: float = DEFAULT_BLACK_RATIO_THRESHOLD,
        white_ratio_threshold: float = DEFAULT_WHITE_RATIO_THRESHOLD,
    ) -> None:
        super().__init__()
        self._frozen_max_diff = frozen_max_diff
        self._black_ratio_threshold = black_ratio_threshold
        self._white_ratio_threshold = white_ratio_threshold

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}
        raw_output = evaluation_case.actual_output or {}

        mode = str(metadata.get("expected_failure_mode", "")).strip().lower()
        if mode not in _VALID_MODES:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        f"FAIL setup: expected_failure_mode must be one of "
                        f"{sorted(_VALID_MODES)}, got {mode!r}"
                    ),
                    label="failure_mode.setup",
                )
            ]

        signals = raw_output.get("signals") or {}
        if not signals:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="FAIL setup: task did not produce signals",
                    label="failure_mode.setup",
                )
            ]

        black_ratio = float(signals.get("black_ratio", 0.0))
        white_ratio = float(signals.get("white_ratio", 0.0))
        max_diff = float(signals.get("max_interframe_diff", 0.0))

        fixture_id = str(metadata.get("fixture_id", "") or "unknown")
        ctx = f"fixture={fixture_id} mode={mode}"

        if mode == "frozen":
            return [self._grade_frozen(max_diff, ctx)]
        if mode == "black":
            return [self._grade_black(black_ratio, ctx)]
        if mode == "white":
            return [self._grade_white(white_ratio, ctx)]
        return self._grade_clean(black_ratio, white_ratio, ctx)

    def _grade_frozen(self, max_diff: float, ctx: str) -> EvaluationOutput:
        passed = max_diff < self._frozen_max_diff
        reason = (
            f"{'PASS' if passed else 'FAIL'} {ctx}: "
            f"max_interframe_diff={max_diff:.4f} "
            f"(threshold<{self._frozen_max_diff})"
        )
        return EvaluationOutput(
            score=1.0 if passed else 0.0,
            test_pass=passed,
            reason=reason,
            label="failure_mode.frozen",
        )

    def _grade_black(self, black_ratio: float, ctx: str) -> EvaluationOutput:
        passed = black_ratio >= self._black_ratio_threshold
        reason = (
            f"{'PASS' if passed else 'FAIL'} {ctx}: "
            f"black_ratio={black_ratio:.4f} "
            f"(threshold>={self._black_ratio_threshold})"
        )
        return EvaluationOutput(
            score=1.0 if passed else 0.0,
            test_pass=passed,
            reason=reason,
            label="failure_mode.black",
        )

    def _grade_white(self, white_ratio: float, ctx: str) -> EvaluationOutput:
        passed = white_ratio >= self._white_ratio_threshold
        reason = (
            f"{'PASS' if passed else 'FAIL'} {ctx}: "
            f"white_ratio={white_ratio:.4f} "
            f"(threshold>={self._white_ratio_threshold})"
        )
        return EvaluationOutput(
            score=1.0 if passed else 0.0,
            test_pass=passed,
            reason=reason,
            label="failure_mode.white",
        )

    def _grade_clean(
        self,
        black_ratio: float,
        white_ratio: float,
        ctx: str,
    ) -> list[EvaluationOutput]:
        outputs: list[EvaluationOutput] = []

        black_ok = black_ratio < self._black_ratio_threshold
        outputs.append(
            EvaluationOutput(
                score=1.0 if black_ok else 0.0,
                test_pass=black_ok,
                reason=(
                    f"{'PASS' if black_ok else 'FAIL'} {ctx}: "
                    f"black_ratio={black_ratio:.4f} "
                    f"(threshold<{self._black_ratio_threshold})"
                ),
                label="failure_mode.clean.not_black",
            )
        )

        white_ok = white_ratio < self._white_ratio_threshold
        outputs.append(
            EvaluationOutput(
                score=1.0 if white_ok else 0.0,
                test_pass=white_ok,
                reason=(
                    f"{'PASS' if white_ok else 'FAIL'} {ctx}: "
                    f"white_ratio={white_ratio:.4f} "
                    f"(threshold<{self._white_ratio_threshold})"
                ),
                label="failure_mode.clean.not_white",
            )
        )

        return outputs


__all__ = [
    "DEFAULT_BLACK_RATIO_THRESHOLD",
    "DEFAULT_FROZEN_MAX_DIFF",
    "DEFAULT_WHITE_RATIO_THRESHOLD",
    "FailureModeEvaluator",
]
