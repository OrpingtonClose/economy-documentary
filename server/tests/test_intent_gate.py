"""Unit tests for the pre-flight R0 constraint gate (INTENT-02, #266).

Covers the invariants declared in :mod:`server.callbacks.intent_gate`:

1. ``evaluate_gate`` passes when duration is inside ± tolerance and
   every required topic is present; fails otherwise.
2. ``run_preflight_gate`` writes the verdict + critique into session
   state, increments the attempt counter, and clears the critique on
   pass.
3. On pass, the lazy-GPU signal (``INTENT_GATE_PASSED``) is set —
   this is the INTENT-05 coupling.
4. On max-retry exhaustion the gate raises :class:`IntentGateHalt`
   with a plain-English message built from the failing verdict.
5. ``reset_intent_gate`` clears the signal for a fresh run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from agents.intent_extractor import BriefIntent, BRIEF_INTENT_KEY  # noqa: E402
from callbacks.intent_gate import (  # noqa: E402
    GATE_ATTEMPT_KEY,
    GATE_CRITIQUE_KEY,
    GATE_VERDICT_KEY,
    INTENT_GATE_PASSED,
    IntentGateHalt,
    MAX_GATE_ATTEMPTS,
    build_critique,
    build_halt_message,
    evaluate_gate,
    reset_intent_gate,
    run_preflight_gate,
    wait_for_intent_gate,
)


def _pag_intent(duration_sec: float = 420.0, tolerance_sec: float = 30.0):
    return BriefIntent(
        duration_sec=duration_sec,
        tolerance_sec=tolerance_sec,
        audience="adhd-friendly",
        tone=["curious"],
        corpus_paths=[],
        required_topics=["PAG", "opioid chemistry"],
        forbidden_topics=["recreational drug use"],
        format_hints={"aspect_ratio": "16:9"},
        confidence={"duration_sec": 0.98},
    )


def _scene(duration_sec: float, *, text: str = "") -> dict:
    return {
        "scene_num": 0,
        "title": "scene",
        "duration_sec": duration_sec,
        "narration": text,
    }


@pytest.fixture(autouse=True)
def _reset_gate():
    reset_intent_gate()
    yield
    reset_intent_gate()


# ---------------------------------------------------------------------------
# Pure evaluate_gate
# ---------------------------------------------------------------------------


def test_evaluate_gate_passes_when_in_tolerance():
    intent = _pag_intent()
    text = "PAG and opioid chemistry are discussed at length."
    scenes = [_scene(210, text=text), _scene(210, text=text)]
    verdict = evaluate_gate(intent, scenes)
    assert verdict.passed is True
    assert verdict.failures == []
    assert verdict.missing_required_topics == []
    assert verdict.total_scene_duration_sec == pytest.approx(420.0)


def test_evaluate_gate_fails_when_duration_short():
    intent = _pag_intent()
    text = "PAG and opioid chemistry covered."
    scenes = [_scene(100, text=text)]
    verdict = evaluate_gate(intent, scenes)
    assert verdict.passed is False
    assert any("outside" in f for f in verdict.failures)


def test_evaluate_gate_fails_when_duration_long():
    intent = _pag_intent(tolerance_sec=30.0)
    text = "PAG and opioid chemistry covered."
    scenes = [_scene(800, text=text)]
    verdict = evaluate_gate(intent, scenes)
    assert verdict.passed is False


def test_evaluate_gate_fails_on_missing_required_topic():
    intent = _pag_intent()
    scenes = [_scene(420, text="PAG only, no other topics mentioned")]
    verdict = evaluate_gate(intent, scenes)
    assert verdict.passed is False
    assert "opioid chemistry" in verdict.missing_required_topics


def test_evaluate_gate_fails_on_forbidden_topic_present():
    intent = _pag_intent()
    scenes = [_scene(
        420,
        text=(
            "PAG opioid chemistry is discussed alongside recreational "
            "drug use by users."
        ),
    )]
    verdict = evaluate_gate(intent, scenes)
    assert verdict.passed is False
    assert "recreational drug use" in verdict.present_forbidden_topics


def test_evaluate_gate_fails_when_scenes_empty():
    intent = _pag_intent()
    verdict = evaluate_gate(intent, [])
    assert verdict.passed is False
    assert any("zero scenes" in f for f in verdict.failures)


# ---------------------------------------------------------------------------
# run_preflight_gate — state mutation + signalling
# ---------------------------------------------------------------------------


def _state_with_intent(intent: BriefIntent, scenes: list[dict]) -> dict:
    return {
        BRIEF_INTENT_KEY: intent.to_json(),
        "scenes": scenes,
    }


def test_run_preflight_gate_passes_and_sets_signal():
    intent = _pag_intent()
    text = "PAG and opioid chemistry covered."
    scenes = [_scene(210, text=text), _scene(210, text=text)]
    state = _state_with_intent(intent, scenes)

    assert not INTENT_GATE_PASSED.is_set()
    verdict = run_preflight_gate(state)
    assert verdict.passed is True
    assert INTENT_GATE_PASSED.is_set()
    assert GATE_CRITIQUE_KEY not in state
    assert GATE_VERDICT_KEY in state


def test_run_preflight_gate_fail_writes_critique():
    intent = _pag_intent()
    scenes = [_scene(100, text="PAG opioid chemistry")]
    state = _state_with_intent(intent, scenes)

    verdict = run_preflight_gate(state, max_attempts=3)
    assert verdict.passed is False
    assert GATE_CRITIQUE_KEY in state
    assert state[GATE_ATTEMPT_KEY] == 1
    assert not INTENT_GATE_PASSED.is_set()


def test_run_preflight_gate_halts_after_max_attempts():
    intent = _pag_intent()
    scenes = [_scene(50, text="PAG opioid chemistry")]
    state = _state_with_intent(intent, scenes)

    # First (MAX - 1) attempts fail without halt.
    for _ in range(MAX_GATE_ATTEMPTS - 1):
        verdict = run_preflight_gate(state)
        assert verdict.passed is False

    # The final attempt halts.
    with pytest.raises(IntentGateHalt):
        run_preflight_gate(state)


def test_run_preflight_gate_retry_flips_pass():
    intent = _pag_intent()
    bad_scenes = [_scene(50, text="PAG opioid chemistry")]
    state = _state_with_intent(intent, bad_scenes)

    run_preflight_gate(state)
    assert GATE_CRITIQUE_KEY in state

    # Director fixes the draft — gate should now pass.
    good_text = "PAG and opioid chemistry."
    state["scenes"] = [_scene(210, text=good_text), _scene(210, text=good_text)]
    verdict = run_preflight_gate(state)
    assert verdict.passed is True
    assert GATE_CRITIQUE_KEY not in state


def test_run_preflight_gate_raises_without_intent():
    state: dict = {"scenes": []}
    with pytest.raises(IntentGateHalt):
        run_preflight_gate(state)


# ---------------------------------------------------------------------------
# INTENT-05 — worker-provisioner gating
# ---------------------------------------------------------------------------


def test_wait_for_intent_gate_times_out_when_unfired():
    assert not INTENT_GATE_PASSED.is_set()
    assert wait_for_intent_gate(timeout_sec=0.01) is False


def test_wait_for_intent_gate_returns_true_after_pass():
    intent = _pag_intent()
    text = "PAG and opioid chemistry covered."
    scenes = [_scene(210, text=text), _scene(210, text=text)]
    state = _state_with_intent(intent, scenes)

    assert wait_for_intent_gate(timeout_sec=0.01) is False
    run_preflight_gate(state)
    assert wait_for_intent_gate(timeout_sec=1.0) is True


def test_reset_intent_gate_clears_signal():
    INTENT_GATE_PASSED.set()
    reset_intent_gate()
    assert not INTENT_GATE_PASSED.is_set()


# ---------------------------------------------------------------------------
# Critique + halt-message formatting
# ---------------------------------------------------------------------------


def test_build_critique_mentions_every_failure():
    intent = _pag_intent()
    scenes = [_scene(50, text="PAG only")]
    verdict = evaluate_gate(intent, scenes)
    critique = build_critique(verdict)
    assert "R0 constraint gate rejected" in critique
    for failure in verdict.failures:
        assert failure in critique


def test_build_halt_message_is_plain_english():
    intent = _pag_intent()
    scenes = [_scene(50, text="PAG only")]
    verdict = evaluate_gate(intent, scenes)
    message = build_halt_message(verdict, max_attempts=MAX_GATE_ATTEMPTS)
    assert "Halting" in message
    assert f"{MAX_GATE_ATTEMPTS} attempts" in message
