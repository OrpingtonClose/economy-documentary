"""Tests for the strict assembler immutability invariants (ARCH-F3 / #164).

PR #162 (ARCH-F1/F2) landed the REPLACE / EXTEND policy enforcer at the
``before_tool_callback`` layer but explicitly left three assembler-level
mutation surfaces out of scope:

1. Freeze-frame / silent-fill gap rendering in
   ``_render_video_track`` / ``_render_audio_track``.
2. ``trim_clip`` / ``source_range`` sub-range extraction in
   ``server/tools/assembly_tools.py``.
3. ``_TRIM_MARGIN = 1.15`` overshoot in ``server/tools/video_tools.py``.

This file exercises the ARCH-F3 eliminations of those three surfaces:

(a) An OTIO ``Gap`` that survives to render time raises
    :class:`UnpluggedGapError` instead of being silently filled.
(b) A ``Clip`` whose declared ``source_range`` does not match the file's
    on-disk duration raises :class:`ClipLengthMismatchError` instead of
    being silently trimmed.
(c) ``server/tools/video_tools`` no longer defines a ``_TRIM_MARGIN``
    overshoot (>1.0) at module scope.
(d) A length mismatch raised inside the video generator propagates up
    through ``execute_with_recovery`` so the REPLACE (regenerate) path
    is triggered rather than a silent trim-to-fit.
"""

from __future__ import annotations

import inspect
import os
import sys

import pytest

# Ensure ``server/`` is on sys.path when pytest is invoked from repo root.
_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from callbacks.strict_assembler import (  # noqa: E402
    CLIP_LENGTH_TOLERANCE_SEC,
    ClipLengthMismatchError,
    UnpluggedGapError,
    ensure_clip_length_matches,
    ensure_item_is_not_gap,
)


# ---------------------------------------------------------------------------
# 1. Exception surface: construction + structured attributes.
# ---------------------------------------------------------------------------

class TestExceptionShape:
    """The two ARCH-F3 errors must carry structured, queryable context."""

    def test_unplugged_gap_error_attrs(self) -> None:
        err = UnpluggedGapError(
            track="V1_Video", scope_ref="scene_003_phrase_002", gap_duration=2.5,
        )
        assert err.track == "V1_Video"
        assert err.scope_ref == "scene_003_phrase_002"
        assert err.gap_duration == pytest.approx(2.5)
        msg = str(err)
        assert "Unplugged OTIO Gap" in msg
        assert "V1_Video" in msg
        assert "scene_003_phrase_002" in msg
        # The error must NOT advertise fabricated filler as an option.
        assert "freeze" in msg.lower()
        assert "silent" in msg.lower()
        assert isinstance(err, RuntimeError)

    def test_clip_length_mismatch_error_attrs(self) -> None:
        err = ClipLengthMismatchError(
            clip_id="scene_003_phrase_002", declared=5.000, actual=5.745,
        )
        assert err.clip_id == "scene_003_phrase_002"
        assert err.declared == pytest.approx(5.000)
        assert err.actual == pytest.approx(5.745)
        msg = str(err)
        assert "scene_003_phrase_002" in msg
        # Message must mention REPLACE as the required remediation.
        assert "REPLACE" in msg
        assert isinstance(err, RuntimeError)

    def test_tolerance_is_a_single_frame_at_60fps(self) -> None:
        # Spec: "a tiny tolerance (e.g. +/-16ms = one frame at 60fps)".
        assert 0.0 < CLIP_LENGTH_TOLERANCE_SEC <= 0.020


# ---------------------------------------------------------------------------
# 2. ``ensure_clip_length_matches`` -- renderer + generator length gate.
# ---------------------------------------------------------------------------

class TestEnsureClipLengthMatches:
    """The length gate is shared by the renderer and the video generator."""

    def test_within_tolerance_passes(self) -> None:
        # 4 ms drift is well within the ±16 ms one-frame-at-60fps budget.
        ensure_clip_length_matches(clip_id="ok", declared=5.000, actual=5.004)

    def test_exact_match_passes(self) -> None:
        ensure_clip_length_matches(clip_id="ok", declared=5.000, actual=5.000)

    @pytest.mark.parametrize("actual", [5.745, 4.200, 5.020])
    def test_over_tolerance_raises_with_structured_fields(self, actual: float) -> None:
        with pytest.raises(ClipLengthMismatchError) as excinfo:
            ensure_clip_length_matches(
                clip_id="scene_003_phrase_002", declared=5.000, actual=actual,
            )
        assert excinfo.value.clip_id == "scene_003_phrase_002"
        assert excinfo.value.declared == pytest.approx(5.000)
        assert excinfo.value.actual == pytest.approx(actual)

    def test_zero_duration_always_raises(self) -> None:
        # A zero-length file is never a valid rendering of a non-zero
        # clip, even if the tolerance would permit the delta.
        with pytest.raises(ClipLengthMismatchError):
            ensure_clip_length_matches(clip_id="zero", declared=0.001, actual=0.0)

    def test_negative_duration_raises(self) -> None:
        with pytest.raises(ClipLengthMismatchError):
            ensure_clip_length_matches(clip_id="bad", declared=5.0, actual=-1.0)


# ---------------------------------------------------------------------------
# 3. ``ensure_item_is_not_gap`` -- the renderer's gap gate (ARCH-F3 test (a)).
# ---------------------------------------------------------------------------

class TestEnsureItemIsNotGap:
    """An OTIO ``Gap`` at render time must raise :class:`UnpluggedGapError`."""

    def test_gap_raises_with_track_scope_and_duration(self) -> None:
        otio = pytest.importorskip("opentimelineio")
        gap = otio.schema.Gap(
            name="gap_between_phrases_001_002",
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(48, 24),  # 2.0s @ 24fps
            ),
        )
        with pytest.raises(UnpluggedGapError) as excinfo:
            ensure_item_is_not_gap(gap, track="V1_Video", lang_suffix="", idx=7)
        assert excinfo.value.track == "V1_Video"
        assert excinfo.value.scope_ref == "gap_between_phrases_001_002"
        assert excinfo.value.gap_duration == pytest.approx(2.0)

    def test_anonymous_gap_falls_back_to_idx_scope(self) -> None:
        otio = pytest.importorskip("opentimelineio")
        gap = otio.schema.Gap(
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(24, 24),  # 1.0s
            ),
        )
        with pytest.raises(UnpluggedGapError) as excinfo:
            ensure_item_is_not_gap(gap, track="A1_Narration", lang_suffix="_ru", idx=3)
        assert excinfo.value.track == "A1_Narration_ru"
        assert excinfo.value.scope_ref == "gap_idx=3"
        assert excinfo.value.gap_duration == pytest.approx(1.0)

    def test_clip_passes_silently(self) -> None:
        otio = pytest.importorskip("opentimelineio")
        # A real Clip should NOT raise -- it's the renderer's job to
        # render it, not to reject it.
        clip = otio.schema.Clip(
            name="scene_001_phrase_000",
            media_reference=otio.schema.ExternalReference(target_url="/nonexistent.wav"),
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(120, 24),  # 5.0s
            ),
        )
        # Must not raise.
        ensure_item_is_not_gap(clip, track="A1_Narration", lang_suffix="", idx=0)


# ---------------------------------------------------------------------------
# 4. ``_TRIM_MARGIN`` was removed (ARCH-F3 test (c)).
# ---------------------------------------------------------------------------

class TestTrimMarginRemoved:
    """The 15% overshoot must be gone from the video generator."""

    def test_video_tools_has_no_trim_margin_over_1(self) -> None:
        from tools import video_tools
        # Either absent entirely, or -- if re-introduced as a historical
        # constant equal to 1.0 -- must not overshoot.
        margin = getattr(video_tools, "_TRIM_MARGIN", None)
        if margin is not None:
            assert margin <= 1.0, (
                "ARCH-F3: video_tools._TRIM_MARGIN must not overshoot the "
                f"requested duration, got {margin!r}"
            )

    def test_video_tools_source_mentions_exact_duration(self) -> None:
        # Belt-and-braces: source scan catches regressions where the
        # literal 1.15 multiplier creeps back into generate_video_clip.
        from tools import video_tools
        src = inspect.getsource(video_tools)
        assert "_TRIM_MARGIN = 1.15" not in src, (
            "ARCH-F3: _TRIM_MARGIN = 1.15 must not be re-introduced."
        )
        assert "duration_sec * 1.15" not in src, (
            "ARCH-F3: the 15%% overshoot multiplier must not reappear."
        )


# ---------------------------------------------------------------------------
# 5. ``trim_clip`` was removed from ``assembly_tools`` (ARCH-F3 test (b)).
# ---------------------------------------------------------------------------

class TestTrimClipRemoved:
    """The assembler must not expose a sub-range extraction tool."""

    def test_trim_clip_not_importable_from_assembly_tools(self) -> None:
        from tools import assembly_tools
        assert not hasattr(assembly_tools, "trim_clip"), (
            "ARCH-F3: assembly_tools.trim_clip must be removed."
        )
        assert not hasattr(assembly_tools, "trim_clip_tool"), (
            "ARCH-F3: the trim_clip FunctionTool wrapper must be removed."
        )

    def test_trim_clip_absent_from_exports(self) -> None:
        from tools import assembly_tools
        assert "trim_clip" not in assembly_tools.__all__
        tool_names = [
            getattr(t, "name", getattr(t, "__name__", "")).lower()
            for t in assembly_tools.assembly_tools
        ]
        assert not any("trim" in n for n in tool_names), (
            f"ARCH-F3: no trim-shaped tool may be registered, got {tool_names}"
        )


# ---------------------------------------------------------------------------
# 6. Gap-filler helpers were removed from ``deterministic_steps`` (test (a)).
# ---------------------------------------------------------------------------

class TestGapFillerHelpersRemoved:
    """The three filler helpers must not exist at module scope."""

    def test_filler_helpers_absent(self) -> None:
        from callbacks import deterministic_steps
        for name in (
            "_generate_silence",
            "_generate_freeze_frame_video",
            "_generate_black_video",
        ):
            assert not hasattr(deterministic_steps, name), (
                f"ARCH-F3: deterministic_steps.{name} must be removed "
                "-- gaps are plugged upstream, not at render time."
            )


# ---------------------------------------------------------------------------
# 7. Length mismatch triggers REPLACE, not trim (ARCH-F3 test (d)).
# ---------------------------------------------------------------------------

class TestLengthMismatchTriggersReplace:
    """End-to-end: a wrong-length clip takes the REPLACE (regenerate) path.

    ``generate_video_clip`` wraps the GPU call in ``execute_with_recovery``
    with ``VIDEO_POLICY``.  ``VIDEO_POLICY.non_retryable_patterns`` is
    empty, which means any ``RuntimeError`` raised inside
    ``_call_gpu_worker`` -- including our :class:`ClipLengthMismatchError`
    -- is retried up to ``max_retries`` times with creative amendments
    (seed changes, prompt tweaks).  That is the REPLACE path.  Critically
    the assembler never trims -- there is no trim call site left.
    """

    def test_video_policy_treats_mismatch_as_retryable(self) -> None:
        from recovery import VIDEO_POLICY
        # Empty non-retryable set == every runtime error goes through
        # retries + creative amendments (aka REPLACE), including
        # ClipLengthMismatchError.
        assert VIDEO_POLICY.non_retryable_patterns == ()
        assert VIDEO_POLICY.max_retries >= 1
        # Creative amendments must exist -- that's the "try a fresh
        # generation" behaviour that REPLACE relies on.
        assert len(VIDEO_POLICY.creative_amendments) >= 1

    def test_video_tools_has_no_trim_call_sites(self) -> None:
        # Double-check nothing in the video generator smuggles in a
        # fallback trim if the length check fires.
        from tools import video_tools
        src = inspect.getsource(video_tools)
        assert "trim_clip" not in src, (
            "ARCH-F3: video_tools must not fall back to trim_clip on "
            "length mismatch -- the recovery ladder triggers REPLACE."
        )
        assert "-ss" not in src, (
            "ARCH-F3: video_tools must not perform ffmpeg sub-range "
            "extraction (-ss) as a post-generation fixup."
        )

    def test_deterministic_steps_has_no_trim_call_sites(self) -> None:
        # The assembler module must not import or call trim_clip either.
        from callbacks import deterministic_steps
        src = inspect.getsource(deterministic_steps)
        # Only the ARCH-F3 audit notes are allowed to mention trim_clip.
        non_comment_lines = [
            ln for ln in src.splitlines()
            if "trim_clip" in ln and not ln.lstrip().startswith("#")
        ]
        assert non_comment_lines == [], (
            "ARCH-F3: deterministic_steps must not contain any live "
            f"trim_clip references, got {non_comment_lines!r}"
        )
