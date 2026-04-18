"""
Strict assembler invariants (ARCH-F3 / #164).

Complements :mod:`callbacks.media_immutability` (which enforces the
REPLACE/EXTEND policy at the ``before_tool_callback`` layer) with two
structural errors raised from inside the renderer and the video
generator:

- :class:`UnpluggedGapError` -- an OTIO :class:`~otio.schema.Gap`
  survived until render time.  The assembler refuses to fabricate
  filler media (freeze-frame, silent-fill, black video); gaps must be
  plugged upstream via ``generate_extension_clip`` (video) or
  regenerated narration (audio).

- :class:`ClipLengthMismatchError` -- a Clip's declared ``source_range``
  does not match the clip's on-disk duration.  The renderer refuses to
  trim or extract sub-ranges; length-mismatched clips must be REPLACEd
  via the content ladder, not fixed up at render time.

Both errors subclass :class:`RuntimeError` so they propagate through
the existing ``_escalate_otio`` / ``execute_with_recovery`` paths
without special handling, while still carrying structured context
(``track`` / ``scope_ref`` / ``gap_duration`` or ``clip_id`` /
``declared`` / ``actual``) for dashboards and tests.
"""

from __future__ import annotations


class UnpluggedGapError(RuntimeError):
    """Raised when the assembler encounters an OTIO Gap at render time.

    Gaps must be plugged upstream -- by ``generate_extension_clip`` for
    video gaps, or regenerated narration for audio gaps -- BEFORE
    reaching the assembler.  The assembler does not fabricate filler
    media under any circumstances (no freeze-frame, no silent-fill, no
    black video).

    Attributes:
        track: OTIO track name the gap was found in (``"V1_Video"`` or
            ``"A1_Narration"``).
        scope_ref: Short identifier for where the gap lives -- the gap's
            OTIO name, or an index-based reference when the gap is
            anonymous.
        gap_duration: Gap duration in seconds (as declared by
            ``item.source_range.duration``).
    """

    def __init__(self, track: str, scope_ref: str, gap_duration: float) -> None:
        self.track = str(track)
        self.scope_ref = str(scope_ref)
        self.gap_duration = float(gap_duration)
        super().__init__(
            f"Unplugged OTIO Gap in track {self.track!r} at "
            f"{self.scope_ref!r} (duration={self.gap_duration:.3f}s). "
            f"Gaps must be plugged by generate_extension_clip (video) or "
            f"regenerated narration (audio) BEFORE reaching the assembler. "
            f"The assembler does not fabricate filler media "
            f"(no freeze-frame, no silent-fill, no black video)."
        )


class ClipLengthMismatchError(RuntimeError):
    """Raised when a Clip's declared length does not match the on-disk file.

    OTIO Clips MUST be generated at their declared exact length.  The
    assembler refuses to extract sub-ranges (``trim_clip`` / ffmpeg
    ``-ss`` / ``-t``); any mismatch between ``source_range`` and the
    clip's actual duration is a generation-time bug that must be
    resolved by REPLACE (regenerate) via the content ladder -- not by
    render-time trimming.

    Used in two places:

    - The renderer (`_render_video_track` / `_render_audio_track`):
      on finding a Clip whose file duration differs from
      ``source_range.duration`` by more than the tolerance, it raises
      this error instead of trimming.

    - The video generator (``generate_video_clip``): on finding the
      freshly-written MP4's measured duration differs from the
      requested ``duration_sec`` by more than the tolerance, it raises
      this error (inside ``_call_gpu_worker``) so that
      ``execute_with_recovery`` triggers REPLACE rather than a silent
      trim.

    Attributes:
        clip_id: Clip name / identifier (e.g. ``scene_003_phrase_002``
            or an output path for generator-side mismatches).
        declared: Declared / requested duration in seconds.
        actual: Measured on-disk duration in seconds.
    """

    def __init__(self, clip_id: str, declared: float, actual: float) -> None:
        self.clip_id = str(clip_id)
        self.declared = float(declared)
        self.actual = float(actual)
        delta = self.actual - self.declared
        super().__init__(
            f"Clip {self.clip_id!r}: declared duration "
            f"({self.declared:.3f}s) does not match actual duration "
            f"({self.actual:.3f}s), delta={delta:+.3f}s. OTIO Clips must "
            f"be generated at their declared exact length. The assembler "
            f"refuses to trim or extract sub-ranges; length-mismatched "
            f"clips must be REPLACEd via the content ladder."
        )


# ---------------------------------------------------------------------------
# Tolerance for duration comparisons.
# ---------------------------------------------------------------------------
#
# ARCH-F3 spec: "a tiny tolerance (e.g. +/-16ms = one frame at 60fps)".
# We use 16 ms = 1 / 60 s rounded up.  Any mismatch strictly greater than
# this triggers ClipLengthMismatchError on both the generator side and
# the renderer side.

CLIP_LENGTH_TOLERANCE_SEC: float = 0.016


# ---------------------------------------------------------------------------
# Track-level tolerance scaling.
# ---------------------------------------------------------------------------
#
# ``CLIP_LENGTH_TOLERANCE_SEC`` applies to a single clip measured against
# its declared ``source_range`` -- it is a container-rounding /
# muxer-boundary budget (one frame at 60 fps).
#
# The track-level check compares the CONCATENATED audio-track duration
# against the CONCATENATED video-track duration.  Two independent drift
# sources accumulate here:
#
# 1. LTX 2.3 grid quantization.  Every video clip's duration is snapped
#    UPFRONT to the nearest LTX ``8k + 1`` frame grid point
#    (``LTX_GRID_STEP_SEC = 8 / 24 ≈ 0.333 s``); see
#    :func:`video_tools.generate_video_clip`.  Nearest-rounding means a
#    single video clip can differ from its requested narration-slot
#    duration by up to one HALF grid step, i.e.
#    ``LTX_GRID_STEP_SEC / 2 ≈ 0.167 s``.  Audio clips carry their
#    original TTS duration and are NOT grid-quantized, so the grid
#    error lives entirely on the video side of the comparison.
#    Summed across ``N`` video clips the drift budget is
#    ``N * GRID_QUANTIZATION_BUDGET_SEC``.
#
# 2. Concat / container rounding.  Each concat boundary contributes up
#    to ``CLIP_LENGTH_TOLERANCE_SEC`` of muxer drift even when the
#    per-clip length gate passed.  Summed across the larger of the two
#    tracks this is ``N * CLIP_LENGTH_TOLERANCE_SEC``.
#
# The track-level tolerance therefore scales with ``N`` by summing both
# per-clip budgets.  ``TRACK_LENGTH_FLOOR_SEC`` is the minimum tolerance
# for a very short track (one- or two-clip tracks still need a non-zero
# budget for muxer rounding).  A track-level mismatch that exceeds the
# budget is a STRUCTURAL failure (e.g. a missing clip, a concat-list
# bug) and must be REPLACEd upstream, not trimmed away.

# LTX 2.3 renders at 24 fps in 8-frame groups of ``8k + 1`` frames.
# The worker snaps any requested frame count to the nearest grid point
# BEFORE generation, so a clip's delivered duration is the requested
# ``duration_sec`` rounded to the nearest multiple of ``8 / 24`` s.
LTX_GRID_STEP_SEC: float = 8.0 / 24.0

# Half the grid step: the worst-case per-clip delta introduced by
# nearest-neighbour snapping.  Used at the track-level check below.
GRID_QUANTIZATION_BUDGET_SEC: float = LTX_GRID_STEP_SEC / 2.0

TRACK_LENGTH_FLOOR_SEC: float = 0.050


# ---------------------------------------------------------------------------
# Module-level helpers shared by the renderer, the video generator, and the
# test-suite.  Keeping the assertions out of the renderer's closure makes
# them trivially testable without having to drive the whole assembly
# callback end-to-end.
# ---------------------------------------------------------------------------

def ensure_clip_length_matches(
    clip_id: str,
    declared: float,
    actual: float,
    tolerance: float = CLIP_LENGTH_TOLERANCE_SEC,
) -> None:
    """Raise :class:`ClipLengthMismatchError` when ``actual`` strays from ``declared``.

    ``declared`` is the OTIO-declared duration (or the generator's
    requested ``duration_sec``); ``actual`` is the on-disk measurement.
    Both are in seconds.  A non-positive ``actual`` always raises -- a
    zero-length file is never a valid rendering of a non-zero clip.
    """
    if actual <= 0 or abs(actual - declared) > tolerance:
        raise ClipLengthMismatchError(clip_id=clip_id, declared=declared, actual=actual)


def track_length_tolerance(num_clips: int) -> float:
    """Return the A/V length-mismatch budget for a concatenated track.

    The concatenated audio-track and video-track durations drift apart
    via two independent mechanisms:

    * **LTX grid quantization** (dominant).  Each of the ``N`` video
      clips has been snapped to the nearest ``8k + 1`` frame grid
      point, introducing up to :data:`GRID_QUANTIZATION_BUDGET_SEC`
      (``≈ 167 ms``) of per-clip delta relative to the narration-slot
      duration it was asked to cover.  Audio is NOT grid-quantized, so
      this delta appears entirely on the video side of the comparison.

    * **Concat / muxer rounding** (secondary).  Each concat boundary
      contributes up to :data:`CLIP_LENGTH_TOLERANCE_SEC` (``16 ms``).

    The track-level budget therefore sums both per-clip budgets scaled
    by ``N`` (we use the larger of the narration-segment and video-
    segment counts, since drift is dominated by the more granular
    side), with :data:`TRACK_LENGTH_FLOOR_SEC` as a minimum for very
    short tracks.

    Before this scaling the budget was ``N * 16 ms``, which under-
    sized the grid-quantization drift by roughly an order of magnitude
    and produced spurious :class:`ClipLengthMismatchError` track
    failures on otherwise healthy assemblies.  See Devin-review
    BUG_pr-review-job-360f6e5a46ce4ee09a50657fa8966af3_0001 on #174.
    """
    n = max(int(num_clips), 1)
    per_clip_budget = GRID_QUANTIZATION_BUDGET_SEC + CLIP_LENGTH_TOLERANCE_SEC
    return max(TRACK_LENGTH_FLOOR_SEC, n * per_clip_budget)


def ensure_track_length_matches(
    track_id: str,
    audio_dur: float,
    video_dur: float,
    num_clips: int,
) -> None:
    """Raise :class:`ClipLengthMismatchError` when track A/V drift exceeds budget.

    ``num_clips`` should be the maximum of the narration and video
    segment counts for the track; the tolerance scales with it via
    :func:`track_length_tolerance`.  The assembler never trims to
    reconcile audio/video drift -- a mismatch beyond the budget is a
    structural problem that must be REPLACEd upstream.
    """
    tolerance = track_length_tolerance(num_clips)
    if abs(video_dur - audio_dur) > tolerance:
        raise ClipLengthMismatchError(
            clip_id=track_id,
            declared=audio_dur,
            actual=video_dur,
        )


def ensure_item_is_not_gap(
    item: object,
    track: str,
    lang_suffix: str,
    idx: int,
) -> None:
    """Raise :class:`UnpluggedGapError` if ``item`` is an OTIO :class:`Gap`.

    This is the assembler's gap-gate: the renderer calls it for every
    item in the narration and video tracks before attempting to render
    the item.  A :class:`Clip` passes through silently; a :class:`Gap`
    aborts the render with a structured exception that carries the
    track name, a short scope reference, and the gap's declared
    duration, so the content ladder can escalate to
    ``generate_extension_clip`` (video) or regenerated narration
    (audio) rather than fabricating filler media at render time.
    """
    # Imported lazily so tests that don't touch OTIO stay lightweight.
    import opentimelineio as otio  # type: ignore

    if isinstance(item, otio.schema.Gap):
        gap_dur = (
            item.source_range.duration.to_seconds()
            if item.source_range is not None
            else 0.0
        )
        name = getattr(item, "name", None)
        raise UnpluggedGapError(
            track=f"{track}{lang_suffix}",
            scope_ref=name or f"gap_idx={idx}",
            gap_duration=gap_dur,
        )


__all__ = [
    "UnpluggedGapError",
    "ClipLengthMismatchError",
    "CLIP_LENGTH_TOLERANCE_SEC",
    "TRACK_LENGTH_FLOOR_SEC",
    "ensure_clip_length_matches",
    "ensure_track_length_matches",
    "track_length_tolerance",
    "ensure_item_is_not_gap",
]
