"""``/components`` surface for :func:`strands_agents.qa_gates.qa_duration_align`.

Slice 9j-quality-pass shipped a frozen-frame "documentary" because
LTX-2.3 was rendering ~3.7 s of video against ~13 s of narration
and no gate compared the two. PR #372 wired
:func:`qa_duration_align` as an orchestrator-callable tool that
hard-fails the run when ``|audio_dur - video_dur| > tolerance_s``.

This module surfaces the same gate as a
``/components/qa_duration_align`` card so the user can drive it in
normal operation against synthetic fixtures (or, via custom_input,
against real artifact paths on disk). No test-only code path: the
task synthesises WAV + MP4 with ffmpeg and dispatches the *real*
:func:`qa_duration_align` tool.

Cases mirror the unit-test surface in
``tests/unit/test_qa_gates.py``:

* ``aligned_pair_passes`` — 4 s audio + 4 s video → ``verdict=pass``,
  ``delta_s ~= 0``.
* ``frozen_frame_regression_fails`` — the slice 9j replay: 13 s
  audio + 3.7 s video → ``verdict=fail``, ``delta_s ~= 9.3``.
* ``within_default_tolerance_passes`` — 4 s audio + 4.3 s video
  (delta ~0.3 < 0.5 default) → ``verdict=pass``.
* ``loose_tolerance_passes`` — 4 s audio + 5 s video, custom
  ``tolerance_s=1.5`` → ``verdict=pass``. Same artifacts with
  default tolerance fail; the case proves the parameter is honored.
* ``audio_missing_fails`` — point at a non-existent WAV path →
  ``verdict=fail`` with ``audio_path does not exist`` in the error.
* ``video_missing_fails`` — point at a non-existent MP4 path →
  ``verdict=fail`` with ``video_path does not exist`` in the error.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.qa_gates import (
    DEFAULT_DURATION_TOLERANCE_S,
    VERDICT_FAIL,
    VERDICT_PASS,
    qa_duration_align,
)


QA_DURATION_ALIGN_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "DurationAlignVerdictEvaluator": (1.0, True),
    "DurationAlignDetailEvaluator": (1.0, True),
}


# ── Case schema ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Pair:
    """Fixture description for a duration-align case."""

    audio_seconds: float | None  # None → leave file uncreated
    video_seconds: float | None
    tolerance_s: float | None = None  # None → use gate default


def _case(
    name: str,
    *,
    pair: _Pair,
    expected_verdict: str,
    expected_detail: dict[str, Any] | None = None,
) -> Case[dict[str, Any], dict[str, Any]]:
    fixture = {
        "audio_seconds": pair.audio_seconds,
        "video_seconds": pair.video_seconds,
    }
    body: dict[str, Any] = {
        "scene_id": f"scene_{name}",
        "fixture": fixture,
    }
    if pair.tolerance_s is not None:
        body["tolerance_s"] = float(pair.tolerance_s)
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"qa-duration-align-{name}",
        input=body,
        expected_output={"verdict": expected_verdict},
        metadata={
            "expected_verdict": expected_verdict,
            "expected_detail": expected_detail or {},
        },
    )


def qa_duration_align_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the canonical ``qa_duration_align`` workbench cases."""
    return [
        _case(
            "aligned_pair_passes",
            pair=_Pair(audio_seconds=4.0, video_seconds=4.0),
            expected_verdict=VERDICT_PASS,
            expected_detail={
                "delta_s_max": 0.5,
                "tolerance_s": DEFAULT_DURATION_TOLERANCE_S,
            },
        ),
        _case(
            "frozen_frame_regression_fails",
            pair=_Pair(audio_seconds=13.0, video_seconds=3.7),
            expected_verdict=VERDICT_FAIL,
            expected_detail={
                "delta_s_min": 8.5,
                "delta_s_max": 10.0,
                "reason_substring": "tolerance",
            },
        ),
        _case(
            "within_default_tolerance_passes",
            pair=_Pair(audio_seconds=4.0, video_seconds=4.3),
            expected_verdict=VERDICT_PASS,
            expected_detail={
                "delta_s_max": 0.5,
            },
        ),
        _case(
            "loose_tolerance_passes",
            pair=_Pair(audio_seconds=4.0, video_seconds=5.0, tolerance_s=1.5),
            expected_verdict=VERDICT_PASS,
            expected_detail={
                "tolerance_s": 1.5,
                "delta_s_min": 0.5,
                "delta_s_max": 1.5,
            },
        ),
        _case(
            "audio_missing_fails",
            pair=_Pair(audio_seconds=None, video_seconds=4.0),
            expected_verdict=VERDICT_FAIL,
            expected_detail={"error_substring": "audio_path does not exist"},
        ),
        _case(
            "video_missing_fails",
            pair=_Pair(audio_seconds=4.0, video_seconds=None),
            expected_verdict=VERDICT_FAIL,
            expected_detail={"error_substring": "video_path does not exist"},
        ),
    ]


# ── Fixture synthesis ────────────────────────────────────────────────


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_silent_wav(target: Path, duration_s: float) -> None:
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=mono:sample_rate=16000",
        "-t",
        f"{duration_s}",
        str(target),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _make_motion_mp4(target: Path, duration_s: float) -> None:
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "mandelbrot=size=128x128:rate=24",
        "-t",
        f"{duration_s}",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        str(target),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ── Task adapter ─────────────────────────────────────────────────────


def qa_duration_align_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Synthesise the case's WAV + MP4 and dispatch the real align gate."""
    payload = case.input or {}
    scene_id = payload.get("scene_id", "scene_0")
    fixture = payload.get("fixture") or {}
    tolerance_override = payload.get("tolerance_s")
    trajectory: list[str] = []

    if not _ffmpeg_available():
        envelope = {
            "tool": "qa_duration_align",
            "scene_id": scene_id,
            "verdict": VERDICT_FAIL,
            "error": "ffmpeg / ffprobe not available on PATH",
        }
        return {
            "output": envelope,
            "trajectory": ["abort: ffmpeg unavailable"],
            "metadata": {"fixture": fixture, "ffmpeg_available": False},
        }

    with tempfile.TemporaryDirectory(prefix="qa-duration-") as tmp:
        tmp_path = Path(tmp)
        audio_path = tmp_path / "audio.wav"
        video_path = tmp_path / "video.mp4"

        audio_seconds = fixture.get("audio_seconds")
        video_seconds = fixture.get("video_seconds")

        if audio_seconds is not None:
            _make_silent_wav(audio_path, float(audio_seconds))
            trajectory.append(f"synthesised {audio_seconds}s wav at {audio_path}")
        else:
            audio_path = tmp_path / "missing.wav"
            trajectory.append(f"left {audio_path} uncreated")

        if video_seconds is not None:
            _make_motion_mp4(video_path, float(video_seconds))
            trajectory.append(f"synthesised {video_seconds}s mp4 at {video_path}")
        else:
            video_path = tmp_path / "missing.mp4"
            trajectory.append(f"left {video_path} uncreated")

        invoke_args: dict[str, Any] = {
            "scene_id": scene_id,
            "audio_path": str(audio_path),
            "video_path": str(video_path),
        }
        if tolerance_override is not None:
            invoke_args["tolerance_s"] = float(tolerance_override)

        envelope = qa_duration_align.invoke(invoke_args)
        trajectory.append(
            f"qa_duration_align -> verdict={envelope.get('verdict')} "
            f"delta_s={envelope.get('delta_s')!r}"
        )

    return {
        "output": envelope,
        "trajectory": trajectory,
        "metadata": {"fixture": fixture, "ffmpeg_available": True},
    }


# ── Evaluators ───────────────────────────────────────────────────────


class DurationAlignVerdictEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin the duration-align verdict to the case's expectation."""

    name = "DurationAlignVerdictEvaluator"

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = (evaluation_case.actual_output or {}).get("verdict")
        expected = (evaluation_case.metadata or {}).get("expected_verdict")
        match = actual == expected
        return [
            EvaluationOutput(
                score=1.0 if match else 0.0,
                test_pass=match,
                reason=(
                    f"verdict={actual!r} "
                    f"{'matches' if match else 'does not match'} "
                    f"expected={expected!r}"
                ),
                label="verdict_match" if match else "verdict_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class DurationAlignDetailEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Score the gate's evidence fields (``delta_s``, ``tolerance_s``, ``error``)."""

    name = "DurationAlignDetailEvaluator"

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        envelope = evaluation_case.actual_output or {}
        expected_verdict = (evaluation_case.metadata or {}).get("expected_verdict")
        expected_detail = (evaluation_case.metadata or {}).get("expected_detail") or {}
        actual_verdict = envelope.get("verdict")

        if actual_verdict != expected_verdict:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        f"verdict mismatch ({actual_verdict!r} != "
                        f"{expected_verdict!r}); detail not evaluated"
                    ),
                    label="detail_skipped_verdict_mismatch",
                )
            ]

        problems: list[str] = []

        delta_s = envelope.get("delta_s")
        d_min = expected_detail.get("delta_s_min")
        d_max = expected_detail.get("delta_s_max")
        if d_min is not None and (delta_s is None or delta_s < d_min):
            problems.append(f"delta_s={delta_s!r} < min {d_min!r}")
        if d_max is not None and (delta_s is None or delta_s > d_max):
            problems.append(f"delta_s={delta_s!r} > max {d_max!r}")

        if "tolerance_s" in expected_detail:
            actual_tol = envelope.get("tolerance_s")
            if actual_tol != expected_detail["tolerance_s"]:
                problems.append(
                    f"tolerance_s={actual_tol!r} != {expected_detail['tolerance_s']!r}"
                )

        substring = expected_detail.get("error_substring")
        if substring:
            err = envelope.get("error") or ""
            if substring not in err:
                problems.append(f"error={err!r} missing substring {substring!r}")

        reason_sub = expected_detail.get("reason_substring")
        if reason_sub:
            reason = envelope.get("reason") or ""
            if reason_sub not in reason:
                problems.append(f"reason={reason!r} missing substring {reason_sub!r}")

        ok = not problems
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason="detail matches" if ok else "; ".join(problems),
                label="detail_match" if ok else "detail_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


# ── Experiment builder ───────────────────────────────────────────────


def build_qa_duration_align_experiment() -> Experiment[dict[str, Any], dict[str, Any]]:
    """Assemble the duration-align :class:`Experiment` for the playground."""
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=qa_duration_align_cases(),
        evaluators=[
            DurationAlignVerdictEvaluator(),
            DurationAlignDetailEvaluator(),
        ],
    )


__all__ = [
    "DurationAlignDetailEvaluator",
    "DurationAlignVerdictEvaluator",
    "QA_DURATION_ALIGN_EVALUATOR_THRESHOLDS",
    "build_qa_duration_align_experiment",
    "qa_duration_align_cases",
    "qa_duration_align_task",
]
