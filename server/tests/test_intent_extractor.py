"""Unit tests for the Intent Extractor (INTENT-01, issue #265).

Covers the invariants declared in
:mod:`server.agents.intent_extractor`:

1. The PAG reference brief parses to ``duration_sec == 420.0 ± 1``
   (the acceptance criterion from the issue).
2. The heuristic fallback produces a valid :class:`BriefIntent`
   populating every hard-constraint field.
3. ``run_intent_extractor`` writes the intent into session state
   under :data:`BRIEF_INTENT_KEY` and is idempotent on re-entry.
4. The LLM path is tried first when ``use_llm=True`` and falls back
   to the heuristic on parse failure.
5. The disk backup (for the ``/agui/restated_brief`` endpoint) is
   written whenever the extractor runs and re-readable afterwards.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from agents.intent_extractor import (  # noqa: E402
    BRIEF_INTENT_KEY,
    BriefIntent,
    DEFAULT_DURATION_SEC,
    DEFAULT_TOLERANCE_SEC,
    IntentExtractionError,
    extract_intent,
    get_brief_intent,
    read_intent_backup,
    run_intent_extractor,
    set_llm_client_factory,
)


PAG_BRIEF = (
    "Make a 7-minute ADHD-friendly documentary about the "
    "Periaqueductal Gray (PAG). It must cover opioid chemistry and "
    "fight-flight-freeze. Do not discuss recreational drug use."
)


@pytest.fixture(autouse=True)
def _reset_llm_factory():
    set_llm_client_factory(None)
    yield
    set_llm_client_factory(None)


@pytest.fixture
def tmp_output_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_OUTPUT_DIR", str(tmp_path))
    yield tmp_path


# ---------------------------------------------------------------------------
# PAG brief — the acceptance criterion for INTENT-01
# ---------------------------------------------------------------------------


def test_pag_brief_duration_heuristic():
    intent = extract_intent(PAG_BRIEF, use_llm=False)
    assert abs(intent.duration_sec - 420.0) <= 1.0


def test_pag_brief_audience_heuristic():
    intent = extract_intent(PAG_BRIEF, use_llm=False)
    assert intent.audience == "adhd-friendly"


def test_pag_brief_required_topics_heuristic():
    intent = extract_intent(PAG_BRIEF, use_llm=False)
    topics_lower = {t.lower() for t in intent.required_topics}
    assert "pag" in topics_lower
    assert any("opioid" in t or "chemistry" in t for t in topics_lower)
    assert any("fight" in t or "freeze" in t for t in topics_lower)


def test_pag_brief_forbidden_topics_heuristic():
    intent = extract_intent(PAG_BRIEF, use_llm=False)
    assert intent.forbidden_topics, "expected at least one forbidden topic"


# ---------------------------------------------------------------------------
# Defaults + invariants
# ---------------------------------------------------------------------------


def test_empty_brief_defaults_to_safe_values():
    intent = extract_intent("make a video", use_llm=False)
    assert intent.duration_sec == DEFAULT_DURATION_SEC
    assert intent.tolerance_sec == DEFAULT_TOLERANCE_SEC
    assert intent.audience == "general"
    assert 0.0 <= intent.confidence.get("duration_sec", 0.0) <= 1.0


def test_duration_supports_seconds_and_hours():
    sec = extract_intent("90-second explainer", use_llm=False)
    assert abs(sec.duration_sec - 90.0) <= 0.01
    hour = extract_intent("1 hour retrospective", use_llm=False)
    assert abs(hour.duration_sec - 3600.0) <= 0.01


def test_tolerance_sec_is_finite_and_non_zero():
    intent = extract_intent(PAG_BRIEF, use_llm=False)
    assert intent.tolerance_sec > 0.0


def test_confidence_dict_has_every_core_key():
    intent = extract_intent(PAG_BRIEF, use_llm=False)
    for key in ("duration_sec", "audience", "required_topics"):
        assert key in intent.confidence
        assert 0.0 <= intent.confidence[key] <= 1.0


def test_brief_intent_round_trips_json():
    intent = extract_intent(PAG_BRIEF, use_llm=False)
    payload = intent.to_json()
    again = BriefIntent.from_json(payload)
    assert again.duration_sec == intent.duration_sec
    assert again.required_topics == intent.required_topics


# ---------------------------------------------------------------------------
# ADK-state integration + backup
# ---------------------------------------------------------------------------


def test_run_intent_extractor_writes_state(tmp_output_dir):
    state: dict = {"original_brief": PAG_BRIEF}
    intent = run_intent_extractor(state, use_llm=False)
    assert BRIEF_INTENT_KEY in state
    cached = get_brief_intent(state)
    assert cached is not None
    assert cached.duration_sec == intent.duration_sec


def test_run_intent_extractor_is_idempotent(tmp_output_dir):
    state: dict = {"original_brief": PAG_BRIEF}
    first = run_intent_extractor(state, use_llm=False)
    snapshot = state[BRIEF_INTENT_KEY]
    second = run_intent_extractor(state, use_llm=False)
    assert state[BRIEF_INTENT_KEY] == snapshot
    assert first.duration_sec == second.duration_sec


def test_run_intent_extractor_accepts_explicit_brief(tmp_output_dir):
    state: dict = {}
    intent = run_intent_extractor(
        state, brief_text=PAG_BRIEF, use_llm=False,
    )
    assert abs(intent.duration_sec - 420.0) <= 1.0


def test_run_intent_extractor_falls_back_to_topic(tmp_output_dir):
    state: dict = {"topic": "3-minute piece on migratory birds"}
    intent = run_intent_extractor(state, use_llm=False)
    assert abs(intent.duration_sec - 180.0) <= 1.0


def test_run_intent_extractor_raises_without_source(tmp_output_dir):
    state: dict = {}
    with pytest.raises(IntentExtractionError):
        run_intent_extractor(state, use_llm=False)


def test_run_intent_extractor_writes_disk_backup(tmp_output_dir):
    state: dict = {"original_brief": PAG_BRIEF}
    run_intent_extractor(state, use_llm=False)
    backup_path = (
        Path(tmp_output_dir) / "timelines" / "_brief_intent_backup.json"
    )
    assert backup_path.exists()
    parsed = json.loads(backup_path.read_text())
    assert abs(float(parsed["duration_sec"]) - 420.0) <= 1.0


def test_read_intent_backup_returns_none_when_absent(tmp_output_dir):
    assert read_intent_backup() is None


def test_read_intent_backup_roundtrips(tmp_output_dir):
    state: dict = {"original_brief": PAG_BRIEF}
    run_intent_extractor(state, use_llm=False)
    loaded = read_intent_backup()
    assert loaded is not None
    assert abs(loaded.duration_sec - 420.0) <= 1.0


# ---------------------------------------------------------------------------
# LLM path — use a stub via set_llm_client_factory
# ---------------------------------------------------------------------------


def test_llm_path_used_when_valid(tmp_output_dir):
    stub_payload = {
        "duration_sec": 420.0,
        "tolerance_sec": 30.0,
        "audience": "adhd-friendly",
        "tone": ["cinematic", "curious"],
        "corpus_paths": [],
        "required_topics": ["PAG", "opioid chemistry", "fight-flight-freeze"],
        "forbidden_topics": ["recreational drug use"],
        "format_hints": {"aspect_ratio": "16:9"},
        "confidence": {"duration_sec": 0.98, "audience": 0.95},
    }

    def _stub(model, system, prompt):
        return json.dumps(stub_payload)

    set_llm_client_factory(lambda: _stub)
    intent = extract_intent(PAG_BRIEF, use_llm=True)
    assert intent.duration_sec == 420.0
    assert "PAG" in intent.required_topics
    assert intent.format_hints["aspect_ratio"] == "16:9"


def test_llm_path_falls_back_on_bad_json():
    def _stub(model, system, prompt):
        return "not valid json at all"

    set_llm_client_factory(lambda: _stub)
    intent = extract_intent(PAG_BRIEF, use_llm=True)
    # Should have fallen back to heuristic — PAG brief parses to 420s.
    assert abs(intent.duration_sec - 420.0) <= 1.0


def test_llm_path_falls_back_on_schema_violation():
    def _stub(model, system, prompt):
        return json.dumps({"duration_sec": -10.0})

    set_llm_client_factory(lambda: _stub)
    intent = extract_intent(PAG_BRIEF, use_llm=True)
    assert intent.duration_sec > 0.0
