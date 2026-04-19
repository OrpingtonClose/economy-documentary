"""Tests for :func:`callbacks.preference_ledger.records_applying_to_slot`
(UI-04b / issue #202).

The helper is the read-side contract that the slot-detail aggregator
(UI-04a) uses to decide which ledger records apply to a clicked slot.
Each scope has its own containment rule (see the helper's docstring);
this file exercises every scope plus the superseded-record flag and the
revision-ascending ordering guarantee.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from callbacks.preference_ledger import (  # noqa: E402
    Origin,
    Polarity,
    Scope,
    Subject,
    append_preference,
    records_applying_to_slot,
)


def _append(
    state,
    *,
    scope,
    scope_ref=None,
    subject=Subject.TONE,
    polarity=Polarity.PREFER,
    content="c",
):
    return append_preference(
        state,
        scope=scope,
        scope_ref=scope_ref,
        polarity=polarity,
        subject=subject,
        content=content,
        origin=Origin(
            l4_event_id="R0",
            reviewer="tester",
            timestamp="2025-01-01T00:00:00Z",
        ),
    )


# ---------------------------------------------------------------------------
# Scope-by-scope
# ---------------------------------------------------------------------------


def test_malformed_slot_id_raises():
    with pytest.raises(ValueError):
        records_applying_to_slot({}, "not-a-slot")


def test_empty_ledger_returns_empty_list():
    assert records_applying_to_slot({}, "V1:1:1") == []


def test_global_records_always_apply():
    state: dict = {}
    _append(state, scope=Scope.GLOBAL, content="g")
    for slot in ("V1:1:1", "A1:2:3", "A2:4:5"):
        records = records_applying_to_slot(state, slot)
        assert len(records) == 1
        assert records[0].scope is Scope.GLOBAL
        assert records[0].content == "g"


def test_stage_unrefed_matches_every_track():
    state: dict = {}
    _append(state, scope=Scope.STAGE, scope_ref=None, content="any-stage")
    for slot in ("V1:1:1", "A1:1:1", "A2:1:1"):
        records = records_applying_to_slot(state, slot)
        assert [r.content for r in records] == ["any-stage"]


def test_stage_refed_matches_only_its_track():
    state: dict = {}
    _append(state, scope=Scope.STAGE, scope_ref="audio", content="audio-only")
    _append(state, scope=Scope.STAGE, scope_ref="video", content="video-only")
    _append(state, scope=Scope.STAGE, scope_ref="music", content="music-only")

    video = {r.content for r in records_applying_to_slot(state, "V1:1:1")}
    narration = {r.content for r in records_applying_to_slot(state, "A1:1:1")}
    music = {r.content for r in records_applying_to_slot(state, "A2:1:1")}

    assert "video-only" in video and "audio-only" not in video
    assert "audio-only" in narration and "video-only" not in narration
    # A2_Music is owned by both audio and music stages.
    assert "music-only" in music and "audio-only" in music
    assert "video-only" not in music


def test_scene_scope_matches_its_scene_only():
    state: dict = {}
    _append(state, scope=Scope.SCENE, scope_ref="1", content="s1-plain")
    _append(state, scope=Scope.SCENE, scope_ref="scene-1", content="s1-dashed")
    _append(state, scope=Scope.SCENE, scope_ref="scene_001", content="s1-padded")
    _append(state, scope=Scope.SCENE, scope_ref="2", content="s2")

    contents = {r.content for r in records_applying_to_slot(state, "V1:1:1")}
    assert contents == {"s1-plain", "s1-dashed", "s1-padded"}
    contents = {r.content for r in records_applying_to_slot(state, "V1:2:1")}
    assert contents == {"s2"}


def test_scene_unrefed_matches_any_scene():
    state: dict = {}
    _append(state, scope=Scope.SCENE, scope_ref=None, content="any-scene")
    for slot in ("V1:1:1", "V1:7:3"):
        records = records_applying_to_slot(state, slot)
        assert [r.content for r in records] == ["any-scene"]


def test_voice_block_only_applies_to_narration():
    state: dict = {}
    _append(state, scope=Scope.VOICE_BLOCK, scope_ref="vb-1", content="vb")

    # Narration with matching voice block
    records = records_applying_to_slot(state, "A1:1:1", voice_block_ref="vb-1")
    assert [r.content for r in records] == ["vb"]

    # Narration without voice_block_ref context
    assert records_applying_to_slot(state, "A1:1:1") == []

    # Narration with different voice block
    assert records_applying_to_slot(state, "A1:1:1", voice_block_ref="vb-2") == []

    # Video / music slots never get voice_block records
    assert records_applying_to_slot(state, "V1:1:1", voice_block_ref="vb-1") == []
    assert records_applying_to_slot(state, "A2:1:1", voice_block_ref="vb-1") == []


def test_voice_block_unrefed_matches_any_narration():
    state: dict = {}
    _append(state, scope=Scope.VOICE_BLOCK, scope_ref=None, content="any-vb")
    records = records_applying_to_slot(state, "A1:3:2")
    assert [r.content for r in records] == ["any-vb"]
    # Still narration-only even when unrefed.
    assert records_applying_to_slot(state, "V1:3:2") == []


def test_artifact_type_scope():
    state: dict = {}
    _append(state, scope=Scope.ARTIFACT_TYPE, scope_ref="video_clip", content="vc")
    _append(state, scope=Scope.ARTIFACT_TYPE, scope_ref="narration", content="nar")
    _append(state, scope=Scope.ARTIFACT_TYPE, scope_ref=None, content="any")

    video = {r.content for r in records_applying_to_slot(state, "V1:1:1")}
    narration = {r.content for r in records_applying_to_slot(state, "A1:1:1")}
    assert video == {"vc", "any"}
    assert narration == {"nar", "any"}


def test_element_scope_matches_multiple_slot_forms():
    state: dict = {}
    _append(state, scope=Scope.ELEMENT, scope_ref="V1:1:1", content="canonical")
    _append(state, scope=Scope.ELEMENT, scope_ref="1_1", content="short")
    _append(state, scope=Scope.ELEMENT, scope_ref="scene_001_phrase_001", content="padded")
    _append(state, scope=Scope.ELEMENT, scope_ref="s1_p1", content="sp")
    _append(state, scope=Scope.ELEMENT, scope_ref="V1:2:1", content="other")

    contents = {r.content for r in records_applying_to_slot(state, "V1:1:1")}
    assert contents == {"canonical", "short", "padded", "sp"}


# ---------------------------------------------------------------------------
# Ordering and supersession
# ---------------------------------------------------------------------------


def test_records_are_returned_revision_ascending():
    state: dict = {}
    _append(state, scope=Scope.GLOBAL, content="first")
    _append(state, scope=Scope.SCENE, scope_ref="1", content="second")
    _append(state, scope=Scope.ELEMENT, scope_ref="V1:1:1", content="third")

    revisions = [
        r.revision for r in records_applying_to_slot(state, "V1:1:1")
    ]
    assert revisions == sorted(revisions)


def test_superseded_flag_on_older_same_subject_scope():
    state: dict = {}
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="1",
        subject=Subject.TONE,
        content="old-tone",
    )
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="1",
        subject=Subject.TONE,
        content="new-tone",
    )
    # A tone record under a different scope should NOT supersede the scene one.
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="global-tone",
    )

    records = records_applying_to_slot(state, "V1:1:1")
    by_content = {r.content: r for r in records}
    assert by_content["old-tone"].metadata.get("superseded") is True
    assert "superseded" not in by_content["new-tone"].metadata
    assert "superseded" not in by_content["global-tone"].metadata


def test_returned_records_are_copies_not_mutated_state():
    state: dict = {}
    _append(state, scope=Scope.SCENE, scope_ref="1", content="a")
    _append(state, scope=Scope.SCENE, scope_ref="1", content="b")

    # Call twice — the second call must see the same state (no mutation).
    first = records_applying_to_slot(state, "V1:1:1")
    second = records_applying_to_slot(state, "V1:1:1")
    assert [r.revision for r in first] == [r.revision for r in second]
    assert [r.metadata for r in first] == [r.metadata for r in second]
