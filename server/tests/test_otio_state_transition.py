"""
Tests for ARCH-E1: Draft → Authoritative OTIO state machine (issue #147).

The OTIO timeline carries a formal ``state`` field that starts as
``draft`` when the timeline is created and crystallises to
``authoritative`` at the end of the audio stage once narration
reconciliation locks pacing.  Once authoritative, downstream stages
bind to the timeline but MAY NOT mutate its authoritative baseline — any
attempt raises :class:`OtioStateViolation` unless an explicit
REPLACE/EXTEND escalation is open.

These tests cover:

- State defaults to ``draft`` and the timeline-file mirror is stamped.
- The after-agent transition callback crystallises state only at end of
  audio AND only when the Timeline Guardian has already passed.
- The transition is idempotent and a no-op for non-audio phases.
- The mutation guard fails loud with a structured :class:`OtioStateViolation`
  on post-crystallisation narration mutations.
- REPLACE / EXTEND escalation windows legitimately re-open the guard.
- ``reset_to_draft`` returns to ``draft`` and allows re-derivation.
- The on-disk timeline file carries the state through process restarts.
- End-to-end wiring: the chained after_agent_callback on ``audio_agent``
  crystallises state only after Timeline Guardian passes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make server/ imports work when running `pytest` from the repo root.
_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from callbacks.otio_state import (  # noqa: E402
    ESCALATION_KEY,
    HISTORY_KEY,
    OTIO_STATE_AUTHORITATIVE,
    OTIO_STATE_DRAFT,
    STATE_KEY,
    OtioStateViolation,
    authoritative_transition_callback,
    begin_escalation,
    end_escalation,
    get_otio_state,
    guard_authoritative_mutation,
    reset_to_draft,
    set_otio_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_callback_context(state: dict) -> MagicMock:
    """Build a minimal CallbackContext stand-in with a mutable state dict."""
    ctx = MagicMock()
    ctx.state = state
    return ctx


@pytest.fixture
def tmp_timeline(tmp_path, monkeypatch):
    """Create a real OTIO file via ``create_timeline`` and yield (state, path).

    Uses a temp ``TIMELINE_DIR`` so the test never touches the real
    ``/tmp/documentary-pipeline/timelines`` directory.
    """
    monkeypatch.setenv("TIMELINE_DIR", str(tmp_path))
    # Ensure otio_tools picks up the new TIMELINE_DIR.
    import importlib

    import tools.otio_tools as otio_tools_mod

    importlib.reload(otio_tools_mod)

    tool_context = MagicMock()
    tool_context.state = {}
    otio_tools_mod.create_timeline(
        topic="state_transition_test",
        num_scenes=2,
        tool_context=tool_context,
    )
    path = tool_context.state["_timeline_path"]
    assert os.path.exists(path)
    return tool_context.state, path


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_default_state_is_draft_when_unset():
    assert get_otio_state({}) == OTIO_STATE_DRAFT
    assert get_otio_state(None) == OTIO_STATE_DRAFT


def test_unknown_state_falls_back_to_draft():
    assert get_otio_state({STATE_KEY: "bogus"}) == OTIO_STATE_DRAFT


def test_create_timeline_marks_draft_on_blackboard_and_on_disk(tmp_timeline):
    state, path = tmp_timeline
    assert get_otio_state(state) == OTIO_STATE_DRAFT

    # On-disk mirror
    import opentimelineio as otio

    timeline = otio.adapters.read_from_file(path)
    assert timeline.metadata["documentary"]["state"] == "draft"
    assert (
        timeline.metadata["documentary"]["state_reason"] == "timeline_created"
    )


def test_mark_timeline_draft_records_history(tmp_timeline):
    state, _path = tmp_timeline
    history = state.get(HISTORY_KEY, [])
    assert history, "history should be recorded on mark_timeline_draft"
    assert history[-1]["to"] == OTIO_STATE_DRAFT


# ---------------------------------------------------------------------------
# Setter validation
# ---------------------------------------------------------------------------


def test_set_otio_state_rejects_invalid_state():
    with pytest.raises(ValueError):
        set_otio_state({}, "frozen")


def test_set_otio_state_requires_blackboard():
    with pytest.raises(ValueError):
        set_otio_state(None, OTIO_STATE_AUTHORITATIVE)


# ---------------------------------------------------------------------------
# Transition callback
# ---------------------------------------------------------------------------


def test_transition_callback_crystallises_at_end_of_audio(tmp_timeline):
    state, path = tmp_timeline
    state["pipeline_phase"] = "audio"
    state["otio_violation"] = None  # guardian cleared

    ctx = _make_callback_context(state)
    authoritative_transition_callback(ctx)

    assert get_otio_state(state) == OTIO_STATE_AUTHORITATIVE

    # On-disk mirror persists across process restarts
    import opentimelineio as otio

    timeline = otio.adapters.read_from_file(path)
    assert (
        timeline.metadata["documentary"]["state"] == OTIO_STATE_AUTHORITATIVE
    )
    assert (
        timeline.metadata["documentary"]["state_reason"]
        == "end_of_audio_reconciliation"
    )


def test_transition_callback_is_idempotent(tmp_timeline):
    state, _path = tmp_timeline
    state["pipeline_phase"] = "audio"
    state["otio_violation"] = None

    ctx = _make_callback_context(state)
    authoritative_transition_callback(ctx)
    first_history_len = len(state[HISTORY_KEY])
    authoritative_transition_callback(ctx)  # second call
    # State is still authoritative; no extra history entry was appended
    assert get_otio_state(state) == OTIO_STATE_AUTHORITATIVE
    assert len(state[HISTORY_KEY]) == first_history_len


def test_transition_callback_noop_for_non_audio_phase(tmp_timeline):
    state, _path = tmp_timeline
    state["pipeline_phase"] = "visual_direction"
    state["otio_violation"] = None

    ctx = _make_callback_context(state)
    authoritative_transition_callback(ctx)
    assert get_otio_state(state) == OTIO_STATE_DRAFT


def test_transition_callback_blocks_when_guardian_flagged_violation(tmp_timeline):
    state, _path = tmp_timeline
    state["pipeline_phase"] = "audio"
    state["otio_violation"] = "OTIO VIOLATION [audio]: WAV file missing"

    ctx = _make_callback_context(state)
    authoritative_transition_callback(ctx)
    # State must NOT crystallise while a violation is outstanding
    assert get_otio_state(state) == OTIO_STATE_DRAFT


# ---------------------------------------------------------------------------
# Mutation guard
# ---------------------------------------------------------------------------


def test_guard_is_noop_in_draft_state():
    state = {STATE_KEY: OTIO_STATE_DRAFT}
    # Must NOT raise
    guard_authoritative_mutation(state, operation="add_narration_clip")


def test_guard_raises_on_mutation_after_crystallisation():
    state = {
        STATE_KEY: OTIO_STATE_AUTHORITATIVE,
        "_timeline_path": "/tmp/fake.otio",
        "pipeline_phase": "visual_direction",
    }
    with pytest.raises(OtioStateViolation) as excinfo:
        guard_authoritative_mutation(state, operation="add_narration_clip")

    err = excinfo.value
    assert err.details["operation"] == "add_narration_clip"
    assert err.details["otio_state"] == OTIO_STATE_AUTHORITATIVE
    assert err.details["timeline_path"] == "/tmp/fake.otio"
    assert err.details["escalation"] is None
    assert err.details["pipeline_phase"] == "visual_direction"
    # Blackboard tagged for dashboards / guardian surface
    assert "OTIO STATE VIOLATION" in state["otio_violation"]


def test_guard_structured_failure_includes_operation_in_message():
    state = {STATE_KEY: OTIO_STATE_AUTHORITATIVE}
    with pytest.raises(OtioStateViolation) as excinfo:
        guard_authoritative_mutation(state, operation="clear_narration_track")
    assert "clear_narration_track" in str(excinfo.value)
    assert "AUTHORITATIVE" in str(excinfo.value)


def test_guard_noop_when_state_is_none_blackboard():
    # Defensive: very early pipeline setup may pass None; guard must not raise.
    guard_authoritative_mutation(None, operation="add_narration_clip")


# ---------------------------------------------------------------------------
# Escalation window
# ---------------------------------------------------------------------------


def test_replace_escalation_permits_mutation():
    state = {STATE_KEY: OTIO_STATE_AUTHORITATIVE}
    begin_escalation(
        state,
        escalation_type="REPLACE",
        reason="director requested new scene",
        opened_by="test",
    )
    # Must NOT raise
    guard_authoritative_mutation(state, operation="add_narration_clip")


def test_extend_escalation_permits_mutation():
    state = {STATE_KEY: OTIO_STATE_AUTHORITATIVE}
    begin_escalation(
        state,
        escalation_type="EXTEND",
        reason="add extra scene",
        opened_by="test",
    )
    guard_authoritative_mutation(state, operation="add_narration_clip")


def test_end_escalation_restores_guard():
    state = {STATE_KEY: OTIO_STATE_AUTHORITATIVE}
    begin_escalation(
        state, escalation_type="REPLACE", reason="r", opened_by="t"
    )
    end_escalation(state)
    with pytest.raises(OtioStateViolation):
        guard_authoritative_mutation(state, operation="add_narration_clip")


def test_begin_escalation_rejects_invalid_type():
    state = {STATE_KEY: OTIO_STATE_AUTHORITATIVE}
    with pytest.raises(ValueError):
        begin_escalation(state, escalation_type="TWEAK", reason="r", opened_by="t")


def test_begin_escalation_requires_reason_and_opened_by():
    state = {STATE_KEY: OTIO_STATE_AUTHORITATIVE}
    with pytest.raises(ValueError):
        begin_escalation(state, escalation_type="REPLACE", reason="", opened_by="t")
    with pytest.raises(ValueError):
        begin_escalation(state, escalation_type="REPLACE", reason="r", opened_by="")


def test_allow_escalation_false_ignores_open_window():
    state = {STATE_KEY: OTIO_STATE_AUTHORITATIVE}
    begin_escalation(
        state, escalation_type="REPLACE", reason="r", opened_by="t"
    )
    with pytest.raises(OtioStateViolation):
        guard_authoritative_mutation(
            state,
            operation="add_narration_clip",
            allow_escalation=False,
        )


# ---------------------------------------------------------------------------
# reset_to_draft — supports full re-derivation
# ---------------------------------------------------------------------------


def test_reset_to_draft_reopens_mutations(tmp_timeline):
    state, path = tmp_timeline
    state["pipeline_phase"] = "audio"
    state["otio_violation"] = None
    authoritative_transition_callback(_make_callback_context(state))
    assert get_otio_state(state) == OTIO_STATE_AUTHORITATIVE

    reset_to_draft(state, reason="director rewrote scene 3", timeline_path=path)
    assert get_otio_state(state) == OTIO_STATE_DRAFT

    # Mutation guard is silent again — post-reset mutations are allowed.
    guard_authoritative_mutation(state, operation="add_narration_clip")

    # On-disk mirror follows
    import opentimelineio as otio

    timeline = otio.adapters.read_from_file(path)
    assert timeline.metadata["documentary"]["state"] == OTIO_STATE_DRAFT


def test_reset_to_draft_requires_reason():
    state = {STATE_KEY: OTIO_STATE_AUTHORITATIVE}
    with pytest.raises(ValueError):
        reset_to_draft(state, reason="")


# ---------------------------------------------------------------------------
# End-to-end: transition closes an open escalation window
# ---------------------------------------------------------------------------


def test_successful_audio_rerun_closes_escalation(tmp_timeline):
    state, _path = tmp_timeline
    # Simulate first crystallisation
    state["pipeline_phase"] = "audio"
    state["otio_violation"] = None
    authoritative_transition_callback(_make_callback_context(state))
    assert get_otio_state(state) == OTIO_STATE_AUTHORITATIVE

    # Escalation: director requested rework, audio re-derives.
    reset_to_draft(state, reason="scene 3 rework")
    begin_escalation(
        state,
        escalation_type="REPLACE",
        reason="scene 3 rework",
        opened_by="production_supervisor",
    )
    assert ESCALATION_KEY in state

    # Re-run audio → re-crystallise.  The transition callback must close
    # the escalation window as part of crystallisation.
    authoritative_transition_callback(_make_callback_context(state))
    assert get_otio_state(state) == OTIO_STATE_AUTHORITATIVE
    assert ESCALATION_KEY not in state


# ---------------------------------------------------------------------------
# Wiring: otio_tools narration mutators honour the guard
# ---------------------------------------------------------------------------


def test_add_narration_clip_blocked_post_authoritative(tmp_timeline):
    state, path = tmp_timeline
    state["pipeline_phase"] = "audio"
    state["otio_violation"] = None
    authoritative_transition_callback(_make_callback_context(state))

    import tools.otio_tools as otio_tools_mod

    tool_context = MagicMock()
    tool_context.state = state

    with pytest.raises(OtioStateViolation) as excinfo:
        otio_tools_mod.add_narration_clip(
            scene_num=1,
            voice="V1",
            wav_path="/tmp/does_not_matter.wav",
            duration=5.0,
            tool_context=tool_context,
        )
    assert excinfo.value.details["operation"] == "add_narration_clip"
    _ = path  # fixture guarantees file exists


def test_clear_narration_track_blocked_post_authoritative(tmp_timeline):
    state, path = tmp_timeline
    state["pipeline_phase"] = "audio"
    state["otio_violation"] = None
    authoritative_transition_callback(_make_callback_context(state))

    import tools.otio_tools as otio_tools_mod

    tool_context = MagicMock()
    tool_context.state = state

    with pytest.raises(OtioStateViolation):
        otio_tools_mod.clear_narration_track(path, tool_context=tool_context)


def test_add_narration_clip_allowed_under_escalation(tmp_timeline, monkeypatch):
    state, _path = tmp_timeline
    state["pipeline_phase"] = "audio"
    state["otio_violation"] = None
    authoritative_transition_callback(_make_callback_context(state))

    begin_escalation(
        state,
        escalation_type="REPLACE",
        reason="re-record scene 1",
        opened_by="test",
    )

    # Create a dummy WAV so the add succeeds end-to-end
    import tools.otio_tools as otio_tools_mod

    wav_path = str(Path(_path).parent / "dummy.wav")
    Path(wav_path).write_bytes(b"RIFF....WAVEfmt ")

    tool_context = MagicMock()
    tool_context.state = state

    result = json.loads(
        otio_tools_mod.add_narration_clip(
            scene_num=1,
            voice="V1",
            wav_path=wav_path,
            duration=5.0,
            tool_context=tool_context,
        )
    )
    assert result["status"] in {"added", "already_exists"}


# ---------------------------------------------------------------------------
# Wiring: end-to-end audio-agent chained callback
# ---------------------------------------------------------------------------


def test_audio_agent_chained_callback_crystallises_after_guardian(tmp_timeline):
    """Full pipeline wiring: the audio agent's chained after_agent_callback
    runs oracle → guardian → transition, and the timeline is authoritative
    after it completes cleanly."""
    state, _path = tmp_timeline
    state["pipeline_phase"] = "audio"

    from agents import audio_agent as audio_agent_mod

    ctx = _make_callback_context(state)

    # Patch the guardian and oracle to no-op success so we isolate the
    # transition behaviour.  ``state["otio_violation"] = None`` mimics a
    # guardian pass.
    def _fake_guardian(callback_context):
        callback_context.state["otio_violation"] = None
        return None

    with patch.object(
        audio_agent_mod, "whisperx_oracle_callback", return_value=None
    ), patch.object(
        audio_agent_mod, "timeline_guardian_callback", side_effect=_fake_guardian
    ):
        audio_agent_mod._chained_after_agent_callback(ctx)

    assert get_otio_state(state) == OTIO_STATE_AUTHORITATIVE


def test_audio_agent_chained_callback_does_not_crystallise_on_guardian_raise(tmp_timeline):
    """If the guardian raises, the transition never runs (exception
    propagates first), and the timeline stays draft."""
    state, _path = tmp_timeline
    state["pipeline_phase"] = "audio"

    from agents import audio_agent as audio_agent_mod

    ctx = _make_callback_context(state)

    with patch.object(
        audio_agent_mod, "whisperx_oracle_callback", return_value=None
    ), patch.object(
        audio_agent_mod,
        "timeline_guardian_callback",
        side_effect=RuntimeError("OTIO VIOLATION [audio]: WAV missing"),
    ):
        with pytest.raises(RuntimeError):
            audio_agent_mod._chained_after_agent_callback(ctx)

    assert get_otio_state(state) == OTIO_STATE_DRAFT
