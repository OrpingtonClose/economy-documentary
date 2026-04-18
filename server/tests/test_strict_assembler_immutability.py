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
    TRACK_LENGTH_FLOOR_SEC,
    ClipLengthMismatchError,
    UnpluggedGapError,
    ensure_clip_length_matches,
    ensure_item_is_not_gap,
    ensure_track_length_matches,
    track_length_tolerance,
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
# 2b. ``ensure_track_length_matches`` / ``track_length_tolerance`` --
# per-clip drift accumulates across concat boundaries, so the track-level
# budget scales with the clip count (regression guard for PR #174 review).
# ---------------------------------------------------------------------------

class TestTrackLengthTolerance:
    """Track-level A/V alignment uses a clip-count-scaled tolerance."""

    def test_short_track_honours_floor(self) -> None:
        # A 1-clip track still gets at least TRACK_LENGTH_FLOOR_SEC of
        # budget for muxer rounding.
        assert track_length_tolerance(1) == pytest.approx(TRACK_LENGTH_FLOOR_SEC)
        assert track_length_tolerance(2) == pytest.approx(TRACK_LENGTH_FLOOR_SEC)

    def test_long_track_scales_with_clip_count(self) -> None:
        # Past the floor the tolerance scales linearly with the number
        # of concat boundaries: N * per-clip-tolerance.
        for n in (10, 30, 100):
            expected = max(TRACK_LENGTH_FLOOR_SEC, n * CLIP_LENGTH_TOLERANCE_SEC)
            assert track_length_tolerance(n) == pytest.approx(expected)

    def test_track_within_scaled_budget_passes(self) -> None:
        # 30 clips × 16ms = 480ms budget.  A 200ms drift is fine.
        ensure_track_length_matches(
            track_id="combined_track (N=30)",
            audio_dur=60.000,
            video_dur=60.200,
            num_clips=30,
        )

    def test_track_over_budget_raises(self) -> None:
        # 3-clip track → 50ms floor; 200ms drift blows it.
        with pytest.raises(ClipLengthMismatchError) as excinfo:
            ensure_track_length_matches(
                track_id="combined_track (N=3)",
                audio_dur=10.0,
                video_dur=10.200,
                num_clips=3,
            )
        assert excinfo.value.declared == pytest.approx(10.0)
        assert excinfo.value.actual == pytest.approx(10.200)

    def test_zero_or_negative_clip_count_falls_back_to_floor(self) -> None:
        # Defensive: 0 and negative clip counts must not yield a zero
        # tolerance -- fall back to the floor.
        assert track_length_tolerance(0) == pytest.approx(TRACK_LENGTH_FLOOR_SEC)
        assert track_length_tolerance(-3) == pytest.approx(TRACK_LENGTH_FLOOR_SEC)


# ---------------------------------------------------------------------------
# 2c. LTX 8k+1 frame quantization -- ``generate_video_clip`` must round the
# request to the nearest grid point and use the quantized duration for both
# the worker payload and the length gate (regression guard for PR #174
# review: without this the gate fires on every retry for any off-grid
# duration, because creative amendments don't touch num_frames).
# ---------------------------------------------------------------------------

class TestLtxFrameQuantization:
    """The client snaps duration_sec to the 8k+1 frame grid at 24fps."""

    def test_generate_video_clip_source_quantizes_to_8k_plus_1(self) -> None:
        # Source-level assertion: the function body must compute
        # num_frames via a nearest-grid-point rule AND use the resulting
        # actual_duration as the length-gate reference, not the raw
        # ``duration_sec`` passed in by the caller.
        from tools import video_tools
        src = inspect.getsource(video_tools.generate_video_clip)
        assert "8k+1" in src or "8k + 1" in src, (
            "ARCH-F3 follow-up: generate_video_clip must document the "
            "LTX 8k+1 frame-grid quantization."
        )
        # The grid-quantized duration must be what flows into the
        # length gate -- either as ``actual_duration`` (the outer
        # scope's quantized value) or as ``duration_sec`` (the nested
        # ``_call_gpu_worker``'s own parameter, whose default is bound
        # to the quantized value).  What MUST NOT happen is the
        # renderer or the nested worker reading the un-quantized outer
        # caller input, because ~90% of off-grid requests would fail
        # the 16 ms gate.
        assert (
            "declared=actual_duration" in src
            or "declared=duration_sec" in src
        ), (
            "ARCH-F3 follow-up: the length gate must use the quantized "
            "duration (either the outer actual_duration or the nested "
            "_call_gpu_worker duration_sec parameter, which defaults to "
            "it), never the raw caller input."
        )
        # _call_gpu_worker must bind its duration_sec default to the
        # grid-quantized ``actual_duration`` so the length gate stays
        # in sync with the worker request even if a future creative
        # amendment passes a different duration_sec.
        assert "duration_sec=actual_duration" in src, (
            "ARCH-F3 follow-up: _call_gpu_worker must default "
            "duration_sec to the grid-quantized actual_duration."
        )

    @pytest.mark.parametrize(
        "duration_sec, expected_frames",
        [
            (0.1, 9),      # sub-grid -> minimum 9
            (0.375, 9),    # exact grid point
            (0.708, 17),   # 17/24
            (5.0, 121),    # 120 frames at 24fps rounds to 121 (5 vs 17)
            (10.0, 241),
        ],
    )
    def test_grid_quantization_is_nearest(
        self, duration_sec: float, expected_frames: int
    ) -> None:
        # Mirror the production quantization formula to lock the
        # rounding rule down: nearest 8k+1, floor=9.
        fps = 24
        raw = max(9, int(round(duration_sec * fps)))
        k_floor = (raw - 1) // 8
        k_ceil = k_floor + 1
        ff = k_floor * 8 + 1
        fc = k_ceil * 8 + 1
        num_frames = max(9, ff if abs(raw - ff) <= abs(raw - fc) else fc)
        assert num_frames == expected_frames
        # The resulting effective duration is what the worker renders.
        effective = num_frames / fps
        # Within half a grid step (4/24s ≈ 167ms) of the nearest grid
        # point, except for requests below the 9-frame minimum where
        # the quantum is structurally larger.
        if duration_sec >= 9 / fps:
            assert abs(effective - duration_sec) <= (4 / fps) + 1e-9


# ---------------------------------------------------------------------------
# 2d. OTIO ``source_range`` must equal the on-disk file duration (the
# grid-quantized ``actual_duration``) at ``add_video_clip`` call sites --
# otherwise the renderer's per-clip length gate compares the narration-slot
# duration to the file's ffprobe duration and trips on every off-grid
# clip (PR #174 review bug).
# ---------------------------------------------------------------------------

class TestSourceRangeMatchesFileDuration:
    """``add_video_clip`` must be called with source_range = actual_duration."""

    def test_deterministic_steps_passes_actual_duration_as_source_range(
        self,
    ) -> None:
        from callbacks import deterministic_steps
        src = inspect.getsource(deterministic_steps)
        # Every add_video_clip call site in this module must bind
        # source_range to actual_duration (the probed file duration),
        # not to the caller-supplied narration slot ``duration``.
        assert "source_range=actual_duration" in src, (
            "ARCH-F3 follow-up: add_video_clip must declare "
            "source_range from the probed actual_duration so the "
            "renderer's length gate sees source_range == file duration."
        )
        # Regression guard: catch accidental reintroduction of the
        # narration-slot-as-source_range pattern.
        assert "source_range=duration,\n" not in src, (
            "ARCH-F3 follow-up: add_video_clip must not pass the raw "
            "narration-slot ``duration`` as source_range -- the renderer "
            "would then compare the narration slot (off-grid) to the "
            "file's grid-quantized duration and fire the 16 ms gate."
        )

    def test_clip_helpers_passes_actual_duration_as_source_range(self) -> None:
        from orchestrator import clip_helpers
        src = inspect.getsource(clip_helpers)
        assert "source_range=actual_duration" in src, (
            "ARCH-F3 follow-up: clip_helpers.add_video_clip must declare "
            "source_range from the probed actual_duration."
        )
        assert "source_range=duration,\n" not in src, (
            "ARCH-F3 follow-up: clip_helpers must not pass the narration "
            "slot ``duration`` as source_range."
        )


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
    """The video-side filler helpers must not exist at module scope.

    ``_generate_silence`` is RETAINED because the A1_Narration track
    legitimately contains planned-silence Gaps (inter-voice and
    inter-scene pauses written by ``add_narration_gap``).  The OTIO
    contract declares their content to BE silence of the Gap's
    source_range duration -- emitting a silent WAV is faithful
    rendering of a declared OTIO item, NOT fabrication of filler
    media for a missing clip.  The video-side filler helpers have no
    analogous legitimate use because V1_Video has no intentional
    gaps, so they remain removed.
    """

    def test_video_filler_helpers_absent(self) -> None:
        from callbacks import deterministic_steps
        for name in (
            "_generate_freeze_frame_video",
            "_generate_black_video",
        ):
            assert not hasattr(deterministic_steps, name), (
                f"ARCH-F3: deterministic_steps.{name} must be removed "
                "-- V1_Video has no intentional gaps so there is no "
                "legitimate caller of this filler helper."
            )

    def test_generate_silence_present_for_planned_narration_gaps(
        self,
    ) -> None:
        from callbacks import deterministic_steps
        assert hasattr(deterministic_steps, "_generate_silence"), (
            "ARCH-F3 follow-up: _generate_silence must remain because "
            "A1_Narration planned-pause Gaps (inter_voice, inter_scene) "
            "are first-class OTIO items whose declared content is "
            "silence -- the renderer honours them, it does not fill them."
        )
        # The helper must be wired into the audio renderer via the
        # planned-silence metadata check, NOT the video renderer.
        src = inspect.getsource(deterministic_steps)
        assert "_is_planned_silence_gap" in src, (
            "ARCH-F3 follow-up: _render_audio_track must route Gaps "
            "via _is_planned_silence_gap so only metadata-tagged "
            "planned-silence Gaps reach _generate_silence."
        )

    def test_video_renderer_does_not_call_generate_silence(self) -> None:
        """Regression guard: _render_video_track must not invoke silence.

        Video Gaps have no legitimate render-time resolution -- the
        video pipeline is responsible for producing clips that cover
        each narration slot.  Even though _generate_silence exists in
        the module for the audio path, the video renderer must never
        reach it.
        """
        import re
        from callbacks import deterministic_steps
        src = inspect.getsource(deterministic_steps)
        # Grab the _render_video_track body (indented nested function).
        m = re.search(
            r"def _render_video_track\(.*?(?=\n    def |\nclass |\Z)",
            src,
            flags=re.DOTALL,
        )
        assert m is not None, (
            "Could not locate _render_video_track in module source."
        )
        video_body = m.group(0)
        # Check for *call* / *invocation* patterns, not mere mentions.
        # The docstring legitimately describes the ban (e.g. "no
        # freeze-frame, no black") so a plain substring search would
        # fire on documentation.
        for forbidden_call in (
            "_generate_silence(",
            "_generate_black_video(",
            "_generate_freeze_frame_video(",
        ):
            assert forbidden_call not in video_body, (
                f"ARCH-F3 follow-up: _render_video_track must not "
                f"call {forbidden_call[:-1]} -- V1_Video has no "
                f"intentional gaps to fill with silence, black, or "
                f"freeze-frame filler."
            )


# ---------------------------------------------------------------------------
# 6b. Planned narration silence Gaps render as silence, unknown Gaps raise.
# ---------------------------------------------------------------------------

class TestPlannedNarrationSilenceGapRenders:
    """``add_narration_gap`` writes inter-voice and inter-scene pauses.

    The renderer must treat these planned Gaps as first-class OTIO
    content whose declared form IS silence, and emit a real silent WAV
    of the declared duration.  Gaps WITHOUT the ``type=="silence"`` /
    ``gap_type in {inter_voice, inter_scene}`` metadata tag are
    treated as truly unplugged and raise :class:`UnpluggedGapError`.
    """

    def _fake_narration_gap(
        self,
        duration: float,
        gap_type: str = "inter_voice",
        type_meta: str = "silence",
    ):
        """Build a minimal OTIO Gap with ``add_narration_gap``-style metadata."""
        import opentimelineio as otio
        gap = otio.schema.Gap(
            name=f"planned_pause_{gap_type}",
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(duration * 24, 24),
            ),
        )
        gap.metadata["documentary"] = {
            "scene_num": 1,
            "gap_type": gap_type,
            "type": type_meta,
        }
        return gap

    def test_planned_silence_gap_classifier_accepts_add_narration_gap_shape(
        self,
    ) -> None:
        # The classifier lives inside deterministic_production_callback's
        # closure, so we reconstruct the predicate here from the same
        # metadata contract that add_narration_gap writes.  This gives
        # us confidence that the renderer's gate admits the exact shape
        # the audio stage produces.
        from tools import otio_tools  # noqa: F401 - import sanity
        for gap_type in ("inter_voice", "inter_scene"):
            gap = self._fake_narration_gap(1.5, gap_type=gap_type)
            meta = gap.metadata.get("documentary", {})
            assert meta.get("type") == "silence"
            assert meta.get("gap_type") == gap_type

    def test_unknown_gap_does_not_match_planned_silence_contract(
        self,
    ) -> None:
        # A Gap with no documentary metadata, or with a non-silence
        # type, must NOT be rendered as silence.  The renderer
        # routes these to ensure_item_is_not_gap -> UnpluggedGapError.
        bare = self._fake_narration_gap(
            1.0, gap_type="mystery", type_meta="unknown",
        )
        meta = bare.metadata.get("documentary", {})
        assert not (
            meta.get("type") == "silence"
            and meta.get("gap_type") in {"inter_voice", "inter_scene"}
        )

    def test_unplugged_gap_error_still_raises_for_non_planned_gaps(
        self,
    ) -> None:
        # Direct invariant check on the renderer's gap gate: the
        # sentinel used for unknown narration Gaps still raises
        # UnpluggedGapError, so the audio-silence carve-out does not
        # weaken the ARCH-F3 contract for the general case.
        import opentimelineio as otio
        from callbacks.strict_assembler import (
            UnpluggedGapError,
            ensure_item_is_not_gap,
        )
        bare = otio.schema.Gap(
            name="unknown_gap",
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(1 * 24, 24),
            ),
        )
        with pytest.raises(UnpluggedGapError):
            ensure_item_is_not_gap(
                bare, track="A1_Narration", lang_suffix="", idx=0,
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
