"""Deterministic title + end card renderer.

Issue #105 requires title and end cards on every deliverable — the PAG
run had no outro and a cold start on a generated clip.  Issue #101
showed LTX-rendered text becomes gibberish, so the cards must be
rendered deterministically with ffmpeg ``drawtext`` on a tone-on-tone
background rather than sent through a diffusion model.

Each card renders as a single ``.mp4`` with matched video codec / pix_fmt
from the master profile and either silent audio or the brand sting
fixture.  The assembler consumes them as explicit OTIO scene entries
tagged ``type='title_card'`` / ``type='end_card'``.

Scene ``type`` taxonomy coordinated with W2 (scripting) and W4 (OTIO):

    scene.type ∈ {title_card, hook, body, outro, end_card}

``title_card`` and ``end_card`` scenes are rendered by this module;
``hook``, ``body``, ``outro`` are the LTX-generated clips managed by
the production stage.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional, Tuple

from .master_profiles import MasterProfile

logger = logging.getLogger(__name__)

# Scene type taxonomy — importable from callers that build OTIO scenes.
SCENE_TYPES: Tuple[str, ...] = (
    "title_card",
    "hook",
    "body",
    "outro",
    "end_card",
)


# Default font: DejaVuSans-Bold ships on every Ubuntu runner (ffmpeg's
# drawtext can't fall back through fontconfig without ``:fontfile=``).
_DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


class TitleCardRenderError(RuntimeError):
    """Raised when ffmpeg fails to render a title / end card."""


def _escape_drawtext(text: str) -> str:
    r"""Escape a string for use inside a ``drawtext`` value.

    drawtext splits on ``:`` (option boundary) and uses ``\`` for
    escapes; we also convert single quotes which would otherwise close
    the argument.  Newlines survive via ``\n`` so callers can wrap
    subtitles onto two lines.
    """
    return (
        text.replace("\\", "\\\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("%", r"\%")
    )


@dataclass(frozen=True)
class CardSpec:
    """Declarative spec for one title / end card."""

    kind: str                     # "title_card" or "end_card"
    duration_sec: float           # 2-3s for title, 3-5s for end
    title: str
    subtitle: str = ""
    bg_color: str = "0x111111"    # tone-on-tone dark grey
    fg_color: str = "0xEAEAEA"    # off-white
    accent_color: str = "0x7AA2F7"
    # Audio bed: ``None`` means silent; a path (wav) means mux that bed in
    # at the profile's target LUFS.
    audio_bed_path: Optional[str] = None
    # Extra lines (e.g. sources list, CTA, channel name).  Each entry is
    # rendered beneath the subtitle with the accent colour.
    extra_lines: tuple = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.kind not in ("title_card", "end_card"):
            raise ValueError(
                f"CardSpec.kind must be 'title_card' or 'end_card', "
                f"got {self.kind!r}"
            )
        if self.duration_sec <= 0:
            raise ValueError(
                f"CardSpec.duration_sec must be > 0, got {self.duration_sec}"
            )


# ---------------------------------------------------------------------------
# Card builders (used by the assembler to construct a default title/end)
# ---------------------------------------------------------------------------

def title_card_for_topic(
    topic: str,
    channel: str,
    duration_sec: float = 2.5,
    audio_bed_path: Optional[str] = None,
) -> CardSpec:
    """Build a title card from the run's topic + channel/brand name."""
    return CardSpec(
        kind="title_card",
        duration_sec=duration_sec,
        title=topic,
        subtitle=channel,
        audio_bed_path=audio_bed_path,
    )


def end_card_for_run(
    channel: str,
    cta: str = "Subscribe for more",
    sources_line: str = "Sources in the description",
    duration_sec: float = 4.0,
    audio_bed_path: Optional[str] = None,
) -> CardSpec:
    """Build a standard end card (CTA + sources pointer + brand)."""
    return CardSpec(
        kind="end_card",
        duration_sec=duration_sec,
        title=channel,
        subtitle=cta,
        extra_lines=(sources_line,),
        audio_bed_path=audio_bed_path,
    )


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _build_drawtext_chain(
    spec: CardSpec,
    profile: MasterProfile,
    font: str,
) -> str:
    """Build the ``drawtext`` filter chain for this card.

    Geometry scales with profile resolution so the same spec reads the
    same way at 1080p, 1920p (9:16 shorts), or 320p preview.
    """
    # Size the title at ~1/12 of the vertical resolution, subtitle at ~1/22.
    title_size = max(24, profile.height // 12)
    subtitle_size = max(16, profile.height // 22)
    extra_size = max(14, profile.height // 28)

    title_y = f"(h/2)-(text_h*1.2)"
    subtitle_y = f"(h/2)+(text_h*0.2)"

    parts = [
        (
            f"drawtext=fontfile={font}"
            f":text='{_escape_drawtext(spec.title)}'"
            f":fontcolor={spec.fg_color}:fontsize={title_size}"
            f":x=(w-text_w)/2:y={title_y}"
        ),
    ]
    if spec.subtitle:
        parts.append(
            f"drawtext=fontfile={font}"
            f":text='{_escape_drawtext(spec.subtitle)}'"
            f":fontcolor={spec.accent_color}:fontsize={subtitle_size}"
            f":x=(w-text_w)/2:y={subtitle_y}"
        )
    # Stack extra lines under the subtitle with accent colour at a
    # smaller size.  Each line is offset by ~1.6x its own height.
    for idx, line in enumerate(spec.extra_lines, start=1):
        if not line:
            continue
        y = f"(h/2)+(text_h*{0.2 + 1.6 * idx})"
        parts.append(
            f"drawtext=fontfile={font}"
            f":text='{_escape_drawtext(line)}'"
            f":fontcolor={spec.fg_color}:fontsize={extra_size}"
            f":x=(w-text_w)/2:y={y}"
        )
    return ",".join(parts)


def render_card(
    spec: CardSpec,
    profile: MasterProfile,
    output_path: str,
    font_path: str = _DEFAULT_FONT,
) -> str:
    """Render a single card to ``output_path`` as an mp4.

    The output uses the profile's video encoder settings so it concats
    losslessly with the main body.  Audio is either silent (anullsrc) at
    the profile's sample rate or the fixture wav at the profile's target
    LUFS — the caller is responsible for loudnorm on the bed beforehand.
    """
    if not os.path.exists(font_path):
        raise TitleCardRenderError(
            f"drawtext font not found: {font_path}.  Install ttf-dejavu or "
            f"pass font_path explicitly."
        )

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    filter_chain = _build_drawtext_chain(spec, profile, font_path)

    # Video source: a solid colour of the right size + fps.
    video_src = [
        "-f", "lavfi",
        "-i",
        f"color=c={spec.bg_color}:s={profile.width}x{profile.height}:"
        f"r={profile.fps}:d={spec.duration_sec}",
    ]

    if spec.audio_bed_path:
        if not os.path.exists(spec.audio_bed_path):
            raise TitleCardRenderError(
                f"audio_bed_path not found: {spec.audio_bed_path}"
            )
        audio_src = ["-i", spec.audio_bed_path]
        audio_map = ["-map", "1:a:0", "-shortest"]
    else:
        # Channel layout MUST match profile.audio_channels so that every
        # segment emerging from render_card / mux has the same
        # AudioSpecificConfig — required for concat_clips(copy_audio=True)
        # to produce a valid stream.  Using ``stereo`` unconditionally
        # (as we originally did) breaks when the body is mono.
        audio_src = [
            "-f", "lavfi",
            "-i",
            f"anullsrc=channel_layout={profile.anullsrc_channel_layout()}:"
            f"sample_rate={profile.audio_sample_rate}",
        ]
        audio_map = ["-map", "1:a:0", "-t", f"{spec.duration_sec}"]

    cmd: list = [
        "ffmpeg", "-y",
        *video_src,
        *audio_src,
        "-vf", filter_chain,
        *profile.video_encode_args(),
        *profile.audio_encode_args(),
        "-map", "0:v:0",
        *audio_map,
        "-movflags", "+faststart",
        output_path,
    ]

    timeout = max(60, int(spec.duration_sec * 30) + 60)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TitleCardRenderError(
            f"drawtext render timed out after {timeout}s for {spec.kind}"
        ) from exc

    if result.returncode != 0:
        raise TitleCardRenderError(
            f"ffmpeg drawtext failed rc={result.returncode}: "
            f"{result.stderr[-500:]}"
        )

    logger.info(
        "Rendered %s (%.2fs, %dx%d) -> %s",
        spec.kind, spec.duration_sec, profile.width, profile.height,
        output_path,
    )
    return output_path
