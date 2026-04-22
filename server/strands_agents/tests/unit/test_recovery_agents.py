"""Unit tests for Component 12 — recovery agents.

These tests exercise the deterministic tools, the
``RecoveryLogger`` hook, the ``recovery_task`` experiment adapter, and
the contract / invariant evaluators. No LLM calls, no network.
"""

from __future__ import annotations

from typing import Any

import pytest
from strands_evals.types.evaluation import EvaluationData

from contracts import (
    RECOVERY_CLASSIFIER_CONTRACT,
    RECOVERY_REMANIFESTER_CONTRACT,
)
from strands_agents.evals.evaluators import ContractComplianceEvaluator
from strands_agents.evals.experiments.recovery import (
    ClassificationVocabularyEvaluator,
    RemanifestInvariantEvaluator,
    _classification_to_action,
    build_recovery_classifier_contract_experiment,
    build_recovery_experiment,
    build_recovery_remanifester_contract_experiment,
    recovery_task,
)
from strands_agents.hooks.recovery_logger import RecoveryLogger
from strands_agents.subagents.recovery_agents import (
    VALID_CLASSIFICATIONS,
    _PRESERVED_FIELDS,
    build_diagnostic_classifier,
    build_remanifestation_agent,
)
from strands_agents.subagents.recovery_agents import (
    classify as _classify_tool,
)
from strands_agents.subagents.recovery_agents import (
    diff_concept as _diff_concept_tool,
)
from strands_agents.subagents.recovery_agents import (
    persist_classification as _persist_classification_tool,
)
from strands_agents.subagents.recovery_agents import (
    propose_revised_concept as _propose_revised_concept_tool,
)


def _unwrap(tool: Any) -> Any:
    for attr in ("original_function", "func", "_func"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            return fn
    assert callable(tool)
    return tool


classify = _unwrap(_classify_tool)
persist_classification = _unwrap(_persist_classification_tool)
propose_revised_concept = _unwrap(_propose_revised_concept_tool)
diff_concept = _unwrap(_diff_concept_tool)


def _concept(scene_id: str = "scene_01") -> dict[str, Any]:
    return {
        "phrase_id": f"phr_{scene_id}",
        "scene_id": scene_id,
        "duration_sec": 4.0,
        "style_lock_applied": True,
        "prompt": "wide shot of a city skyline at dusk",
        "negative_prompt": "",
        "camera_movement": "",
        "shot_type": "wide",
    }


def _style_lock() -> dict[str, Any]:
    return {"style": "cinematic_documentary", "camera_movement": "slow push in"}


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------


class TestClassify:
    def test_cuda_oom_is_transient(self) -> None:
        out = classify("CUDA out of memory", [], _concept())
        assert out["class"] == "transient"
        assert "cuda_oom" in out["signals"]

    def test_connection_reset_is_transient(self) -> None:
        out = classify("connection reset by peer", [], _concept())
        assert out["class"] == "transient"

    def test_timeout_is_transient(self) -> None:
        out = classify("socket.timeout while calling /ltx/generate", [], _concept())
        assert out["class"] == "transient"

    def test_style_mismatch_is_fixable(self) -> None:
        out = classify(
            "output style doesn't match cinematic_documentary", [], _concept()
        )
        assert out["class"] == "fixable"

    def test_prompt_incoherent_is_fixable(self) -> None:
        out = classify("generation incoherent, prompt too vague", [], _concept())
        assert out["class"] == "fixable"

    def test_qa_rejected_is_fixable(self) -> None:
        out = classify("QA rejected: content_mismatch", [], _concept())
        assert out["class"] == "fixable"

    def test_same_fixable_3x_is_persistent(self) -> None:
        err = "output style doesn't match cinematic_documentary"
        history = [{"error": err}, {"error": err}]
        out = classify(err, history, _concept())
        assert out["class"] == "persistent"
        assert out["repeat_count"] == 2

    def test_all_workers_500_is_catastrophic(self) -> None:
        out = classify("all workers returned 500 on /ltx/generate", [], _concept())
        assert out["class"] == "catastrophic"

    def test_disk_full_is_catastrophic(self) -> None:
        out = classify("no space left on device", [], _concept())
        assert out["class"] == "catastrophic"

    def test_determinism_on_identical_inputs(self) -> None:
        err = "CUDA out of memory"
        concept = _concept()
        out_1 = classify(err, [], concept)
        out_2 = classify(err, [], concept)
        assert out_1 == out_2

    def test_unknown_error_defaults_to_fixable(self) -> None:
        out = classify("something entirely novel happened", [], _concept())
        assert out["class"] == "fixable"
        assert "no signals matched" in out["reasoning"]


# ---------------------------------------------------------------------------
# persist_classification()
# ---------------------------------------------------------------------------


class TestPersistClassification:
    def test_round_trips_valid_payload(self) -> None:
        out = persist_classification(
            "scene_01",
            {"class": "transient", "hint": "retry"},
        )
        assert out == {
            "artifact_id": "scene_01",
            "classification": {"class": "transient", "hint": "retry"},
            "persisted": True,
        }

    def test_rejects_empty_artifact_id(self) -> None:
        with pytest.raises(ValueError):
            persist_classification("", {"class": "transient"})

    def test_rejects_missing_class_key(self) -> None:
        with pytest.raises(ValueError):
            persist_classification("scene_01", {"hint": "retry"})

    def test_rejects_unknown_class(self) -> None:
        with pytest.raises(ValueError):
            persist_classification("scene_01", {"class": "weird"})


# ---------------------------------------------------------------------------
# propose_revised_concept() / diff_concept()
# ---------------------------------------------------------------------------


class TestRemanifestation:
    def test_preserves_required_fields(self) -> None:
        original = _concept()
        revised = propose_revised_concept(
            original,
            "generation incoherent",
            "adjust prompt to be more specific",
            _style_lock(),
        )
        for key in _PRESERVED_FIELDS:
            assert revised[key] == original[key], (
                f"field {key} was mutated: {original[key]!r} -> {revised[key]!r}"
            )

    def test_makes_at_least_one_actionable_change(self) -> None:
        original = _concept()
        revised = propose_revised_concept(
            original,
            "output style mismatch",
            "add cinematic style",
            _style_lock(),
        )
        changed = {
            k for k in set(original) | set(revised)
            if original.get(k) != revised.get(k)
        }
        assert changed & {"prompt", "negative_prompt", "camera_movement"}

    def test_honors_style_lock_camera_movement(self) -> None:
        original = _concept()
        original["camera_movement"] = ""  # empty
        revised = propose_revised_concept(
            original,
            "output style mismatch",
            "add cinematic style",
            _style_lock(),
        )
        assert revised["camera_movement"] == "slow push in"

    def test_style_lock_not_overridden_when_present(self) -> None:
        original = _concept()
        original["camera_movement"] = "hand-held whip pan"
        revised = propose_revised_concept(
            original, "QA rejected", "revise prompt", _style_lock()
        )
        # We keep the pre-existing camera movement — the tool only fills
        # in a blank, it never overwrites an existing setting.
        assert revised["camera_movement"] == "hand-held whip pan"

    def test_empty_concept_raises(self) -> None:
        with pytest.raises(ValueError):
            propose_revised_concept({}, "err", "hint", _style_lock())

    def test_missing_scene_id_raises(self) -> None:
        bad = _concept()
        del bad["scene_id"]
        with pytest.raises(ValueError):
            propose_revised_concept(bad, "err", "hint", _style_lock())

    def test_diff_concept_reports_changed_fields(self) -> None:
        original = _concept()
        revised = propose_revised_concept(
            original, "style mismatch", "cinematic style", _style_lock()
        )
        diff = diff_concept(original, revised)
        assert "prompt" in diff["changed_fields"]
        assert "phrase_id" in diff["preserved_fields"]
        assert "scene_id" in diff["preserved_fields"]

    def test_diff_concept_symmetric_no_op(self) -> None:
        original = _concept()
        diff = diff_concept(original, original)
        assert diff["changed_fields"] == []
        assert set(diff["preserved_fields"]) == set(original.keys())

    def test_revision_is_deterministic_on_same_inputs(self) -> None:
        c1 = _concept()
        r1 = propose_revised_concept(c1, "err", "hint", _style_lock())
        r2 = propose_revised_concept(c1, "err", "hint", _style_lock())
        assert r1 == r2


# ---------------------------------------------------------------------------
# RecoveryLogger
# ---------------------------------------------------------------------------


class TestRecoveryLogger:
    def test_appends_classifier_entry(self) -> None:
        log = RecoveryLogger()
        entry = log.record_classification(
            "scene_01", {"class": "fixable", "reasoning": "x", "signals": ["a"]}
        )
        assert entry["agent"] == "classifier"
        assert log.entries() == [entry]
        assert log.count_for("scene_01") == 1

    def test_appends_remanifester_entry(self) -> None:
        log = RecoveryLogger()
        entry = log.record_remanifestation(
            "scene_01", {"changed_fields": ["prompt"]}
        )
        assert entry["agent"] == "remanifester"
        assert entry["changed_fields"] == ["prompt"]

    def test_one_entry_per_decision(self) -> None:
        log = RecoveryLogger()
        log.record_classification("scene_01", {"class": "fixable"})
        log.record_remanifestation("scene_01", {"changed_fields": ["prompt"]})
        assert log.count_for("scene_01") == 2

    def test_bounded_max_entries(self) -> None:
        log = RecoveryLogger(max_entries=3)
        for i in range(10):
            log.record_classification(
                f"scene_{i:02d}", {"class": "transient"}
            )
        assert len(log.entries()) == 3
        # Oldest evicted, newest kept.
        assert log.entries()[-1]["artifact_id"] == "scene_09"

    def test_rejects_invalid_max_entries(self) -> None:
        with pytest.raises(ValueError):
            RecoveryLogger(max_entries=0)

    def test_rejects_missing_class_key(self) -> None:
        log = RecoveryLogger()
        with pytest.raises(ValueError):
            log.record_classification("scene_01", {"hint": "retry"})

    def test_clear_empties_log(self) -> None:
        log = RecoveryLogger()
        log.record_classification("scene_01", {"class": "fixable"})
        log.clear()
        assert log.entries() == []


# ---------------------------------------------------------------------------
# recovery_task + experiment
# ---------------------------------------------------------------------------


class TestRecoveryTask:
    def test_runs_all_8_cases(self) -> None:
        exp = build_recovery_experiment()
        for case in exp.cases:
            env = recovery_task(case)
            assert env["output"]["classification"] in VALID_CLASSIFICATIONS
            assert env["trajectory"][0] == "classify"

    def test_classify_only_cases_skip_remanifest(self) -> None:
        exp = build_recovery_experiment()
        for case in exp.cases:
            meta = dict(case.metadata or {})
            if meta.get("flow") != "classify_only":
                continue
            env = recovery_task(case)
            assert env["output"]["remanifested"] is False
            assert "propose_revised_concept" not in env["trajectory"]

    def test_full_recovery_cases_run_all_three_tools(self) -> None:
        exp = build_recovery_experiment()
        for case in exp.cases:
            meta = dict(case.metadata or {})
            if meta.get("flow") != "full_recovery":
                continue
            env = recovery_task(case)
            assert env["trajectory"] == [
                "classify",
                "propose_revised_concept",
                "diff_concept",
            ]
            assert env["output"]["remanifested"] is True
            assert env["output"]["revised_concept"] is not None
            assert env["output"]["diff"] is not None

    def test_recovery_log_has_exactly_one_entry_per_decision(self) -> None:
        exp = build_recovery_experiment()
        for case in exp.cases:
            env = recovery_task(case)
            log = env["output"]["recovery_log"]
            classifier_entries = [e for e in log if e["agent"] == "classifier"]
            remanifester_entries = [
                e for e in log if e["agent"] == "remanifester"
            ]
            # Every case records exactly one classification.
            assert len(classifier_entries) == 1, case.name
            # Remanifestations are recorded only when they ran.
            expected = 1 if env["output"]["remanifested"] else 0
            assert len(remanifester_entries) == expected, case.name

    def test_classification_to_action_mapping(self) -> None:
        assert _classification_to_action("transient") == "retry"
        assert _classification_to_action("fixable") == "fix"
        assert _classification_to_action("persistent") == "escalate"
        assert _classification_to_action("catastrophic") == "abort"

    def test_persistent_case_expected_action_is_escalate(self) -> None:
        exp = build_recovery_experiment()
        case = next(c for c in exp.cases if c.name == "same_error_3x")
        env = recovery_task(case)
        assert env["output"]["action"] == "escalate"

    def test_catastrophic_case_expected_action_is_abort(self) -> None:
        exp = build_recovery_experiment()
        case = next(c for c in exp.cases if c.name == "worker_500_all")
        env = recovery_task(case)
        assert env["output"]["action"] == "abort"


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------


class TestClassificationVocabularyEvaluator:
    def test_passes_for_valid_label(self) -> None:
        ev = ClassificationVocabularyEvaluator()
        out = ev.evaluate(
            EvaluationData(
                input={},
                actual_output={"classification": "fixable"},
                expected_output=None,
                metadata={},
            )
        )
        assert len(out) == 1
        assert out[0].score == 1.0
        assert out[0].test_pass is True

    def test_fails_for_invalid_label(self) -> None:
        ev = ClassificationVocabularyEvaluator()
        out = ev.evaluate(
            EvaluationData(
                input={},
                actual_output={"classification": "definitely_not_a_class"},
                expected_output=None,
                metadata={},
            )
        )
        assert out[0].score == 0.0
        assert out[0].test_pass is False


class TestRemanifestInvariantEvaluator:
    def test_passes_on_happy_path(self) -> None:
        exp = build_recovery_experiment()
        case = next(c for c in exp.cases if c.name == "wrong_style_output")
        env = recovery_task(case)
        ev = RemanifestInvariantEvaluator()
        out = ev.evaluate(
            EvaluationData(
                input=case.input,
                actual_output=env["output"],
                expected_output=case.expected_output,
                metadata=env["metadata"],
            )
        )
        assert all(o.test_pass for o in out), [
            (o.label, o.reason) for o in out if not o.test_pass
        ]

    def test_skips_when_no_remanifestation(self) -> None:
        exp = build_recovery_experiment()
        case = next(c for c in exp.cases if c.name == "cuda_oom")
        env = recovery_task(case)
        ev = RemanifestInvariantEvaluator()
        out = ev.evaluate(
            EvaluationData(
                input=case.input,
                actual_output=env["output"],
                expected_output=case.expected_output,
                metadata=env["metadata"],
            )
        )
        # Single 'not applicable' clause, passing.
        assert len(out) == 1
        assert out[0].test_pass is True
        assert "not_applicable" in (out[0].label or "")

    def test_detects_mutated_preserved_field(self) -> None:
        ev = RemanifestInvariantEvaluator()
        original = _concept()
        revised = dict(original)
        revised["duration_sec"] = 999.0  # mutation!
        revised["prompt"] = "revised prompt"
        out = ev.evaluate(
            EvaluationData(
                input={"concept": original},
                actual_output={
                    "revised_concept": revised,
                    "recovery_log": [],
                },
                expected_output=None,
                metadata={},
            )
        )
        failed = [o for o in out if not o.test_pass]
        assert any("duration_sec" in (o.label or "") for o in failed)

    def test_log_integrity_clause_when_opted_in(self) -> None:
        ev = RemanifestInvariantEvaluator()
        original = _concept()
        revised = dict(original)
        revised["prompt"] = "revised prompt"
        out = ev.evaluate(
            EvaluationData(
                input={"concept": original},
                actual_output={
                    "revised_concept": revised,
                    "recovery_log": [
                        {"agent": "classifier", "artifact_id": "x"},
                        {"agent": "remanifester", "artifact_id": "x"},
                    ],
                },
                expected_output=None,
                metadata={"expect_exactly_one_log_per_agent": True},
            )
        )
        integrity = [o for o in out if "log_integrity" in (o.label or "")]
        assert len(integrity) == 1
        assert integrity[0].test_pass is True

    def test_log_integrity_fails_on_missing_remanifester_entry(self) -> None:
        ev = RemanifestInvariantEvaluator()
        original = _concept()
        revised = dict(original)
        revised["prompt"] = "revised prompt"
        out = ev.evaluate(
            EvaluationData(
                input={"concept": original},
                actual_output={
                    "revised_concept": revised,
                    "recovery_log": [
                        {"agent": "classifier", "artifact_id": "x"},
                    ],
                },
                expected_output=None,
                metadata={"expect_exactly_one_log_per_agent": True},
            )
        )
        integrity = [o for o in out if "log_integrity" in (o.label or "")]
        assert len(integrity) == 1
        assert integrity[0].test_pass is False


class TestContractEvaluatorsOnRecoveryTask:
    def test_classifier_contract_passes_for_all_cases(self) -> None:
        exp = build_recovery_classifier_contract_experiment()
        evaluator = exp.evaluators[0]
        assert isinstance(evaluator, ContractComplianceEvaluator)
        assert evaluator.contract is RECOVERY_CLASSIFIER_CONTRACT
        for case in exp.cases:
            env = recovery_task(case)
            out = evaluator.evaluate(
                EvaluationData(
                    input=case.input,
                    actual_output=env["output"]["classifier_state"],
                    expected_output=case.expected_output,
                    metadata=env["metadata"],
                )
            )
            assert all(o.test_pass for o in out), (
                case.name,
                [(o.label, o.reason) for o in out if not o.test_pass],
            )

    def test_remanifester_contract_passes_only_for_full_recovery_cases(
        self,
    ) -> None:
        exp = build_recovery_remanifester_contract_experiment()
        evaluator = exp.evaluators[0]
        assert isinstance(evaluator, ContractComplianceEvaluator)
        assert evaluator.contract is RECOVERY_REMANIFESTER_CONTRACT
        assert exp.cases, "remanifester contract experiment has no cases"
        for case in exp.cases:
            env = recovery_task(case)
            out = evaluator.evaluate(
                EvaluationData(
                    input=case.input,
                    actual_output=env["output"]["remanifester_state"],
                    expected_output=case.expected_output,
                    metadata=env["metadata"],
                )
            )
            assert all(o.test_pass for o in out), (
                case.name,
                [(o.label, o.reason) for o in out if not o.test_pass],
            )


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


class TestFactories:
    def test_build_diagnostic_classifier_wires_tools(self) -> None:
        agent = build_diagnostic_classifier()
        tool_names = {t.tool_name for t in agent.tool_registry.registry.values()}
        assert "classify" in tool_names
        assert "persist_classification" in tool_names

    def test_build_remanifestation_agent_wires_tools(self) -> None:
        agent = build_remanifestation_agent()
        tool_names = {t.tool_name for t in agent.tool_registry.registry.values()}
        assert "propose_revised_concept" in tool_names
        assert "diff_concept" in tool_names

    def test_build_recovery_experiment_has_8_cases(self) -> None:
        exp = build_recovery_experiment()
        assert len(exp.cases) == 8

    def test_offline_experiment_has_no_llm_evaluators(self) -> None:
        exp = build_recovery_experiment()
        names = {type(ev).__name__ for ev in exp.evaluators}
        assert "EscalationDecisionEvaluator" not in names

    def test_judge_model_adds_escalation_decision_evaluator(self) -> None:
        exp = build_recovery_experiment(judge_model="openai/gpt-4o-mini")
        names = {type(ev).__name__ for ev in exp.evaluators}
        assert "EscalationDecisionEvaluator" in names
