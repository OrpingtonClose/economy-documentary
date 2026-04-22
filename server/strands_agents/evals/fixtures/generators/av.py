"""Deterministic combined audio+video fixture generator.

Takes an already-generated video fixture (an ``.mp4`` with no audio
track) and an already-generated audio fixture (a mono 16-bit PCM
``.wav``) and muxes them together into a single ``.mp4`` with
both streams. Optionally, the audio track can be deliberately
offset in time — the desync class of failure mode.

Offset semantics (sign-of-``audio_offset_sec``):

* ``0.0`` — straight mux, no shift.
* ``> 0`` — **audio behind**. A silent head of ``audio_offset_sec``
  seconds is prepended to the audio via ``adelay``; the video
  starts at t=0 and the audio joins later. In the final file
  the audio onset falls after the video content onset.
* ``< 0`` — **audio ahead**. A black head of
  ``abs(audio_offset_sec)`` seconds is prepended to the video via
  ``tpad``; the audio starts at t=0 and the video content joins
  later. In the final file the audio onset falls before the video
  content onset.

The output duration is exactly ``video_duration + abs(audio_offset_sec)``
when an offset is applied, and ``video_duration`` otherwise. This
matches production assembly behaviour, where pauses fall around
scene boundaries and the timeline length follows the video rail.

The audio is re-encoded to AAC because that is what assembly emits
in production; the video stream is either copied unchanged (no
offset) or re-encoded with ``tpad`` prepend (ahead case). The
encoder options mirror :mod:`video.py` so the re-encoded half
stays deterministic.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..manifest import compute_sha256

_FFMPEG_BIN = "ffmpeg"

# Pinned, deterministic ffmpeg mux options. Any change here will
# change every AV fixture's sha256 and will be caught by the
# determinism test.
_AUDIO_OUTPUT_FLAGS: tuple[str, ...] = (
    "-c:a", "aac",
    "-b:a", "64k",
    "-ar", "16000",
    "-ac", "1",
)

# Match video.py's deterministic encoder options for any
# re-encode path (audio_ahead case).
_VIDEO_REENCODE_FLAGS: tuple[str, ...] = (
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-qp", "23",
    "-pix_fmt", "yuv420p",
)

_COMMON_META_FLAGS: tuple[str, ...] = (
    "-metadata", "encoder=fixture",
    "-y",
)


@dataclass(frozen=True)
class AVSpec:
    """Typed spec for a single deterministic AV fixture.

    Attributes:
        video_source: Path (relative to the fixtures root) of the
            source video fixture to mux in. Must be an ``.mp4``
            with no audio track (as produced by :mod:`video`).
        audio_source: Path (relative to the fixtures root) of the
            source audio fixture to mux in. Must be a mono 16-bit
            PCM WAV (as produced by :mod:`audio`).
        audio_offset_sec: Signed audio offset in seconds.
            ``0.0`` means straight mux. ``> 0`` means audio
            behind (silence prepended to audio). ``< 0`` means
            audio ahead (black prepended to video).
        extras: Reserved for future knobs.
    """

    video_source: str
    audio_source: str
    audio_offset_sec: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)


def generate_av(
    spec: AVSpec, out_path: Path, *, fixtures_root: Path
) -> tuple[Path, str]:
    """Render a combined AV fixture according to ``spec``.

    Args:
        spec: The mux recipe.
        out_path: Where to write the combined ``.mp4``.
        fixtures_root: Directory the source video/audio paths are
            resolved against (typically ``fixtures/``).

    Returns:
        ``(out_path, sha256_hex)``.

    Raises:
        RuntimeError: If ffmpeg is not on ``PATH``.
        FileNotFoundError: If the source video or audio fixture
            does not exist.
    """
    if shutil.which(_FFMPEG_BIN) is None:
        raise RuntimeError(
            "ffmpeg binary not found on PATH — install ffmpeg to "
            "regenerate AV fixtures"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    video_path = fixtures_root / spec.video_source
    audio_path = fixtures_root / spec.audio_source
    if not video_path.exists():
        raise FileNotFoundError(
            f"source video fixture not found: {video_path}"
        )
    if not audio_path.exists():
        raise FileNotFoundError(
            f"source audio fixture not found: {audio_path}"
        )

    cmd: list[str] = [
        _FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
    ]

    offset = spec.audio_offset_sec
    if offset == 0.0:
        # Straight mux: copy video, re-encode audio to AAC.
        cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])
        cmd.extend(["-c:v", "copy"])
        cmd.extend(_AUDIO_OUTPUT_FLAGS)
        cmd.extend(["-shortest"])
    elif offset > 0:
        # Audio behind: prepend silence to audio; video unchanged.
        ms = int(round(offset * 1000))
        filter_complex = (
            f"[1:a]adelay={ms}|{ms},"
            f"apad=pad_dur={offset:.3f}[a]"
        )
        cmd.extend(["-filter_complex", filter_complex])
        cmd.extend(["-map", "0:v:0", "-map", "[a]"])
        cmd.extend(["-c:v", "copy"])
        cmd.extend(_AUDIO_OUTPUT_FLAGS)
    else:
        # Audio ahead: prepend black to video via tpad; audio
        # unchanged.
        pad_dur = abs(offset)
        filter_complex = (
            f"[0:v]tpad=start_duration={pad_dur:.3f}:color=black[v]"
        )
        cmd.extend(["-filter_complex", filter_complex])
        cmd.extend(["-map", "[v]", "-map", "1:a:0"])
        cmd.extend(_VIDEO_REENCODE_FLAGS)
        cmd.extend(_AUDIO_OUTPUT_FLAGS)

    cmd.extend(_COMMON_META_FLAGS)
    cmd.append(str(out_path))

    subprocess.run(cmd, check=True)
    return out_path, compute_sha256(out_path)


__all__ = ["AVSpec", "generate_av"]
