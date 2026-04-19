"""
Tests for the OTIO centrepiece read model (ARCH-H1, issue #156).

These cover :mod:`server.otio_timeline_model` — the pure-read aggregator
that assembles the three-track dashboard view (V1_Video / A1_Narration /
A2_Music) from a draft-or-authoritative OTIO file on disk, overlaid
with artifact status, video status JSONs and narration reconciliation
report.

Invariants asserted:

* Scale-accurate slot windows (start/duration come straight from OTIO).
* ``draft`` vs ``authoritative`` surfaces the ``state`` carried on the
  OTIO file's root metadata.
* Slot IDs round-trip through :func:`make_slot_id` / :func:`parse_slot_id`.
* Reconciliation rows are emitted only in ``draft`` state.
* Builder never writes to disk or mutates state (no B2 calls, no
  ``_status.json`` edits).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from otio_timeline_model import (  # noqa: E402
    CANONICAL_TRACKS,
    TRACK_A1_NARRATION,
    TRACK_A2_MUSIC,
    TRACK_V1_VIDEO,
    build_timeline_view,
    make_slot_id,
    parse_slot_id,
)


# ---------------------------------------------------------------------------
# Fixture: synthetic OTIO with three tracks and a narration reconciliation
# ---------------------------------------------------------------------------


def _clip(name: str, duration: float, scene: int, phrase: int) -> dict:
    return {
        "OTIO_SCHEMA": "Clip.1",
        "name": name,
        "source_range": {
            "duration": {"value": duration, "rate": 1.0},
            "start_time": {"value": 0, "rate": 1.0},
        },
        "metadata": {"documentary": {"scene_num": scene, "phrase_idx": phrase}},
    }


def _gap(duration: float) -> dict:
    return {
        "OTIO_SCHEMA": "Gap.1",
        "name": "gap",
        "source_range": {
            "duration": {"value": duration, "rate": 1.0},
            "start_time": {"value": 0, "rate": 1.0},
        },
        "metadata": {},
    }


def _track(name: str, kind: str, children: list[dict]) -> dict:
    return {
        "OTIO_SCHEMA": "Track.1",
        "name": name,
        "kind": kind,
        "children": children,
    }


def _write_otio(path: Path, state: str, tracks: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timeline = {
        "OTIO_SCHEMA": "Timeline.1",
        "name": "test_timeline",
        "metadata": {"documentary": {"state": state}},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "tracks",
            "children": tracks,
        },
    }
    path.write_text(json.dumps(timeline))


@pytest.fixture
def pipeline_dir(tmp_path: Path) -> Path:
    """A fake ``PIPELINE_OUTPUT_DIR`` with a draft OTIO + two narration blocks."""
    video = _track("V1_Video", "Video", [
        _clip("scene_001_phrase_001", 4.0, 1, 1),
        _gap(0.5),
        _clip("scene_001_phrase_002", 3.2, 1, 2),
    ])
    narration = _track("A1_Narration", "Audio", [
        _clip("scene_001_phrase_001_narr", 3.8, 1, 1),
        _gap(0.2),
        _clip("scene_001_phrase_002_narr", 3.0, 1, 2),
    ])
    music = _track("A2_Music", "Audio", [
        _clip("scene_001_bed", 7.7, 1, 0),
    ])
    _write_otio(tmp_path / "timelines" / "draft.otio", "draft", [video, narration, music])

    recon = [
        {"scene_num": 1, "phrase_idx": 1,
         "scripted_duration_sec": 3.8, "measured_duration_sec": 4.1},
        {"scene_num": 1, "phrase_idx": 2,
         "scripted_duration_sec": 3.0, "measured_duration_sec": 2.7},
    ]
    recon_dir = tmp_path / "audio"
    recon_dir.mkdir(parents=True, exist_ok=True)
    (recon_dir / "_narration_reconciliation.json").write_text(json.dumps(recon))

    return tmp_path


# ---------------------------------------------------------------------------
# Slot id round-trip
# ---------------------------------------------------------------------------


def test_slot_id_round_trip():
    for track in (TRACK_V1_VIDEO, TRACK_A1_NARRATION, TRACK_A2_MUSIC):
        sid = make_slot_id(track, 7, 3)
        parsed = parse_slot_id(sid)
        assert parsed == (track, 7, 3)


def test_parse_slot_id_rejects_garbage():
    with pytest.raises(ValueError):
        parse_slot_id("not-a-slot")
    with pytest.raises(ValueError):
        parse_slot_id("ZZ:1:2")
    with pytest.raises(ValueError):
        parse_slot_id("V1:a:b")


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def test_empty_output_dir_returns_three_canonical_tracks(tmp_path: Path):
    view = build_timeline_view(str(tmp_path))
    assert view.state == "draft"
    names = [t.name for t in view.tracks]
    assert names == list(CANONICAL_TRACKS)
    assert all(len(t.slots) == 0 for t in view.tracks)


def test_scale_accurate_slot_windows(pipeline_dir: Path):
    view = build_timeline_view(str(pipeline_dir))
    video = next(t for t in view.tracks if t.name == TRACK_V1_VIDEO)
    # The second non-gap clip must start at 4.0 + 0.5 (gap) = 4.5s.
    non_gap = [s for s in video.slots if s.status != "gap"]
    assert [s.duration_sec for s in non_gap] == [4.0, 3.2]
    assert non_gap[0].start_sec == pytest.approx(0.0)
    assert non_gap[1].start_sec == pytest.approx(4.5)


def test_total_duration_is_max_of_tracks(pipeline_dir: Path):
    view = build_timeline_view(str(pipeline_dir))
    # V1_Video total: 4.0 + 0.5 + 3.2 = 7.7; A1: 3.8 + 0.2 + 3.0 = 7.0;
    # A2: 7.7.  Max is 7.7.
    assert view.total_duration_sec == pytest.approx(7.7)


def test_draft_state_emits_reconciliation_rows(pipeline_dir: Path):
    view = build_timeline_view(str(pipeline_dir))
    assert view.state == "draft"
    assert len(view.reconciliation) == 2
    row_one = view.reconciliation[0]
    assert row_one["scene_num"] == 1
    assert row_one["phrase_idx"] == 1
    assert row_one["scripted_duration_sec"] == pytest.approx(3.8)
    assert row_one["measured_duration_sec"] == pytest.approx(4.1)
    assert row_one["skew_sec"] == pytest.approx(0.3)


def test_authoritative_state_drops_reconciliation_overlay(tmp_path: Path):
    narration = _track("A1_Narration", "Audio", [
        _clip("scene_001_phrase_001_narr", 3.8, 1, 1),
    ])
    _write_otio(tmp_path / "timelines" / "auth.otio", "authoritative", [narration])
    view = build_timeline_view(str(tmp_path))
    assert view.state == "authoritative"
    assert view.reconciliation == []


def test_feedback_artifacts_mark_slots_delivered(pipeline_dir: Path):
    artifacts = [
        {
            "type": "video_clip",
            "status": "approved",
            "scene_num": 1,
            "phrase_idx": 1,
            "preview_url": "/media/v/1_1.mp4",
        },
    ]
    view = build_timeline_view(str(pipeline_dir), feedback_artifacts=artifacts)
    v = next(t for t in view.tracks if t.name == TRACK_V1_VIDEO)
    first = [s for s in v.slots if s.status != "gap"][0]
    assert first.status == "delivered"
    assert first.preview_url == "/media/v/1_1.mp4"


def test_video_status_marks_failed(pipeline_dir: Path):
    # Write a failing status JSON for scene 1 phrase 2
    video_dir = pipeline_dir / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / "scene_001_phrase_002_status.json").write_text(json.dumps({
        "quality": "rejected",
        "qa_reason": "prompt mismatch: inconsistent subject.",
        "attempts": 3,
    }))
    view = build_timeline_view(str(pipeline_dir))
    v = next(t for t in view.tracks if t.name == TRACK_V1_VIDEO)
    failed = [s for s in v.slots if s.scene_num == 1 and s.phrase_idx == 2]
    assert len(failed) == 1
    assert failed[0].status == "failed"
    assert "prompt mismatch" in failed[0].failure_reason


def test_no_mutation_of_input_files(pipeline_dir: Path):
    otio_path = pipeline_dir / "timelines" / "draft.otio"
    before = otio_path.read_bytes()
    build_timeline_view(str(pipeline_dir))
    build_timeline_view(str(pipeline_dir))
    assert otio_path.read_bytes() == before


def test_ignores_unknown_tracks(tmp_path: Path):
    sfx_track = _track("SFX", "Audio", [_clip("whoosh", 1.0, 1, 0)])
    v = _track("V1_Video", "Video", [_clip("scene_001_phrase_001", 2.0, 1, 1)])
    _write_otio(tmp_path / "timelines" / "t.otio", "draft", [v, sfx_track])
    view = build_timeline_view(str(tmp_path))
    # SFX is not a canonical track; it must not appear.
    assert {t.name for t in view.tracks} == set(CANONICAL_TRACKS)
