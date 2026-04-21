"""Live-judge proof of robustness for Component 10 (production-supervisor).

Clear-cut contracts proved here:

1. Deterministic: :func:`evaluate_visual_artifact_quality` flags each
   hard-gate violation cleanly — frame-count mismatch, duration
   mismatch, unsupported codec, black-frame ceiling exceeded.  A
   clean artifact passes.
2. Live: before dispatch, a concept's prompt must stay on-topic.
   Gemini judges on-topic prompts as on-topic and rejects obviously
   off-topic prompts (e.g., cyberpunk imagery for a Weimar
   hyperinflation documentary).  If this judgment flips, the
   off-topic prompt would reach the LTX worker.
"""

from __future__ import annotations

from typing import Any


from strands_agents.artifact_qa import (
    ALLOWED_CODECS,
    BLACK_FRAME_CEILING,
    DEFAULT_FPS,
    DURATION_TOLERANCE_SEC,
    evaluate_visual_artifact_quality,
)

from .._judges import judge_text_yes
from ..conftest import requires_google_api


_TOPIC = "the 1923 Weimar hyperinflation"


def _artifact(
    *,
    path: str = "b2://render/scene-1.mp4",
    frames: int | None = None,
    duration_sec: float = 5.0,
    codec: str = "h264",
    black_frame_fraction: float = 0.0,
    fps: int = DEFAULT_FPS,
) -> dict[str, Any]:
    if frames is None:
        frames = int(round(duration_sec * fps))
    return {
        "artifact_path": path,
        "frames": frames,
        "duration_sec": duration_sec,
        "codec": codec,
        "black_frame_fraction": black_frame_fraction,
    }


# ---------------------------------------------------------------------------
# Deterministic QA
# ---------------------------------------------------------------------------


def test_clean_artifact_passes_qa() -> None:
    result = evaluate_visual_artifact_quality.__wrapped__(
        artifact=_artifact(),
        target_duration_sec=5.0,
    )
    assert result["verdict"] == "pass"
    assert result["passed"]
    assert result["issues"] == []


def test_frame_count_mismatch_fails_qa() -> None:
    # 5s at 24fps = 120 frames; send 60 frames (half-render).
    result = evaluate_visual_artifact_quality.__wrapped__(
        artifact=_artifact(frames=60, duration_sec=5.0),
        target_duration_sec=5.0,
    )
    assert result["verdict"] == "fail"
    assert any(issue["code"] == "frame_count_mismatch" for issue in result["issues"])


def test_duration_mismatch_fails_qa() -> None:
    # Target 5s, artifact reports 7s — way outside tolerance.
    over_tolerance = 5.0 + DURATION_TOLERANCE_SEC + 2.0
    result = evaluate_visual_artifact_quality.__wrapped__(
        artifact=_artifact(duration_sec=over_tolerance),
        target_duration_sec=5.0,
    )
    assert result["verdict"] == "fail"
    assert any(issue["code"] == "duration_mismatch" for issue in result["issues"])


def test_unsupported_codec_fails_qa() -> None:
    bad_codec = "vp9"  # definitely not in ALLOWED_CODECS
    assert bad_codec not in ALLOWED_CODECS
    result = evaluate_visual_artifact_quality.__wrapped__(
        artifact=_artifact(codec=bad_codec),
        target_duration_sec=5.0,
    )
    assert result["verdict"] == "fail"
    assert any(issue["code"] == "codec_unsupported" for issue in result["issues"])


def test_black_frame_ceiling_exceeded_fails_qa() -> None:
    bad_fraction = BLACK_FRAME_CEILING + 0.1
    result = evaluate_visual_artifact_quality.__wrapped__(
        artifact=_artifact(black_frame_fraction=bad_fraction),
        target_duration_sec=5.0,
    )
    assert result["verdict"] == "fail"
    assert any(
        issue["code"] == "black_frame_ceiling_exceeded" for issue in result["issues"]
    )


def test_black_frame_warning_passes_without_other_fails() -> None:
    # Above half-ceiling but below ceiling — warn, not fail.
    warn_fraction = BLACK_FRAME_CEILING / 2.0 + 0.001
    result = evaluate_visual_artifact_quality.__wrapped__(
        artifact=_artifact(black_frame_fraction=warn_fraction),
        target_duration_sec=5.0,
    )
    assert result["verdict"] == "warn"
    assert not result["passed"]


# ---------------------------------------------------------------------------
# Live semantic gate: concept prompt must stay on-topic
# ---------------------------------------------------------------------------


_ON_TOPIC_PROMPT = (
    "Grainy 16mm archival footage of a Berlin street in 1923. A "
    "family pushes a wooden wheelbarrow overloaded with bundles of "
    "devalued banknotes down a cobblestone lane toward a bakery with "
    "an empty window. Sepia tones, muted daylight, handheld camera "
    "with a slow dolly in."
)

_OFF_TOPIC_PROMPT = (
    "A neon-drenched cyberpunk skyline at night, towering "
    "holographic advertisements in Japanese, flying cars weaving "
    "between glass skyscrapers in heavy rain.  Rendered in glossy "
    "Blade Runner 2049 style with volumetric neon fog."
)


@requires_google_api
def test_on_topic_concept_prompt_passes_semantic_gate() -> None:
    verdict = judge_text_yes(
        "You are reviewing a visual concept prompt about to be sent "
        "to a video renderer.  Is this prompt on-topic for a short "
        f"documentary about {_TOPIC}? Answer with a single word: "
        f"yes or no.\n\nPrompt:\n---\n{_ON_TOPIC_PROMPT}\n---"
    )
    assert not verdict.disabled, f"judge disabled: {verdict.error}"
    assert verdict.is_yes, f"on-topic prompt rejected by judge: {verdict.answer!r}"


@requires_google_api
def test_off_topic_concept_prompt_fails_semantic_gate() -> None:
    verdict = judge_text_yes(
        "You are reviewing a visual concept prompt about to be sent "
        "to a video renderer.  Is this prompt on-topic for a short "
        f"documentary about {_TOPIC}? Answer with a single word: "
        f"yes or no.\n\nPrompt:\n---\n{_OFF_TOPIC_PROMPT}\n---"
    )
    assert not verdict.disabled, f"judge disabled: {verdict.error}"
    assert not verdict.is_yes, (
        f"blatantly off-topic prompt accepted by judge: {verdict.answer!r}"
    )
