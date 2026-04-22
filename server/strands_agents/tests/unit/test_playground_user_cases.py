"""Unit tests for the Component Playground save-as-case surface.

The Strands experiment in ``playground_user_cases.py`` covers the
happy-path lifecycle against a real ``TestClient``. These unit tests
pin the pieces that are awkward to express as HTTP cases:

* :class:`UserCase` validation (bad name, bad role) surfaces as
  ``ValueError`` before the storage layer is touched.
* :func:`preview_diff` round-trips deterministically when the sidecar
  file does not yet exist (the first-ever save of a case is the
  trickiest diff to get right).
* :func:`load_user_cases` tolerates malformed files — a corrupted
  sidecar must not 500 the catalog endpoint.
* The Strands experiment passes every case with a hard-gated
  ``SubsetMatchEvaluator``, matching the CI contract used by
  ``test_playground_catalog.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from strands_agents.evals.experiments.playground_user_cases import (
    PLAYGROUND_USER_CASES_EVALUATOR_THRESHOLDS,
    build_playground_user_cases_experiment,
    playground_user_cases_task,
    reset_experiment_state,
)
from strands_agents.playground import (
    DuplicateCaseNameError,
    UserCase,
    append_user_case,
    load_user_cases,
    preview_diff,
)


def test_user_case_rejects_whitespace_in_name() -> None:
    with pytest.raises(ValueError, match="name must match"):
        UserCase(name="bad name", input={})


def test_user_case_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="role must be one of"):
        UserCase(name="good_name", role="mystery", input={})


def test_preview_diff_on_empty_file_reports_file_not_yet_existing(
    tmp_path: Path,
) -> None:
    case = UserCase(name="first_case", role="pass", input={"x": 1})
    preview = preview_diff("c02", case, base_dir=tmp_path)
    assert preview["existed"] is False
    assert preview["case_count_before"] == 0
    assert preview["case_count_after"] == 1
    # A file that didn't previously exist still produces a diff that
    # can be applied — the unified-diff ``/dev/null`` convention is
    # handled by the ``before=""`` branch.
    assert "first_case" in preview["after"]
    assert preview["before"] == ""
    # ``diff`` must include the new-case name so the UI can show
    # the user what's about to land.
    assert "first_case" in preview["diff"]


def test_preview_diff_preserves_existing_cases(tmp_path: Path) -> None:
    append_user_case(
        "c02",
        UserCase(name="existing_case", role="pass", input={"y": 2}),
        base_dir=tmp_path,
    )
    preview = preview_diff(
        "c02",
        UserCase(name="second_case", role="edge", input={"z": 3}),
        base_dir=tmp_path,
    )
    assert preview["existed"] is True
    assert preview["case_count_before"] == 1
    assert preview["case_count_after"] == 2
    assert "existing_case" in preview["after"]
    assert "second_case" in preview["after"]


def test_append_is_append_only(tmp_path: Path) -> None:
    append_user_case(
        "c02",
        UserCase(name="one", input={}),
        base_dir=tmp_path,
    )
    with pytest.raises(DuplicateCaseNameError):
        append_user_case(
            "c02",
            UserCase(name="one", input={"changed": True}),
            base_dir=tmp_path,
        )


def test_append_stamps_created_at(tmp_path: Path) -> None:
    stamped = append_user_case(
        "c02",
        UserCase(name="timestamped", input={}),
        base_dir=tmp_path,
    )
    assert stamped.created_at is not None
    # Re-load and confirm the stamp persisted verbatim.
    reloaded = load_user_cases("c02", base_dir=tmp_path)
    assert [c.name for c in reloaded] == ["timestamped"]
    assert reloaded[0].created_at == stamped.created_at


def test_malformed_sidecar_returns_empty_list(tmp_path: Path) -> None:
    (tmp_path / "c02.json").write_text("{not valid json", encoding="utf-8")
    assert load_user_cases("c02", base_dir=tmp_path) == []


def test_sidecar_with_wrong_root_type_returns_empty_list(tmp_path: Path) -> None:
    (tmp_path / "c02.json").write_text(json.dumps({"not": "a list"}))
    assert load_user_cases("c02", base_dir=tmp_path) == []


def test_sidecar_drops_malformed_entries_but_keeps_valid_ones(
    tmp_path: Path,
) -> None:
    (tmp_path / "c02.json").write_text(
        json.dumps(
            [
                {"name": "valid", "role": "pass", "input": {"k": 1}},
                {"name": "also valid", "role": "pass", "input": {}},
                # Invalid: bad name (spaces). Should be dropped but not raise.
                {"name": "invalid entry", "role": "pass", "input": {}},
                {"name": "also_valid", "role": "edge", "input": {"k": 2}},
            ]
        )
    )
    cases = load_user_cases("c02", base_dir=tmp_path)
    assert [c.name for c in cases] == ["valid", "also_valid"]


def test_save_user_case_preview_and_commit_share_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``preview`` and ``case`` in one response must share ``created_at``.

    ``UserCase.stamped`` fills ``created_at`` at call time. Previously
    the endpoint called it twice — once inside ``preview_diff`` and
    once inside ``append_user_case`` — so the response's diff payload
    and the committed case carried different ``created_at`` values and
    the diff never matched what landed on disk. The endpoint now
    stamps once up front; this test pins that contract.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from playground import router as playground_router

    monkeypatch.setenv("PLAYGROUND_USER_CASES_DIR", str(tmp_path))

    app = FastAPI()
    app.include_router(playground_router)
    client = TestClient(app)

    response = client.post(
        "/playground/components/c02/user-cases",
        json={
            "name": "stamped_once",
            "role": "pass",
            "input": {"k": 1},
            "confirm": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    committed_stamp = body["case"]["created_at"]
    assert committed_stamp is not None
    assert committed_stamp in body["preview"]["after"], (
        "preview.after must embed the same timestamp as the committed case"
    )
    assert committed_stamp in body["preview"]["diff"], (
        "preview.diff must embed the same timestamp as the committed case"
    )
    # And the on-disk file agrees.
    reloaded = load_user_cases("c02", base_dir=tmp_path)
    assert [c.created_at for c in reloaded] == [committed_stamp]


def test_user_cases_experiment_passes_every_case() -> None:
    reset_experiment_state()
    try:
        experiment = build_playground_user_cases_experiment()
        reports = experiment.run_evaluations(task=playground_user_cases_task)
    finally:
        reset_experiment_state()

    assert len(reports) == 1
    report = reports[0]
    assert all(report.test_passes), [
        (case["name"], reason)
        for case, passed, reason in zip(
            report.cases, report.test_passes, report.reasons
        )
        if not passed
    ]
    threshold, hard_gate = PLAYGROUND_USER_CASES_EVALUATOR_THRESHOLDS[
        report.evaluator_name
    ]
    assert hard_gate is True
    assert report.overall_score >= threshold
