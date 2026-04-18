"""
Unit tests for the Preference Interpreter (ARCH-A2, issue #132).

Covers the behavioural contract declared in
``server/agents/preference_interpreter.py``:

1. One directive -> one or more records appended to the ledger.
2. Implicit scope inference ("scene 3", "Cassandra sounds flat").
3. Explicit scope override via ``scope_hint``, including the
   "explicit generalisation" escape hatch.
4. Closed vocabularies enforced (scope / polarity / subject).
5. Fail-loud on malformed directives, closed-vocab misses, GLOBAL+scope_ref.
6. Heuristic fallback when the LLM backend is unavailable (``use_llm=False``).
7. ADK agent wrapper wires into ``before_agent_callback`` and asserts
   stage-boundary invariants via ``after_agent_callback``.

None of these tests touch a real LLM provider -- the LLM client factory is
swapped out via :func:`set_llm_client_factory`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

# Make server/ imports work when running ``pytest`` from the repo root.
_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from agents import preference_interpreter as pi  # noqa: E402
from agents.preference_interpreter import (  # noqa: E402
    PREFERENCE_INTERPRETER_INPUT_KEY,
    InterpreterError,
    interpret_directive,
    set_llm_client_factory,
)
from callbacks.preference_ledger import (  # noqa: E402
    PREFERENCE_LEDGER_KEY,
    Polarity,
    PreferenceRecord,
    Scope,
    Subject,
    current_revision,
    list_preferences,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_llm(response: dict[str, Any] | str) -> Callable[[], pi.LLMCallable]:
    """Return a zero-arg factory that yields an LLM callable returning ``response``.

    ``response`` may be either a dict (serialised via ``json.dumps``) or a
    raw string (useful for malformed-response tests).
    """
    payload = response if isinstance(response, str) else json.dumps(response)

    def factory() -> pi.LLMCallable:
        def call(model: str, system: str, prompt: str) -> str:
            return payload

        return call

    return factory


@pytest.fixture(autouse=True)
def _reset_llm_factory():
    """Ensure no test leaks an LLM factory into the next."""
    yield
    set_llm_client_factory(None)


def _kwargs(**overrides: Any) -> dict[str, Any]:
    defaults = {
        "reviewer": "alice",
        "l4_event_id": "L4-001",
        "timestamp": "2026-04-18T12:00:00Z",
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# 1. Happy-path LLM parse: single record + multiple records per directive
# ---------------------------------------------------------------------------


def test_single_record_from_llm_is_appended_to_ledger():
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "global",
                        "scope_ref": None,
                        "polarity": "prefer",
                        "subject": "pacing",
                        "content": "tighter pacing overall",
                    }
                ]
            }
        )
    )
    records = interpret_directive(
        "make the pacing tighter", state=state, **_kwargs()
    )
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, PreferenceRecord)
    assert rec.scope is Scope.GLOBAL
    assert rec.polarity is Polarity.PREFER
    assert rec.subject is Subject.PACING
    assert rec.origin.l4_event_id == "L4-001"
    assert current_revision(state) == 1
    assert len(list_preferences(state)) == 1


def test_one_directive_yields_multiple_records():
    """Spec example: 'rewrite scene 3 and I prefer shorter narration'
    should produce one scene-3 record plus one global record."""
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "scene",
                        "scope_ref": "scene-3",
                        "polarity": "require",
                        "subject": "narrative_structure",
                        "content": "rewrite scene 3",
                    },
                    {
                        "scope": "global",
                        "scope_ref": None,
                        "polarity": "prefer",
                        "subject": "duration",
                        "content": "shorter narration",
                    },
                ]
            }
        )
    )
    records = interpret_directive(
        "rewrite scene 3 and I prefer shorter narration",
        state=state,
        **_kwargs(),
    )
    assert len(records) == 2
    assert records[0].scope is Scope.SCENE
    assert records[0].scope_ref == "scene-3"
    assert records[1].scope is Scope.GLOBAL
    assert records[1].scope_ref is None
    # Revisions are monotonic -- the ledger assigned them, not the
    # interpreter.
    assert [r.revision for r in records] == [1, 2]


# ---------------------------------------------------------------------------
# 2. Implicit scope inference via heuristics
# ---------------------------------------------------------------------------


def test_heuristic_infers_scene_scope():
    state: dict = {}
    records = interpret_directive(
        "please rewrite scene 3 to be more dramatic",
        state=state,
        use_llm=False,
        **_kwargs(),
    )
    assert len(records) == 1
    assert records[0].scope is Scope.SCENE
    assert records[0].scope_ref == "scene-3"


def test_heuristic_infers_speaker_scope_from_cassandra_example():
    """'Cassandra sounds flat' should be speaker-scoped, not global.

    This is called out explicitly in the ARCH-A2 acceptance criteria.
    """
    state: dict = {}
    records = interpret_directive(
        "Cassandra sounds flat",
        state=state,
        use_llm=False,
        **_kwargs(),
    )
    assert len(records) == 1
    rec = records[0]
    assert rec.scope is Scope.VOICE_BLOCK
    assert rec.scope_ref == "Cassandra"
    assert rec.polarity is Polarity.AVOID  # "sounds flat" -> avoid
    assert rec.subject is Subject.SPEAKER_ROLE


def test_heuristic_default_scope_is_global():
    state: dict = {}
    records = interpret_directive(
        "prefer a warmer tone",
        state=state,
        use_llm=False,
        **_kwargs(),
    )
    assert len(records) == 1
    assert records[0].scope is Scope.GLOBAL
    assert records[0].subject is Subject.TONE


# ---------------------------------------------------------------------------
# 3. Explicit scope override via scope_hint
# ---------------------------------------------------------------------------


def test_scope_hint_applied_to_unscoped_llm_records():
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "global",
                        "scope_ref": None,
                        "polarity": "avoid",
                        "subject": "tone",
                        "content": "less robotic tone",
                    }
                ]
            }
        )
    )
    records = interpret_directive(
        "less robotic tone",
        state=state,
        scope_hint={"scope": "scene", "scope_ref": "scene-7"},
        **_kwargs(),
    )
    assert len(records) == 1
    assert records[0].scope is Scope.SCENE
    assert records[0].scope_ref == "scene-7"
    # The hint-applied metadata is preserved on the record.
    assert records[0].metadata.get("scope_hint_applied") is True


def test_scope_hint_respects_explicit_generalisation():
    """'globally' overrides the dashboard slot hint."""
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "global",
                        "scope_ref": None,
                        "polarity": "prefer",
                        "subject": "duration",
                        "content": "globally prefer shorter narration",
                    }
                ]
            }
        )
    )
    records = interpret_directive(
        "globally prefer shorter narration",
        state=state,
        scope_hint={"scope": "scene", "scope_ref": "scene-3"},
        **_kwargs(),
    )
    assert len(records) == 1
    # "globally" generalises; scope_hint is ignored.
    assert records[0].scope is Scope.GLOBAL
    assert records[0].scope_ref is None


def test_scope_hint_does_not_override_already_scoped_llm_records():
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "scene",
                        "scope_ref": "scene-9",
                        "polarity": "require",
                        "subject": "narrative_structure",
                        "content": "rewrite scene 9",
                    }
                ]
            }
        )
    )
    records = interpret_directive(
        "rewrite scene 9",
        state=state,
        scope_hint={"scope": "scene", "scope_ref": "scene-3"},
        **_kwargs(),
    )
    assert records[0].scope is Scope.SCENE
    # LLM's explicit scene-9 is preserved; the hint's scene-3 does NOT win.
    assert records[0].scope_ref == "scene-9"


def test_heuristic_honours_scope_hint_for_unscoped_directive():
    state: dict = {}
    records = interpret_directive(
        "tighter pacing",
        state=state,
        scope_hint={"scope": "stage", "scope_ref": "audio"},
        use_llm=False,
        **_kwargs(),
    )
    assert records[0].scope is Scope.STAGE
    assert records[0].scope_ref == "audio"


def test_invalid_scope_hint_is_rejected_fail_loud():
    state: dict = {}
    with pytest.raises(InterpreterError):
        interpret_directive(
            "prefer warmer tone",
            state=state,
            scope_hint={"scope": "not_a_real_scope"},
            use_llm=False,
            **_kwargs(),
        )


# ---------------------------------------------------------------------------
# 4. Origin plumbing -- every record is traceable back to the L4 event
# ---------------------------------------------------------------------------


def test_every_record_carries_l4_event_id_and_reviewer():
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "scene",
                        "scope_ref": "scene-1",
                        "polarity": "require",
                        "subject": "narrative_structure",
                        "content": "tighten scene 1",
                    },
                    {
                        "scope": "global",
                        "scope_ref": None,
                        "polarity": "prefer",
                        "subject": "pacing",
                        "content": "snappier pacing",
                    },
                ]
            }
        )
    )
    records = interpret_directive(
        "tighten scene 1 and snappier pacing overall",
        state=state,
        reviewer="bob",
        l4_event_id="L4-42",
        timestamp="2026-04-18T13:00:00Z",
    )
    assert len(records) == 2
    for rec in records:
        assert rec.origin.l4_event_id == "L4-42"
        assert rec.origin.reviewer == "bob"
        assert rec.origin.timestamp == "2026-04-18T13:00:00Z"


# ---------------------------------------------------------------------------
# 5. Closed vocabulary enforcement -- fail loud on unknown tokens
# ---------------------------------------------------------------------------


def test_llm_emitting_unknown_scope_is_fail_loud():
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "universe",  # not in Scope
                        "scope_ref": None,
                        "polarity": "prefer",
                        "subject": "tone",
                        "content": "cosmic vibe",
                    }
                ]
            }
        )
    )
    with pytest.raises(InterpreterError, match="closed-vocab miss"):
        interpret_directive(
            "cosmic vibe",
            state=state,
            **_kwargs(),
        )


def test_llm_emitting_unknown_polarity_is_fail_loud():
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "global",
                        "scope_ref": None,
                        "polarity": "maybe",  # not in Polarity
                        "subject": "tone",
                        "content": "meh",
                    }
                ]
            }
        )
    )
    with pytest.raises(InterpreterError, match="closed-vocab miss"):
        interpret_directive("meh", state=state, **_kwargs())


def test_llm_emitting_unknown_subject_is_fail_loud():
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "global",
                        "scope_ref": None,
                        "polarity": "prefer",
                        "subject": "color_palette",  # not in Subject
                        "content": "warmer palette",
                    }
                ]
            }
        )
    )
    with pytest.raises(InterpreterError, match="closed-vocab miss"):
        interpret_directive("warmer palette", state=state, **_kwargs())


def test_global_scope_with_scope_ref_is_fail_loud():
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "global",
                        "scope_ref": "scene-3",  # illegal for GLOBAL
                        "polarity": "prefer",
                        "subject": "tone",
                        "content": "warmer tone",
                    }
                ]
            }
        )
    )
    with pytest.raises(InterpreterError):
        interpret_directive("warmer tone", state=state, **_kwargs())


# ---------------------------------------------------------------------------
# 6. Malformed directive / LLM parse failures
# ---------------------------------------------------------------------------


def test_empty_directive_rejected():
    state: dict = {}
    with pytest.raises(ValueError):
        interpret_directive("   ", state=state, **_kwargs())


def test_missing_reviewer_rejected():
    state: dict = {}
    with pytest.raises(ValueError):
        interpret_directive(
            "warmer tone",
            state=state,
            reviewer="",
            l4_event_id="L4-1",
        )


def test_missing_l4_event_id_rejected():
    state: dict = {}
    with pytest.raises(ValueError):
        interpret_directive(
            "warmer tone",
            state=state,
            reviewer="alice",
            l4_event_id="",
        )


def test_llm_invalid_json_falls_back_to_heuristics():
    """Malformed LLM JSON should not blow up the directive -- heuristics
    take over.  The pipeline only fails loud when BOTH paths fail."""
    state: dict = {}
    set_llm_client_factory(_stub_llm("not-json-at-all"))
    records = interpret_directive(
        "rewrite scene 5",
        state=state,
        **_kwargs(),
    )
    assert len(records) == 1
    assert records[0].scope is Scope.SCENE
    assert records[0].scope_ref == "scene-5"
    assert records[0].metadata.get("parser") == "heuristic"


def test_llm_missing_records_key_falls_back_to_heuristics():
    state: dict = {}
    set_llm_client_factory(_stub_llm({"not_records": []}))
    records = interpret_directive(
        "rewrite scene 2",
        state=state,
        **_kwargs(),
    )
    assert len(records) == 1
    assert records[0].scope is Scope.SCENE
    assert records[0].scope_ref == "scene-2"


# ---------------------------------------------------------------------------
# 7. Ledger integration -- append-only, monotonic revisions, state blob shape
# ---------------------------------------------------------------------------


def test_ledger_is_serialised_as_json_string_after_append():
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "global",
                        "scope_ref": None,
                        "polarity": "prefer",
                        "subject": "tone",
                        "content": "warmer tone",
                    }
                ]
            }
        )
    )
    interpret_directive("warmer tone", state=state, **_kwargs())
    raw = state[PREFERENCE_LEDGER_KEY]
    assert isinstance(raw, str)
    decoded = json.loads(raw)
    assert isinstance(decoded, list)
    assert len(decoded) == 1
    assert decoded[0]["revision"] == 1


def test_two_directives_append_monotonically():
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "global",
                        "scope_ref": None,
                        "polarity": "prefer",
                        "subject": "tone",
                        "content": "warmer tone",
                    }
                ]
            }
        )
    )
    interpret_directive(
        "warmer tone", state=state, reviewer="alice", l4_event_id="L4-1"
    )
    interpret_directive(
        "warmer tone again",
        state=state,
        reviewer="alice",
        l4_event_id="L4-2",
    )
    revs = [r.revision for r in list_preferences(state)]
    assert revs == [1, 2]


# ---------------------------------------------------------------------------
# 8. ADK Agent wrapper -- before/after callbacks
# ---------------------------------------------------------------------------


class _FakeCallbackContext:
    def __init__(self, state: dict):
        self.state = state


def test_before_agent_callback_runs_interpreter_from_staged_directive():
    state: dict = {
        PREFERENCE_INTERPRETER_INPUT_KEY: {
            "text": "rewrite scene 3",
            "reviewer": "alice",
            "l4_event_id": "L4-17",
            "timestamp": "2026-04-18T12:00:00Z",
        }
    }
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "scene",
                        "scope_ref": "scene-3",
                        "polarity": "require",
                        "subject": "narrative_structure",
                        "content": "rewrite scene 3",
                    }
                ]
            }
        )
    )

    ctx = _FakeCallbackContext(state)
    content = pi._interpreter_before_agent_callback(ctx)

    # Ledger actually got the record.
    records = list_preferences(state)
    assert len(records) == 1
    assert records[0].scope is Scope.SCENE
    assert records[0].scope_ref == "scene-3"

    # The callback returned a short summary via Content so the LLM is skipped.
    summary_text = content.parts[0].text
    assert "PREFERENCE_INTERPRETER" in summary_text
    assert "L4-17" in summary_text
    assert "scene:scene-3" in summary_text


def test_before_agent_callback_accepts_json_string_blob():
    state: dict = {
        PREFERENCE_INTERPRETER_INPUT_KEY: json.dumps(
            {
                "text": "warmer tone",
                "reviewer": "alice",
                "l4_event_id": "L4-22",
            }
        )
    }
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "global",
                        "scope_ref": None,
                        "polarity": "prefer",
                        "subject": "tone",
                        "content": "warmer tone",
                    }
                ]
            }
        )
    )
    ctx = _FakeCallbackContext(state)
    pi._interpreter_before_agent_callback(ctx)
    records = list_preferences(state)
    assert len(records) == 1
    assert records[0].origin.l4_event_id == "L4-22"


def test_before_agent_callback_noop_when_no_directive_staged():
    state: dict = {}
    ctx = _FakeCallbackContext(state)
    content = pi._interpreter_before_agent_callback(ctx)
    assert "noop" in content.parts[0].text
    assert current_revision(state) == 0


def test_before_agent_callback_fails_loud_on_missing_l4_event_id():
    state: dict = {
        PREFERENCE_INTERPRETER_INPUT_KEY: {
            "text": "warmer tone",
            "reviewer": "alice",
            # l4_event_id intentionally omitted
        }
    }
    ctx = _FakeCallbackContext(state)
    with pytest.raises(InterpreterError, match="l4_event_id"):
        pi._interpreter_before_agent_callback(ctx)


def test_after_agent_callback_passes_when_ledger_has_appended_revisions():
    # Simulate a completed before_agent_callback run.
    state: dict = {}
    set_llm_client_factory(
        _stub_llm(
            {
                "records": [
                    {
                        "scope": "global",
                        "scope_ref": None,
                        "polarity": "prefer",
                        "subject": "tone",
                        "content": "warmer tone",
                    }
                ]
            }
        )
    )
    state[PREFERENCE_INTERPRETER_INPUT_KEY] = {
        "text": "warmer tone",
        "reviewer": "alice",
        "l4_event_id": "L4-9",
    }
    ctx = _FakeCallbackContext(state)
    pi._interpreter_before_agent_callback(ctx)

    # Should not raise -- the appended rev is in the ledger.
    assert pi._interpreter_after_agent_callback(ctx) is None


def test_after_agent_callback_fails_loud_on_missing_revision():
    state: dict = {
        "_preference_interpreter_last_revisions": [99],
        PREFERENCE_LEDGER_KEY: json.dumps([]),
    }
    ctx = _FakeCallbackContext(state)
    with pytest.raises(InterpreterError, match="append-only invariant"):
        pi._interpreter_after_agent_callback(ctx)


# ---------------------------------------------------------------------------
# 9. Module contract / surface area
# ---------------------------------------------------------------------------


def test_module_exports_expected_surface():
    exported = set(pi.__all__)
    assert {
        "interpret_directive",
        "InterpreterError",
        "set_llm_client_factory",
        "PREFERENCE_INTERPRETER_INPUT_KEY",
        "PREFERENCE_INTERPRETER_SUMMARY_KEY",
        "preference_interpreter_agent",
    }.issubset(exported)
