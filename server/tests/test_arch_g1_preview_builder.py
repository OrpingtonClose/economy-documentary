"""ARCH-G1 (issue #153) — preview builder tests.

Exercises :mod:`server.previews.builder`:

- honest placeholders for missing / failed / in-progress slots,
- idempotency (byte-identical re-run on the same state),
- cheap re-run (no ffmpeg work when the hash matches),
- fail-loud on inconsistent OTIO,
- pipeline non-advancement (the builder is a pure reader over state).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from typing import Any
from unittest import mock

import opentimelineio as otio
import pytest

from previews import builder
from previews.builder import (
    PREVIEW_ARTIFACT_KIND,
    PREVIEW_SLOT_OVERRIDES_KEY,
    PreviewInconsistencyError,
    SlotKind,
    SlotStatus,
    build_preview,
    compute_input_hash,
    plan_preview,
)


# ---------------------------------------------------------------------------
# Fixtures — build a minimal OTIO timeline on disk
# ---------------------------------------------------------------------------


def _make_clip(name: str, duration_sec: float, target_url: str, **doc) -> otio.schema.Clip:
    clip = otio.schema.Clip(
        name=name,
        media_reference=otio.schema.ExternalReference(target_url=target_url),
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 24),
            duration=otio.opentime.RationalTime(duration_sec * 24, 24),
        ),
    )
    clip.metadata["documentary"] = doc
    return clip


def _make_gap(name: str, duration_sec: float, **doc) -> otio.schema.Gap:
    gap = otio.schema.Gap(
        name=name,
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 24),
            duration=otio.opentime.RationalTime(duration_sec * 24, 24),
        ),
    )
    gap.metadata["documentary"] = doc
    return gap


def _write_silent_wav(path: str, seconds: float) -> None:
    import wave

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    frames = int(seconds * 48000)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(b"\x00\x00" * frames)


def _write_small_mp4(path: str, seconds: float) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=0x223344:s=320x240:r=24:d={seconds:.3f}",
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t", f"{seconds:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2",
        "-shortest",
        path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture
def timeline_two_scenes(tmp_path):
    """Two scenes: scene 1 has delivered video + narration; scene 2 is all gaps."""
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    narr_s1 = str(media_dir / "scene_001_V1.wav")
    video_s1 = str(media_dir / "scene_001_phrase_001.mp4")
    _write_silent_wav(narr_s1, 2.0)
    _write_small_mp4(video_s1, 2.0)

    timeline = otio.schema.Timeline(name="test")
    video_track = otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video)
    narr_track = otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio)

    # Scene 1 — delivered.
    video_track.append(
        _make_clip(
            "scene_001_phrase_001", 2.0, f"file://{video_s1}",
            scene_num=1, type="body",
        )
    )
    narr_track.append(
        _make_clip(
            "scene_001_V1", 2.0, f"file://{narr_s1}",
            scene_num=1, type="narration", scripted_text="Hello world.",
        )
    )
    # Scene 2 — missing video + missing narration.
    video_track.append(
        _make_gap(
            "scene_002_V1", 2.0, scene_num=2, status="empty", gap_type="missing",
        )
    )
    narr_track.append(
        _make_gap(
            "scene_002_V1_narration", 2.0, scene_num=2, status="empty",
        )
    )

    timeline.tracks.append(video_track)
    timeline.tracks.append(narr_track)
    tl_path = str(tmp_path / "timeline.otio")
    otio.adapters.write_to_file(timeline, tl_path)
    return tl_path, str(media_dir)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class TestPlanning:

    def test_classifies_delivered_and_missing(self, timeline_two_scenes):
        tl_path, _ = timeline_two_scenes
        state = {"_timeline_path": tl_path}
        plans = plan_preview(state)
        by_key = {p.slot_key: p for p in plans}

        assert by_key["scene_001_phrase_001"].status == SlotStatus.DELIVERED
        assert by_key["scene_001_phrase_001"].media_path.endswith(".mp4")
        assert by_key["scene_001_V1"].status == SlotStatus.DELIVERED
        assert by_key["scene_002_V1"].status == SlotStatus.MISSING
        assert by_key["scene_002_V1"].kind == SlotKind.VIDEO
        assert by_key["scene_002_V1_narration"].status == SlotStatus.MISSING
        assert by_key["scene_002_V1_narration"].kind == SlotKind.NARRATION

    def test_overrides_take_precedence(self, timeline_two_scenes):
        tl_path, _ = timeline_two_scenes
        state = {
            "_timeline_path": tl_path,
            PREVIEW_SLOT_OVERRIDES_KEY: {
                "scene_002_V1": {
                    "status": "in_progress",
                    "rung": "L2 CREATIVE — trying alt provider",
                },
            },
        }
        plans = plan_preview(state)
        scene2 = next(p for p in plans if p.slot_key == "scene_002_V1")
        assert scene2.status == SlotStatus.IN_PROGRESS
        assert scene2.rung_text == "L2 CREATIVE — trying alt provider"

    def test_failed_override(self, timeline_two_scenes):
        tl_path, _ = timeline_two_scenes
        state = {
            "_timeline_path": tl_path,
            PREVIEW_SLOT_OVERRIDES_KEY: {
                "scene_002_V1": {"status": "failed", "reason": "all rungs exhausted"},
            },
        }
        plans = plan_preview(state)
        scene2 = next(p for p in plans if p.slot_key == "scene_002_V1")
        assert scene2.status == SlotStatus.FAILED
        assert scene2.failure_reason == "all rungs exhausted"

    def test_eta_from_fleet_state(self, timeline_two_scenes):
        tl_path, _ = timeline_two_scenes
        state = {
            "_timeline_path": tl_path,
            "fleet_state": {"eta_by_slot": {"scene_002_V1": "ETA: 4 min"}},
        }
        plans = plan_preview(state)
        scene2 = next(p for p in plans if p.slot_key == "scene_002_V1")
        assert scene2.status == SlotStatus.MISSING
        assert scene2.eta_text == "ETA: 4 min"

    def test_eta_from_worker_state(self, timeline_two_scenes):
        tl_path, _ = timeline_two_scenes
        state = {
            "_timeline_path": tl_path,
            "worker_state": {
                "w1": {"current_job": "scene_002_V1", "eta_sec": 180},
            },
        }
        plans = plan_preview(state)
        scene2 = next(p for p in plans if p.slot_key == "scene_002_V1")
        assert scene2.eta_text == "ETA: 3.0 min"


# ---------------------------------------------------------------------------
# Fail-loud invariants
# ---------------------------------------------------------------------------


class TestFailLoud:

    def test_missing_timeline_raises(self, tmp_path):
        state: dict[str, Any] = {"_timeline_path": str(tmp_path / "no.otio")}
        with pytest.raises(PreviewInconsistencyError):
            plan_preview(state)

    def test_negative_duration_raises(self, tmp_path):
        timeline = otio.schema.Timeline(name="bad")
        video_track = otio.schema.Track(
            name="V1_Video", kind=otio.schema.TrackKind.Video
        )
        # Zero-duration gap.
        video_track.append(
            otio.schema.Gap(
                name="scene_001_V1",
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, 24),
                    duration=otio.opentime.RationalTime(0, 24),
                ),
            )
        )
        narr_track = otio.schema.Track(
            name="A1_Narration", kind=otio.schema.TrackKind.Audio
        )
        timeline.tracks.append(video_track)
        timeline.tracks.append(narr_track)
        tl_path = str(tmp_path / "bad.otio")
        otio.adapters.write_to_file(timeline, tl_path)

        state = {"_timeline_path": tl_path}
        with pytest.raises(PreviewInconsistencyError):
            plan_preview(state)

    def test_missing_required_track_raises(self, tmp_path):
        timeline = otio.schema.Timeline(name="bad")
        # Only A1, no V1.
        narr_track = otio.schema.Track(
            name="A1_Narration", kind=otio.schema.TrackKind.Audio
        )
        narr_track.append(
            _make_gap("scene_001", 1.0, scene_num=1, type="silence")
        )
        timeline.tracks.append(narr_track)
        tl_path = str(tmp_path / "bad.otio")
        otio.adapters.write_to_file(timeline, tl_path)

        with pytest.raises(PreviewInconsistencyError):
            plan_preview({"_timeline_path": tl_path})


# ---------------------------------------------------------------------------
# Input hash / idempotency
# ---------------------------------------------------------------------------


class TestInputHash:

    def test_same_state_same_hash(self, timeline_two_scenes):
        tl_path, _ = timeline_two_scenes
        state = {"_timeline_path": tl_path}
        h1 = compute_input_hash(plan_preview(state), tl_path, "draft")
        h2 = compute_input_hash(plan_preview(state), tl_path, "draft")
        assert h1 == h2

    def test_different_override_changes_hash(self, timeline_two_scenes):
        tl_path, _ = timeline_two_scenes
        state_a = {"_timeline_path": tl_path}
        state_b = {
            "_timeline_path": tl_path,
            PREVIEW_SLOT_OVERRIDES_KEY: {
                "scene_002_V1": {"status": "failed", "reason": "x"},
            },
        }
        h_a = compute_input_hash(plan_preview(state_a), tl_path, "draft")
        h_b = compute_input_hash(plan_preview(state_b), tl_path, "draft")
        assert h_a != h_b


# ---------------------------------------------------------------------------
# Full build — uses real ffmpeg
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not available on PATH"
)
class TestBuildPreviewRendered:

    def test_build_produces_mp4_and_manifest(self, timeline_two_scenes, tmp_path):
        tl_path, _ = timeline_two_scenes
        state = {"_timeline_path": tl_path}
        out_dir = str(tmp_path / "previews")

        manifest = build_preview(
            state, trigger_reason="test_trigger", output_dir=out_dir
        )
        assert manifest.kind == PREVIEW_ARTIFACT_KIND
        assert os.path.exists(manifest.preview_path)
        assert os.path.exists(manifest.manifest_path)
        assert manifest.trigger_reason == "test_trigger"
        # 2 tracks × 2 slots per track = 4 slots total.
        assert len(manifest.slots) == 4
        assert manifest.counts[SlotStatus.DELIVERED.value] == 2
        assert manifest.counts[SlotStatus.MISSING.value] == 2

    def test_idempotent_byte_identical(self, timeline_two_scenes, tmp_path):
        tl_path, _ = timeline_two_scenes
        state = {"_timeline_path": tl_path}
        out_dir = str(tmp_path / "previews")

        m1 = build_preview(state, trigger_reason="t1", output_dir=out_dir)
        with open(m1.preview_path, "rb") as fh:
            bytes1 = fh.read()
        digest1 = hashlib.sha256(bytes1).hexdigest()
        mtime1 = os.path.getmtime(m1.preview_path)

        # Re-run on the same state — should short-circuit.
        m2 = build_preview(state, trigger_reason="t2", output_dir=out_dir)
        assert m2.preview_path == m1.preview_path
        assert m2.input_hash == m1.input_hash
        assert os.path.getmtime(m2.preview_path) == mtime1  # not re-written
        with open(m2.preview_path, "rb") as fh:
            bytes2 = fh.read()
        assert hashlib.sha256(bytes2).hexdigest() == digest1

    def test_manifest_marked_preview_assembly(self, timeline_two_scenes, tmp_path):
        tl_path, _ = timeline_two_scenes
        state = {"_timeline_path": tl_path}
        out_dir = str(tmp_path / "previews")
        manifest = build_preview(state, trigger_reason="t", output_dir=out_dir)
        with open(manifest.manifest_path) as fh:
            data = json.load(fh)
        assert data["kind"] == PREVIEW_ARTIFACT_KIND


# ---------------------------------------------------------------------------
# Cheap re-run — verify no ffmpeg calls when hash matches
# ---------------------------------------------------------------------------


class TestCheapRerun:

    def test_cheap_rerun_skips_ffmpeg(self, timeline_two_scenes, tmp_path):
        """When preview + manifest already exist with matching hash, no
        ffmpeg subprocess call is made."""
        tl_path, _ = timeline_two_scenes
        state = {"_timeline_path": tl_path}
        out_dir = str(tmp_path / "previews")

        # First build: run normally (may require ffmpeg; skip if unavailable).
        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg not available")
        manifest = build_preview(state, trigger_reason="first", output_dir=out_dir)
        assert os.path.exists(manifest.preview_path)

        # Second build: patch subprocess.run and verify it's NOT called.
        with mock.patch.object(builder.subprocess, "run") as mrun:
            manifest2 = build_preview(
                state, trigger_reason="second", output_dir=out_dir
            )
            assert mrun.call_count == 0
        assert manifest2.preview_path == manifest.preview_path


# ---------------------------------------------------------------------------
# Pipeline-non-advancement invariant
# ---------------------------------------------------------------------------


class TestPipelineNonAdvancement:

    def test_builder_does_not_mutate_state(self, timeline_two_scenes, tmp_path):
        """The builder must not mutate pipeline-advancing state keys."""
        tl_path, _ = timeline_two_scenes
        state: dict[str, Any] = {
            "_timeline_path": tl_path,
            "pipeline_phase": "audio",
            "_narration_reconciliation_passed": True,
            "approved_stages": {"audio": True},
        }
        before = json.dumps(state, sort_keys=True, default=str)

        if shutil.which("ffmpeg") is None:
            # Still invoke plan_preview which is the pure planner.
            plan_preview(state)
        else:
            build_preview(
                state, trigger_reason="t", output_dir=str(tmp_path / "p")
            )

        after = json.dumps(state, sort_keys=True, default=str)
        assert before == after, (
            "builder mutated blackboard state — pipeline-non-advancement "
            "invariant violated"
        )


# ---------------------------------------------------------------------------
# Honest placeholders — no silent substitution
# ---------------------------------------------------------------------------


class TestHonestPlaceholders:

    def test_missing_audio_has_no_delivered_path(self, timeline_two_scenes):
        tl_path, _ = timeline_two_scenes
        state = {"_timeline_path": tl_path}
        plans = plan_preview(state)
        missing_narr = next(
            p for p in plans
            if p.kind == SlotKind.NARRATION and p.status == SlotStatus.MISSING
        )
        assert missing_narr.media_path is None, (
            "missing narration must not silently point to a neighbouring clip"
        )

    def test_failed_slot_carries_reason(self, timeline_two_scenes):
        tl_path, _ = timeline_two_scenes
        state = {
            "_timeline_path": tl_path,
            PREVIEW_SLOT_OVERRIDES_KEY: {
                "scene_002_V1": {"status": "failed", "reason": "ladder exhausted"},
            },
        }
        plans = plan_preview(state)
        failed = next(p for p in plans if p.slot_key == "scene_002_V1")
        assert failed.status == SlotStatus.FAILED
        assert "ladder" in (failed.failure_reason or "")

    def test_intentional_silence_distinguished_from_missing(self, tmp_path):
        """A narration gap tagged ``type=silence`` is an intentional pause
        (not a missing block) and must classify differently so the
        renderer does not add a ``[pending]`` caption."""
        timeline = otio.schema.Timeline(name="t")
        video_track = otio.schema.Track(
            name="V1_Video", kind=otio.schema.TrackKind.Video
        )
        video_track.append(
            _make_gap(
                "scene_001_V1", 1.0, scene_num=1, status="empty",
                gap_type="missing",
            )
        )
        narr_track = otio.schema.Track(
            name="A1_Narration", kind=otio.schema.TrackKind.Audio
        )
        narr_track.append(
            _make_gap(
                "scene_001_pause", 1.0, scene_num=1, type="silence",
                gap_type="inter_voice",
            )
        )
        timeline.tracks.append(video_track)
        timeline.tracks.append(narr_track)
        tl_path = str(tmp_path / "t.otio")
        otio.adapters.write_to_file(timeline, tl_path)

        plans = plan_preview({"_timeline_path": tl_path})
        pause = next(p for p in plans if p.slot_key == "scene_001_pause")
        assert pause.status == SlotStatus.INTENTIONAL_SILENCE
