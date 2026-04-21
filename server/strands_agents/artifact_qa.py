"""Component 10 — deterministic per-artifact QA.

The production SubAgent calls :func:`evaluate_visual_artifact_quality`
on every completed LTX video clip before accepting it into the timeline.
The check is deterministic (no LLM) and covers the four hard gates from
``docs/strands-migration/components/10-production-supervisor.md``:

* **Frame count** — ``frames`` must equal ``duration_sec * fps`` within
  one frame of tolerance. A frame-count mismatch usually means the
  worker truncated the render.
* **Duration** — ``duration_sec`` must match the scene's target within
  :data:`DURATION_TOLERANCE_SEC`. Under-run means the worker gave up;
  over-run means the LTX sampler drifted.
* **Codec** — must be in :data:`ALLOWED_CODECS`. Unknown codecs break
  the OTIO assembly stage.
* **Black frames** — ``black_frame_fraction`` must be below
  :data:`BLACK_FRAME_CEILING`. Above that threshold the clip is almost
  certainly a renderer failure even if every other signal is healthy.

The tool returns a structured verdict (``pass`` / ``fail`` / ``warn``)
with one :class:`ArtifactIssue` per failed check so the SubAgent can
decide between ``retry_scene``, ``fix_scene``, ``skip_scene``, and
``request_escalation`` without re-running the LLM.
"""

from __future__ import annotations

import logging
from typing import Any

from strands import tool

logger = logging.getLogger(__name__)


#: Default frame-rate used when the artifact payload does not carry
#: one. LTX defaults to 24 fps throughout the documentary pipeline.
DEFAULT_FPS: int = 24

#: Permissible deviation between ``duration_sec`` reported by the
#: worker and the scene's target duration. Anything larger is a fail.
DURATION_TOLERANCE_SEC: float = 0.2

#: Maximum fraction of black frames tolerated before the clip is
#: rejected. Matches the value in
#: ``server/critique/visual_invariants.py``.
BLACK_FRAME_CEILING: float = 0.05

#: Codecs the downstream OTIO assembly pipeline can ingest.
ALLOWED_CODECS: frozenset[str] = frozenset({"h264", "h265", "hevc"})

#: Verdict labels carried by the tool output. Aligned with the
#: :class:`strands_agents.evals.evaluators.critique_store` vocabulary.
VERDICT_PASS: str = "pass"
VERDICT_WARN: str = "warn"
VERDICT_FAIL: str = "fail"


def _coerce_float(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric, got bool")
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"{field} must be numeric, got {type(value).__name__}")


def _coerce_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be int, got bool")
    if isinstance(value, int):
        return value
    raise ValueError(f"{field} must be int, got {type(value).__name__}")


@tool
def evaluate_visual_artifact_quality(
    artifact: dict[str, Any],
    target_duration_sec: float,
    *,
    fps: int = DEFAULT_FPS,
    duration_tolerance_sec: float = DURATION_TOLERANCE_SEC,
    black_frame_ceiling: float = BLACK_FRAME_CEILING,
) -> dict[str, Any]:
    """Run deterministic per-artifact QA on one rendered video clip.

    Args:
        artifact: Completion payload returned by the LTX worker. Must
            carry ``artifact_path`` (str), ``frames`` (int),
            ``duration_sec`` (float), ``codec`` (str), and
            ``black_frame_fraction`` (float). Extra keys are ignored.
        target_duration_sec: Scene's target duration. Used to detect
            under- or over-run.
        fps: Expected frame-rate. Defaults to :data:`DEFAULT_FPS`.
        duration_tolerance_sec: Allowed deviation around
            ``target_duration_sec``. Defaults to
            :data:`DURATION_TOLERANCE_SEC`.
        black_frame_ceiling: Max fraction of black frames tolerated.
            Defaults to :data:`BLACK_FRAME_CEILING`.

    Returns:
        Dict with:

        * ``verdict`` — one of :data:`VERDICT_PASS` /
          :data:`VERDICT_WARN` / :data:`VERDICT_FAIL`.
        * ``passed`` — bool (``verdict == pass``).
        * ``issues`` — list of ``{code, severity, actual, expected,
          message}`` dicts, one per failed check.
        * ``checks`` — dict with per-check results for evaluator
          consumption.

    Raises:
        ValueError: On malformed artifact payload or non-positive
            ``target_duration_sec``.
    """
    if target_duration_sec <= 0:
        raise ValueError(
            f"target_duration_sec must be > 0, got {target_duration_sec}"
        )
    if not isinstance(artifact, dict):
        raise ValueError(
            f"artifact must be dict, got {type(artifact).__name__}"
        )

    artifact_path = artifact.get("artifact_path")
    if not isinstance(artifact_path, str) or not artifact_path:
        raise ValueError("artifact.artifact_path must be a non-empty string")

    frames = _coerce_int(artifact.get("frames"), field="artifact.frames")
    duration_sec = _coerce_float(
        artifact.get("duration_sec"), field="artifact.duration_sec"
    )
    black_frame_fraction = _coerce_float(
        artifact.get("black_frame_fraction", 0.0),
        field="artifact.black_frame_fraction",
    )
    codec = artifact.get("codec")
    if not isinstance(codec, str) or not codec:
        raise ValueError("artifact.codec must be a non-empty string")

    issues: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    # Frame-count check — tolerant of ±1 to account for rounding.
    expected_frames = max(1, int(round(target_duration_sec * fps)))
    frame_delta = frames - expected_frames
    frame_ok = abs(frame_delta) <= 1
    checks["frame_count"] = {
        "ok": frame_ok,
        "actual": frames,
        "expected": expected_frames,
        "delta": frame_delta,
    }
    if not frame_ok:
        issues.append(
            {
                "code": "frame_count_mismatch",
                "severity": VERDICT_FAIL,
                "actual": frames,
                "expected": expected_frames,
                "message": (
                    f"frames={frames} deviates from expected {expected_frames} "
                    f"(delta={frame_delta})"
                ),
            }
        )

    # Duration check — tolerance-bounded.
    duration_delta = duration_sec - target_duration_sec
    duration_ok = abs(duration_delta) <= duration_tolerance_sec
    checks["duration"] = {
        "ok": duration_ok,
        "actual": duration_sec,
        "expected": target_duration_sec,
        "delta": duration_delta,
    }
    if not duration_ok:
        issues.append(
            {
                "code": "duration_mismatch",
                "severity": VERDICT_FAIL,
                "actual": duration_sec,
                "expected": target_duration_sec,
                "message": (
                    f"duration_sec={duration_sec:.3f} deviates from "
                    f"{target_duration_sec:.3f} "
                    f"by {duration_delta:+.3f}s (tolerance "
                    f"±{duration_tolerance_sec:.3f}s)"
                ),
            }
        )

    # Codec check — strict allow-list.
    codec_lower = codec.lower()
    codec_ok = codec_lower in ALLOWED_CODECS
    checks["codec"] = {
        "ok": codec_ok,
        "actual": codec,
        "expected": sorted(ALLOWED_CODECS),
    }
    if not codec_ok:
        issues.append(
            {
                "code": "codec_unsupported",
                "severity": VERDICT_FAIL,
                "actual": codec,
                "expected": sorted(ALLOWED_CODECS),
                "message": (
                    f"codec={codec!r} not in the assembly allow-list "
                    f"{sorted(ALLOWED_CODECS)}"
                ),
            }
        )

    # Black-frame fraction — warn at half the ceiling, fail above.
    warn_threshold = black_frame_ceiling / 2.0
    if black_frame_fraction > black_frame_ceiling:
        black_verdict = VERDICT_FAIL
    elif black_frame_fraction > warn_threshold:
        black_verdict = VERDICT_WARN
    else:
        black_verdict = VERDICT_PASS
    checks["black_frames"] = {
        "ok": black_verdict == VERDICT_PASS,
        "actual": black_frame_fraction,
        "ceiling": black_frame_ceiling,
        "verdict": black_verdict,
    }
    if black_verdict == VERDICT_FAIL:
        issues.append(
            {
                "code": "black_frame_ceiling_exceeded",
                "severity": VERDICT_FAIL,
                "actual": black_frame_fraction,
                "expected": black_frame_ceiling,
                "message": (
                    f"black_frame_fraction={black_frame_fraction:.3f} "
                    f"exceeds ceiling {black_frame_ceiling:.3f}"
                ),
            }
        )
    elif black_verdict == VERDICT_WARN:
        issues.append(
            {
                "code": "black_frame_warning",
                "severity": VERDICT_WARN,
                "actual": black_frame_fraction,
                "expected": warn_threshold,
                "message": (
                    f"black_frame_fraction={black_frame_fraction:.3f} "
                    f"above warn threshold {warn_threshold:.3f}"
                ),
            }
        )

    has_fail = any(issue["severity"] == VERDICT_FAIL for issue in issues)
    has_warn = any(issue["severity"] == VERDICT_WARN for issue in issues)
    if has_fail:
        verdict = VERDICT_FAIL
    elif has_warn:
        verdict = VERDICT_WARN
    else:
        verdict = VERDICT_PASS

    logger.debug(
        "artifact_path=<%s>, verdict=<%s>, issue_count=<%d> | artifact qa complete",
        artifact_path,
        verdict,
        len(issues),
    )

    return {
        "verdict": verdict,
        "passed": verdict == VERDICT_PASS,
        "issues": issues,
        "checks": checks,
        "artifact_path": artifact_path,
    }


__all__ = [
    "ALLOWED_CODECS",
    "BLACK_FRAME_CEILING",
    "DEFAULT_FPS",
    "DURATION_TOLERANCE_SEC",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "VERDICT_WARN",
    "evaluate_visual_artifact_quality",
]
