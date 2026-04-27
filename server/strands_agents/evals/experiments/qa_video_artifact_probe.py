"""``/components`` surface for :func:`strands_agents.qa_gates.qa_video_artifact_probe`.

The orchestrator wires three deterministic QA gates after every
``launch_visual_production`` (slice 9l, PR #372). Until this slice
those gates lived only as orchestrator-callable tools — there was no
playground card a user could drive in normal operation to inspect or
debug a rendered MP4. The slice 9j frozen-frame regression hid behind
exactly that gap: every assertion in the run was a plumbing check,
none of them decoded a frame.

This module surfaces :func:`qa_video_artifact_probe` as a
``/components/qa_video_artifact_probe`` card so the same gate the
orchestrator runs is exercisable end-to-end via the workbench, with
no test-only branches and no ffmpeg / ffprobe substitution.

Cases:

* ``probe_motion_clip`` — synthesise a 4 s mandelbrot motion MP4
  with ffmpeg, probe it, expect ``verdict=pass`` plus the right
  ``width / height / codec`` and a plausible ``duration_s``.
* ``probe_still_clip`` — synthesise a 3 s solid-grey MP4. Probe
  still passes (the artifact is well-formed); the *stills* gate
  is a separate card.
* ``probe_missing_file`` — point at a path that doesn't exist;
  expect ``verdict=fail`` with ``does not exist`` in the error.
* ``probe_truncated_artifact`` — write a single byte to disk; expect
  ``verdict=fail`` with ``too small`` in the error (catches
  worker disk-full / partial-write modes).

Both evaluators are hard gates: a probe that picks the wrong verdict
or returns the wrong artifact metadata is a data-quality regression
the orchestrator would silently roll forward on.
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
    VERDICT_FAIL,
    VERDICT_PASS,
    qa_video_artifact_probe,
)


#: Hard gates: probe verdict + artifact-detail must both line up with
#: the case's expectation. A drift on either is a regression.
QA_VIDEO_ARTIFACT_PROBE_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "ProbeVerdictEvaluator": (1.0, True),
    "ProbeDetailEvaluator": (1.0, True),
}


# ── Case schema ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Fixture:
    """How the task should synthesise the input MP4 for a case.

    ``kind`` selects the ffmpeg recipe; ``duration_s`` controls how
    long the clip is. ``write_byte`` short-circuits the synthesis to
    a one-byte file (truncation case). ``skip_creation`` leaves the
    path uncreated so the gate hits the missing-file branch.
    """

    kind: str = "motion"  # "motion" | "still" | "raw"
    duration_s: float = 4.0
    fps: int = 24
    width: int = 128
    height: int = 128
    write_byte: bool = False
    skip_creation: bool = False


def _case(
    name: str,
    *,
    fixture: _Fixture,
    expected_verdict: str,
    expected_detail: dict[str, Any] | None = None,
) -> Case[dict[str, Any], dict[str, Any]]:
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"qa-video-artifact-probe-{name}",
        input={
            "scene_id": f"scene_{name}",
            "fixture": {
                "kind": fixture.kind,
                "duration_s": fixture.duration_s,
                "fps": fixture.fps,
                "width": fixture.width,
                "height": fixture.height,
                "write_byte": fixture.write_byte,
                "skip_creation": fixture.skip_creation,
            },
        },
        expected_output={"verdict": expected_verdict},
        metadata={
            "expected_verdict": expected_verdict,
            "expected_detail": expected_detail or {},
        },
    )


def qa_video_artifact_probe_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the canonical ``qa_video_artifact_probe`` workbench cases."""
    return [
        _case(
            "probe_motion_clip",
            fixture=_Fixture(kind="motion", duration_s=4.0),
            expected_verdict=VERDICT_PASS,
            expected_detail={
                "width": 128,
                "height": 128,
                "codec": "h264",
                "duration_s_min": 3.5,
                "duration_s_max": 4.5,
                "size_bytes_min": 1024,
            },
        ),
        _case(
            "probe_still_clip",
            fixture=_Fixture(kind="still", duration_s=3.0),
            expected_verdict=VERDICT_PASS,
            expected_detail={
                "width": 128,
                "height": 128,
                "codec": "h264",
                "duration_s_min": 2.5,
                "duration_s_max": 3.5,
                "size_bytes_min": 256,
            },
        ),
        _case(
            "probe_missing_file",
            fixture=_Fixture(skip_creation=True),
            expected_verdict=VERDICT_FAIL,
            expected_detail={"error_substring": "does not exist"},
        ),
        _case(
            "probe_truncated_artifact",
            fixture=_Fixture(write_byte=True),
            expected_verdict=VERDICT_FAIL,
            expected_detail={"error_substring": "too small"},
        ),
    ]


# ── Fixture synthesis ────────────────────────────────────────────────


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _synthesize_mp4(fixture: dict[str, Any], target: Path) -> None:
    """Write the requested fixture to ``target`` using ffmpeg.

    Pure subprocess, no test-only path. The same ffmpeg the
    production assembly stage uses.
    """
    kind = fixture.get("kind", "motion")
    fps = int(fixture.get("fps", 24))
    width = int(fixture.get("width", 128))
    height = int(fixture.get("height", 128))
    duration_s = float(fixture.get("duration_s", 4.0))

    if kind == "motion":
        source = f"mandelbrot=size={width}x{height}:rate={fps}"
    elif kind == "still":
        source = f"color=color=gray:size={width}x{height}:rate={fps}"
    else:
        raise ValueError(f"unsupported fixture kind: {kind}")

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


def qa_video_artifact_probe_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Synthesise the case's fixture and dispatch the real probe gate.

    Returns the standard playground envelope: ``output`` is the raw
    gate envelope, ``trajectory`` records the synthesis steps, and
    ``metadata.fixture`` echoes the inputs so the evaluator can read
    them.
    """
    payload = case.input or {}
    scene_id = payload.get("scene_id", "scene_0")
    fixture = payload.get("fixture") or {}
    trajectory: list[str] = []

    if not _ffmpeg_available():
        envelope = {
            "tool": "qa_video_artifact_probe",
            "scene_id": scene_id,
            "verdict": VERDICT_FAIL,
            "error": "ffmpeg / ffprobe not available on PATH",
        }
        return {
            "output": envelope,
            "trajectory": ["abort: ffmpeg unavailable"],
            "metadata": {"fixture": fixture, "ffmpeg_available": False},
        }

    with tempfile.TemporaryDirectory(prefix="qa-probe-") as tmp:
        tmp_path = Path(tmp)
        video_path = tmp_path / "video.mp4"

        if fixture.get("write_byte"):
            video_path.write_bytes(b"x")
            trajectory.append(f"wrote 1 byte to {video_path}")
        elif fixture.get("skip_creation"):
            video_path = tmp_path / "missing.mp4"
            trajectory.append(f"skipped creation of {video_path}")
        else:
            _synthesize_mp4(fixture, video_path)
            trajectory.append(
                f"synthesised {fixture.get('kind')} mp4 "
                f"({fixture.get('duration_s')}s) at {video_path}"
            )

        envelope = qa_video_artifact_probe.invoke(
            {"scene_id": scene_id, "video_path": str(video_path)}
        )
        trajectory.append(
            f"qa_video_artifact_probe -> verdict={envelope.get('verdict')}"
        )

    return {
        "output": envelope,
        "trajectory": trajectory,
        "metadata": {"fixture": fixture, "ffmpeg_available": True},
    }


# ── Evaluators ───────────────────────────────────────────────────────


class ProbeVerdictEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin the probe verdict to the case's ``expected_verdict``."""

    name = "ProbeVerdictEvaluator"

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


class ProbeDetailEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Score the artifact-detail fields the probe surfaces.

    For pass cases checks ``width / height / codec`` plus a
    ``duration_s`` range and a ``size_bytes`` floor. For fail cases
    checks the error string contains the expected substring.
    """

    name = "ProbeDetailEvaluator"

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        envelope = evaluation_case.actual_output or {}
        expected_detail = (evaluation_case.metadata or {}).get("expected_detail") or {}
        expected_verdict = (evaluation_case.metadata or {}).get("expected_verdict")
        actual_verdict = envelope.get("verdict")

        # If the verdict is wrong already, the detail evaluator can't
        # score the right surface; defer to the verdict evaluator and
        # mark this one as failed for visibility.
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

        if expected_verdict == VERDICT_PASS:
            for key in ("width", "height", "codec"):
                if key in expected_detail and envelope.get(key) != expected_detail[key]:
                    problems.append(
                        f"{key}={envelope.get(key)!r} != {expected_detail[key]!r}"
                    )
            duration = envelope.get("duration_s")
            d_min = expected_detail.get("duration_s_min")
            d_max = expected_detail.get("duration_s_max")
            if d_min is not None and (duration is None or duration < d_min):
                problems.append(f"duration_s={duration!r} < min {d_min!r}")
            if d_max is not None and (duration is None or duration > d_max):
                problems.append(f"duration_s={duration!r} > max {d_max!r}")
            size_min = expected_detail.get("size_bytes_min")
            if size_min is not None:
                size = envelope.get("size_bytes")
                if size is None or size < size_min:
                    problems.append(f"size_bytes={size!r} < min {size_min!r}")
        else:  # fail case
            substring = expected_detail.get("error_substring")
            if substring:
                err = envelope.get("error") or ""
                if substring not in err:
                    problems.append(f"error={err!r} missing substring {substring!r}")

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


def build_qa_video_artifact_probe_experiment() -> Experiment[
    dict[str, Any], dict[str, Any]
]:
    """Assemble the probe :class:`Experiment` for the playground."""
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=qa_video_artifact_probe_cases(),
        evaluators=[
            ProbeVerdictEvaluator(),
            ProbeDetailEvaluator(),
        ],
    )


__all__ = [
    "ProbeDetailEvaluator",
    "ProbeVerdictEvaluator",
    "QA_VIDEO_ARTIFACT_PROBE_EVALUATOR_THRESHOLDS",
    "build_qa_video_artifact_probe_experiment",
    "qa_video_artifact_probe_cases",
    "qa_video_artifact_probe_task",
]
