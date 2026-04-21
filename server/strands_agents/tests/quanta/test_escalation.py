"""Direct-proof tests for :mod:`strands_agents.quanta.escalation`."""

from __future__ import annotations

from typing import Any

from strands_agents.quanta import decide_escalation_action


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "target": {"scope": "scene", "id": "s1"},
        "error_class": "",
        "error_message": "",
    }
    base.update(overrides)
    return base


class TestDecideEscalationAction:
    def test_missing_target_is_abort(self) -> None:
        out = decide_escalation_action({"error_class": "transient"})
        assert out["action"] == "abort"
        assert "contract_violation" in out["rationale"]

    def test_invariant_violation_is_abort(self) -> None:
        out = decide_escalation_action(_payload(error_class="invariant_violation"))
        assert out["action"] == "abort"

    def test_catastrophic_worker_crash_escalates_with_summary(self) -> None:
        out = decide_escalation_action(_payload(worker_crashed=True))
        assert out["action"] == "escalate_to_human"
        assert out["human_summary"] is not None
        assert len(out["human_summary"]) > 0

    def test_transient_with_budget_remaining_retries(self) -> None:
        out = decide_escalation_action(
            _payload(error_class="transient", retries=0, retries_max=3)
        )
        assert out["action"] == "retry"

    def test_transient_budget_exhausted_escalates(self) -> None:
        out = decide_escalation_action(
            _payload(error_class="transient", retries=3, retries_max=3)
        )
        assert out["action"] == "escalate_to_human"

    def test_persistent_with_style_lock_permitting_skip_skips(self) -> None:
        out = decide_escalation_action(
            _payload(error_class="persistent_fail", style_lock_permits_skip=True)
        )
        assert out["action"] == "skip"

    def test_persistent_with_style_lock_forbidding_skip_escalates(self) -> None:
        out = decide_escalation_action(
            _payload(error_class="persistent_fail", style_lock_permits_skip=False)
        )
        assert out["action"] == "escalate_to_human"

    def test_style_drift_returns_fix_with_patches(self) -> None:
        out = decide_escalation_action(_payload(error_class="style_drift"))
        assert out["action"] == "fix"
        assert out["state_patches"] is not None

    def test_unknown_error_class_escalates(self) -> None:
        out = decide_escalation_action(_payload(error_class="novel_thing"))
        assert out["action"] == "escalate_to_human"

    def test_deterministic(self) -> None:
        p = _payload(error_class="transient", retries=1, retries_max=3)
        assert decide_escalation_action(p) == decide_escalation_action(p)

    def test_escalate_to_human_always_has_human_summary(self) -> None:
        # Invariant — enforced by _enforce_human_summary.
        out = decide_escalation_action(_payload(error_class="novel_x"))
        assert out["action"] == "escalate_to_human"
        assert isinstance(out["human_summary"], str)
        assert out["human_summary"].strip() != ""
