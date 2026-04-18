"""
Unit tests for ARCH-A3 run-start Preference Ledger seed (#133).

Covers the invariants declared in
:mod:`server.callbacks.run_start_seed`:

1. Every R0 record is GLOBAL-scoped, has ``scope_ref is None``, and
   carries ``origin.l4_event_id == "R0"`` + ``origin.reviewer ==
   "system"``.
2. The seed covers every canonical baseline subject (tone, voice,
   pacing, visual_style, narrative_structure, speaker_role, duration)
   even when the brief is silent.
3. The heuristic (``use_llm=False``) produces a deterministic,
   non-empty seed.
4. The LLM path is tried first when ``use_llm=True`` and falls back
   to the heuristic on parse failure / backend error (no silent
   degradation of the ledger -- still non-empty).
5. Seeding is idempotent -- re-running on a ledger that already
   contains R0 records returns ``[]``.
6. Raises when neither ``brief_text``, ``state['original_brief']``
   nor ``state['topic']`` is available.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
    current_revision,
    list_preferences,
)
from callbacks.run_start_seed import (  # noqa: E402
    ORIGINAL_BRIEF_KEY,
    R0_EVENT_ID,
    R0_REVIEWER,
    R0_SUMMARY_KEY,
    RunStartSeedError,
    seed_ledger_from_brief,
    set_llm_client_factory,
)


_BASELINE_SUBJECTS = {
    Subject.TONE,
    Subject.VOICE,
    Subject.PACING,
    Subject.VISUAL_STYLE,
    Subject.NARRATIVE_STRUCTURE,
    Subject.SPEAKER_ROLE,
    Subject.DURATION,
}


def _fresh_state() -> dict:
    return {PREFERENCE_LEDGER_KEY: "[]"}


# ---------------------------------------------------------------------------
# Invariants on emitted records
# ---------------------------------------------------------------------------


def test_heuristic_seed_covers_every_baseline_subject():
    state = _fresh_state()
    records = seed_ledger_from_brief(
        state,
        brief_text="Create a documentary about octopus cognition.",
        use_llm=False,
    )
    assert len(records) == len(_BASELINE_SUBJECTS)
    subjects = {r.subject for r in records}
    assert subjects == _BASELINE_SUBJECTS


def test_every_r0_record_is_global_with_system_origin():
    state = _fresh_state()
    records = seed_ledger_from_brief(
        state, brief_text="A playful short doc.", use_llm=False
    )
    for record in records:
        assert record.scope is Scope.GLOBAL
        assert record.scope_ref is None
        assert record.origin.l4_event_id == R0_EVENT_ID
        assert record.origin.reviewer == R0_REVIEWER
        assert record.origin.timestamp  # non-empty


def test_seed_writes_records_into_ledger_in_revision_order():
    state = _fresh_state()
    records = seed_ledger_from_brief(
        state, brief_text="Doc", use_llm=False
    )
    assert current_revision(state) == records[-1].revision
    ledger = list_preferences(state)
    # Seeded records are the only ledger entries.
    assert len(ledger) == len(records)
    for prev, cur in zip(ledger, ledger[1:]):
        assert cur.revision == prev.revision + 1


def test_seed_summary_is_written_to_blackboard():
    state = _fresh_state()
    seed_ledger_from_brief(state, brief_text="Doc", use_llm=False)
    summary = state[R0_SUMMARY_KEY]
    assert summary.startswith("R0_SEED:")
    assert "records=" in summary


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_seed_is_idempotent_on_already_seeded_ledger():
    state = _fresh_state()
    first = seed_ledger_from_brief(state, brief_text="Doc", use_llm=False)
    assert first  # non-empty
    second = seed_ledger_from_brief(state, brief_text="Doc", use_llm=False)
    assert second == []
    # Ledger still only has the first-pass records.
    assert len(list_preferences(state)) == len(first)


def test_seed_noops_when_existing_r0_records_present_even_if_non_seed_records_follow():
    state = _fresh_state()
    seed_ledger_from_brief(state, brief_text="Doc", use_llm=False)
    # Simulate an L4 reviewer appending afterwards.
    append_preference(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        polarity=Polarity.PREFER,
        subject=Subject.TONE,
        content="warmer tone for the opener",
        origin=Origin(
            l4_event_id="L4-001",
            reviewer="alice",
            timestamp="2026-04-18T12:00:00Z",
        ),
    )
    before_len = len(list_preferences(state))
    added = seed_ledger_from_brief(state, brief_text="Doc", use_llm=False)
    assert added == []
    assert len(list_preferences(state)) == before_len


# ---------------------------------------------------------------------------
# Brief resolution
# ---------------------------------------------------------------------------


def test_seed_reads_brief_from_state_when_argument_omitted():
    state = _fresh_state()
    state[ORIGINAL_BRIEF_KEY] = "Doc about bees."
    records = seed_ledger_from_brief(state, use_llm=False)
    assert records


def test_seed_falls_back_to_topic_when_no_brief():
    state = _fresh_state()
    state["topic"] = "Deep-sea geology"
    records = seed_ledger_from_brief(state, use_llm=False)
    assert records


def test_seed_raises_when_no_brief_and_no_topic():
    state = _fresh_state()
    with pytest.raises(RunStartSeedError):
        seed_ledger_from_brief(state, use_llm=False)


# ---------------------------------------------------------------------------
# Heuristic polarity detection
# ---------------------------------------------------------------------------


def test_heuristic_picks_up_require_polarity_for_must_phrasing():
    state = _fresh_state()
    brief = (
        "Documentary about urban foxes. The pacing must be snappy to "
        "keep an ADHD-friendly audience engaged."
    )
    records = seed_ledger_from_brief(state, brief_text=brief, use_llm=False)
    pacing = next(r for r in records if r.subject is Subject.PACING)
    assert pacing.polarity is Polarity.REQUIRE


def test_heuristic_picks_up_forbid_polarity_for_never_phrasing():
    state = _fresh_state()
    brief = "Short doc about libraries. Never use a sarcastic tone."
    records = seed_ledger_from_brief(state, brief_text=brief, use_llm=False)
    tone = next(r for r in records if r.subject is Subject.TONE)
    assert tone.polarity is Polarity.FORBID


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------


def test_llm_path_is_used_when_available_and_respects_baseline():
    state = _fresh_state()

    captured: dict = {}

    def fake_llm(model: str, system: str, prompt: str) -> str:
        captured["prompt"] = prompt
        return (
            '{"records": ['
            '  {"scope":"global","scope_ref":null,"polarity":"require",'
            '   "subject":"tone","content":"warm and hopeful"},'
            '  {"scope":"global","scope_ref":null,"polarity":"prefer",'
            '   "subject":"voice","content":"measured narration"},'
            '  {"scope":"global","scope_ref":null,"polarity":"prefer",'
            '   "subject":"pacing","content":"even"},'
            '  {"scope":"global","scope_ref":null,"polarity":"prefer",'
            '   "subject":"visual_style","content":"observational"},'
            '  {"scope":"global","scope_ref":null,"polarity":"prefer",'
            '   "subject":"narrative_structure","content":"three-act"},'
            '  {"scope":"global","scope_ref":null,"polarity":"prefer",'
            '   "subject":"speaker_role","content":"single narrator"},'
            '  {"scope":"global","scope_ref":null,"polarity":"prefer",'
            '   "subject":"duration","content":"short-form"}'
            "]}"
        )

    set_llm_client_factory(lambda: fake_llm)
    try:
        records = seed_ledger_from_brief(state, brief_text="Brief", use_llm=True)
    finally:
        set_llm_client_factory(None)

    assert captured, "fake LLM was not invoked"
    tone = next(r for r in records if r.subject is Subject.TONE)
    assert tone.content == "warm and hopeful"
    assert tone.polarity is Polarity.REQUIRE


def test_llm_path_falls_back_to_heuristic_on_bad_json():
    state = _fresh_state()

    def bad_llm(model: str, system: str, prompt: str) -> str:
        return "not json at all"

    set_llm_client_factory(lambda: bad_llm)
    try:
        records = seed_ledger_from_brief(
            state, brief_text="Brief about whales.", use_llm=True
        )
    finally:
        set_llm_client_factory(None)

    # Heuristic fallback still produces the full baseline.
    assert {r.subject for r in records} == _BASELINE_SUBJECTS


def test_llm_path_falls_back_to_heuristic_on_exception():
    state = _fresh_state()

    def raising_llm(model: str, system: str, prompt: str) -> str:
        raise RuntimeError("genai backend not reachable")

    set_llm_client_factory(lambda: raising_llm)
    try:
        records = seed_ledger_from_brief(
            state, brief_text="Brief", use_llm=True
        )
    finally:
        set_llm_client_factory(None)

    assert {r.subject for r in records} == _BASELINE_SUBJECTS


def test_llm_partial_cover_is_filled_by_heuristic():
    state = _fresh_state()

    def partial_llm(model: str, system: str, prompt: str) -> str:
        # Only supplies TONE -- other baselines must come from heuristic.
        return (
            '{"records": ['
            '  {"scope":"global","scope_ref":null,"polarity":"prefer",'
            '   "subject":"tone","content":"LLM-tone"}'
            "]}"
        )

    set_llm_client_factory(lambda: partial_llm)
    try:
        records = seed_ledger_from_brief(state, brief_text="Doc", use_llm=True)
    finally:
        set_llm_client_factory(None)

    assert {r.subject for r in records} == _BASELINE_SUBJECTS
    tone = next(r for r in records if r.subject is Subject.TONE)
    assert tone.content == "LLM-tone"
