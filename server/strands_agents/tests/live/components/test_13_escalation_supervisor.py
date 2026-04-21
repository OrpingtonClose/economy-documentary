"""Live-judge proof of robustness for Component 13 (escalation-supervisor).

Clear-cut contracts proved here:

1. Deterministic decision table: canonical diagnostic payloads always
   map to the right action — catastrophic crashes escalate, invariant
   violations abort, budget-exhaustion asks a human, style drift
   fixes, transient retries retry, persistent non-critical failures
   skip.
2. Deterministic safety: every ``escalate_to_human`` ships with a
   non-empty ``human_summary`` so an operator is never asked to
   approve a ticket they can't read.
3. Live: Claude confirms that the ``rationale`` on an escalate-to-
   human decision actually matches the chosen action — a broken
   rationale ("retry the worker") paired with an action
   ("escalate_to_human") would fail here.
"""

from __future__ import annotations

from typing import Any


from strands_agents.subagents.escalation import decide_escalation_action

from .._judges import judge_text_yes
from ..conftest import requires_google_api


def _target(scope: str = "scene", ident: str = "s-1") -> dict[str, Any]:
    return {"scope": scope, "id": ident}


# ---------------------------------------------------------------------------
# Deterministic decision table
# ---------------------------------------------------------------------------


def test_catastrophic_worker_crash_escalates() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(),
            "error_class": "catastrophic_worker_crash",
            "error_message": "LTX worker SIGSEGV",
            "workers_healthy": 0,
        }
    )
    assert decision["action"] == "escalate_to_human"
    assert decision["human_summary"]


def test_worker_crashed_flag_alone_escalates() -> None:
    # Even without error_class, worker_crashed=True should escalate.
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(),
            "worker_crashed": True,
            "error_message": "pod terminated unexpectedly",
        }
    )
    assert decision["action"] == "escalate_to_human"
    assert decision["human_summary"]


def test_invariant_violation_aborts() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(),
            "error_class": "invariant_violation",
            "error_message": "AGENTS.md #3 violated",
        }
    )
    assert decision["action"] == "abort"


def test_budget_exhausted_escalates() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(scope="run", ident="run-42"),
            "error_class": "budget_exhausted_whole_stage",
            "failed_scenes": 4,
            "total_scenes": 12,
        }
    )
    assert decision["action"] == "escalate_to_human"
    assert decision["human_summary"]


def test_style_drift_triggers_fix() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(),
            "error_class": "style_drift",
            "error_message": "output breaks style_lock",
        }
    )
    assert decision["action"] == "fix"
    patches = decision.get("state_patches") or {}
    assert patches.get("regenerate_concept") is True
    assert patches.get("enforce_style_lock") is True


def test_transient_within_budget_retries() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(),
            "error_class": "transient_retry",
            "retries": 1,
            "retries_max": 3,
        }
    )
    assert decision["action"] == "retry"


def test_transient_over_budget_escalates() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(),
            "error_class": "transient_retry",
            "retries": 3,
            "retries_max": 3,
        }
    )
    assert decision["action"] == "escalate_to_human"
    assert decision["human_summary"]


def test_persistent_fail_with_skip_permit_skips() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(),
            "error_class": "persistent_fail",
            "style_lock_permits_skip": True,
        }
    )
    assert decision["action"] == "skip"


def test_persistent_fail_without_skip_permit_escalates() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(),
            "error_class": "persistent_fail",
            "style_lock_permits_skip": False,
        }
    )
    assert decision["action"] == "escalate_to_human"
    assert decision["human_summary"]


def test_timing_loop_stuck_skips() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(scope="run", ident="run-1"),
            "error_class": "timing_loop_stuck",
        }
    )
    assert decision["action"] == "skip"


def test_unknown_error_class_escalates_as_safe_default() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(),
            "error_class": "some_new_never_before_seen_thing",
            "error_message": "???",
        }
    )
    assert decision["action"] == "escalate_to_human"
    assert decision["human_summary"]


def test_missing_target_aborts() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "error_class": "transient_retry",
            "retries": 0,
            "retries_max": 3,
        }
    )
    assert decision["action"] == "abort"
    assert "contract_violation" in decision["rationale"]


def test_every_human_escalation_carries_human_summary() -> None:
    """Fail-closed: any escalate_to_human path must surface a summary."""
    payloads = [
        {
            "target": _target(),
            "error_class": "catastrophic_worker_crash",
            "error_message": "segfault",
        },
        {
            "target": _target(),
            "error_class": "budget_exhausted_whole_stage",
        },
        {
            "target": _target(),
            "error_class": "transient_retry",
            "retries": 3,
            "retries_max": 3,
        },
        {
            "target": _target(),
            "error_class": "persistent_fail",
            "style_lock_permits_skip": False,
        },
        {
            "target": _target(),
            "error_class": "something_unknown",
            "error_message": "???",
        },
    ]
    for payload in payloads:
        decision = decide_escalation_action.__wrapped__(diagnostic_payload=payload)
        assert decision["action"] == "escalate_to_human"
        assert decision["human_summary"], (
            f"human_summary missing for payload {payload!r}"
        )


# ---------------------------------------------------------------------------
# Live: Claude confirms rationale matches action
# ---------------------------------------------------------------------------


@requires_google_api
def test_live_claude_confirms_rationale_matches_escalation() -> None:
    decision = decide_escalation_action.__wrapped__(
        diagnostic_payload={
            "target": _target(scope="run", ident="run-123"),
            "error_class": "catastrophic_worker_crash",
            "error_message": "LTX worker SIGSEGV on 3 consecutive jobs",
            "workers_healthy": 0,
        }
    )
    assert decision["action"] == "escalate_to_human"
    verdict = judge_text_yes(
        "You are reviewing a documentary pipeline's escalation "
        "decision.  Given the action the system took and the "
        "rationale it recorded, is the rationale consistent with "
        "the action? (A rationale about retrying a worker would NOT "
        "be consistent with escalating to a human; a rationale about "
        "a crashed worker pool WOULD be consistent with escalating.) "
        "Answer with a single word: yes or no.\n\n"
        f"Action: {decision['action']}\n"
        f"Rationale: {decision['rationale']}\n"
        f"Human summary: {decision['human_summary']}"
    )
    assert not verdict.disabled, f"judge disabled: {verdict.error}"
    assert verdict.is_yes, (
        f"Claude thinks rationale is inconsistent with action: {verdict.answer!r}"
    )
