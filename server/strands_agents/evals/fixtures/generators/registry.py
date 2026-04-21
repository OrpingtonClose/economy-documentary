"""Declared set of fixtures and their generator specs.

This module is the single place where "which fixtures exist" is
declared. Each entry ties:

- a stable fixture id,
- the axis (clear-cut question) it answers,
- the expected binary verdict the judge stack must return,
- the natural-language prompt asked of the judge,
- the :class:`VideoSpec` / :class:`AudioSpec` that produces the
  deterministic bytes.

The :func:`build_all` function walks this list, regenerates every
fixture on disk, and writes a fresh ``manifest.json`` with the
current sha256s. Callers can use it as a library (regenerate a
single entry) or through the ``__main__`` CLI (regenerate the whole
corpus).

The principle: judges are measured on pairs. Every positive fixture
(judge must accept) has a sibling negative fixture (judge must
reject). A judge that can't tell them apart is a candidate for
discard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..manifest import FixtureEntry, FixtureManifest
from .audio import AudioSpec, generate_audio
from .video import VideoSpec, generate_video


@dataclass(frozen=True)
class FixtureDeclaration:
    """Declarative spec for a single fixture.

    Attributes:
        id: Stable identifier. Used in ``Case.name`` and manifest
            lookups.
        axis: Clear-cut question the fixture exercises
            (``text_present``, ``color_dominance``, etc.).
        media: ``"video"`` or ``"audio"``.
        relative_path: Path under ``fixtures/`` on disk.
        expected_verdict: ``"yes"``, ``"no"``, or ``"reject"`` —
            what the judge stack MUST return.
        prompt: Short binary question for the judge.
        video_spec: Populated for video fixtures; ``None`` for audio.
        audio_spec: Populated for audio fixtures; ``None`` for video.
        public_url: Optional public URL (used for providers that need
            a URL instead of raw bytes).
    """

    id: str
    axis: str
    media: str
    relative_path: str
    expected_verdict: str
    prompt: str
    video_spec: VideoSpec | None = None
    audio_spec: AudioSpec | None = None
    public_url: str | None = None


# Video fixtures.
#
# Positive/negative pairs exercise the binary judge calls a clear-cut
# gate can make. Every pair uses the same prompt so a judge that flips
# on ``HELLO`` / ``GOODBYE`` is caught immediately.
_VIDEO_DECLS: tuple[FixtureDeclaration, ...] = (
    # --- text-present: HELLO vs GOODBYE ---
    FixtureDeclaration(
        id="video_hello_red",
        axis="text_present_hello",
        media="video",
        relative_path="video/video_hello_red.mp4",
        expected_verdict="yes",
        prompt="Does this video show the English word HELLO on screen? Answer yes or no.",
        video_spec=VideoSpec(
            kind="text_on_color",
            extras={"color": "red", "text": "HELLO", "font_size": 72},
        ),
    ),
    FixtureDeclaration(
        id="video_goodbye_green",
        axis="text_present_hello",
        media="video",
        relative_path="video/video_goodbye_green.mp4",
        expected_verdict="no",
        prompt="Does this video show the English word HELLO on screen? Answer yes or no.",
        video_spec=VideoSpec(
            kind="text_on_color",
            extras={"color": "green", "text": "GOODBYE", "font_size": 72},
        ),
    ),
    # --- color-dominance: red vs green ---
    FixtureDeclaration(
        id="video_solid_red",
        axis="color_red",
        media="video",
        relative_path="video/video_solid_red.mp4",
        expected_verdict="yes",
        prompt="Is this video predominantly red? Answer yes or no.",
        video_spec=VideoSpec(kind="solid_color", extras={"color": "red"}),
    ),
    FixtureDeclaration(
        id="video_solid_green",
        axis="color_red",
        media="video",
        relative_path="video/video_solid_green.mp4",
        expected_verdict="no",
        prompt="Is this video predominantly red? Answer yes or no.",
        video_spec=VideoSpec(kind="solid_color", extras={"color": "green"}),
    ),
    # --- motion-vs-still ---
    FixtureDeclaration(
        id="video_moving_text",
        axis="motion_present",
        media="video",
        relative_path="video/video_moving_text.mp4",
        expected_verdict="yes",
        prompt="Does anything move in this video? Answer yes or no.",
        video_spec=VideoSpec(
            kind="moving_text",
            extras={"color": "blue", "text": "MOVING", "font_size": 48},
        ),
    ),
    FixtureDeclaration(
        id="video_still_frame",
        axis="motion_present",
        media="video",
        relative_path="video/video_still_frame.mp4",
        expected_verdict="no",
        prompt="Does anything move in this video? Answer yes or no.",
        video_spec=VideoSpec(kind="solid_color", extras={"color": "blue"}),
    ),
    # --- failure modes: must be rejected ---
    FixtureDeclaration(
        id="video_frozen",
        axis="failure_frozen",
        media="video",
        relative_path="video/video_frozen.mp4",
        expected_verdict="reject",
        prompt=(
            "This video is supposed to show dynamic content. "
            "Does it look like a single frozen frame held for the "
            "whole duration? Answer yes or no."
        ),
        video_spec=VideoSpec(
            kind="frozen_frame",
            extras={"color": "blue", "text": "FROZEN"},
        ),
    ),
    FixtureDeclaration(
        id="video_black",
        axis="failure_black",
        media="video",
        relative_path="video/video_black.mp4",
        expected_verdict="reject",
        prompt=(
            "Is this video almost entirely black, with no visible "
            "content? Answer yes or no."
        ),
        video_spec=VideoSpec(kind="black_frame"),
    ),
    FixtureDeclaration(
        id="video_white",
        axis="failure_blown_out",
        media="video",
        relative_path="video/video_white.mp4",
        expected_verdict="reject",
        prompt=(
            "Is this video almost entirely pure white, with no visible "
            "content? Answer yes or no."
        ),
        video_spec=VideoSpec(kind="white_frame"),
    ),
)

# Audio fixtures.
_AUDIO_DECLS: tuple[FixtureDeclaration, ...] = (
    # --- narration present/absent ---
    FixtureDeclaration(
        id="audio_hello_narration",
        axis="narration_present",
        media="audio",
        relative_path="audio/audio_hello_narration.wav",
        expected_verdict="yes",
        prompt=(
            "Does this audio clip contain intelligible English speech? "
            "Answer yes or no."
        ),
        audio_spec=AudioSpec(
            kind="narration",
            extras={"text": "hello world how are you today"},
        ),
    ),
    FixtureDeclaration(
        id="audio_silence",
        axis="narration_present",
        media="audio",
        relative_path="audio/audio_silence.wav",
        expected_verdict="no",
        prompt=(
            "Does this audio clip contain intelligible English speech? "
            "Answer yes or no."
        ),
        audio_spec=AudioSpec(kind="silence", duration_sec=2.0),
    ),
    # --- language check ---
    FixtureDeclaration(
        id="audio_english_narration",
        axis="language_english",
        media="audio",
        relative_path="audio/audio_english_narration.wav",
        expected_verdict="yes",
        prompt=(
            "Is the spoken language in this audio English? Answer "
            "yes or no."
        ),
        audio_spec=AudioSpec(
            kind="narration",
            extras={"text": "the economy grew by two percent last quarter"},
        ),
    ),
    FixtureDeclaration(
        id="audio_spanish_narration",
        axis="language_english",
        media="audio",
        relative_path="audio/audio_spanish_narration.wav",
        expected_verdict="no",
        prompt=(
            "Is the spoken language in this audio English? Answer "
            "yes or no."
        ),
        audio_spec=AudioSpec(
            kind="narration",
            extras={"text": "la economia crecio dos por ciento", "voice": "es"},
        ),
    ),
    # --- failure modes ---
    FixtureDeclaration(
        id="audio_noise_only",
        axis="failure_noise",
        media="audio",
        relative_path="audio/audio_noise_only.wav",
        expected_verdict="reject",
        prompt=(
            "Is this audio clip just white noise or static, with no "
            "intelligible speech? Answer yes or no."
        ),
        audio_spec=AudioSpec(
            kind="white_noise",
            duration_sec=2.0,
            extras={"seed": 0, "amplitude": 0.3},
        ),
    ),
    FixtureDeclaration(
        id="audio_clipping",
        axis="failure_clipping",
        media="audio",
        relative_path="audio/audio_clipping.wav",
        expected_verdict="reject",
        prompt=(
            "Does this audio sound heavily distorted, like it is "
            "clipping or over-amplified? Answer yes or no."
        ),
        audio_spec=AudioSpec(
            kind="clipping",
            extras={"text": "hello world this is a distorted test"},
        ),
    ),
)


def all_declarations() -> tuple[FixtureDeclaration, ...]:
    """Return every declared fixture (video + audio)."""
    return _VIDEO_DECLS + _AUDIO_DECLS


def _regenerate_one(decl: FixtureDeclaration, root: Path) -> FixtureEntry:
    """Regenerate a single fixture on disk and return its manifest entry."""
    out_path = root / decl.relative_path
    if decl.media == "video":
        assert decl.video_spec is not None, f"{decl.id} missing video_spec"
        _, sha = generate_video(decl.video_spec, out_path)
        generator: dict[str, Any] = {
            "kind": decl.video_spec.kind,
            "width": decl.video_spec.width,
            "height": decl.video_spec.height,
            "duration_sec": decl.video_spec.duration_sec,
            "fps": decl.video_spec.fps,
            "extras": dict(decl.video_spec.extras),
        }
    elif decl.media == "audio":
        assert decl.audio_spec is not None, f"{decl.id} missing audio_spec"
        _, sha = generate_audio(decl.audio_spec, out_path)
        generator = {
            "kind": decl.audio_spec.kind,
            "duration_sec": decl.audio_spec.duration_sec,
            "sample_rate": decl.audio_spec.sample_rate,
            "extras": dict(decl.audio_spec.extras),
        }
    else:
        raise ValueError(f"unknown media {decl.media!r} for fixture {decl.id!r}")

    return FixtureEntry(
        id=decl.id,
        axis=decl.axis,
        media=decl.media,
        relative_path=decl.relative_path,
        sha256=sha,
        expected_verdict=decl.expected_verdict,
        prompt=decl.prompt,
        public_url=decl.public_url,
        generator=generator,
    )


def build_all(root: Path) -> FixtureManifest:
    """Regenerate every declared fixture under ``root`` and return the manifest.

    Side effect: writes ``root/manifest.json`` with one entry per
    declaration.

    Args:
        root: The ``fixtures/`` package directory.

    Returns:
        A :class:`FixtureManifest` mirroring what was written to disk.
    """
    entries = tuple(_regenerate_one(decl, root) for decl in all_declarations())
    manifest = FixtureManifest(entries=entries)
    manifest_json = {
        "fixtures": [e.to_dict() for e in entries],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest_json, indent=2, sort_keys=True) + "\n"
    )
    return manifest
