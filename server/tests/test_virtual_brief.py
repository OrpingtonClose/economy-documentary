"""
Unit tests for the Virtual Brief assembler (ARCH-A4, issue #134).

Exercises the contract from ``server/callbacks/virtual_brief.py`` against
the Preference Ledger substrate (ARCH-A1, #131):

1. Scope containment -- hierarchical: broader-or-equal records apply to a
   narrower request; narrower records never leak upward; stage- and
   scope-ref matching behave as specified.
2. Specificity ordering -- ``global < stage < scene < voice_block <
   artifact_type < element`` in the sorted brief.
3. Recency tie-breaking -- within the same specificity, the record with
   the higher revision wins the effective decision.
4. Hard polarity dominance -- ``require`` / ``forbid`` beat ``prefer`` /
   ``avoid`` even when the hard record is less specific or older.
5. Hard conflict detection -- ``REQUIRE`` + ``FORBID`` on the same
   subject+scope surfaces a :class:`HardConflict` and blocks the
   subject from ``decisions`` (no silent resolution).
6. Fail-loud input validation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make server/ imports work when running ``pytest`` from the repo root.
_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from callbacks.preference_ledger import (  # noqa: E402
    PREFERENCE_LEDGER_KEY,
    Origin,
    Polarity,
    Scope,
    Subject,
    append_preference,
)
from callbacks.virtual_brief import (  # noqa: E402
    VIRTUAL_BRIEF_OUTPUT_KEY,
    EffectiveDecision,
    HardConflict,
    VirtualBrief,
    _brief_to_dict,
    assemble_virtual_brief,
    assemble_virtual_brief_tool,
    build_virtual_brief_agent,
    virtual_brief_after_agent_callback,
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


def _append(
    state,
    *,
    scope: Scope,
    polarity: Polarity = Polarity.PREFER,
    subject: Subject = Subject.TONE,
    content: str = "placeholder",
    scope_ref=None,
    event_id: str = "L4-001",
):
    return append_preference(
        state,
        scope=scope,
        scope_ref=scope_ref,
        polarity=polarity,
        subject=subject,
        content=content,
        origin=_origin(event_id=event_id),
    )


# ---------------------------------------------------------------------------
# Empty / no-op behaviour
# ---------------------------------------------------------------------------


def test_empty_ledger_returns_empty_brief():
    brief = assemble_virtual_brief({}, stage="audio")
    assert isinstance(brief, VirtualBrief)
    assert brief.applicable_records == ()
    assert brief.decisions == {}
    assert brief.hard_conflicts == ()
    assert brief.has_hard_conflict is False


def test_empty_ledger_with_scope_and_subject_still_ok():
    brief = assemble_virtual_brief(
        {}, scope=Scope.SCENE, scope_ref="scene-1", subject=Subject.TONE
    )
    assert brief.applicable_records == ()
    assert brief.decisions == {}


def test_missing_ledger_key_is_empty_not_error():
    # An absent key is treated as an empty ledger by list_preferences().
    brief = assemble_virtual_brief({})
    assert brief.applicable_records == ()


# ---------------------------------------------------------------------------
# Scope containment
# ---------------------------------------------------------------------------


def test_global_record_matches_any_request():
    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        polarity=Polarity.PREFER,
        subject=Subject.TONE,
        content="warm",
    )

    # Broadest request.
    brief = assemble_virtual_brief(state)
    assert len(brief.applicable_records) == 1

    # Narrow scene request.
    brief_scene = assemble_virtual_brief(
        state, stage="audio", scope=Scope.SCENE, scope_ref="scene-1"
    )
    assert len(brief_scene.applicable_records) == 1
    assert brief_scene.applicable_records[0].scope is Scope.GLOBAL


def test_stage_record_matches_only_that_stage():
    state: dict = {}
    _append(
        state,
        scope=Scope.STAGE,
        scope_ref="audio",
        polarity=Polarity.PREFER,
        subject=Subject.VOICE,
        content="mezzo",
    )

    matched = assemble_virtual_brief(state, stage="audio")
    other = assemble_virtual_brief(state, stage="visual_direction")
    none = assemble_virtual_brief(state)  # no stage given

    assert len(matched.applicable_records) == 1
    assert other.applicable_records == ()
    assert none.applicable_records == ()


def test_stage_record_with_null_scope_ref_matches_any_stage():
    state: dict = {}
    _append(
        state,
        scope=Scope.STAGE,
        scope_ref=None,
        polarity=Polarity.PREFER,
        subject=Subject.PACING,
        content="brisk",
    )

    for stage in ("audio", "visual_direction", "production", None):
        brief = assemble_virtual_brief(state, stage=stage)
        assert len(brief.applicable_records) == 1


def test_scene_record_matches_only_matching_scene_ref():
    state: dict = {}
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        polarity=Polarity.PREFER,
        subject=Subject.TONE,
        content="warm",
    )

    match = assemble_virtual_brief(state, scope=Scope.SCENE, scope_ref="scene-1")
    miss = assemble_virtual_brief(state, scope=Scope.SCENE, scope_ref="scene-2")
    assert len(match.applicable_records) == 1
    assert miss.applicable_records == ()


def test_scene_record_without_scope_ref_matches_any_scene():
    state: dict = {}
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref=None,
        polarity=Polarity.PREFER,
        subject=Subject.TONE,
        content="warm",
    )

    for ref in ("scene-1", "scene-2", "scene-99"):
        brief = assemble_virtual_brief(state, scope=Scope.SCENE, scope_ref=ref)
        assert len(brief.applicable_records) == 1


def test_narrower_record_does_not_leak_into_broader_request():
    state: dict = {}
    _append(
        state,
        scope=Scope.ELEMENT,
        scope_ref="clip-7",
        polarity=Polarity.REQUIRE,
        subject=Subject.DURATION,
        content="<=3s",
    )

    # Broad request (no scope given).
    brief = assemble_virtual_brief(state, stage="production")
    assert brief.applicable_records == ()

    # Intermediate-specificity request.
    brief_scene = assemble_virtual_brief(
        state, scope=Scope.SCENE, scope_ref="scene-1"
    )
    assert brief_scene.applicable_records == ()


def test_broader_unrefed_record_applies_to_narrower_request():
    """A scene-level record with scope_ref=None should apply to a
    voice-block request under that scene (conservative match: we apply
    unrefed broader records to any narrower request)."""
    state: dict = {}
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref=None,
        polarity=Polarity.PREFER,
        subject=Subject.TONE,
        content="warm",
    )
    brief = assemble_virtual_brief(
        state, scope=Scope.VOICE_BLOCK, scope_ref="vb-1"
    )
    assert len(brief.applicable_records) == 1


def test_broader_refed_record_does_not_auto_propagate_to_narrower_ref():
    """Without a scope chain, a scene-3 record does NOT silently apply to
    a voice_block request -- we can't prove the ref belongs to scene-3.

    Ledger authors must either leave broader records unrefed or add an
    explicit narrower record.
    """
    state: dict = {}
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-3",
        polarity=Polarity.PREFER,
        subject=Subject.TONE,
        content="warm",
    )
    brief = assemble_virtual_brief(
        state, scope=Scope.VOICE_BLOCK, scope_ref="vb-1"
    )
    assert brief.applicable_records == ()


# ---------------------------------------------------------------------------
# Specificity ordering
# ---------------------------------------------------------------------------


def test_specificity_ordering_in_applicable_records():
    state: dict = {}
    # Insert deliberately out of specificity order.
    _append(
        state,
        scope=Scope.ELEMENT,
        scope_ref="el-1",
        subject=Subject.TONE,
        content="element",
    )
    _append(state, scope=Scope.GLOBAL, subject=Subject.TONE, content="global")
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.TONE,
        content="scene",
    )
    _append(
        state,
        scope=Scope.STAGE,
        scope_ref="audio",
        subject=Subject.TONE,
        content="stage",
    )
    _append(
        state,
        scope=Scope.ARTIFACT_TYPE,
        scope_ref="narration",
        subject=Subject.TONE,
        content="artifact",
    )
    _append(
        state,
        scope=Scope.VOICE_BLOCK,
        scope_ref="vb-1",
        subject=Subject.TONE,
        content="voice_block",
    )

    brief = assemble_virtual_brief(
        state,
        stage="audio",
        scope=Scope.ELEMENT,
        scope_ref="el-1",
    )
    # Not all deeper records have matching refs -- check the ones that do
    # are ordered correctly by specificity in the sorted list.
    scopes = [r.scope for r in brief.applicable_records]
    # Global and stage always apply. The scene/voice_block/artifact_type
    # records are scoped to specific refs different from "el-1", so they
    # don't apply. Element record matches.
    assert scopes == [Scope.GLOBAL, Scope.STAGE, Scope.ELEMENT]


def test_specificity_order_with_unrefed_broader_records():
    state: dict = {}
    _append(state, scope=Scope.GLOBAL, content="g")
    _append(state, scope=Scope.STAGE, scope_ref=None, content="s")
    _append(state, scope=Scope.SCENE, scope_ref=None, content="sc")
    _append(state, scope=Scope.VOICE_BLOCK, scope_ref=None, content="vb")
    _append(state, scope=Scope.ARTIFACT_TYPE, scope_ref=None, content="at")
    _append(
        state,
        scope=Scope.ELEMENT,
        scope_ref="el-1",
        content="el",
    )

    brief = assemble_virtual_brief(
        state, stage="audio", scope=Scope.ELEMENT, scope_ref="el-1"
    )
    scopes = [r.scope for r in brief.applicable_records]
    assert scopes == [
        Scope.GLOBAL,
        Scope.STAGE,
        Scope.SCENE,
        Scope.VOICE_BLOCK,
        Scope.ARTIFACT_TYPE,
        Scope.ELEMENT,
    ]


# ---------------------------------------------------------------------------
# Recency tie-breaking
# ---------------------------------------------------------------------------


def test_recency_tiebreak_within_same_specificity():
    state: dict = {}
    r1 = _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="first",
        polarity=Polarity.PREFER,
    )
    r2 = _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="second",
        polarity=Polarity.PREFER,
    )
    assert r2.revision > r1.revision

    brief = assemble_virtual_brief(state)
    # Sorted order: older first.
    assert [r.content for r in brief.applicable_records] == ["first", "second"]
    # Decision: newer wins.
    decision = brief.decisions[Subject.TONE]
    assert decision.content == "second"
    assert decision.record.revision == r2.revision


def test_more_specific_beats_older_less_specific():
    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="global-warm",
        polarity=Polarity.PREFER,
    )
    scene_rec = _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.TONE,
        content="scene-cold",
        polarity=Polarity.PREFER,
    )
    brief = assemble_virtual_brief(
        state, scope=Scope.SCENE, scope_ref="scene-1"
    )
    assert brief.decisions[Subject.TONE].content == "scene-cold"
    assert brief.decisions[Subject.TONE].record.revision == scene_rec.revision


# ---------------------------------------------------------------------------
# Hard-polarity dominance
# ---------------------------------------------------------------------------


def test_hard_require_dominates_soft_prefer_even_when_less_specific():
    state: dict = {}
    # Global REQUIRE should beat a newer, more specific PREFER.
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="formal",
        polarity=Polarity.REQUIRE,
    )
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.TONE,
        content="casual",
        polarity=Polarity.PREFER,
    )

    brief = assemble_virtual_brief(
        state, scope=Scope.SCENE, scope_ref="scene-1"
    )
    assert brief.decisions[Subject.TONE].polarity is Polarity.REQUIRE
    assert brief.decisions[Subject.TONE].content == "formal"


def test_hard_forbid_dominates_soft_avoid():
    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.MUSIC,
        content="no synth pads",
        polarity=Polarity.FORBID,
    )
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.MUSIC,
        content="dislike dubstep",
        polarity=Polarity.AVOID,
    )

    brief = assemble_virtual_brief(state)
    assert brief.decisions[Subject.MUSIC].polarity is Polarity.FORBID


def test_soft_cannot_displace_hard_even_when_newer_and_more_specific():
    state: dict = {}
    # Older hard REQUIRE.
    _append(
        state,
        scope=Scope.STAGE,
        scope_ref="audio",
        subject=Subject.VOICE,
        content="mezzo",
        polarity=Polarity.REQUIRE,
    )
    # Newer narrower soft PREFER -- must NOT win.
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.VOICE,
        content="alto",
        polarity=Polarity.PREFER,
    )
    brief = assemble_virtual_brief(
        state, stage="audio", scope=Scope.SCENE, scope_ref="scene-1"
    )
    assert brief.decisions[Subject.VOICE].polarity is Polarity.REQUIRE
    assert brief.decisions[Subject.VOICE].content == "mezzo"


def test_hard_vs_hard_same_polarity_later_wins():
    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.REQUIRE,
    )
    winner = _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warmer",
        polarity=Polarity.REQUIRE,
    )

    brief = assemble_virtual_brief(state)
    assert brief.hard_conflicts == ()
    assert brief.decisions[Subject.TONE].record.revision == winner.revision


# ---------------------------------------------------------------------------
# Hard conflict detection (the re-escalation signal)
# ---------------------------------------------------------------------------


def test_require_vs_forbid_same_subject_scope_is_hard_conflict():
    state: dict = {}
    rec_req = _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.MUSIC,
        content="orchestral",
        polarity=Polarity.REQUIRE,
    )
    rec_forb = _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.MUSIC,
        content="orchestral",
        polarity=Polarity.FORBID,
    )

    brief = assemble_virtual_brief(state)
    assert brief.has_hard_conflict is True
    assert len(brief.hard_conflicts) == 1
    conflict = brief.hard_conflicts[0]
    assert isinstance(conflict, HardConflict)
    assert conflict.subject is Subject.MUSIC
    assert conflict.scope is Scope.GLOBAL
    assert conflict.scope_ref is None
    # Both records are present in the conflict payload.
    revs = {r.revision for r in conflict.records}
    assert revs == {rec_req.revision, rec_forb.revision}
    # Decisions map excludes the conflicted subject.
    assert Subject.MUSIC not in brief.decisions


def test_hard_conflict_only_within_same_scope_and_scope_ref():
    state: dict = {}
    # Scene-1 REQUIRE vs scene-2 FORBID -- different scope_refs, NOT a conflict.
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.MUSIC,
        content="orchestral",
        polarity=Polarity.REQUIRE,
    )
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-2",
        subject=Subject.MUSIC,
        content="orchestral",
        polarity=Polarity.FORBID,
    )

    brief = assemble_virtual_brief(
        state, scope=Scope.SCENE, scope_ref="scene-1"
    )
    assert brief.hard_conflicts == ()
    assert brief.decisions[Subject.MUSIC].polarity is Polarity.REQUIRE


def test_soft_vs_soft_opposites_are_not_hard_conflicts():
    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.PREFER,
    )
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.AVOID,
    )
    brief = assemble_virtual_brief(state)
    assert brief.hard_conflicts == ()
    # One of them wins by recency.
    assert Subject.TONE in brief.decisions


def test_hard_conflict_at_narrower_scope_does_not_mask_broader_decision():
    """A hard conflict on subject X at scene-1 should not erase the global
    decision for subject X at other scenes -- but per-request we only
    surface what applies to THIS scope."""
    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.REQUIRE,
    )
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.REQUIRE,
    )
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.FORBID,
    )

    # scene-1 request: conflict surfaced, no decision for TONE.
    brief_conflict = assemble_virtual_brief(
        state, scope=Scope.SCENE, scope_ref="scene-1"
    )
    assert brief_conflict.has_hard_conflict is True
    assert Subject.TONE not in brief_conflict.decisions

    # scene-2 request: the scene-1 records don't apply; global REQUIRE wins.
    brief_clean = assemble_virtual_brief(
        state, scope=Scope.SCENE, scope_ref="scene-2"
    )
    assert brief_clean.hard_conflicts == ()
    assert brief_clean.decisions[Subject.TONE].polarity is Polarity.REQUIRE


# ---------------------------------------------------------------------------
# Subject filter
# ---------------------------------------------------------------------------


def test_subject_filter_narrows_records_and_decisions():
    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.PREFER,
    )
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.MUSIC,
        content="orchestral",
        polarity=Polarity.PREFER,
    )

    brief = assemble_virtual_brief(state, subject=Subject.TONE)
    assert len(brief.applicable_records) == 1
    assert brief.applicable_records[0].subject is Subject.TONE
    assert set(brief.decisions) == {Subject.TONE}


def test_subject_filter_accepts_string_form():
    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warm",
    )
    brief = assemble_virtual_brief(state, subject="tone")
    assert brief.subject is Subject.TONE
    assert len(brief.applicable_records) == 1


# ---------------------------------------------------------------------------
# Input validation -- fail loud
# ---------------------------------------------------------------------------


def test_unknown_scope_string_raises():
    with pytest.raises(ValueError, match="unknown scope"):
        assemble_virtual_brief({}, scope="not-a-scope")


def test_unknown_subject_string_raises():
    with pytest.raises(ValueError, match="unknown subject"):
        assemble_virtual_brief({}, subject="not-a-subject")


def test_scope_ref_without_scope_raises():
    with pytest.raises(ValueError, match="scope_ref requires a scope"):
        assemble_virtual_brief({}, scope_ref="scene-1")


def test_scope_ref_on_global_scope_raises():
    with pytest.raises(ValueError, match="scope_ref must be None for Scope.GLOBAL"):
        assemble_virtual_brief({}, scope=Scope.GLOBAL, scope_ref="nope")


def test_empty_stage_string_raises():
    with pytest.raises(ValueError, match="stage must be"):
        assemble_virtual_brief({}, stage="")


def test_empty_scope_ref_string_raises():
    with pytest.raises(ValueError, match="scope_ref must be a non-empty string"):
        assemble_virtual_brief({}, scope=Scope.SCENE, scope_ref="")


def test_malformed_ledger_bubbles_up():
    state = {PREFERENCE_LEDGER_KEY: "not valid json"}
    with pytest.raises(ValueError):
        assemble_virtual_brief(state)


def test_scope_accepts_string_form():
    state: dict = {}
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.TONE,
        content="warm",
    )
    brief = assemble_virtual_brief(state, scope="scene", scope_ref="scene-1")
    assert len(brief.applicable_records) == 1


# ---------------------------------------------------------------------------
# Serialisation + tool wrapper
# ---------------------------------------------------------------------------


def test_brief_to_dict_roundtrips_via_json():
    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.REQUIRE,
    )
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.FORBID,
    )
    brief = assemble_virtual_brief(state)
    payload = _brief_to_dict(brief)
    encoded = json.dumps(payload)  # must be JSON-serialisable.
    decoded = json.loads(encoded)
    assert decoded["hard_conflicts"]
    assert decoded["hard_conflicts"][0]["subject"] == Subject.TONE.value


def test_tool_wrapper_returns_dict_with_expected_keys():
    state: dict = {}
    _append(
        state,
        scope=Scope.STAGE,
        scope_ref="audio",
        subject=Subject.PACING,
        content="brisk",
        polarity=Polarity.PREFER,
    )
    result = assemble_virtual_brief_tool(state, stage="audio")
    assert isinstance(result, dict)
    assert set(result) >= {
        "stage",
        "scope",
        "scope_ref",
        "subject",
        "applicable_records",
        "decisions",
        "hard_conflicts",
    }
    assert result["stage"] == "audio"
    assert result["decisions"][Subject.PACING.value]["content"] == "brisk"


def test_tool_wrapper_fails_loud_on_bad_scope():
    with pytest.raises(ValueError):
        assemble_virtual_brief_tool({}, scope="not-a-scope")


# ---------------------------------------------------------------------------
# ADK Agent wrapper
# ---------------------------------------------------------------------------


def test_build_virtual_brief_agent_returns_something_usable():
    agent = build_virtual_brief_agent()
    # Always have a name, tools list, after_agent_callback, and output_key.
    assert getattr(agent, "name", None) == "virtual_brief_agent"
    assert getattr(agent, "output_key", None) == VIRTUAL_BRIEF_OUTPUT_KEY
    tools = getattr(agent, "tools", None)
    assert tools is not None and len(tools) >= 1
    assert assemble_virtual_brief_tool in tools
    assert getattr(agent, "after_agent_callback", None) is not None


def test_after_agent_callback_raises_on_hard_conflict_state():
    class _Ctx:
        def __init__(self, state):
            self.state = state

    # Build a state that contains a hard-conflict brief under the
    # blackboard key. Use the tool wrapper (it serialises the brief to
    # a dict for us).
    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.MUSIC,
        content="orchestral",
        polarity=Polarity.REQUIRE,
    )
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.MUSIC,
        content="orchestral",
        polarity=Polarity.FORBID,
    )
    brief_dict = assemble_virtual_brief_tool(state)
    ctx_state = {VIRTUAL_BRIEF_OUTPUT_KEY: brief_dict}

    with pytest.raises(RuntimeError, match="HardConflict"):
        virtual_brief_after_agent_callback(_Ctx(ctx_state))


def test_after_agent_callback_noop_on_clean_brief():
    class _Ctx:
        def __init__(self, state):
            self.state = state

    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.PREFER,
    )
    ctx_state = {VIRTUAL_BRIEF_OUTPUT_KEY: assemble_virtual_brief_tool(state)}
    # Must not raise.
    assert virtual_brief_after_agent_callback(_Ctx(ctx_state)) is None


def test_after_agent_callback_noop_when_key_absent():
    class _Ctx:
        def __init__(self, state):
            self.state = state

    assert virtual_brief_after_agent_callback(_Ctx({})) is None


# ---------------------------------------------------------------------------
# VirtualBrief convenience API
# ---------------------------------------------------------------------------


def test_decision_for_accepts_string_and_enum():
    state: dict = {}
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.REQUIRE,
    )
    brief = assemble_virtual_brief(state)
    assert brief.decision_for(Subject.TONE).content == "warm"
    assert brief.decision_for("tone").content == "warm"
    assert brief.decision_for(Subject.MUSIC) is None


def test_effective_decision_exposes_winning_record():
    state: dict = {}
    winner = _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.TONE,
        content="intimate",
        polarity=Polarity.REQUIRE,
    )
    _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warm",
        polarity=Polarity.PREFER,
    )
    brief = assemble_virtual_brief(
        state, scope=Scope.SCENE, scope_ref="scene-1"
    )
    decision = brief.decisions[Subject.TONE]
    assert isinstance(decision, EffectiveDecision)
    assert decision.record.revision == winner.revision
    assert decision.polarity is Polarity.REQUIRE
