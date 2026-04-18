"""Tests for the canonical escalation action menu + supervisor_escalate.

Closes #61, #73, #76, #77, #102, #103.

The four required tests (per spec):
    - test_action_menu_parses_all_signatures
    - test_supervisor_escalate_returns_typed_action
    - test_supervisor_escalate_counts_llm_calls
    - test_supervisor_invariant_fires_on_escalation_without_llm

These tests do NOT hit Gemini — ``set_llm_client_factory`` is used to
swap in a deterministic mock so the decision layer is exercised end-to-end
without network or API-key requirements.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

# Ensure ``server/`` is on sys.path when pytest is invoked from the repo root.
_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from orchestrator.escalation_menu import (  # noqa: E402
    ACTION_NAMES,
    EscalationAction,
    EscalationActionError,
    EscalationContext,
    EscalationInvariantViolation,
    MAX_SPEED_FACTOR,
    assert_escalation_invariant,
)


# ---------------------------------------------------------------------------
# 1. Action-menu signature parsing
# ---------------------------------------------------------------------------

def test_action_menu_parses_all_signatures():
    """Every canonical action round-trips through the dataclass + from_dict.

    - Valid payloads for all 8 actions construct successfully.
    - Missing required fields raise EscalationActionError.
    - Type mismatches raise EscalationActionError.
    - Bounds (speed_factor <= 1.15, non-zero seed_delta, etc.) are enforced.
    """
    valid_payloads: dict[str, dict] = {
        "regenerate_clip": {
            "action": "regenerate_clip",
            "clip_id": "s003_p002",
            "prompt_delta": "emphasise kitchen interior",
            "seed_delta": 7,
        },
        "generate_extension_clip": {
            "action": "generate_extension_clip",
            "scene_id": "s003",
            "duration_needed": 1.4,
        },
        "speed_up_narration": {
            "action": "speed_up_narration",
            "scene_id": "s004",
            "speed_factor": 1.1,
        },
        "trim_narration": {
            "action": "trim_narration",
            "scene_id": "s005",
            "max_cut_sec": 0.75,
        },
        "freeze_frame_fill": {
            "action": "freeze_frame_fill",
            "scene_id": "s006",
            "duration_needed": 0.8,
        },
        "replace_with_brand_card": {
            "action": "replace_with_brand_card",
            "scene_id": "s007",
        },
        "rewrite_scene": {
            "action": "rewrite_scene",
            "scene_id": "s008",
            "guidance": "Shorten to 20s; keep kitchen setting.",
        },
        "abort_run": {
            "action": "abort_run",
            "reason": "Budget exhausted — no safe recovery.",
        },
        # Ops / deployment menu (PR-2)
        "recycle_worker": {
            "action": "recycle_worker",
            "worker_url": "http://127.0.0.1:8000",
            "reason": "vram_pressure + 3 consecutive QA fails",
        },
        "provision_extra_worker": {
            "action": "provision_extra_worker",
            "role": "video",
            "count": 2,
        },
        "wait_for_worker_recovery": {
            "action": "wait_for_worker_recovery",
            "worker_url": "http://127.0.0.1:8000",
            "timeout_sec": 120.0,
        },
        "freeze_batch_and_replan": {
            "action": "freeze_batch_and_replan",
            "reason": "fleet saturated, cost burn > budget",
        },
    }

    # Every canonical action must have a valid payload in this test.
    assert set(valid_payloads.keys()) == set(ACTION_NAMES), (
        "Test payloads drift from ACTION_NAMES — update valid_payloads."
    )

    # All 8 parse cleanly via from_dict AND round-trip via to_dict.
    for name, payload in valid_payloads.items():
        action = EscalationAction.from_dict(payload)
        assert action.action == name
        as_dict = action.to_dict()
        # to_dict strips Nones; all required fields must still be present.
        rebuilt = EscalationAction.from_dict(as_dict)
        assert rebuilt.action == name
        assert rebuilt.to_dict() == as_dict
        # Level is 1/2/3 and matches the action name's declared level.
        assert rebuilt.level in (1, 2, 3)

    # Missing required field.
    with pytest.raises(EscalationActionError):
        EscalationAction.from_dict({
            "action": "regenerate_clip",
            "clip_id": "s003_p002",
            # prompt_delta missing
            "seed_delta": 3,
        })

    # Unknown action.
    with pytest.raises(EscalationActionError):
        EscalationAction.from_dict({"action": "nuke_everything"})

    # Type mismatch: speed_factor as str.
    with pytest.raises(EscalationActionError):
        EscalationAction.from_dict({
            "action": "speed_up_narration",
            "scene_id": "s001",
            "speed_factor": "fast",
        })

    # Bounds: speed_factor > MAX_SPEED_FACTOR.
    with pytest.raises(EscalationActionError):
        EscalationAction(
            action="speed_up_narration",
            scene_id="s001",
            speed_factor=MAX_SPEED_FACTOR + 0.01,
        )

    # Bounds: seed_delta must be non-zero.
    with pytest.raises(EscalationActionError):
        EscalationAction(
            action="regenerate_clip",
            clip_id="x",
            prompt_delta="delta",
            seed_delta=0,
        )

    # Bounds: duration_needed must be > 0.
    with pytest.raises(EscalationActionError):
        EscalationAction(
            action="generate_extension_clip",
            scene_id="s001",
            duration_needed=0.0,
        )

    # int → float coercion works (JSON often sends ints for number fields).
    coerced = EscalationAction.from_dict({
        "action": "generate_extension_clip",
        "scene_id": "s001",
        "duration_needed": 2,  # int, expected float
    })
    assert isinstance(coerced.duration_needed, float)
    assert coerced.duration_needed == 2.0


# ---------------------------------------------------------------------------
# Fixtures for supervisor tests
# ---------------------------------------------------------------------------

@pytest.fixture
def supervisor_module():
    """Import and reset the supervisor module.

    We import inside the fixture so heavy top-level side effects (ADK
    agent construction) only happen once per process.  Each test gets a
    fresh counter snapshot.
    """
    from agents import production_supervisor

    production_supervisor.reset_run_counters()
    yield production_supervisor
    # Always restore the default client factory and reset counters.
    production_supervisor.set_llm_client_factory(
        production_supervisor._default_llm_call
    )
    production_supervisor.reset_run_counters()


def _fake_context() -> EscalationContext:
    return EscalationContext(
        failing_artifact="clip s003_p002",
        artifact_descriptor={"prompt": "kitchen scene", "duration": 5.0},
        timeline_state_snapshot={"scene_count": 12, "total_duration": 180.0},
        user_original_prompt="Make a 3-minute doc on sourdough.",
        budget_remaining=42.0,
        escalation_history=[],
    )


# ---------------------------------------------------------------------------
# 2. supervisor_escalate returns a typed action
# ---------------------------------------------------------------------------

def test_supervisor_escalate_returns_typed_action(supervisor_module):
    """supervisor_escalate returns a validated EscalationAction from Gemini."""

    def fake_llm(model, system, prompt):
        assert "CANONICAL ESCALATION ACTIONS" in system
        assert "clip s003_p002" in prompt
        return json.dumps({
            "action": "regenerate_clip",
            "clip_id": "s003_p002",
            "prompt_delta": "emphasise warm kitchen lighting",
            "seed_delta": 11,
            "llm_reasoning": "Lighting was off in the first attempt.",
        })

    supervisor_module.set_llm_client_factory(fake_llm)

    action = supervisor_module.supervisor_escalate(_fake_context())

    assert isinstance(action, EscalationAction)
    assert action.action == "regenerate_clip"
    assert action.clip_id == "s003_p002"
    assert action.seed_delta == 11
    assert action.level == 1
    assert action.llm_model  # stamped by supervisor_escalate
    assert action.llm_reasoning == "Lighting was off in the first attempt."


def test_supervisor_escalate_retries_on_parse_failure(supervisor_module):
    """2x retry on parse failure, then abort_run fallback."""
    call_log: list[str] = []

    def fake_llm(model, system, prompt):
        call_log.append(prompt)
        # Return garbage every time — should exhaust 3 attempts then abort.
        return "not json at all, completely broken"

    supervisor_module.set_llm_client_factory(fake_llm)

    action = supervisor_module.supervisor_escalate(_fake_context())

    assert len(call_log) == 3, "Expected initial + 2 retries"
    assert action.action == "abort_run"
    assert "exhausted" in (action.reason or "")


def test_supervisor_escalate_recovers_after_first_parse_failure(supervisor_module):
    """After a parse failure the supervisor should retry with stricter prompt."""
    responses = iter([
        "garbage-not-json",
        json.dumps({
            "action": "speed_up_narration",
            "scene_id": "s003",
            "speed_factor": 1.1,
        }),
    ])

    def fake_llm(model, system, prompt):
        return next(responses)

    supervisor_module.set_llm_client_factory(fake_llm)

    action = supervisor_module.supervisor_escalate(_fake_context())
    assert action.action == "speed_up_narration"
    snap = supervisor_module.get_run_counters()
    # One escalation, two LLM calls (first failed, second succeeded).
    assert snap["escalations_per_run"] == 1
    assert snap["llm_calls_per_run"] == 2
    assert snap["parse_failures_per_run"] == 1


# ---------------------------------------------------------------------------
# 3. LLM-call counter
# ---------------------------------------------------------------------------

def test_supervisor_escalate_counts_llm_calls(supervisor_module):
    """Every call to supervisor_escalate increments llm_calls_per_run."""
    def fake_llm(model, system, prompt):
        return json.dumps({
            "action": "abort_run",
            "reason": "test",
        })

    supervisor_module.set_llm_client_factory(fake_llm)
    assert supervisor_module.get_run_counters()["llm_calls_per_run"] == 0

    for i in range(3):
        supervisor_module.supervisor_escalate(_fake_context())

    snap = supervisor_module.get_run_counters()
    assert snap["llm_calls_per_run"] == 3
    assert snap["escalations_per_run"] == 3


# ---------------------------------------------------------------------------
# 4. Hard invariant
# ---------------------------------------------------------------------------

def test_supervisor_invariant_fires_on_escalation_without_llm(supervisor_module):
    """The hard invariant fails if an escalation happens with zero LLM calls.

    This is the #102 acceptance criterion.  We simulate the regression by
    directly bumping the escalation counter (which is exactly what a
    round-robin fall-through would do) and assert the invariant detects it.
    """
    supervisor_module.reset_run_counters()
    # Directly simulate a round-robin fall-through escalation path that
    # skipped the supervisor — no LLM calls made.
    supervisor_module._counters.incr_escalations()

    with pytest.raises(EscalationInvariantViolation):
        supervisor_module.assert_supervisor_invariant_at_end_of_run()

    # Functional form used by CI should also fail.
    with pytest.raises(EscalationInvariantViolation):
        assert_escalation_invariant(
            escalations_per_run=1,
            llm_calls_per_run=0,
        )

    # Sanity: invariant passes when the supervisor was consulted.
    supervisor_module.reset_run_counters()

    def fake_llm(model, system, prompt):
        return json.dumps({
            "action": "speed_up_narration",
            "scene_id": "s001",
            "speed_factor": 1.05,
        })

    supervisor_module.set_llm_client_factory(fake_llm)
    supervisor_module.supervisor_escalate(_fake_context())

    # Now escalations=1 and llm_calls=1 → invariant passes.
    supervisor_module.assert_supervisor_invariant_at_end_of_run()


def test_invariant_trivially_passes_when_no_escalations():
    """If no escalations happened, llm_calls=0 is fine."""
    assert_escalation_invariant(escalations_per_run=0, llm_calls_per_run=0)
