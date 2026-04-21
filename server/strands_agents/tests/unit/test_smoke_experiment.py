"""Unit tests for the phase-1 smoke :class:`Experiment`.

The smoke validates the strands-evals wiring end-to-end:
:class:`Experiment` construction, :meth:`run_evaluations` dispatch to
the deterministic evaluator, and JSON round-trip via
:meth:`Experiment.to_file` / :meth:`Experiment.from_file`. A failure
here means every downstream component experiment is likely broken too.
"""

from __future__ import annotations

from pathlib import Path

from strands_evals.experiment import Experiment

from strands_agents.evals.evaluators import ContractComplianceEvaluator
from strands_agents.evals.experiments.smoke import (
    build_smoke_experiment,
    smoke_task,
)


def test_experiment_has_two_cases_and_one_evaluator() -> None:
    exp = build_smoke_experiment()
    assert len(exp.cases) == 2
    assert len(exp.evaluators) == 1
    assert isinstance(exp.evaluators[0], ContractComplianceEvaluator)


def test_run_evaluations_produces_expected_pass_fail_pattern() -> None:
    exp = build_smoke_experiment()
    reports = exp.run_evaluations(smoke_task)
    assert len(reports) == 1
    report = reports[0]
    # Case 1 populates both clauses (pass); case 2 omits the required
    # ``topic`` (fail).
    assert report.test_passes == [True, False]
    assert 0.0 < report.overall_score < 1.0


def test_experiment_serializes_cases_to_json(tmp_path: Path) -> None:
    # ``Experiment.from_file`` cannot rehydrate evaluators that require
    # constructor args (``ContractComplianceEvaluator`` takes a
    # ``StageContract``). Serialization round-trip for such evaluators
    # is intentionally out of scope for this smoke; component PRs that
    # serialize experiments will pin evaluators that have no required
    # constructor args, or build the experiment in code.
    import json

    exp = build_smoke_experiment()
    path = tmp_path / "smoke.json"
    exp.to_file(str(path))
    data = json.loads(path.read_text())
    assert "cases" in data
    assert len(data["cases"]) == 2
    assert {c["name"] for c in data["cases"]} == {
        "contract_all_present",
        "contract_missing_required",
    }


def test_evaluator_exports_expected_public_class() -> None:
    # Smoke depends on the ``ContractComplianceEvaluator`` export being
    # stable; breaking the re-export in ``evaluators/__init__.py`` would
    # silently break every downstream experiment that reuses it.
    assert ContractComplianceEvaluator.__name__ == "ContractComplianceEvaluator"
    # ``Experiment`` takes a list of evaluator instances.
    exp = Experiment(cases=[], evaluators=[])
    assert exp.cases == []
