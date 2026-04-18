"""
Unit tests for the Preference Ledger storage substrate (ARCH-A1, issue #131).

Covers the invariants declared in ``server/callbacks/preference_ledger.py``:

1. Append-only -- existing records never mutate, no delete API exists.
2. Monotonic revision -- each append gets ``current_revision + 1``.
3. Blackboard-only access -- state is read/written under the dedicated
   ``PREFERENCE_LEDGER_KEY``.
4. Fail loud -- invalid enums / missing fields raise immediately.
5. Closed vocabularies -- scope/polarity/subject reject unknown members.
6. Query-by-scope returns scope-equal records in insertion order, honouring
   an optional ``scope_ref`` filter.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make server/ imports work when running `pytest` from the repo root.
_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from callbacks.preference_ledger import (  # noqa: E402
    PREFERENCE_LEDGER_KEY,
    Origin,
    Polarity,
    PreferenceRecord,
    Scope,
    Subject,
    append_preference,
    current_revision,
    list_preferences,
    query_by_scope,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _origin(event_id: str = "L4-001", reviewer: str = "alice") -> Origin:
    return Origin(
        l4_event_id=event_id,
        reviewer=reviewer,
        timestamp="2026-04-18T12:00:00Z",
    )


def _append_scene_prefer(state, scope_ref="scene-1", content="warmer tone"):
    return append_preference(
        state,
        scope=Scope.SCENE,
        scope_ref=scope_ref,
        polarity=Polarity.PREFER,
        subject=Subject.TONE,
        content=content,
        origin=_origin(),
    )


# ---------------------------------------------------------------------------
# Empty-state behaviour
# ---------------------------------------------------------------------------


def test_empty_state_has_zero_revision_and_no_records():
    state: dict = {}
    assert current_revision(state) == 0
    assert list_preferences(state) == []
    assert query_by_scope(state, Scope.GLOBAL) == []


def test_missing_key_is_treated_as_empty():
    # Key absent entirely.
    assert current_revision({}) == 0
    # Key present but empty string (how ADK often initialises JSON-string slots).
    assert current_revision({PREFERENCE_LEDGER_KEY: ""}) == 0
    # Key present as empty list (test-friendly form).
    assert current_revision({PREFERENCE_LEDGER_KEY: []}) == 0


# ---------------------------------------------------------------------------
# Append + revision monotonicity
# ---------------------------------------------------------------------------


def test_append_assigns_revision_1_then_monotonic():
    state: dict = {}
    r1 = _append_scene_prefer(state, content="first")
    r2 = _append_scene_prefer(state, content="second")
    r3 = _append_scene_prefer(state, content="third")
    assert [r.revision for r in (r1, r2, r3)] == [1, 2, 3]
    assert current_revision(state) == 3


def test_append_persists_across_reloads_via_json_string():
    state: dict = {}
    _append_scene_prefer(state, content="first")
    _append_scene_prefer(state, content="second")
    # The ledger is serialised as a JSON string under the dedicated key.
    raw = state[PREFERENCE_LEDGER_KEY]
    assert isinstance(raw, str)
    decoded = json.loads(raw)
    assert isinstance(decoded, list)
    assert len(decoded) == 2
    assert [entry["revision"] for entry in decoded] == [1, 2]
    assert [entry["content"] for entry in decoded] == ["first", "second"]


def test_append_accepts_string_enum_forms():
    state: dict = {}
    record = append_preference(
        state,
        scope="scene",
        scope_ref="scene-1",
        polarity="require",
        subject="duration",
        content="exactly 45s",
        origin=_origin(),
    )
    assert record.scope is Scope.SCENE
    assert record.polarity is Polarity.REQUIRE
    assert record.subject is Subject.DURATION


def test_append_accepts_mapping_origin():
    state: dict = {}
    record = append_preference(
        state,
        scope=Scope.GLOBAL,
        polarity=Polarity.FORBID,
        subject=Subject.MUSIC,
        content="no synth pads",
        origin={
            "l4_event_id": "L4-17",
            "reviewer": "bob",
            "timestamp": "2026-04-18T12:05:00Z",
        },
    )
    assert record.origin.l4_event_id == "L4-17"
    assert record.origin.reviewer == "bob"


# ---------------------------------------------------------------------------
# Append-only guarantee: no mutation, no delete
# ---------------------------------------------------------------------------


def test_returned_record_is_frozen():
    state: dict = {}
    record = _append_scene_prefer(state)
    with pytest.raises(Exception):
        # Frozen dataclass -- assignment must raise.
        record.content = "mutated"  # type: ignore[misc]


def test_prior_records_unchanged_after_subsequent_appends():
    state: dict = {}
    first = _append_scene_prefer(state, content="keep me").to_dict()
    _append_scene_prefer(state, content="newer")
    _append_scene_prefer(state, scope_ref="scene-2", content="other scene")

    raw = json.loads(state[PREFERENCE_LEDGER_KEY])
    assert raw[0] == first
    assert raw[0]["content"] == "keep me"
    assert raw[0]["revision"] == 1


def test_no_delete_or_update_api_exported():
    import callbacks.preference_ledger as module

    # Sanity: verify the module only exports append/query surfaces (plus schema
    # types and current_revision). Any accidental ``delete_``/``update_``
    # helper would break the append-only invariant.
    exported = set(module.__all__)
    forbidden_prefixes = ("delete_", "update_", "remove_", "pop_", "clear_", "set_")
    for name in exported:
        assert not name.startswith(forbidden_prefixes), (
            f"Ledger must be append-only; disallowed export: {name}"
        )


# ---------------------------------------------------------------------------
# Fail-loud validation
# ---------------------------------------------------------------------------


def test_unknown_scope_rejected():
    state: dict = {}
    with pytest.raises(ValueError):
        append_preference(
            state,
            scope="not_a_scope",
            polarity=Polarity.PREFER,
            subject=Subject.TONE,
            content="x",
            origin=_origin(),
        )


def test_unknown_polarity_rejected():
    state: dict = {}
    with pytest.raises(ValueError):
        append_preference(
            state,
            scope=Scope.GLOBAL,
            polarity="maybe",
            subject=Subject.TONE,
            content="x",
            origin=_origin(),
        )


def test_unknown_subject_rejected():
    state: dict = {}
    with pytest.raises(ValueError):
        append_preference(
            state,
            scope=Scope.GLOBAL,
            polarity=Polarity.PREFER,
            subject="color_palette",
            content="x",
            origin=_origin(),
        )


def test_empty_content_rejected():
    state: dict = {}
    with pytest.raises(ValueError):
        append_preference(
            state,
            scope=Scope.GLOBAL,
            polarity=Polarity.PREFER,
            subject=Subject.TONE,
            content="   ",
            origin=_origin(),
        )


def test_missing_origin_fields_rejected():
    state: dict = {}
    with pytest.raises(ValueError):
        append_preference(
            state,
            scope=Scope.GLOBAL,
            polarity=Polarity.PREFER,
            subject=Subject.TONE,
            content="x",
            origin={"l4_event_id": "L4-1", "reviewer": "alice"},  # missing timestamp
        )


def test_bad_origin_type_rejected():
    state: dict = {}
    with pytest.raises(TypeError):
        append_preference(
            state,
            scope=Scope.GLOBAL,
            polarity=Polarity.PREFER,
            subject=Subject.TONE,
            content="x",
            origin="L4-1 / alice / now",  # type: ignore[arg-type]
        )


def test_global_scope_rejects_scope_ref():
    state: dict = {}
    with pytest.raises(ValueError):
        append_preference(
            state,
            scope=Scope.GLOBAL,
            scope_ref="not_allowed",
            polarity=Polarity.PREFER,
            subject=Subject.TONE,
            content="x",
            origin=_origin(),
        )


def test_malformed_stored_state_raises_on_read():
    state = {PREFERENCE_LEDGER_KEY: "{not json"}
    with pytest.raises(ValueError):
        list_preferences(state)
    with pytest.raises(ValueError):
        current_revision(state)


def test_stored_state_wrong_shape_raises():
    state = {PREFERENCE_LEDGER_KEY: json.dumps({"not": "a list"})}
    with pytest.raises(ValueError):
        list_preferences(state)


def test_stored_state_wrong_type_raises():
    state = {PREFERENCE_LEDGER_KEY: 42}
    with pytest.raises(TypeError):
        list_preferences(state)


def test_stored_entry_missing_revision_raises():
    bad_entry = {
        "scope": "global",
        "polarity": "prefer",
        "subject": "tone",
        "content": "x",
        "origin": {
            "l4_event_id": "L4-1",
            "reviewer": "alice",
            "timestamp": "2026-04-18T12:00:00Z",
        },
        # no revision
    }
    state = {PREFERENCE_LEDGER_KEY: json.dumps([bad_entry])}
    with pytest.raises(ValueError):
        current_revision(state)


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------


def test_record_to_dict_from_dict_round_trip():
    record = PreferenceRecord(
        scope=Scope.SCENE,
        scope_ref="scene-3",
        polarity=Polarity.AVOID,
        subject=Subject.VISUAL_STYLE,
        content="no lens flares",
        origin=_origin("L4-42", "carol"),
        revision=7,
        metadata={"source_slot": "visual_direction"},
    )
    restored = PreferenceRecord.from_dict(record.to_dict())
    assert restored == record


def test_list_preferences_returns_typed_records():
    state: dict = {}
    _append_scene_prefer(state)
    records = list_preferences(state)
    assert len(records) == 1
    assert isinstance(records[0], PreferenceRecord)
    assert isinstance(records[0].scope, Scope)
    assert isinstance(records[0].polarity, Polarity)
    assert isinstance(records[0].subject, Subject)
    assert isinstance(records[0].origin, Origin)


# ---------------------------------------------------------------------------
# Query-by-scope semantics
# ---------------------------------------------------------------------------


def test_query_by_scope_returns_only_matching_scope():
    state: dict = {}
    append_preference(
        state,
        scope=Scope.GLOBAL,
        polarity=Polarity.PREFER,
        subject=Subject.TONE,
        content="serious",
        origin=_origin(),
    )
    _append_scene_prefer(state, scope_ref="scene-1", content="warmer")
    _append_scene_prefer(state, scope_ref="scene-2", content="colder")

    scene_records = query_by_scope(state, Scope.SCENE)
    assert [r.content for r in scene_records] == ["warmer", "colder"]
    assert all(r.scope is Scope.SCENE for r in scene_records)

    global_records = query_by_scope(state, Scope.GLOBAL)
    assert [r.content for r in global_records] == ["serious"]


def test_query_by_scope_ref_filters_within_scope():
    state: dict = {}
    _append_scene_prefer(state, scope_ref="scene-1", content="first scene pref")
    _append_scene_prefer(state, scope_ref="scene-2", content="second scene pref")
    _append_scene_prefer(state, scope_ref="scene-1", content="another for scene 1")

    scene1 = query_by_scope(state, Scope.SCENE, scope_ref="scene-1")
    assert [r.content for r in scene1] == ["first scene pref", "another for scene 1"]

    scene2 = query_by_scope(state, Scope.SCENE, scope_ref="scene-2")
    assert [r.content for r in scene2] == ["second scene pref"]

    missing = query_by_scope(state, Scope.SCENE, scope_ref="scene-999")
    assert missing == []


def test_query_by_scope_accepts_string_scope():
    state: dict = {}
    _append_scene_prefer(state)
    assert query_by_scope(state, "scene") == query_by_scope(state, Scope.SCENE)


def test_query_by_scope_preserves_insertion_order():
    state: dict = {}
    _append_scene_prefer(state, content="a")
    _append_scene_prefer(state, content="b")
    _append_scene_prefer(state, content="c")
    records = query_by_scope(state, Scope.SCENE)
    assert [r.revision for r in records] == [1, 2, 3]
    assert [r.content for r in records] == ["a", "b", "c"]


def test_query_does_not_mutate_state():
    state: dict = {}
    _append_scene_prefer(state)
    before = state[PREFERENCE_LEDGER_KEY]
    _ = query_by_scope(state, Scope.SCENE)
    _ = list_preferences(state)
    _ = current_revision(state)
    assert state[PREFERENCE_LEDGER_KEY] == before


def test_unknown_query_scope_rejected():
    state: dict = {}
    _append_scene_prefer(state)
    with pytest.raises(ValueError):
        query_by_scope(state, "not_a_scope")


# ---------------------------------------------------------------------------
# Closed-vocabulary coverage (sanity on issue #131 spec)
# ---------------------------------------------------------------------------


def test_scope_vocabulary_matches_spec():
    assert {s.value for s in Scope} == {
        "global",
        "stage",
        "scene",
        "voice_block",
        "artifact_type",
        "element",
    }


def test_polarity_vocabulary_matches_spec():
    assert {p.value for p in Polarity} == {"prefer", "avoid", "require", "forbid"}


def test_subject_vocabulary_matches_spec():
    assert {s.value for s in Subject} == {
        "tone",
        "voice",
        "pacing",
        "visual_style",
        "narrative_structure",
        "speaker_role",
        "duration",
        "music",
    }
