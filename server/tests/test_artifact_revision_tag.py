"""
Unit tests for artifact revision tagging (ARCH-B1, issue #137).

Covers the invariants declared in
``server/callbacks/artifact_revision_tag.py``:

1. Tag shape validation (rejects negatives, wrong types, empty strings).
2. ``tag_artifact`` snapshots :func:`current_revision` at call time.
3. Immutability — a second ``tag_artifact`` on the same key raises
   :class:`ArtifactAlreadyTaggedError`.
4. Fail loud on missing Preference Ledger state.
5. Fail loud on missing / empty artifact under the producer callback.
6. ``clear_tag`` allows re-tagging (the re-manifestation path, ARCH-B3).
7. ``make_revision_tagging_callback`` auto-tags an agent's ``output_key``
   and defaults ``stage`` to ``ctx.agent_name``.
8. JSON round-trip through the blackboard is lossless and tags survive
   the JSON-string convention used by the ledger.
"""

from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make server/ imports work when running `pytest` from the repo root.
_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from callbacks.artifact_revision_tag import (  # noqa: E402
    ARTIFACT_REVISION_TAGS_KEY,
    ArtifactAlreadyTaggedError,
    ArtifactRevisionTag,
    MissingArtifactError,
    MissingLedgerStateError,
    clear_tag,
    get_tag,
    has_tag,
    list_tags,
    make_revision_tagging_callback,
    tag_artifact,
)
from callbacks.preference_ledger import (  # noqa: E402
    PREFERENCE_LEDGER_KEY,
    Origin,
    Polarity,
    Scope,
    Subject,
    append_preference,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_empty_ledger(state: dict) -> None:
    """Mark the ledger as seeded (R0 complete) but with zero entries."""
    state[PREFERENCE_LEDGER_KEY] = json.dumps([])


def _append_scene_prefer(state: dict, content: str = "warmer tone") -> None:
    """Append one scene-scoped preference so revision advances."""
    append_preference(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        polarity=Polarity.PREFER,
        subject=Subject.TONE,
        content=content,
        origin=Origin(
            l4_event_id="L4-001",
            reviewer="alice",
            timestamp="2026-04-18T12:00:00Z",
        ),
    )


def _ctx(state: dict, agent_name: str = "scenario_director") -> SimpleNamespace:
    """Minimal CallbackContext stand-in — just ``state`` + ``agent_name``."""
    return SimpleNamespace(state=state, agent_name=agent_name)


def _fixed_now() -> datetime:
    return datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# ArtifactRevisionTag shape
# ---------------------------------------------------------------------------


def test_tag_is_frozen_and_immutable():
    tag = ArtifactRevisionTag(
        ledger_revision=3, derived_at="2026-04-18T12:00:00Z", stage="s"
    )
    with pytest.raises(FrozenInstanceError):
        tag.ledger_revision = 4  # type: ignore[misc]


def test_tag_rejects_negative_revision():
    with pytest.raises(ValueError):
        ArtifactRevisionTag(
            ledger_revision=-1, derived_at="t", stage="s"
        )


def test_tag_rejects_bool_revision():
    # bool is an int subclass in Python; explicit reject to prevent True/False sneaking in.
    with pytest.raises(TypeError):
        ArtifactRevisionTag(
            ledger_revision=True, derived_at="t", stage="s"  # type: ignore[arg-type]
        )


def test_tag_rejects_non_int_revision():
    with pytest.raises(TypeError):
        ArtifactRevisionTag(
            ledger_revision="1", derived_at="t", stage="s"  # type: ignore[arg-type]
        )


def test_tag_rejects_empty_derived_at():
    with pytest.raises(ValueError):
        ArtifactRevisionTag(ledger_revision=0, derived_at="", stage="s")


def test_tag_rejects_empty_stage():
    with pytest.raises(ValueError):
        ArtifactRevisionTag(ledger_revision=0, derived_at="t", stage="")


def test_tag_accepts_revision_zero():
    tag = ArtifactRevisionTag(
        ledger_revision=0, derived_at="t", stage="s"
    )
    assert tag.ledger_revision == 0


def test_tag_dict_roundtrip():
    tag = ArtifactRevisionTag(
        ledger_revision=5,
        derived_at="2026-04-18T12:00:00+00:00",
        stage="scenario_director",
    )
    restored = ArtifactRevisionTag.from_dict(tag.to_dict())
    assert restored == tag


def test_from_dict_rejects_missing_fields():
    with pytest.raises(ValueError):
        ArtifactRevisionTag.from_dict(
            {"ledger_revision": 1, "derived_at": "t"}
        )


def test_from_dict_rejects_non_mapping():
    with pytest.raises(TypeError):
        ArtifactRevisionTag.from_dict([1, 2, 3])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# tag_artifact — fail-loud preconditions
# ---------------------------------------------------------------------------


def test_tag_artifact_fails_when_ledger_missing():
    state: dict = {}
    with pytest.raises(MissingLedgerStateError):
        tag_artifact(state, "scenes", stage="scenario_director")


def test_tag_artifact_works_when_ledger_is_empty_but_seeded():
    state: dict = {}
    _seed_empty_ledger(state)
    tag = tag_artifact(state, "scenes", stage="scenario_director")
    assert tag.ledger_revision == 0
    assert tag.stage == "scenario_director"


def test_tag_artifact_rejects_empty_artifact_key():
    state: dict = {}
    _seed_empty_ledger(state)
    with pytest.raises(ValueError):
        tag_artifact(state, "", stage="scenario_director")


def test_tag_artifact_rejects_empty_stage():
    state: dict = {}
    _seed_empty_ledger(state)
    with pytest.raises(ValueError):
        tag_artifact(state, "scenes", stage="")


# ---------------------------------------------------------------------------
# tag_artifact — snapshot semantics
# ---------------------------------------------------------------------------


def test_tag_artifact_snapshots_current_revision():
    state: dict = {}
    _seed_empty_ledger(state)
    _append_scene_prefer(state)
    _append_scene_prefer(state, content="cooler tone")

    tag = tag_artifact(
        state, "scenes", stage="scenario_director", now=_fixed_now()
    )
    assert tag.ledger_revision == 2
    assert tag.derived_at == _fixed_now().isoformat()
    assert tag.stage == "scenario_director"


def test_tag_artifact_returned_value_matches_stored_tag():
    state: dict = {}
    _seed_empty_ledger(state)
    returned = tag_artifact(
        state, "scenes", stage="scenario_director", now=_fixed_now()
    )
    stored = get_tag(state, "scenes")
    assert stored == returned


def test_tag_artifact_multiple_keys_coexist():
    state: dict = {}
    _seed_empty_ledger(state)
    tag_artifact(state, "scenes", stage="scenario_director")
    _append_scene_prefer(state)
    tag_artifact(state, "visual_concepts", stage="visual_concepter")

    tags = list_tags(state)
    assert set(tags) == {"scenes", "visual_concepts"}
    assert tags["scenes"].ledger_revision == 0
    assert tags["visual_concepts"].ledger_revision == 1


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_tag_artifact_is_immutable_once_set():
    state: dict = {}
    _seed_empty_ledger(state)
    tag_artifact(state, "scenes", stage="scenario_director")
    with pytest.raises(ArtifactAlreadyTaggedError):
        tag_artifact(state, "scenes", stage="scenario_director")


def test_immutability_holds_after_revision_advance():
    state: dict = {}
    _seed_empty_ledger(state)
    tag_artifact(state, "scenes", stage="scenario_director", now=_fixed_now())
    _append_scene_prefer(state)  # revision is now 1
    # Even at a later revision, the tag is locked.
    with pytest.raises(ArtifactAlreadyTaggedError):
        tag_artifact(state, "scenes", stage="scenario_director")
    # And the original snapshot is preserved.
    assert get_tag(state, "scenes").ledger_revision == 0


# ---------------------------------------------------------------------------
# clear_tag — the re-manifestation path (ARCH-B3)
# ---------------------------------------------------------------------------


def test_clear_tag_removes_tag():
    state: dict = {}
    _seed_empty_ledger(state)
    tag_artifact(state, "scenes", stage="scenario_director")
    clear_tag(state, "scenes")
    assert get_tag(state, "scenes") is None
    assert not has_tag(state, "scenes")


def test_clear_tag_allows_retag_at_new_revision():
    state: dict = {}
    _seed_empty_ledger(state)
    tag_artifact(state, "scenes", stage="scenario_director")
    _append_scene_prefer(state)
    clear_tag(state, "scenes")
    new_tag = tag_artifact(state, "scenes", stage="scenario_director")
    assert new_tag.ledger_revision == 1


def test_clear_tag_fails_loud_when_absent():
    state: dict = {}
    _seed_empty_ledger(state)
    with pytest.raises(KeyError):
        clear_tag(state, "scenes")


# ---------------------------------------------------------------------------
# JSON round-trip through the blackboard
# ---------------------------------------------------------------------------


def test_blackboard_storage_is_a_json_string():
    state: dict = {}
    _seed_empty_ledger(state)
    tag_artifact(state, "scenes", stage="scenario_director")
    raw = state[ARTIFACT_REVISION_TAGS_KEY]
    assert isinstance(raw, str)
    decoded = json.loads(raw)
    assert "scenes" in decoded
    assert decoded["scenes"]["stage"] == "scenario_director"


def test_tags_survive_json_string_round_trip():
    """A fresh state rehydrated from the JSON string reads the same tags."""
    state: dict = {}
    _seed_empty_ledger(state)
    tag_artifact(state, "scenes", stage="scenario_director", now=_fixed_now())

    # Simulate loading the blackboard afresh — only the serialised string survives.
    rehydrated = {
        PREFERENCE_LEDGER_KEY: state[PREFERENCE_LEDGER_KEY],
        ARTIFACT_REVISION_TAGS_KEY: state[ARTIFACT_REVISION_TAGS_KEY],
    }
    tag = get_tag(rehydrated, "scenes")
    assert tag is not None
    assert tag.ledger_revision == 0
    assert tag.stage == "scenario_director"


def test_malformed_json_fails_loud():
    state = {ARTIFACT_REVISION_TAGS_KEY: "{not-json"}
    with pytest.raises(ValueError):
        has_tag(state, "scenes")


def test_wrong_json_shape_fails_loud():
    state = {ARTIFACT_REVISION_TAGS_KEY: json.dumps([1, 2, 3])}
    with pytest.raises(ValueError):
        has_tag(state, "scenes")


# ---------------------------------------------------------------------------
# Universal producer-side callback
# ---------------------------------------------------------------------------


def test_callback_tags_output_key_automatically():
    state: dict = {}
    _seed_empty_ledger(state)
    _append_scene_prefer(state)
    state["scenes"] = json.dumps([{"scene_num": 1}])

    cb = make_revision_tagging_callback("scenes")
    cb(_ctx(state, agent_name="scenario_director"))

    tag = get_tag(state, "scenes")
    assert tag is not None
    assert tag.ledger_revision == 1
    # Default stage falls back to the running agent's name.
    assert tag.stage == "scenario_director"


def test_callback_stage_override_wins_over_agent_name():
    state: dict = {}
    _seed_empty_ledger(state)
    state["scenes"] = "non-empty"

    cb = make_revision_tagging_callback("scenes", stage="stage_one")
    cb(_ctx(state, agent_name="scenario_director"))

    assert get_tag(state, "scenes").stage == "stage_one"


def test_callback_fails_loud_on_missing_artifact():
    state: dict = {}
    _seed_empty_ledger(state)
    cb = make_revision_tagging_callback("scenes")
    with pytest.raises(MissingArtifactError):
        cb(_ctx(state))


def test_callback_fails_loud_on_empty_string_artifact():
    state: dict = {}
    _seed_empty_ledger(state)
    state["scenes"] = ""
    cb = make_revision_tagging_callback("scenes")
    with pytest.raises(MissingArtifactError):
        cb(_ctx(state))


def test_callback_fails_loud_on_empty_list_artifact():
    state: dict = {}
    _seed_empty_ledger(state)
    state["scenes"] = []
    cb = make_revision_tagging_callback("scenes")
    with pytest.raises(MissingArtifactError):
        cb(_ctx(state))


def test_callback_permits_missing_artifact_when_require_artifact_false():
    state: dict = {}
    _seed_empty_ledger(state)
    cb = make_revision_tagging_callback("scenes", require_artifact=False)
    # Empty artifact is tolerated — tag still attaches (e.g. optional phase output).
    cb(_ctx(state))
    assert has_tag(state, "scenes")


def test_callback_is_immutable_by_default():
    state: dict = {}
    _seed_empty_ledger(state)
    state["scenes"] = "draft"
    cb = make_revision_tagging_callback("scenes")
    cb(_ctx(state))
    with pytest.raises(ArtifactAlreadyTaggedError):
        cb(_ctx(state))


def test_callback_retag_on_reproduce_refreshes_tag():
    """LoopAgent-composed producers refresh on each iteration."""
    state: dict = {}
    _seed_empty_ledger(state)
    state["scenes"] = "draft"
    cb = make_revision_tagging_callback("scenes", retag_on_reproduce=True)

    cb(_ctx(state, agent_name="scenario_director"))
    assert get_tag(state, "scenes").ledger_revision == 0

    # New preference lands mid-loop; next iteration must re-tag at revision 1.
    _append_scene_prefer(state)
    cb(_ctx(state, agent_name="scenario_director"))
    assert get_tag(state, "scenes").ledger_revision == 1


def test_callback_returns_none():
    state: dict = {}
    _seed_empty_ledger(state)
    state["scenes"] = "x"
    cb = make_revision_tagging_callback("scenes")
    assert cb(_ctx(state)) is None


def test_callback_factory_rejects_empty_output_key():
    with pytest.raises(ValueError):
        make_revision_tagging_callback("")


def test_callback_factory_rejects_empty_stage_override():
    with pytest.raises(ValueError):
        make_revision_tagging_callback("scenes", stage="")


def test_callback_is_named_for_observability():
    cb = make_revision_tagging_callback("scenes")
    assert cb.__name__ == "tag_revision_after_scenes"
    # Docstring references the output key so log lines are self-documenting.
    assert "scenes" in (cb.__doc__ or "")


# ---------------------------------------------------------------------------
# Proof-of-pattern integration: scenario_director
# ---------------------------------------------------------------------------


def test_content_analyst_has_tagging_after_agent_callback():
    """The visual_director's content_analyst agent carries the universal
    ARCH-B1 after_agent_callback as its proof-of-pattern wiring.

    We assert on shape (callback name + output_key link) rather than
    invoking the callback end-to-end, because the underlying Agent
    construction pulls in heavy transitive imports that are exercised
    by the pipeline smoke tests, not by this unit test.
    """
    from agents import visual_director as vd_mod

    cb = vd_mod.content_analyst.after_agent_callback
    assert cb is not None
    assert cb.__name__ == "tag_revision_after_content_analysis"


def test_scenario_director_tags_scenes_after_loop(monkeypatch):
    """The scenario_director's chained after_agent_callback must attach an
    ``_artifact_revision_tags["scenes"]`` entry once the loop completes.

    This is the ARCH-B1 proof-of-pattern wiring: one concrete producer
    (the scenario LoopAgent) using the universal callback.
    """
    # Bypass heavy imports by stubbing the module-level deterministic step
    # the scenario_director calls first — the tagging side-effect is what
    # we actually care about.
    import callbacks.deterministic_steps as det

    def _passthrough(ctx):
        return None

    monkeypatch.setattr(det, "clean_scenes_after_scenario", _passthrough)

    from agents import scenario_director as sd_mod

    state: dict = {}
    _seed_empty_ledger(state)
    _append_scene_prefer(state)
    state["scenes"] = json.dumps([{"scene_num": 1, "title": "x"}])
    state["pipeline_phase"] = "scenario"

    ctx = _ctx(state, agent_name="scenario_director")
    # The scenario_director's after_agent_callback is the chained tagging wrapper.
    sd_mod.scenario_director.after_agent_callback(ctx)

    tag = get_tag(state, "scenes")
    assert tag is not None
    assert tag.ledger_revision == 1
    assert tag.stage == "scenario_director"
