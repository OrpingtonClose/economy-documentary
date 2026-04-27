"""``/components`` surface for :func:`strands_agents.qa_gates.qa_stills_judge`.

Per the user directive following slice 9j ("stills are a hard fail"),
the orchestrator now calls :func:`qa_stills_judge` after every
``launch_visual_production``. The gate decodes ``num_samples`` evenly-
spaced frames from the rendered MP4 and computes the mean L1 inter-
frame pixel delta; below ``min_mean_pixel_delta`` the clip is judged
a still and the run hard-fails. No vision LLM call — the gate is
deterministic and hermetic.

This module surfaces the gate as a ``/components/qa_stills_judge``
card with synthetic fixtures driven through ffmpeg's ``lavfi``
filtergraph:

* ``frozen_grey_fails`` — solid-grey 4 s clip (zero motion). Mean
  delta drops below floor → ``verdict=fail``.
* ``frozen_black_fails`` — solid-black 4 s clip → ``verdict=fail``.
* ``mandelbrot_motion_passes`` — 4 s mandelbrot zoom (high motion).
  Mean delta well above floor → ``verdict=pass``.
* ``testsrc_motion_passes`` — 4 s ffmpeg ``testsrc`` pattern (built-in
  moving timecode + bars). Mean delta above floor → ``verdict=pass``.
* ``loose_threshold_lets_through_low_motion`` — solid grey but with
  ``min_mean_pixel_delta=0.0``. Same artifact, different threshold;
  passes. Proves the parameter is honored.
* ``video_missing_fails`` — non-existent path → ``verdict=fail`` with
  ``video_path does not exist`` in the error.
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
    DEFAULT_MIN_MEAN_PIXEL_DELTA,
    DEFAULT_STILLS_NUM_SAMPLES,
    VERDICT_FAIL,
    VERDICT_PASS,
    qa_stills_judge,
)


QA_STILLS_JUDGE_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "StillsJudgeVerdictEvaluator": (1.0, True),
    "StillsJudgeDetailEvaluator": (1.0, True),
}


# ── Case schema ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _StillsFixture:
    """Fixture description for a stills-judge case.

    ``kind`` selects the ffmpeg ``lavfi`` source — ``grey`` / ``black``
    are zero-motion, ``mandelbrot`` / ``testsrc`` are high motion.
    ``skip_creation`` leaves the path uncreated to drive the missing-
    file branch. ``num_samples`` and ``min_mean_pixel_delta`` are
    optional gate parameters; ``None`` falls back to the gate defaults.
    """

    kind: str = "mandelbrot"
    duration_s: float = 4.0
    fps: int = 24
    width: int = 128
    height: int = 128
    skip_creation: bool = False
    num_samples: int | None = None
    min_mean_pixel_delta: float | None = None


def _case(
    name: str,
    *,
    fixture: _StillsFixture,
    expected_verdict: str,
    expected_detail: dict[str, Any] | None = None,
) -> Case[dict[str, Any], dict[str, Any]]:
    body: dict[str, Any] = {
        "scene_id": f"scene_{name}",
        "fixture": {
            "kind": fixture.kind,
            "duration_s": fixture.duration_s,
            "fps": fixture.fps,
            "width": fixture.width,
            "height": fixture.height,
            "skip_creation": fixture.skip_creation,
        },
    }
    if fixture.num_samples is not None:
        body["num_samples"] = int(fixture.num_samples)
    if fixture.min_mean_pixel_delta is not None:
        body["min_mean_pixel_delta"] = float(fixture.min_mean_pixel_delta)
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"qa-stills-judge-{name}",
        input=body,
        expected_output={"verdict": expected_verdict},
        metadata={
            "expected_verdict": expected_verdict,
            "expected_detail": expected_detail or {},
        },
    )


def qa_stills_judge_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the canonical ``qa_stills_judge`` workbench cases."""
    return [
        _case(
            "frozen_grey_fails",
            fixture=_StillsFixture(kind="grey", duration_s=4.0),
            expected_verdict=VERDICT_FAIL,
            expected_detail={
                "mean_pixel_delta_max": DEFAULT_MIN_MEAN_PIXEL_DELTA,
                "min_mean_pixel_delta": DEFAULT_MIN_MEAN_PIXEL_DELTA,
                "reason_substring": "below floor",
            },
        ),
        _case(
            "frozen_black_fails",
            fixture=_StillsFixture(kind="black", duration_s=4.0),
            expected_verdict=VERDICT_FAIL,
            expected_detail={
                "mean_pixel_delta_max": DEFAULT_MIN_MEAN_PIXEL_DELTA,
                "reason_substring": "below floor",
            },
        ),
        _case(
            "mandelbrot_motion_passes",
            fixture=_StillsFixture(kind="mandelbrot", duration_s=4.0),
            expected_verdict=VERDICT_PASS,
            expected_detail={
                "mean_pixel_delta_min": DEFAULT_MIN_MEAN_PIXEL_DELTA,
                "min_mean_pixel_delta": DEFAULT_MIN_MEAN_PIXEL_DELTA,
            },
        ),
        _case(
            "testsrc_motion_passes",
            fixture=_StillsFixture(kind="testsrc", duration_s=4.0),
            expected_verdict=VERDICT_PASS,
            expected_detail={
                "mean_pixel_delta_min": DEFAULT_MIN_MEAN_PIXEL_DELTA,
            },
        ),
        _case(
            "loose_threshold_lets_through_low_motion",
            fixture=_StillsFixture(
                kind="grey",
                duration_s=4.0,
                min_mean_pixel_delta=0.0,
            ),
            expected_verdict=VERDICT_PASS,
            expected_detail={"min_mean_pixel_delta": 0.0},
        ),
        _case(
            "video_missing_fails",
            fixture=_StillsFixture(skip_creation=True),
            expected_verdict=VERDICT_FAIL,
            expected_detail={"error_substring": "video_path does not exist"},
        ),
    ]


# ── Fixture synthesis ────────────────────────────────────────────────


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


_LAVFI_SOURCES: dict[str, str] = {
    "grey": "color=color=gray:size={width}x{height}:rate={fps}",
    "black": "color=color=black:size={width}x{height}:rate={fps}",
    "mandelbrot": "mandelbrot=size={width}x{height}:rate={fps}",
    "testsrc": "testsrc=size={width}x{height}:rate={fps}",
}


def _synthesize_mp4(fixture: dict[str, Any], target: Path) -> None:
    kind = fixture.get("kind", "mandelbrot")
    fps = int(fixture.get("fps", 24))
    width = int(fixture.get("width", 128))
    height = int(fixture.get("height", 128))
    duration_s = float(fixture.get("duration_s", 4.0))

    template = _LAVFI_SOURCES.get(kind)
    if template is None:
        raise ValueError(f"unsupported fixture kind: {kind}")
    source = template.format(width=width, height=height, fps=fps)

    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        source,
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


def qa_stills_judge_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Synthesise the case's MP4 and dispatch the real stills-judge gate."""
    payload = case.input or {}
    scene_id = payload.get("scene_id", "scene_0")
    fixture = payload.get("fixture") or {}
    num_samples_override = payload.get("num_samples")
    delta_override = payload.get("min_mean_pixel_delta")
    trajectory: list[str] = []

    if not _ffmpeg_available():
        envelope = {
            "tool": "qa_stills_judge",
            "scene_id": scene_id,
            "verdict": VERDICT_FAIL,
            "error": "ffmpeg / ffprobe not available on PATH",
        }
        return {
            "output": envelope,
            "trajectory": ["abort: ffmpeg unavailable"],
            "metadata": {"fixture": fixture, "ffmpeg_available": False},
        }

    with tempfile.TemporaryDirectory(prefix="qa-stills-") as tmp:
        tmp_path = Path(tmp)
        video_path = tmp_path / "video.mp4"

        if fixture.get("skip_creation"):
            video_path = tmp_path / "missing.mp4"
            trajectory.append(f"left {video_path} uncreated")
        else:
            _synthesize_mp4(fixture, video_path)
            trajectory.append(
                f"synthesised {fixture.get('kind')} mp4 "
                f"({fixture.get('duration_s')}s) at {video_path}"
            )

        invoke_args: dict[str, Any] = {
            "scene_id": scene_id,
            "video_path": str(video_path),
        }
        if num_samples_override is not None:
            invoke_args["num_samples"] = int(num_samples_override)
        if delta_override is not None:
            invoke_args["min_mean_pixel_delta"] = float(delta_override)

        envelope = qa_stills_judge.invoke(invoke_args)
        trajectory.append(
            f"qa_stills_judge -> verdict={envelope.get('verdict')} "
            f"mean_pixel_delta={envelope.get('mean_pixel_delta')!r}"
        )

    return {
        "output": envelope,
        "trajectory": trajectory,
        "metadata": {"fixture": fixture, "ffmpeg_available": True},
    }


# ── Evaluators ───────────────────────────────────────────────────────


class StillsJudgeVerdictEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin the stills-judge verdict to the case's ``expected_verdict``."""

    name = "StillsJudgeVerdictEvaluator"

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


class StillsJudgeDetailEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Score the gate's evidence (``mean_pixel_delta``, ``reason``, ``error``)."""

    name = "StillsJudgeDetailEvaluator"

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

        delta = envelope.get("mean_pixel_delta")
        d_min = expected_detail.get("mean_pixel_delta_min")
        d_max = expected_detail.get("mean_pixel_delta_max")
        if d_min is not None and (delta is None or delta < d_min):
            problems.append(f"mean_pixel_delta={delta!r} < min {d_min!r}")
        if d_max is not None and (delta is None or delta > d_max):
            problems.append(f"mean_pixel_delta={delta!r} > max {d_max!r}")

        if "min_mean_pixel_delta" in expected_detail:
            actual_min = envelope.get("min_mean_pixel_delta")
            if actual_min != expected_detail["min_mean_pixel_delta"]:
                problems.append(
                    f"min_mean_pixel_delta={actual_min!r} != "
                    f"{expected_detail['min_mean_pixel_delta']!r}"
                )

        if "num_samples" in expected_detail:
            actual_n = envelope.get("num_samples")
            if actual_n != expected_detail["num_samples"]:
                problems.append(
                    f"num_samples={actual_n!r} != {expected_detail['num_samples']!r}"
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


def build_qa_stills_judge_experiment() -> Experiment[dict[str, Any], dict[str, Any]]:
    """Assemble the stills-judge :class:`Experiment` for the playground."""
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=qa_stills_judge_cases(),
        evaluators=[
            StillsJudgeVerdictEvaluator(),
            StillsJudgeDetailEvaluator(),
        ],
    )


__all__ = [
    "DEFAULT_STILLS_NUM_SAMPLES",
    "QA_STILLS_JUDGE_EVALUATOR_THRESHOLDS",
    "StillsJudgeDetailEvaluator",
    "StillsJudgeVerdictEvaluator",
    "build_qa_stills_judge_experiment",
    "qa_stills_judge_cases",
    "qa_stills_judge_task",
]
