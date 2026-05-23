"""Strands-evals experiment for the Component Playground save-as-case flow.

Exercises the user-cases surface that lands in PR 8 of
``docs/strands-migration/plans/component-playground.md``:

* ``GET /playground/components/{id}/user-cases`` (empty + populated);
* ``POST /playground/components/{id}/user-cases`` preview (``confirm=False``);
* ``POST /playground/components/{id}/user-cases`` commit (``confirm=True``);
* Duplicate-name rejection (409 against both canonical and user corpora);
* Invalid name / role / component id rejection;
* Round-trip: a committed case is runnable by name via the existing
  ``/run`` endpoint — proves the save path threads through
  :class:`Component.cases` lookup without a code change on the run side.

The task runs against a :class:`fastapi.testclient.TestClient` that
mounts only the playground router. ``PLAYGROUND_USER_CASES_DIR`` is
redirected to a process-local temp directory so the experiment never
writes into the repo tree.

Idiomatic shape mirrors ``playground_catalog.py``:

    from strands_agents.evals.experiments.playground_user_cases import (
        build_playground_user_cases_experiment,
        playground_user_cases_task,
    )

    experiment = build_playground_user_cases_experiment()
    reports = experiment.run_evaluations(task=playground_user_cases_task)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from strands_evals.case import Case  # type: ignore[import-not-found]
from strands_evals.experiment import Experiment  # type: ignore[import-not-found]

from playground import router as playground_router  # type: ignore[attr-defined]
from strands_agents.evals.experiments.playground_catalog import (
    SubsetMatchEvaluator,
)

#: The user-cases experiment reuses the catalog's hard-gate subset
#: evaluator. We do not introduce a new threshold class here — the
#: save-as-case surface is structural, not semantic.
PLAYGROUND_USER_CASES_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "SubsetMatchEvaluator": (1.0, True),
}


def _build_user_cases_app() -> FastAPI:
    app = FastAPI(title="playground-user-cases-eval")
    app.include_router(playground_router)
    return app


#: Every experiment run points the user-cases sidecar at a fresh
#: temp directory so test case writes never pollute the repo tree.
#: A single directory is reused across all cases in one experiment so
#: "commit → re-list" sequences see each other's writes.
_STATE: dict[str, Any] = {"tmp_dir": None, "client": None}


def _ensure_client() -> TestClient:
    if _STATE["client"] is None:
        tmp = tempfile.mkdtemp(prefix="playground-user-cases-")
        os.environ["PLAYGROUND_USER_CASES_DIR"] = tmp
        _STATE["tmp_dir"] = tmp
        _STATE["client"] = TestClient(_build_user_cases_app())
    return _STATE["client"]


def reset_experiment_state() -> None:
    """Drop the cached client + temp dir so a re-run starts empty.

    Tests call this between runs so the user-corpus assertions stay
    deterministic — e.g. the "list is empty before commit" case only
    holds when no earlier case has already committed.
    """
    tmp = _STATE.get("tmp_dir")
    if tmp:
        try:
            for path in Path(tmp).glob("*.json"):
                path.unlink(missing_ok=True)
        except OSError:
            pass
    _STATE["client"] = None
    _STATE["tmp_dir"] = None
    os.environ.pop("PLAYGROUND_USER_CASES_DIR", None)


def playground_user_cases_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Dispatch the case's HTTP call and return ``{"output": {status, body}}``.

    Supports both ``GET`` and ``POST`` so the whole lifecycle
    (list-empty → preview → commit → list-populated → run) can be
    described declaratively by case inputs.
    """
    client = _ensure_client()
    method = case.input["method"].upper()
    path = case.input["path"]
    body = case.input.get("body")
    if method == "GET":
        response = client.get(path)
    else:
        response = client.request(method, path, json=body)
    try:
        parsed: Any = response.json()
    except ValueError:
        parsed = response.text
    return {"output": {"status": response.status_code, "body": parsed}}


def _case(
    name: str,
    method: str,
    path: str,
    expected_status: int,
    expected_body: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Case[dict[str, Any], dict[str, Any]]:
    """Assemble one HTTP-replay case with subset-match expectations."""
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"playground-user-cases-{name}",
        input={"method": method, "path": path, "body": body},
        expected_output={
            "status": expected_status,
            "body": expected_body if expected_body is not None else {},
        },
    )


def playground_user_cases_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Build the case corpus for the save-as-case surface.

    Ordering matters — the commit cases run before the list-populated
    cases, and the run-by-name case runs last. The strands-evals
    runner iterates in declaration order, which is how we get a
    deterministic sequence without a separate test harness.
    """
    reset_experiment_state()
    component = "c02"  # timing evaluator: @tool, no model deps, always runnable.
    # Pick a canonical case name we can test the collision branch
    # against — any real case name in ``c02`` works; we just want to
    # prove the save handler rejects shadows of canonical entries.
    canonical_collision = "intent_exact"

    cases: list[Case[dict[str, Any], dict[str, Any]]] = []

    # 1. An empty user corpus round-trips as []
    cases.append(
        _case(
            name="user_corpus_starts_empty",
            method="GET",
            path=f"/playground/components/{component}/user-cases",
            expected_status=200,
            expected_body={"component_id": component, "total": 0},
        )
    )

    # 2. Preview without confirm returns diff + does not write
    preview_body = {
        "name": "user_preview_only",
        "role": "edge",
        "input": {
            "scenes": [],
            "alignment": [],
            "target_duration_sec": 300.0,
        },
        "notes": "preview-only request",
    }
    cases.append(
        _case(
            name="preview_returns_diff_and_not_committed",
            method="POST",
            path=f"/playground/components/{component}/user-cases",
            expected_status=200,
            expected_body={
                "component_id": component,
                "committed": False,
                "preview": {"case_count_after": 1, "existed": False},
            },
            body=preview_body,
        )
    )

    # 3. Preview alone doesn't populate GET list
    cases.append(
        _case(
            name="user_corpus_still_empty_after_preview_only",
            method="GET",
            path=f"/playground/components/{component}/user-cases",
            expected_status=200,
            expected_body={"component_id": component, "total": 0},
        )
    )

    # 4. Commit (confirm=True) writes
    commit_body = {
        "name": "user_committed_edge",
        "role": "edge",
        "input": {
            "scenes": [
                {"script": "short scene", "duration_sec": 10.0},
            ],
            "alignment": [
                {"start": 0.0, "end": 10.0, "text": "short scene"},
            ],
            "target_duration_sec": 10.0,
        },
        "notes": "committed by user-cases experiment",
        "confirm": True,
    }
    cases.append(
        _case(
            name="commit_writes_case",
            method="POST",
            path=f"/playground/components/{component}/user-cases",
            expected_status=200,
            expected_body={
                "component_id": component,
                "committed": True,
                "case": {"name": "user_committed_edge", "source": "user"},
            },
            body=commit_body,
        )
    )

    # 5. After commit the GET listing reflects the write
    cases.append(
        _case(
            name="user_corpus_populated_after_commit",
            method="GET",
            path=f"/playground/components/{component}/user-cases",
            expected_status=200,
            expected_body={
                "component_id": component,
                "total": 1,
                "user_cases": [
                    {"name": "user_committed_edge", "source": "user"},
                ],
            },
        )
    )

    # 6. Duplicate-name commit is rejected
    cases.append(
        _case(
            name="duplicate_user_name_rejected",
            method="POST",
            path=f"/playground/components/{component}/user-cases",
            expected_status=409,
            expected_body={},
            body={**commit_body, "notes": "second attempt"},
        )
    )

    # 7. Canonical collision is rejected
    cases.append(
        _case(
            name="canonical_name_collision_rejected",
            method="POST",
            path=f"/playground/components/{component}/user-cases",
            expected_status=409,
            expected_body={},
            body={
                "name": canonical_collision,
                "role": "pass",
                "input": {},
                "confirm": True,
            },
        )
    )

    # 8. Invalid role is rejected (422 by Pydantic? No — we run it
    # through our own validator, which returns ``422`` from FastAPI's
    # request-model parsing. Subset-match only checks ``status``.)
    cases.append(
        _case(
            name="invalid_role_rejected",
            method="POST",
            path=f"/playground/components/{component}/user-cases",
            expected_status=400,
            expected_body={},
            body={
                "name": "user_bad_role",
                "role": "not_a_role",
                "input": {},
                "confirm": True,
            },
        )
    )

    # 9. Invalid name (spaces) is rejected
    cases.append(
        _case(
            name="invalid_name_rejected",
            method="POST",
            path=f"/playground/components/{component}/user-cases",
            expected_status=400,
            expected_body={},
            body={
                "name": "bad name with spaces",
                "role": "pass",
                "input": {},
                "confirm": True,
            },
        )
    )

    # 10. Unknown component returns 404 on preview and commit
    cases.append(
        _case(
            name="unknown_component_preview_404",
            method="POST",
            path="/playground/components/c99/user-cases",
            expected_status=404,
            expected_body={},
            body={
                "name": "user_any",
                "role": "pass",
                "input": {},
            },
        )
    )

    # 11. Unknown component returns 404 on GET list
    cases.append(
        _case(
            name="unknown_component_list_404",
            method="GET",
            path="/playground/components/c99/user-cases",
            expected_status=404,
            expected_body={},
        )
    )

    # 12. Committed user case replays through the /run endpoint by
    # name. c02 timing task returns a structured timing report with
    # a ``timing_passed`` field; we only assert the top-level shape.
    cases.append(
        _case(
            name="committed_case_runnable_by_name",
            method="POST",
            path=f"/playground/components/{component}/run",
            expected_status=200,
            expected_body={
                "status": "OK",
                "component_id": component,
                "case_name": "user_committed_edge",
            },
            body={"case_name": "user_committed_edge"},
        )
    )

    # 13. Committed user case shows up in ``/components/{id}`` detail
    cases.append(
        _case(
            name="committed_case_appears_in_detail",
            method="GET",
            path=f"/playground/components/{component}",
            expected_status=200,
            expected_body={
                "id": component,
                "user_cases": [
                    {"name": "user_committed_edge", "source": "user"},
                ],
            },
        )
    )

    return cases


def build_playground_user_cases_experiment() -> Experiment[Any, Any]:
    """Assemble the :class:`Experiment` the runner consumes."""
    return Experiment(
        cases=playground_user_cases_cases(),
        evaluators=[SubsetMatchEvaluator()],
    )


__all__ = ["build_playground_user_cases_experiment",
    "playground_user_cases_cases",
    "playground_user_cases_task",
    "reset_experiment_state",]
