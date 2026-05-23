"""Named render master profiles for the documentary assembler.

A :class:`MasterProfile` bundles every render decision that affects a final
deliverable — resolution, fps, codec, colour space, and the target loudness
envelope.  Assembly tools consume a profile rather than hard-coding values.

The PAG run on 2026-04-17 shipped at 512x320 @ 600 kbps with integrated
loudness -21.7 LUFS and LRA 7.4 LU — unacceptable for broadcast.  Profiles
in this module define the acceptable master specs (YouTube long-form,
YouTube Shorts 9:16) and mark the preview spec as preview-only.

The guard :func:`guard_profile_for_filename` is called from
``assembly_tools`` whenever a final-named file is about to be written —
it raises :class:`PreviewProfileForbidden` if the caller tries to use a
preview profile for a filename containing ``"final"``, unless the
``preview_final_ok`` flag is explicitly set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


class PreviewProfileForbidden(RuntimeError):
    """Raised when a preview profile is used for a final-named artifact."""


@dataclass(frozen=True)
class MasterProfile:
    """Immutable description of a render target.

    All fields are locked at instantiation because a partially-applied
    profile (e.g. right resolution, wrong colour space) is a silent
    quality defect.  Callers that need a variant should use
    :meth:`variant` which returns a new frozen instance.
    """

    name: str

    # Video geometry + timing
    width: int
    height: int
    fps: int

    # Video codec
    video_codec: str = "libx264"
    preset: str = "slow"
    crf: int = 18
    pixel_format: str = "yuv420p"
    color_space: str = "bt709"

    # Audio codec
    audio_codec: str = "aac"
    audio_bitrate: str = "256k"
    audio_sample_rate: int = 48000
    # Channel count is part of the profile contract because concat with
    # ``-c:a copy`` requires byte-identical AudioSpecificConfig across
    # every segment (title card + body + end card).  TTS narration is
    # mono, title/end-card silent beds are synthesised, and music beds
    # may be stereo — the profile pins a single channel count and every
    # segment encoder emits ``-ac N`` so they all match.  Default is
    # stereo (2) which matches broadcast norms and upmixes mono
    # narration losslessly.
    audio_channels: int = 2

    # Loudness envelope (integrated + true peak)
    integrated_lufs: float = -14.0
    true_peak_db: float = -1.0
    # Loudness range (LU) tolerance for the final master.  Narration-centric
    # masters must be tighter than the 7 LU used for music; we enforce
    # ≤ 5 LU by default (see ``loudness_normalization.verify_master``).
    max_lra: float = 5.0

    # Intent flag: preview profiles are blocked from producing final
    # deliverables unless the caller explicitly opts-in.
    preview_only: bool = False

    # Free-form notes surfaced in log lines / dashboards.
    description: str = ""

    # Cached shared kwargs used by ffmpeg wrappers.
    ffmpeg_color_args: tuple = field(default_factory=lambda: ())

    def __post_init__(self) -> None:
        # Build deterministic colour-tag args so the encoder writes the
        # expected metadata (bt709 primaries / transfer / matrix) into
        # the muxed container.
        object.__setattr__(
            self,
            "ffmpeg_color_args",
            (
                "-color_primaries", self.color_space,
                "-color_trc", self.color_space,
                "-colorspace", self.color_space,
            ),
        )

    # ------------------------------------------------------------------
    # Helpers consumed by assembly_tools / loudness_normalization
    # ------------------------------------------------------------------
    def video_encode_args(self) -> list:
        """Return the full ffmpeg arg list for the video encoder."""
        return [
            "-c:v", self.video_codec,
            "-preset", self.preset,
            "-crf", str(self.crf),
            "-pix_fmt", self.pixel_format,
            *self.ffmpeg_color_args,
            "-r", str(self.fps),
        ]

    def audio_encode_args(self) -> list:
        """Return the full ffmpeg arg list for the audio encoder."""
        return [
            "-c:a", self.audio_codec,
            "-b:a", self.audio_bitrate,
            "-ar", str(self.audio_sample_rate),
            "-ac", str(self.audio_channels),
        ]

    def anullsrc_channel_layout(self) -> str:
        """Return the ``anullsrc`` channel_layout name for this profile.

        ``ffmpeg`` only accepts layout names (``mono``, ``stereo``,
        ``5.1``, …) — not raw channel counts — on the ``anullsrc``
        source, so title/end cards need this mapping to emit silent
        audio that matches the profile's ``audio_channels``.
        """
        return {
            1: "mono",
            2: "stereo",
            3: "2.1",
            4: "quad",
            5: "4.1",
            6: "5.1",
            8: "7.1",
        }.get(self.audio_channels, "stereo")


# ---------------------------------------------------------------------------
# Named profiles
# ---------------------------------------------------------------------------

YOUTUBE_1080P = MasterProfile(
    name="YOUTUBE_1080P",
    width=1920,
    height=1080,
    fps=24,
    video_codec="libx264",
    preset="slow",
    crf=18,
    pixel_format="yuv420p",
    color_space="bt709",
    audio_codec="aac",
    audio_bitrate="256k",
    audio_sample_rate=48000,
    integrated_lufs=-14.0,
    true_peak_db=-1.0,
    max_lra=5.0,
    preview_only=False,
    description="YouTube long-form 1080p24 master — documentary deliverable.",
)


YOUTUBE_SHORTS_1080P_9_16 = MasterProfile(
    name="YOUTUBE_SHORTS_1080P_9_16",
    width=1080,
    height=1920,
    fps=30,
    video_codec="libx264",
    preset="slow",
    crf=18,
    pixel_format="yuv420p",
    color_space="bt709",
    audio_codec="aac",
    audio_bitrate="256k",
    audio_sample_rate=48000,
    integrated_lufs=-14.0,
    true_peak_db=-1.0,
    max_lra=5.0,
    preview_only=False,
    description="YouTube Shorts 1080x1920 @ 30fps vertical master.",
)


PREVIEW_512P = MasterProfile(
    name="PREVIEW_512P",
    width=512,
    height=320,
    fps=24,
    video_codec="libx264",
    preset="veryfast",
    crf=23,
    pixel_format="yuv420p",
    color_space="bt709",
    audio_codec="aac",
    audio_bitrate="128k",
    audio_sample_rate=48000,
    integrated_lufs=-16.0,
    true_peak_db=-1.5,
    max_lra=7.0,
    preview_only=True,
    description=(
        "PREVIEW ONLY — 512x320 low-bitrate proxy for dashboard playback. "
        "Never ship a file produced with this profile under a filename "
        "containing 'final'; ``guard_profile_for_filename`` enforces this."
    ),
)


PROFILES: Dict[str, MasterProfile] = {
    p.name: p
    for p in (YOUTUBE_1080P, YOUTUBE_SHORTS_1080P_9_16, PREVIEW_512P)
}


DEFAULT_PROFILE = YOUTUBE_1080P


def guard_profile_for_filename(
    profile: MasterProfile,
    output_path: str,
    preview_final_ok: bool = False,
) -> None:
    """Raise if a preview profile is used for a final-named artifact.

    The pipeline writes preview artifacts and final deliverables into the
    same output tree; filename is the only unambiguous signal of intent
    that crosses the module boundary.  Any filename containing ``final``
    (case-insensitive) is treated as a deliverable and must use a
    non-preview profile — unless the caller explicitly sets
    ``preview_final_ok=True`` (maps to the ``--preview-final-ok`` CLI flag).
    """
    if not profile.preview_only:
        return
    basename = output_path.rsplit("/", 1)[-1].lower()
    if "final" in basename and not preview_final_ok:
        raise PreviewProfileForbidden(
            f"Refusing to write {output_path!r} with preview profile "
            f"{profile.name!r}: filename contains 'final'.  Pass "
            f"preview_final_ok=True (or --preview-final-ok) to override."
        )
