"""AVDesyncEvaluator — deterministic assembly-layer A/V invariant gate.

Grades a combined audio+video fixture's signal-level primitives
(see :mod:`av_desync_detectors`) against the expected failure
mode declared on the :class:`Case`. Offline, deterministic, no
LLM. This is the assembly-layer counterpart to
:class:`strands_agents.evals.evaluators.failure_mode.FailureModeEvaluator`
(video-only) and
:class:`strands_agents.evals.evaluators.audio_failure_mode.AudioFailureModeEvaluator`
(audio-only).

Input shape
-----------
:class:`EvaluationData` populated by an AV desync task:

* ``input`` — manifest entry dict.
* ``actual_output`` — ``{"signals": {...}, "local_path": str}``
  where ``signals`` is :class:`AVSignals`-shaped.
* ``metadata.expected_desync_mode`` — one of:

  * ``"synced"`` — audio_onset and video_content_onset must both
    be present *and* their absolute difference must stay within
    :data:`DEFAULT_SYNC_TOLERANCE_SEC`. Emits three clauses
    (audio-present, video-present, within-tolerance).
  * ``"audio_ahead"`` — audio_onset < video_content_onset with a
    gap at least :data:`DEFAULT_DESYNC_MIN_SEC`.
  * ``"audio_behind"`` — audio_onset > video_content_onset with
    the same minimum gap.
  * ``"audio_missing"`` — audio rail must be silent
    (audio_onset is None and audio_rms below the floor).
  * ``"video_missing"`` — video rail must be dark
    (video_content_onset is None).

Grading
-------
Hard gate: the relevant clause(s) pass or fail the case. The
evaluator emits one :class:`EvaluationOutput` per grading clause
so per-case diagnostics stay readable.

Design notes
------------
* Thresholds are class-level knobs so an experiment author can
  widen them for a different fixture family without touching the
  detector primitives.
* The tolerance asymmetry is deliberate: ``DEFAULT_SYNC_TOLERANCE_SEC``
  (150 ms) is tight enough that production-rendered
  subtitle-aligned narration stays inside it, while
  ``DEFAULT_DESYNC_MIN_SEC`` (300 ms) is well beyond the
  perceptual threshold humans flag as "audio and lips don't
  match". The band between the two is deliberately unclaimed —
  a fixture that lands there is a generator bug.
* ``audio_missing`` uses both the onset check *and* an RMS floor
  so a clip with 1 rogue sample doesn't falsely clear the gate.
"""

from __future__ import annotations

from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

DEFAULT_SYNC_TOLERANCE_SEC: float = 0.15
DEFAULT_DESYNC_MIN_SEC: float = 0.3
DEFAULT_AUDIO_MISSING_RMS_FLOOR: float = 0.001

_VALID_MODES = frozenset(
    {
        "synced",
        "audio_ahead",
        "audio_behind",
        "audio_missing",
        "video_missing",
    }
)


class AVDesyncEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Grade an AV fixture against its expected desync mode.

    Args:
        sync_tolerance_sec: Upper bound on
            ``|audio_onset - video_content_onset|`` for a
            ``"synced"`` case to pass. Defaults to
            :data:`DEFAULT_SYNC_TOLERANCE_SEC`.
        desync_min_sec: Lower bound on the signed gap for
            ``"audio_ahead"`` / ``"audio_behind"`` cases to pass.
            Defaults to :data:`DEFAULT_DESYNC_MIN_SEC`.
        audio_missing_rms_floor: Upper bound on overall audio RMS
            for a ``"audio_missing"`` case to pass. Defaults to
            :data:`DEFAULT_AUDIO_MISSING_RMS_FLOOR`.
    """

    def __init__(
        self,
        *,
        sync_tolerance_sec: float = DEFAULT_SYNC_TOLERANCE_SEC,
        desync_min_sec: float = DEFAULT_DESYNC_MIN_SEC,
        audio_missing_rms_floor: float = DEFAULT_AUDIO_MISSING_RMS_FLOOR,
    ) -> None:
        super().__init__()
        self._sync_tolerance_sec = sync_tolerance_sec
        self._desync_min_sec = desync_min_sec
        self._audio_missing_rms_floor = audio_missing_rms_floor

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}
        raw_output = evaluation_case.actual_output or {}

        mode = str(metadata.get("expected_desync_mode", "")).strip().lower()
        if mode not in _VALID_MODES:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        f"FAIL setup: expected_desync_mode must be one of "
                        f"{sorted(_VALID_MODES)}, got {mode!r}"
                    ),
                    label="av_desync.setup",
                )
            ]

        signals = raw_output.get("signals") or {}
        if not signals:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="FAIL setup: task did not produce signals",
                    label="av_desync.setup",
                )
            ]

        audio_onset = signals.get("audio_onset_sec")
        video_onset = signals.get("video_content_onset_sec")
        desync = signals.get("desync_sec")
        audio_rms = float(signals.get("audio_rms", 0.0))

        fixture_id = str(metadata.get("fixture_id", "") or "unknown")
        ctx = f"fixture={fixture_id} mode={mode}"

        if mode == "synced":
            return self._grade_synced(
                audio_onset, video_onset, desync, ctx
            )
        if mode == "audio_ahead":
            return [self._grade_directional(desync, ctx, expected_sign=-1)]
        if mode == "audio_behind":
            return [self._grade_directional(desync, ctx, expected_sign=+1)]
        if mode == "audio_missing":
            return [self._grade_audio_missing(audio_onset, audio_rms, ctx)]
        return [self._grade_video_missing(video_onset, ctx)]

    def _grade_synced(
        self,
        audio_onset: float | None,
        video_onset: float | None,
        desync: float | None,
        ctx: str,
    ) -> list[EvaluationOutput]:
        outputs: list[EvaluationOutput] = []

        audio_present = audio_onset is not None
        outputs.append(
            EvaluationOutput(
                score=1.0 if audio_present else 0.0,
                test_pass=audio_present,
                reason=(
                    f"{'PASS' if audio_present else 'FAIL'} {ctx}: "
                    f"audio_onset_sec={audio_onset}"
                ),
                label="av_desync.synced.audio_present",
            )
        )

        video_present = video_onset is not None
        outputs.append(
            EvaluationOutput(
                score=1.0 if video_present else 0.0,
                test_pass=video_present,
                reason=(
                    f"{'PASS' if video_present else 'FAIL'} {ctx}: "
                    f"video_content_onset_sec={video_onset}"
                ),
                label="av_desync.synced.video_present",
            )
        )

        if desync is None:
            within_tol = False
            reason_tail = "desync_sec=None (one rail absent)"
        else:
            within_tol = abs(desync) <= self._sync_tolerance_sec
            reason_tail = (
                f"desync_sec={desync:+.3f} "
                f"(tolerance<=±{self._sync_tolerance_sec})"
            )
        outputs.append(
            EvaluationOutput(
                score=1.0 if within_tol else 0.0,
                test_pass=within_tol,
                reason=(
                    f"{'PASS' if within_tol else 'FAIL'} {ctx}: "
                    f"{reason_tail}"
                ),
                label="av_desync.synced.within_tolerance",
            )
        )

        return outputs

    def _grade_directional(
        self, desync: float | None, ctx: str, *, expected_sign: int
    ) -> EvaluationOutput:
        label = (
            "av_desync.audio_ahead"
            if expected_sign < 0
            else "av_desync.audio_behind"
        )
        if desync is None:
            return EvaluationOutput(
                score=0.0,
                test_pass=False,
                reason=(
                    f"FAIL {ctx}: desync_sec=None (one or both onsets "
                    f"missing)"
                ),
                label=label,
            )
        if expected_sign < 0:
            passed = desync <= -self._desync_min_sec
            bound_str = f"desync_sec<=-{self._desync_min_sec}"
        else:
            passed = desync >= self._desync_min_sec
            bound_str = f"desync_sec>={self._desync_min_sec}"
        return EvaluationOutput(
            score=1.0 if passed else 0.0,
            test_pass=passed,
            reason=(
                f"{'PASS' if passed else 'FAIL'} {ctx}: "
                f"desync_sec={desync:+.3f} ({bound_str})"
            ),
            label=label,
        )

    def _grade_audio_missing(
        self,
        audio_onset: float | None,
        audio_rms: float,
        ctx: str,
    ) -> EvaluationOutput:
        passed = (
            audio_onset is None
            and audio_rms <= self._audio_missing_rms_floor
        )
        return EvaluationOutput(
            score=1.0 if passed else 0.0,
            test_pass=passed,
            reason=(
                f"{'PASS' if passed else 'FAIL'} {ctx}: "
                f"audio_onset_sec={audio_onset} "
                f"audio_rms={audio_rms:.6f} "
                f"(rms_floor<={self._audio_missing_rms_floor})"
            ),
            label="av_desync.audio_missing",
        )

    def _grade_video_missing(
        self, video_onset: float | None, ctx: str
    ) -> EvaluationOutput:
        passed = video_onset is None
        return EvaluationOutput(
            score=1.0 if passed else 0.0,
            test_pass=passed,
            reason=(
                f"{'PASS' if passed else 'FAIL'} {ctx}: "
                f"video_content_onset_sec={video_onset}"
            ),
            label="av_desync.video_missing",
        )


__all__ = [
    "DEFAULT_AUDIO_MISSING_RMS_FLOOR",
    "DEFAULT_DESYNC_MIN_SEC",
    "DEFAULT_SYNC_TOLERANCE_SEC",
    "AVDesyncEvaluator",
]
