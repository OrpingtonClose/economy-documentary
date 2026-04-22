"""AudioFailureModeEvaluator — deterministic QA gate for audio fixtures.

Grades an audio fixture's signal-level primitives (see
:mod:`audio_failure_mode_detectors`) against the expected failure
mode declared on the :class:`Case`. Offline, deterministic, no LLM.
This is the audio counterpart to
:class:`strands_agents.evals.evaluators.failure_mode.FailureModeEvaluator`.

Input shape
-----------
:class:`EvaluationData` populated by an audio-failure-mode task:

* ``input`` — manifest entry dict.
* ``actual_output`` — ``{"signals": {...}, "local_path": str}`` where
  ``signals`` is :class:`AudioSignals`-shaped.
* ``metadata.expected_failure_mode`` — one of:

  * ``"silence"`` — RMS must be at or below
    :data:`DEFAULT_SILENCE_RMS_THRESHOLD`.
  * ``"clipping"`` — clipping ratio must be at or above
    :data:`DEFAULT_CLIPPING_RATIO_THRESHOLD`.
  * ``"noise"`` — spectral flatness must be at or above
    :data:`DEFAULT_NOISE_FLATNESS_THRESHOLD`.
  * ``"clean"`` — RMS must exceed the silence threshold *and*
    clipping ratio must stay below the clipping threshold *and*
    spectral flatness must stay below the noise threshold.

Grading
-------
Hard gate: the relevant clause passes or fails the case. The
evaluator emits one :class:`EvaluationOutput` per grading clause so
per-case diagnostics stay readable. ``"clean"`` emits three clauses
(not-silence, not-clipping, not-noise) because all three must hold.

Design notes
------------
* Thresholds are class-level knobs so an experiment author can
  widen them for a different fixture family without touching the
  detector primitives.
* The clipping detector is deliberately asymmetric: a narration
  clip with one loud syllable at ``peak=1.0`` does *not* fail
  clean-mode unless its *ratio* of clipped samples exceeds the
  threshold. Peak alone is not a failure signal.
* Noise and clipping are disjoint failure modes because clipping
  concentrates energy in the time domain (few samples at the rail)
  while noise spreads energy in the frequency domain (flat
  spectrum). A fixture that trips both at once is a generator bug.
"""

from __future__ import annotations

from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

DEFAULT_SILENCE_RMS_THRESHOLD: float = 0.001
DEFAULT_CLIPPING_RATIO_THRESHOLD: float = 0.01
DEFAULT_NOISE_FLATNESS_THRESHOLD: float = 0.7

_VALID_MODES = frozenset({"silence", "clipping", "noise", "clean"})


class AudioFailureModeEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Grade an audio fixture against its expected failure mode.

    Args:
        silence_rms_threshold: Upper bound on the RMS amplitude for
            a ``"silence"`` case to be accepted; also the lower
            bound the RMS must exceed for a ``"clean"`` case.
            Defaults to :data:`DEFAULT_SILENCE_RMS_THRESHOLD`.
        clipping_ratio_threshold: Lower bound on the clipping ratio
            for a ``"clipping"`` case to be accepted; also the
            upper bound the detector must stay below for a
            ``"clean"`` case. Defaults to
            :data:`DEFAULT_CLIPPING_RATIO_THRESHOLD`.
        noise_flatness_threshold: Lower bound on spectral flatness
            for a ``"noise"`` case to be accepted; also the upper
            bound the detector must stay below for a ``"clean"``
            case. Defaults to
            :data:`DEFAULT_NOISE_FLATNESS_THRESHOLD`.
    """

    def __init__(
        self,
        *,
        silence_rms_threshold: float = DEFAULT_SILENCE_RMS_THRESHOLD,
        clipping_ratio_threshold: float = DEFAULT_CLIPPING_RATIO_THRESHOLD,
        noise_flatness_threshold: float = DEFAULT_NOISE_FLATNESS_THRESHOLD,
    ) -> None:
        super().__init__()
        self._silence_rms_threshold = silence_rms_threshold
        self._clipping_ratio_threshold = clipping_ratio_threshold
        self._noise_flatness_threshold = noise_flatness_threshold

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
                    label="audio_failure_mode.setup",
                )
            ]

        signals = raw_output.get("signals") or {}
        if not signals:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="FAIL setup: task did not produce signals",
                    label="audio_failure_mode.setup",
                )
            ]

        rms = float(signals.get("rms", 0.0))
        clipping = float(signals.get("clipping_ratio", 0.0))
        flatness = float(signals.get("spectral_flatness", 0.0))

        fixture_id = str(metadata.get("fixture_id", "") or "unknown")
        ctx = f"fixture={fixture_id} mode={mode}"

        if mode == "silence":
            return [self._grade_silence(rms, ctx)]
        if mode == "clipping":
            return [self._grade_clipping(clipping, ctx)]
        if mode == "noise":
            return [self._grade_noise(flatness, ctx)]
        return self._grade_clean(rms, clipping, flatness, ctx)

    def _grade_silence(self, rms: float, ctx: str) -> EvaluationOutput:
        passed = rms <= self._silence_rms_threshold
        reason = (
            f"{'PASS' if passed else 'FAIL'} {ctx}: "
            f"rms={rms:.6f} "
            f"(threshold<={self._silence_rms_threshold})"
        )
        return EvaluationOutput(
            score=1.0 if passed else 0.0,
            test_pass=passed,
            reason=reason,
            label="audio_failure_mode.silence",
        )

    def _grade_clipping(self, clipping: float, ctx: str) -> EvaluationOutput:
        passed = clipping >= self._clipping_ratio_threshold
        reason = (
            f"{'PASS' if passed else 'FAIL'} {ctx}: "
            f"clipping_ratio={clipping:.6f} "
            f"(threshold>={self._clipping_ratio_threshold})"
        )
        return EvaluationOutput(
            score=1.0 if passed else 0.0,
            test_pass=passed,
            reason=reason,
            label="audio_failure_mode.clipping",
        )

    def _grade_noise(self, flatness: float, ctx: str) -> EvaluationOutput:
        passed = flatness >= self._noise_flatness_threshold
        reason = (
            f"{'PASS' if passed else 'FAIL'} {ctx}: "
            f"spectral_flatness={flatness:.4f} "
            f"(threshold>={self._noise_flatness_threshold})"
        )
        return EvaluationOutput(
            score=1.0 if passed else 0.0,
            test_pass=passed,
            reason=reason,
            label="audio_failure_mode.noise",
        )

    def _grade_clean(
        self,
        rms: float,
        clipping: float,
        flatness: float,
        ctx: str,
    ) -> list[EvaluationOutput]:
        outputs: list[EvaluationOutput] = []

        not_silent = rms > self._silence_rms_threshold
        outputs.append(
            EvaluationOutput(
                score=1.0 if not_silent else 0.0,
                test_pass=not_silent,
                reason=(
                    f"{'PASS' if not_silent else 'FAIL'} {ctx}: "
                    f"rms={rms:.6f} "
                    f"(threshold>{self._silence_rms_threshold})"
                ),
                label="audio_failure_mode.clean.not_silent",
            )
        )

        not_clipping = clipping < self._clipping_ratio_threshold
        outputs.append(
            EvaluationOutput(
                score=1.0 if not_clipping else 0.0,
                test_pass=not_clipping,
                reason=(
                    f"{'PASS' if not_clipping else 'FAIL'} {ctx}: "
                    f"clipping_ratio={clipping:.6f} "
                    f"(threshold<{self._clipping_ratio_threshold})"
                ),
                label="audio_failure_mode.clean.not_clipping",
            )
        )

        not_noise = flatness < self._noise_flatness_threshold
        outputs.append(
            EvaluationOutput(
                score=1.0 if not_noise else 0.0,
                test_pass=not_noise,
                reason=(
                    f"{'PASS' if not_noise else 'FAIL'} {ctx}: "
                    f"spectral_flatness={flatness:.4f} "
                    f"(threshold<{self._noise_flatness_threshold})"
                ),
                label="audio_failure_mode.clean.not_noise",
            )
        )

        return outputs


__all__ = [
    "DEFAULT_CLIPPING_RATIO_THRESHOLD",
    "DEFAULT_NOISE_FLATNESS_THRESHOLD",
    "DEFAULT_SILENCE_RMS_THRESHOLD",
    "AudioFailureModeEvaluator",
]
