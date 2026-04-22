"""Unit tests for the escalation-supervisor SubAgent (component 13).

Covers:

* Deterministic decision rule table — all 8 fixture payloads map to
  the expected action under :func:`decide_escalation_action`.
* Decision tool invariants — ``escalate_to_human`` always carries a
  non-empty ``human_summary``; ``fix`` always carries a
  ``state_patches`` dict; ``abort`` reserved for invariant and
  contract violations.
* SubAgent wiring — :func:`build_escalation_supervisor` returns a
  fully-populated :class:`SubAgent` TypedDict with the 5 tools wired.
* ``write_escalation_decision`` round-trips JSON to disk.
* Experiment factories — the primary experiment runs all 8 cases
  through the action-match + human-summary evaluators and every case
  passes; the contract-scoped experiment passes on every case; the
  judge-scoped factory constructs cleanly without calling the judge.
* :class:`ActionEqualsEvaluator` and
  :class:`HumanSummaryRequiredEvaluator` return the expected
  :class:`EvaluationOutput` shape on both happy and sad paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from strands_evals.case import Case
from strands_evals.types.evaluation import EvaluationData

from strands_agents.evals.experiments.escalation import (
    ActionEqualsEvaluator,
    HumanSummaryRequiredEvaluator,
    _CASE_TABLE,
    build_escalation_contract_experiment,
    build_escalation_experiment,
    build_escalation_judge_experiment,
    escalation_contract_task,
    escalation_judge_task,
    escalation_task,
)
from strands_agents.subagents.escalation import (
    ESCALATION_SUPERVISOR_PROMPT,
    EscalationDecision,
    build_escalation_supervisor,
    decide_escalation_action,
    read_file,
    read_telemetry_snapshot,
    request_human_approval,
    write_escalation_decision,
)

FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "escalation"
)


def _load(filename: str) -> dict[str, Any]:
    with (FIXTURES / filename).open("r", encoding="utf-8") as handle:
        return json.load(handle)


# ─────────────────────────────────────────────────────────────────────
# Decision rule table
# ─────────────────────────────────────────────────────────────────────


class TestDecideEscalationAction:
    """Heuristic decision table must cover all 8 spec cases."""

    @pytest.mark.parametrize(
        ("case_name", "fixture_file", "expected_action"),
        [
            (case_name, fixture_file, expected_action)
            for case_name, fixture_file, expected_action, _ in _CASE_TABLE
        ],
    )
    def test_expected_action_for_fixture(
        self,
        case_name: str,
        fixture_file: str,
        expected_action: str,
    ) -> None:
        payload = _load(fixture_file)
        decision = decide_escalation_action(payload)
        assert decision["action"] == expected_action, (
            f"{case_name}: expected {expected_action!r}, "
            f"got {decision['action']!r} (rationale={decision['rationale']!r})"
        )

    def test_all_decisions_carry_rationale_and_confidence(self) -> None:
        for _, fixture_file, _, _ in _CASE_TABLE:
            payload = _load(fixture_file)
            decision = decide_escalation_action(payload)
            assert decision["rationale"], "empty rationale"
            assert 0.0 <= decision["confidence"] <= 1.0
            assert decision["target"]["scope"] in {"scene", "stage", "run"}
            assert decision["target"]["id"]

    def test_escalate_to_human_cases_carry_human_summary(self) -> None:
        for _, fixture_file, expected_action, _ in _CASE_TABLE:
            if expected_action != "escalate_to_human":
                continue
            payload = _load(fixture_file)
            decision = decide_escalation_action(payload)
            assert decision.get("human_summary"), (
                f"escalate_to_human fixture {fixture_file} missing "
                "human_summary"
            )

    def test_fix_cases_carry_state_patches(self) -> None:
        for _, fixture_file, expected_action, _ in _CASE_TABLE:
            if expected_action != "fix":
                continue
            payload = _load(fixture_file)
            decision = decide_escalation_action(payload)
            patches = decision.get("state_patches")
            assert patches, f"fix fixture {fixture_file} missing state_patches"
            assert isinstance(patches, dict)

    def test_bad_payload_triggers_abort_on_contract_violation(self) -> None:
        payload = _load("bad_payload_payload.json")
        decision = decide_escalation_action(payload)
        assert decision["action"] == "abort"
        assert "contract_violation" in decision["rationale"].lower()

    def test_invariant_violation_triggers_abort(self) -> None:
        payload = _load("invariant_violation_payload.json")
        decision = decide_escalation_action(payload)
        assert decision["action"] == "abort"
        assert "invariant" in decision["rationale"].lower()

    def test_transient_retry_uses_budget_count(self) -> None:
        payload = _load("transient_retry_payload.json")
        decision = decide_escalation_action(payload)
        assert decision["action"] == "retry"
        assert "2/3" in decision["rationale"]

    def test_transient_exhausted_escalates(self) -> None:
        payload = {
            "error_class": "transient_retry",
            "retries": 3,
            "retries_max": 3,
            "error_message": "500",
            "target": {"scope": "scene", "id": "s3"},
        }
        decision = decide_escalation_action(payload)
        assert decision["action"] == "escalate_to_human"

    def test_persistent_respects_style_lock_override(self) -> None:
        payload = _load("persistent_fail_payload.json")
        payload["style_lock_permits_skip"] = False
        decision = decide_escalation_action(payload)
        assert decision["action"] == "escalate_to_human"
        assert decision["human_summary"]

    def test_catastrophic_without_worker_crash_still_triggers(self) -> None:
        payload = {
            "error_class": "catastrophic_worker_crash",
            "error_message": "pool empty",
            "workers_healthy": [],
            "target": {"scope": "run", "id": "run_1"},
        }
        decision = decide_escalation_action(payload)
        assert decision["action"] == "escalate_to_human"
        assert "All workers unhealthy" in (decision.get("human_summary") or "")

    def test_unknown_error_class_defaults_to_escalate(self) -> None:
        payload = {
            "error_class": "novel_error_mode",
            "error_message": "unexpected",
            "target": {"scope": "stage", "id": "unknown_stage"},
        }
        decision = decide_escalation_action(payload)
        assert decision["action"] == "escalate_to_human"
        assert decision["confidence"] <= 0.5

    def test_decision_is_deterministic(self) -> None:
        payload = _load("style_drift_payload.json")
        first = decide_escalation_action(payload)
        second = decide_escalation_action(payload)
        assert first == second


# ─────────────────────────────────────────────────────────────────────
# SubAgent wiring
# ─────────────────────────────────────────────────────────────────────


class TestSubAgentDeclaration:
    """:class:`SubAgent` TypedDict must be fully populated."""

    def test_subagent_fields(self) -> None:
        subagent = build_escalation_supervisor(model="openai/gpt-4o")
        assert subagent["name"] == "escalation"
        assert subagent["description"]
        assert subagent["system_prompt"] == ESCALATION_SUPERVISOR_PROMPT
        assert subagent["model"] == "openai/gpt-4o"
        tools = list(subagent["tools"])
        assert len(tools) == 5

    def test_subagent_respects_env_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRANDS_THINKER_MODEL", "anthropic/claude-3-7-sonnet")
        subagent = build_escalation_supervisor()
        assert subagent["model"] == "anthropic/claude-3-7-sonnet"

    def test_subagent_prompt_mentions_all_five_actions(self) -> None:
        for action in ("fix", "retry", "skip", "escalate_to_human", "abort"):
            assert action in ESCALATION_SUPERVISOR_PROMPT


# ─────────────────────────────────────────────────────────────────────
# Tool stubs
# ─────────────────────────────────────────────────────────────────────


class TestToolStubs:
    """Thin wrappers used by the SubAgent at runtime."""

    def test_read_file_reads_text(self, tmp_path: Path) -> None:
        target = tmp_path / "scenes.json"
        target.write_text('{"ok": true}', encoding="utf-8")
        assert read_file(str(target)) == '{"ok": true}'

    def test_read_telemetry_snapshot_returns_stub(self) -> None:
        snapshot = read_telemetry_snapshot("scene_s3")
        assert snapshot["target_id"] == "scene_s3"
        assert snapshot["recent_errors"] == []
        assert snapshot["recent_spans"] == []

    def test_request_human_approval_returns_pending(self) -> None:
        result = request_human_approval("needs review")
        assert result["status"] == "pending"
        assert result["summary"] == "needs review"

    def test_write_escalation_decision_round_trip(self, tmp_path: Path) -> None:
        decision: EscalationDecision = {
            "action": "retry",
            "target": {"scope": "scene", "id": "s3"},
            "rationale": "transient",
            "confidence": 0.8,
            "human_summary": None,
            "state_patches": None,
        }
        out_path = tmp_path / "nested" / "decision.json"
        written = write_escalation_decision(dict(decision), str(out_path))
        assert Path(written).exists()
        with Path(written).open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        assert loaded["action"] == "retry"
        assert loaded["target"] == {"scope": "scene", "id": "s3"}


# ─────────────────────────────────────────────────────────────────────
# Task adapters
# ─────────────────────────────────────────────────────────────────────


class TestTaskAdapters:
    """Each adapter must return the ``output``-wrapped shape
    :class:`Experiment` consumes."""

    @pytest.fixture
    def transient_case(self) -> Case:
        payload = _load("transient_retry_payload.json")
        return Case(
            name="transient_retry",
            input={"diagnostic": payload},
            expected_output={"action": "retry"},
            metadata={"expected_action": "retry", "diagnostic": payload},
        )

    def test_primary_task(self, transient_case: Case) -> None:
        envelope = escalation_task(transient_case)
        assert "output" in envelope
        assert envelope["output"]["action"] == "retry"
        trajectory = envelope["trajectory"]
        assert trajectory and trajectory[0]["tool"] == "decide_escalation_action"

    def test_contract_task_projects_state(self, transient_case: Case) -> None:
        envelope = escalation_contract_task(transient_case)
        state = envelope["output"]
        assert "diagnostic" in state
        assert "decision" in state
        assert state["decision"]["action"] == "retry"

    def test_judge_task_collapses_vocabulary(self) -> None:
        payload = _load("budget_exhausted_payload.json")
        case = Case(
            name="budget",
            input={"diagnostic": payload},
            expected_output={"action": "escalate_to_human"},
            metadata={"expected_action": "escalate_to_human", "diagnostic": payload},
        )
        envelope = escalation_judge_task(case)
        assert envelope["output"]["action"] == "escalate"


# ─────────────────────────────────────────────────────────────────────
# Custom evaluators
# ─────────────────────────────────────────────────────────────────────


def _evaluation_data(
    actual: dict[str, Any],
    *,
    expected_action: str,
) -> EvaluationData[dict[str, Any], dict[str, Any]]:
    return EvaluationData(
        input={"diagnostic": {}},
        actual_output=actual,
        expected_output={"action": expected_action},
        metadata={"expected_action": expected_action},
    )


class TestActionEqualsEvaluator:
    def test_happy_path_passes(self) -> None:
        evaluator = ActionEqualsEvaluator()
        data = _evaluation_data({"action": "retry"}, expected_action="retry")
        outputs = evaluator.evaluate(data)
        assert len(outputs) == 1
        assert outputs[0].test_pass is True
        assert outputs[0].score == 1.0

    def test_mismatch_fails(self) -> None:
        evaluator = ActionEqualsEvaluator()
        data = _evaluation_data({"action": "skip"}, expected_action="retry")
        outputs = evaluator.evaluate(data)
        assert outputs[0].test_pass is False
        assert outputs[0].score == 0.0

    def test_missing_action_fails(self) -> None:
        evaluator = ActionEqualsEvaluator()
        data = _evaluation_data({}, expected_action="retry")
        outputs = evaluator.evaluate(data)
        assert outputs[0].test_pass is False

    def test_evaluate_async_matches_sync(self) -> None:
        import asyncio

        evaluator = ActionEqualsEvaluator()
        data = _evaluation_data({"action": "retry"}, expected_action="retry")
        sync_out = evaluator.evaluate(data)
        async_out = asyncio.run(evaluator.evaluate_async(data))
        assert sync_out[0].score == async_out[0].score


class TestHumanSummaryRequiredEvaluator:
    def test_non_human_action_is_not_applicable(self) -> None:
        evaluator = HumanSummaryRequiredEvaluator()
        data = _evaluation_data({"action": "retry"}, expected_action="retry")
        outputs = evaluator.evaluate(data)
        assert outputs[0].test_pass is True
        assert outputs[0].label == "not_applicable"

    def test_human_action_with_summary_passes(self) -> None:
        evaluator = HumanSummaryRequiredEvaluator()
        data = _evaluation_data(
            {"action": "escalate_to_human", "human_summary": "operator review"},
            expected_action="escalate_to_human",
        )
        outputs = evaluator.evaluate(data)
        assert outputs[0].test_pass is True
        assert outputs[0].label == "human_summary_ok"

    def test_human_action_without_summary_fails(self) -> None:
        evaluator = HumanSummaryRequiredEvaluator()
        data = _evaluation_data(
            {"action": "escalate_to_human", "human_summary": ""},
            expected_action="escalate_to_human",
        )
        outputs = evaluator.evaluate(data)
        assert outputs[0].test_pass is False
        assert outputs[0].label == "human_summary_missing"

    def test_human_action_with_whitespace_only_fails(self) -> None:
        evaluator = HumanSummaryRequiredEvaluator()
        data = _evaluation_data(
            {"action": "escalate_to_human", "human_summary": "   "},
            expected_action="escalate_to_human",
        )
        outputs = evaluator.evaluate(data)
        assert outputs[0].test_pass is False


# ─────────────────────────────────────────────────────────────────────
# Experiment factories end-to-end
# ─────────────────────────────────────────────────────────────────────


class TestExperimentRuns:
    """Running :func:`Experiment.run_evaluations` end-to-end against
    the deterministic decision tool should pass every case and every
    hard-gate evaluator."""

    def test_primary_experiment_all_pass(self) -> None:
        experiment = build_escalation_experiment()
        reports = experiment.run_evaluations(escalation_task)
        assert len(reports) == 2, "one report per evaluator expected"
        for report in reports:
            assert report.overall_score == pytest.approx(1.0), (
                f"evaluator={report.evaluator_name} "
                f"score={report.overall_score} reasons={report.reasons}"
            )
            assert all(report.test_passes), report.reasons
            assert len(report.cases) == 8

    def test_contract_experiment_all_pass(self) -> None:
        experiment = build_escalation_contract_experiment()
        reports = experiment.run_evaluations(escalation_contract_task)
        for report in reports:
            assert all(report.test_passes), (
                f"{report.evaluator_name}: {report.reasons}"
            )

    def test_judge_experiment_constructs(self) -> None:
        """The judge factory wires the LLM evaluator without calling it."""
        experiment = build_escalation_judge_experiment(judge_model=None)
        assert experiment.cases
        assert experiment.evaluators
        assert experiment.evaluators[0].__class__.__name__ == (
            "EscalationDecisionEvaluator"
        )

    def test_case_count_matches_spec(self) -> None:
        experiment = build_escalation_experiment()
        case_names = {case.name for case in experiment.cases}
        assert case_names == {
            "transient_retry",
            "persistent_fail",
            "invariant_violation",
            "budget_exhausted_whole_stage",
            "catastrophic_worker_crash",
            "style_drift",
            "timing_loop_stuck",
            "bad_payload",
        }
