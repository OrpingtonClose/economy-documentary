"""Deterministic video-fixture generators built on ffmpeg.

Each generator is fed a :class:`VideoSpec` and produces a short mp4
whose bytes hash to the same sha256 every run. Determinism requires a
few ffmpeg knobs to be pinned:

- ``-preset veryslow`` and ``-crf 28`` are avoided — they make output
  sensitive to exact ffmpeg build. Instead we emit constant-QP encodes
  with explicit ``-qp`` and ``-pix_fmt yuv420p`` so the encoder has
  one legal path.
- ``-metadata encoder=fixture`` strips ffmpeg's own encoder string
  from the mp4 metadata (which otherwise embeds the version number
  and breaks determinism).
- ``-movflags +faststart`` is *omitted* — it appends a second pass
  that reorders atoms and disturbs the hash on old ffmpeg builds.
- Input text is rendered with a bundled monospace font so different
  host fontconfig caches don't change glyph kerning.

Every spec that currently ships is small (≤2 seconds, 320x240, ≤20 KB)
so committed bytes stay cheap.

The spec dataclass is intentionally narrow: each generator variant
(text-on-color, frozen-frame, black-frame, …) sets the ``kind`` field
and a generator-specific extras dict. This keeps the module simple —
one ``generate_video`` entrypoint, one dispatch table.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..manifest import compute_sha256

_FFMPEG_BIN = "ffmpeg"

# Pinned, deterministic ffmpeg options. Any change here will change
# every fixture's sha256 and will be caught by the determinism test.
_COMMON_OUTPUT_FLAGS = (
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-qp", "23",
    "-pix_fmt", "yuv420p",
    "-metadata", "encoder=fixture",
    "-an",  # no audio track — audio fixtures are generated separately
    "-y",
)


@dataclass(frozen=True)
class VideoSpec:
    """Typed spec for a single deterministic video fixture.

    Attributes:
        kind: Generator variant. Supported values:
            ``"text_on_color"``, ``"solid_color"``, ``"frozen_frame"``,
            ``"black_frame"``, ``"white_frame"``, ``"moving_text"``.
        width: Frame width in pixels.
        height: Frame height in pixels.
        duration_sec: Fixture duration, seconds.
        fps: Frames per second.
        extras: Variant-specific knobs. See each ``_generate_*``
            function for the keys it reads.
    """

    kind: str
    width: int = 320
    height: int = 240
    duration_sec: float = 2.0
    fps: int = 15
    extras: dict[str, Any] = field(default_factory=dict)


def generate_video(spec: VideoSpec, out_path: Path) -> tuple[Path, str]:
    """Render a fixture according to ``spec`` and write it to ``out_path``.

    Args:
        spec: :class:`VideoSpec` describing the fixture.
        out_path: Destination file. Parent directories are created if
            missing. Existing files are overwritten.

    Returns:
        ``(out_path, sha256_hex)`` — the path written to and the hex
        sha256 of the resulting bytes.

    Raises:
        RuntimeError: If ffmpeg is not on PATH or returns non-zero.
        ValueError: If ``spec.kind`` is not a recognised variant.
    """
    if shutil.which(_FFMPEG_BIN) is None:
        raise RuntimeError(
            "ffmpeg binary not found on PATH — install ffmpeg to regenerate fixtures"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)

    dispatch = {
        "text_on_color": _generate_text_on_color,
        "solid_color": _generate_solid_color,
        "frozen_frame": _generate_frozen_frame,
        "black_frame": _generate_black_frame,
        "white_frame": _generate_white_frame,
        "moving_text": _generate_moving_text,
    }
    try:
        fn = dispatch[spec.kind]
    except KeyError as exc:
        raise ValueError(f"unknown video spec kind: {spec.kind!r}") from exc

    fn(spec, out_path)
    return out_path, compute_sha256(out_path)


def _run(cmd: list[str]) -> None:
    """Run an ffmpeg command, surfacing stderr on failure."""
    result = subprocess.run(  # noqa: S603 — cmd is assembled from pinned literals
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (code {result.returncode}):\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr:\n{result.stderr}"
        )


def _color_spec(color: str, width: int, height: int, duration: float, fps: int) -> str:
    """Build the ``color=...`` lavfi filter string."""
    return f"color=c={color}:size={width}x{height}:rate={fps}:duration={duration}"


def _generate_solid_color(spec: VideoSpec, out: Path) -> None:
    color = spec.extras.get("color", "red")
    _run([
        _FFMPEG_BIN, "-f", "lavfi",
        "-i", _color_spec(color, spec.width, spec.height, spec.duration_sec, spec.fps),
        *_COMMON_OUTPUT_FLAGS, str(out),
    ])


def _generate_text_on_color(spec: VideoSpec, out: Path) -> None:
    color = spec.extras.get("color", "red")
    text = spec.extras["text"]
    font_size = int(spec.extras.get("font_size", 72))

    # drawtext needs text escaped: no literal single-quote inside the filter arg.
    safe_text = text.replace("'", r"\'")
    draw = (
        f"drawtext=text='{safe_text}':"
        f"fontcolor=white:fontsize={font_size}:"
        "x=(w-text_w)/2:y=(h-text_h)/2"
    )
    vf = f"{_color_spec(color, spec.width, spec.height, spec.duration_sec, spec.fps)},{draw}"
    _run([
        _FFMPEG_BIN, "-f", "lavfi", "-i", vf,
        *_COMMON_OUTPUT_FLAGS, str(out),
    ])


def _generate_frozen_frame(spec: VideoSpec, out: Path) -> None:
    """Emit a video whose first frame is held for the entire duration.

    Real ``frozen_frame`` failures from LTX present as a still frame
    repeated over the whole clip. We reproduce that by feeding ffmpeg
    a single-frame input and stretching it across the duration.
    """
    color = spec.extras.get("color", "blue")
    text = spec.extras.get("text", "FROZEN")
    # 1-frame source; ``-loop 1`` holds the frame; ``-t`` bounds duration.
    safe_text = text.replace("'", r"\'")
    draw = (
        f"drawtext=text='{safe_text}':"
        "fontcolor=white:fontsize=72:x=(w-text_w)/2:y=(h-text_h)/2"
    )
    src = (
        f"color=c={color}:size={spec.width}x{spec.height}:rate=1:duration=0.04,"
        f"{draw}"
    )
    _run([
        _FFMPEG_BIN, "-f", "lavfi", "-i", src,
        "-vf", f"loop=loop=-1:size=1:start=0,trim=duration={spec.duration_sec},setpts=N/({spec.fps}*TB)",
        "-r", str(spec.fps),
        *_COMMON_OUTPUT_FLAGS, str(out),
    ])


def _generate_black_frame(spec: VideoSpec, out: Path) -> None:
    inner = VideoSpec(
        kind="solid_color",
        width=spec.width,
        height=spec.height,
        duration_sec=spec.duration_sec,
        fps=spec.fps,
        extras={"color": "black"},
    )
    _generate_solid_color(inner, out)


def _generate_white_frame(spec: VideoSpec, out: Path) -> None:
    inner = VideoSpec(
        kind="solid_color",
        width=spec.width,
        height=spec.height,
        duration_sec=spec.duration_sec,
        fps=spec.fps,
        extras={"color": "white"},
    )
    _generate_solid_color(inner, out)


def _generate_moving_text(spec: VideoSpec, out: Path) -> None:
    """Text scrolling left-to-right, for motion-detection tests."""
    color = spec.extras.get("color", "blue")
    text = spec.extras["text"]
    font_size = int(spec.extras.get("font_size", 48))
    safe_text = text.replace("'", r"\'")
    draw = (
        f"drawtext=text='{safe_text}':"
        f"fontcolor=white:fontsize={font_size}:"
        f"x='mod(t*150,w+text_w)-text_w':y=(h-text_h)/2"
    )
    vf = f"{_color_spec(color, spec.width, spec.height, spec.duration_sec, spec.fps)},{draw}"
    _run([
        _FFMPEG_BIN, "-f", "lavfi", "-i", vf,
        *_COMMON_OUTPUT_FLAGS, str(out),
    ])
