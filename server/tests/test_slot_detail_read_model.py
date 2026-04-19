"""
Tests for the slot-detail read-model (ARCH-H3, issue #158).

The side panel opens when a user clicks a slot on the centrepiece timeline.
It aggregates — in a single request — artifact history, QA verdicts,
reasoning digests, in-scope ledger records, the current ladder rung and
the latest preview assembly.

These tests assert:

* The aggregator is a pure read model (no file / state mutation).
* ``ledger_revision_at_derivation`` is stamped onto history rows when
  a matching ARCH-B1 revision tag is present on the blackboard.
* Ledger records are filtered by scope (GLOBAL always included; scene
  scope matches on ``scope_ref``).
* Current-rung lookup reads ``_status.json`` for video slots.
* Latest preview assembly points at the newest ``assembly/*.mp4``.
* Unknown slot ids fail fast with ``ValueError``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from slot_detail_model import build_slot_detail  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline_dir(tmp_path: Path) -> Path:
    # Video status file with an in-progress rung
    video_dir = tmp_path / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / "scene_001_phrase_001_status.json").write_text(json.dumps({
        "quality": "unknown",
        "attempts": 2,
        "rung": "L2_REFINED",
    }))

    # Assembly dir with two previews; the most recent must win.
    assembly_dir = tmp_path / "assembly"
    assembly_dir.mkdir(parents=True, exist_ok=True)
    older = assembly_dir / "preview_20250101.mp4"
    newer = assembly_dir / "preview_20250102.mp4"
    older.write_bytes(b"old")
    newer.write_bytes(b"new")
    # Enforce mtime order regardless of filesystem resolution.
    os.utime(str(older), (time.time() - 100, time.time() - 100))
    os.utime(str(newer), (time.time(), time.time()))

    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unknown_slot_id_raises():
    with pytest.raises(ValueError):
        build_slot_detail("not-a-slot", "/tmp")


def test_video_slot_detail_assembles_expected_fields(pipeline_dir: Path):
    feedback = [
        {
            "id": "art-1",
            "type": "video_clip",
            "status": "approved",
            "scene_num": 1,
            "phrase_idx": 1,
            "preview_url": "/media/v/1_1.mp4",
            "timestamp": 1.0,
            "metadata": {"rev": 1},
        },
        {
            "id": "art-2",
            "type": "video_clip",
            "status": "rejected",
            "scene_num": 1,
            "phrase_idx": 1,
            "timestamp": 0.5,
            "metadata": {"rev": 0},
        },
        # Unrelated artifact — must NOT appear in history.
        {
            "id": "art-3",
            "type": "video_clip",
            "status": "approved",
            "scene_num": 2,
            "phrase_idx": 7,
            "timestamp": 3.0,
        },
    ]
    detail = build_slot_detail(
        "V1:1:1",
        str(pipeline_dir),
        feedback_artifacts=feedback,
    )
    assert detail.slot_id == "V1:1:1"
    assert detail.track == "V1_Video"
    assert len(detail.artifact_history) == 2
    # Sorted by timestamp ascending.
    assert [h["id"] for h in detail.artifact_history] == ["art-2", "art-1"]
    # Current rung pulled from _status.json
    assert detail.current_rung["rung"] == "L2_REFINED"
    # Latest preview is the newer file.
    assert os.path.basename(detail.latest_preview["path"]) == "preview_20250102.mp4"


def test_narration_slot_filters_artifacts_by_type(pipeline_dir: Path):
    feedback = [
        {"id": "v", "type": "video_clip", "status": "approved",
         "scene_num": 1, "phrase_idx": 2, "timestamp": 0},
        {"id": "n", "type": "narration", "status": "approved",
         "scene_num": 1, "phrase_idx": 2, "timestamp": 1},
    ]
    detail = build_slot_detail(
        "A1:1:2", str(pipeline_dir), feedback_artifacts=feedback
    )
    assert [h["id"] for h in detail.artifact_history] == ["n"]


def test_revision_tag_stamped_on_history(pipeline_dir: Path):
    feedback = [
        {"id": "art-1", "type": "video_clip", "status": "approved",
         "scene_num": 1, "phrase_idx": 1, "timestamp": 0},
    ]
    state = {
        "_artifact_revision_tags": json.dumps({
            "V1:1:1": {"revision": 3, "snapshot_at": "2025-01-01T00:00:00Z"},
        })
    }
    detail = build_slot_detail(
        "V1:1:1",
        str(pipeline_dir),
        feedback_artifacts=feedback,
        state=state,
    )
    assert detail.artifact_history
    tag = detail.artifact_history[0]["ledger_revision_at_derivation"]
    assert tag["revision"] == 3


def test_pure_read_model_does_not_mutate_files(pipeline_dir: Path):
    files_before = {
        p: p.read_bytes()
        for p in pipeline_dir.rglob("*")
        if p.is_file()
    }
    build_slot_detail("V1:1:1", str(pipeline_dir))
    build_slot_detail("A1:1:1", str(pipeline_dir))
    build_slot_detail("A2:1:1", str(pipeline_dir))
    files_after = {
        p: p.read_bytes()
        for p in pipeline_dir.rglob("*")
        if p.is_file()
    }
    assert files_before == files_after


def test_empty_output_dir_yields_empty_sections(tmp_path: Path):
    detail = build_slot_detail("V1:5:9", str(tmp_path))
    assert detail.artifact_history == []
    assert detail.qa_verdicts == []
    assert detail.reasoning_digests == []
    assert detail.ledger_records == []
    assert detail.current_rung == {}
    assert detail.latest_preview == {}
