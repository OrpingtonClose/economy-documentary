"""Component Playground — read-only catalog endpoints.

FastAPI router that surfaces the 15 atomic components of
``server/strands_agents`` to the standalone ``frontend-playground``
workbench. Plan:
``docs/strands-migration/plans/component-playground.md``.

This module intentionally only exposes *read* endpoints in this first
increment. The run / evaluate / save-as-case endpoints arrive in later
PRs per the plan's work breakdown; when they do they'll mount here.

Nothing in :mod:`server.agui` moves. ``/playground`` is additive.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from strands_evals.case import Case
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.playground import (
    Component,
    DeclaredModel,
    DuplicateCaseNameError,
    DuplicateWorkerError,
    EvaluatorDeclaration,
    MODEL_UNREACHABLE,
    ReachabilityStatus,
    UserCase,
    VoiceAlreadyPinnedError,
    VoiceOnNonTtsWorkerError,
    Worker,
    WorkerAlreadyHasVoiceError,
    WorkerNotFoundError,
    WorkerRegistry,
    WorkerRegistryError,
    WorkerRole,
    append_user_case,
    get_component,
    get_default_registry,
    iter_components,
    load_user_cases,
    preview_diff,
    probe_models,
)
from strands_agents.playground.events import (
    Event,
    RunStream,
    get_registry,
    reset_active_stream,
    set_active_stream,
)
from strands_agents.playground.langfuse import (
    frontend_config as langfuse_frontend_config,
    langfuse_trace_url,
)
from strands_agents.playground.narrator import interpret_run, narrator_loop
from strands_agents.playground.telemetry import playground_tracer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/playground", tags=["playground"])

# Slice 9i: mount the operator-console approval router under
# ``/playground/approval/...`` so the same-origin frontend can post
# operator decisions without crossing the playground prefix
# boundary. ``api.approval`` exports ``router()`` returning a router
# pre-bound to the process-wide ``PendingInterruptQueue`` singleton.
# Tests build their own queue via :func:`api.approval.build_router`.
try:
    from api.approval import router as _approval_router_factory

    router.include_router(_approval_router_factory())
except ImportError:  # pragma: no cover - defensive; always installed in this repo
    logger.warning("api.approval router unavailable; HITL endpoints disabled")


def _serialise_model(model: DeclaredModel) -> dict[str, str]:
    return {"id": model.id, "provider": model.provider, "role": model.role}


def _serialise_evaluator(evaluator: EvaluatorDeclaration) -> dict[str, Any]:
    return {
        "name": evaluator.name,
        "threshold": evaluator.threshold,
        "hard_gate": evaluator.hard_gate,
    }


def _component_summary(component: Component) -> dict[str, Any]:
    """Lightweight serialisation for list endpoints."""
    return {
        "id": component.id,
        "title": component.title,
        "kind": component.kind,
        "row": component.row,
        "summary": component.summary,
        "declared_models": [_serialise_model(m) for m in component.declared_models],
        "evaluators": [_serialise_evaluator(e) for e in component.evaluators()],
        "case_count": len(component.cases()),
    }


_EDGE_NAME_HINTS: tuple[str, ...] = (
    "edge_",
    "_edge",
    "minor_",
    "short_",
    "noop",
    "within_",
)
_NEG_NAME_HINTS: tuple[str, ...] = (
    "failure_",
    "_failure",
    "_unreachable",
    "reject",
    "fail_",
    "_fail",
    "bad_",
    "violation",
    "over_by_",
)


def _case_role(name: str | None) -> str:
    """Classify a case name into ``pass`` / ``neg`` / ``edge``.

    Matches the chip colouring used by the test-case atlas — keeps the
    playground and the atlas in visual sync without a second hand-
    maintained list.
    """
    if not name:
        return "pass"
    lowered = name.lower()
    for hint in _NEG_NAME_HINTS:
        if hint in lowered:
            return "neg"
    for hint in _EDGE_NAME_HINTS:
        if hint in lowered:
            return "edge"
    return "pass"


def _serialise_case(case: Any) -> dict[str, Any]:
    """Project a ``strands_evals.case.Case`` into a JSON-friendly dict.

    The Case type is a Pydantic model so ``model_dump`` handles most of
    the shape; we add the playground's ``role`` classification on top.
    """
    payload = case.model_dump(mode="json", exclude_none=True)
    payload["role"] = _case_role(payload.get("name"))
    return payload


@router.get("/config/langfuse", response_class=JSONResponse)
async def get_langfuse_config() -> dict[str, Any]:
    """Return whether Langfuse observability is wired and at which host.

    The frontend polls this once on app load to decide whether to
    render the "View Trace" button next to the live status rail.
    Credentials never leave the backend — only the public host URL
    plus an ``enabled`` flag.
    """
    return langfuse_frontend_config()


@router.get("/components", response_class=JSONResponse)
async def list_components() -> dict[str, Any]:
    """Return the 15 components with metadata suitable for a sidebar."""
    components = [_component_summary(c) for c in iter_components()]
    return {"components": components, "total": len(components)}


@router.get("/components/{component_id}", response_class=JSONResponse)
async def get_component_detail(component_id: str) -> dict[str, Any]:
    """Return one component's full metadata, including its cases.

    ``cases`` contains the canonical corpus — the same cases CI runs
    against. ``user_cases`` contains anything the user has saved via
    ``POST /components/{id}/user-cases``. Both carry the ``role``
    classification so the frontend can colour their chips identically.
    """
    component = get_component(component_id)
    if component is None:
        raise HTTPException(
            status_code=404, detail=f"unknown component: {component_id}"
        )
    detail = _component_summary(component)
    detail["cases"] = [_serialise_case(c) for c in component.cases()]
    detail["user_cases"] = _user_case_payloads(component_id)
    return detail


@router.get("/components/{component_id}/cases", response_class=JSONResponse)
async def list_component_cases(component_id: str) -> dict[str, Any]:
    """Return just the case list for a component."""
    component = get_component(component_id)
    if component is None:
        raise HTTPException(
            status_code=404, detail=f"unknown component: {component_id}"
        )
    cases = [_serialise_case(c) for c in component.cases()]
    return {
        "component_id": component.id,
        "cases": cases,
        "total": len(cases),
    }


def _serialise_reachability(status: ReachabilityStatus) -> dict[str, Any]:
    return {
        "model_id": status.model_id,
        "provider": status.provider,
        "reachable": status.reachable,
        "reason": status.reason,
        "checked_at": status.checked_at,
        "latency_ms": status.latency_ms,
    }


@router.get("/components/{component_id}/models/health", response_class=JSONResponse)
async def component_models_health(component_id: str) -> dict[str, Any]:
    """Return reachability for every model the component declares.

    Deterministic components (@tool functions with no LLM) declare no
    models and get ``all_reachable=True`` trivially.

    Per the plan: unreachable models produce ``MODEL_UNREACHABLE`` in
    PR 3's run endpoint. This endpoint is the catalog-side view of the
    same status so the UI can render a green/red dot up front.
    """
    component = get_component(component_id)
    if component is None:
        raise HTTPException(
            status_code=404, detail=f"unknown component: {component_id}"
        )
    statuses = probe_models(component.declared_models)
    return {
        "component_id": component.id,
        "models": [_serialise_reachability(s) for s in statuses],
        "total": len(statuses),
        "all_reachable": all(s.reachable for s in statuses),
        "unreachable_sentinel": MODEL_UNREACHABLE,
    }


def _infer_schema(cases: list[Case[Any, Any]]) -> dict[str, Any]:
    """Return a per-key type-name schema inferred from cases.

    The playground frontend uses this to render an input editor. We
    don't claim JSON-Schema fidelity — the goal is enough structure
    to drive the form, cheap to compute, and easy to inspect.
    """
    keys: dict[str, set[str]] = {}
    for case in cases:
        payload = case.input if isinstance(case.input, dict) else {}
        for key, value in payload.items():
            keys.setdefault(key, set()).add(type(value).__name__)
    return {
        "fields": [
            {"name": name, "types": sorted(type_names)}
            for name, type_names in sorted(keys.items())
        ],
        "sample_input": (
            cases[0].input if cases and isinstance(cases[0].input, dict) else {}
        ),
    }


@router.get("/components/{component_id}/schema", response_class=JSONResponse)
async def component_schema(component_id: str) -> dict[str, Any]:
    """Return an inferred input schema for the component."""
    component = get_component(component_id)
    if component is None:
        raise HTTPException(
            status_code=404, detail=f"unknown component: {component_id}"
        )
    return {
        "component_id": component.id,
        "schema": _infer_schema(component.cases()),
    }


class RunRequest(BaseModel):
    """Body for ``POST /playground/components/{id}/run``.

    Either ``case_name`` or ``custom_input`` must be provided.
    ``custom_input`` takes precedence when both are supplied.
    """

    case_name: str | None = Field(
        default=None,
        description="Name of a registered case; looked up on the component.",
    )
    custom_input: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Arbitrary input dict. When set, bypasses the registered "
            "case and runs the component against this payload "
            "directly."
        ),
    )


RUN_STATUS_OK: str = "OK"
RUN_STATUS_MODEL_UNREACHABLE: str = MODEL_UNREACHABLE
RUN_STATUS_NO_TASK_ADAPTER: str = "NO_TASK_ADAPTER"
RUN_STATUS_TASK_ERROR: str = "TASK_ERROR"


@router.post("/components/{component_id}/run", response_class=JSONResponse)
def run_component(component_id: str, request: RunRequest) -> dict[str, Any]:
    """Run a single case against the component.

    Intentionally declared ``def`` rather than ``async def``. The
    component ``task`` callables may do blocking work (temp-dir
    allocation in ``assembly_task``, classifier inference in
    ``recovery_task``, ffmpeg in the visual path). A synchronous
    handler lets FastAPI run the whole call in its threadpool
    instead of stalling the event loop on the blocking sections.

    Order of operations:

    1. Resolve the component or 404.
    2. Probe every declared model. If the declared set is non-empty
       and **none** are reachable → return ``MODEL_UNREACHABLE`` with
       the unreachable set surfaced. The plan pins this as a hard-gate
       failure. Partial reachability (some green, some red) is not a
       gate on the run itself — the run drives one reachable model at
       a time, and the red entries are exposed on the catalog endpoint
       so the UI can flag them as discard candidates.
    3. Resolve the case (registered or custom). Unknown registered
       name → 400.
    4. Load the task adapter. Missing → ``NO_TASK_ADAPTER``.
    5. Dispatch. Exceptions return ``TASK_ERROR`` with the exception
       string — this is a debugging surface, not user-facing.

    The envelope shape matches ``strands-evals`` task envelopes so
    the PR 4 evaluator endpoint can forward the same body through
    the component's declared evaluator stack.
    """
    component = get_component(component_id)
    if component is None:
        raise HTTPException(
            status_code=404, detail=f"unknown component: {component_id}"
        )

    # A run drives ONE model at a time (picked from the declared set
    # by the component's task adapter). MODEL_UNREACHABLE fires only
    # when no declared model is reachable — that's the case where the
    # run can't proceed at all. A partially-reachable declared set
    # (e.g. canonical green, one candidate red because its key is
    # invalid) is still a valid run against the reachable model; the
    # unreachable entries surface on the catalog endpoint so the UI can
    # render the red dot and flag the model as a discard candidate,
    # per the "no artificial model pinning" rule.
    reachability = probe_models(component.declared_models)
    unreachable = [s for s in reachability if not s.reachable]
    if reachability and not any(s.reachable for s in reachability):
        return {
            "status": RUN_STATUS_MODEL_UNREACHABLE,
            "component_id": component.id,
            "unreachable_models": [_serialise_reachability(s) for s in unreachable],
            "output": None,
            "trajectory": None,
        }

    case: Case[Any, Any] | None = None
    if request.custom_input is not None:
        case_name = request.case_name or "custom_input"
        case = Case[Any, Any](
            name=case_name,
            session_id=f"playground-run-{component.id}-{case_name}",
            input=request.custom_input,
        )
    elif request.case_name is not None:
        case = _lookup_case_by_name(component, request.case_name)
        if case is None:
            user_total = len(_user_case_payloads(component.id))
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown case: {request.case_name} (component {component.id} "
                    f"has {len(component.cases())} canonical + {user_total} user cases)"
                ),
            )
    else:
        raise HTTPException(
            status_code=400,
            detail="either case_name or custom_input is required",
        )

    task = component.task()
    if task is None:
        return {
            "status": RUN_STATUS_NO_TASK_ADAPTER,
            "component_id": component.id,
            "case_name": case.name,
            "output": None,
            "trajectory": None,
        }

    try:
        result = task(case)
    except Exception as exc:  # noqa: BLE001 — we surface the error to the UI
        logger.exception("component run failed: %s/%s", component.id, case.name)
        return {
            "status": RUN_STATUS_TASK_ERROR,
            "component_id": component.id,
            "case_name": case.name,
            "error": str(exc),
            "output": None,
            "trajectory": None,
        }

    if isinstance(result, dict):
        output = result.get("output", result)
        trajectory = result.get("trajectory")
    else:
        output = result
        trajectory = None
    return {
        "status": RUN_STATUS_OK,
        "component_id": component.id,
        "case_name": case.name,
        "output": output,
        "trajectory": trajectory,
    }


class EvaluateRequest(BaseModel):
    """Body for ``POST /playground/components/{id}/evaluate``.

    The playground's typical flow is: call ``/run`` to get an output,
    then forward that output plus the originating case (or a custom
    case) here. The endpoint scores the output against every
    evaluator the component declares.

    Either ``case_name`` or ``custom_input`` must be provided.
    ``actual_output`` is required — it is the payload being judged.
    """

    case_name: str | None = Field(
        default=None,
        description="Registered case to draw expected_output / trajectory from.",
    )
    custom_input: dict[str, Any] | None = Field(
        default=None,
        description="Custom input when no registered case matches.",
    )
    custom_expected: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional custom expected_output for the evaluation. Falls "
            "back to the case's expected_output when omitted."
        ),
    )
    actual_output: Any = Field(
        ...,
        description="The output produced by the component — what we score.",
    )
    actual_trajectory: list[Any] | None = Field(
        default=None,
        description="Optional tool-call / step trajectory for TrajectoryEvaluators.",
    )


EVAL_STATUS_OK: str = "OK"
EVAL_STATUS_NO_EVALUATORS: str = "NO_EVALUATORS"
EVAL_STATUS_EVALUATOR_ERROR: str = "EVALUATOR_ERROR"


def _serialise_evaluation_output(output: EvaluationOutput) -> dict[str, Any]:
    return {
        "score": output.score,
        "test_pass": output.test_pass,
        "reason": output.reason,
        "label": output.label,
    }


def _build_evaluation_data(
    case: Case[Any, Any],
    actual_output: Any,
    actual_trajectory: list[Any] | None,
    custom_expected: dict[str, Any] | None,
) -> EvaluationData[Any, Any]:
    """Assemble the :class:`EvaluationData` each evaluator consumes.

    We copy every case-side hint the judges might need (expected
    output, expected trajectory, expected assertion, metadata) so
    deterministic and LLM evaluators get the same context the CI
    harness gives them. ``custom_expected`` wins when provided — it's
    how the workbench lets a user override the golden answer while
    keeping the case identity.
    """
    expected_output: Any = case.expected_output
    if custom_expected is not None:
        expected_output = custom_expected
    return EvaluationData(
        input=case.input,
        actual_output=actual_output,
        name=case.name,
        expected_output=expected_output,
        expected_assertion=case.expected_assertion,
        expected_trajectory=case.expected_trajectory,
        actual_trajectory=actual_trajectory,
        metadata=case.metadata,
    )


@router.post("/components/{component_id}/evaluate", response_class=JSONResponse)
def evaluate_component(component_id: str, request: EvaluateRequest) -> dict[str, Any]:
    """Score a component output against its declared evaluator stack.

    Intentionally synchronous: LLM-as-judge evaluators may make
    blocking HTTP calls to remote model APIs. Running under FastAPI's
    threadpool keeps the event loop responsive.

    Per-evaluator pass judgment: the evaluator's mean ``score`` across
    all returned :class:`EvaluationOutput` entries must be ``>=``
    the component's declared ``threshold`` from
    ``*_EVALUATOR_THRESHOLDS``. ``hard_gate`` evaluators that fail
    set ``overall_passed`` to ``False``.
    """
    component = get_component(component_id)
    if component is None:
        raise HTTPException(
            status_code=404, detail=f"unknown component: {component_id}"
        )

    if request.custom_input is not None:
        case_name = request.case_name or "custom_input"
        case: Case[Any, Any] = Case[Any, Any](
            name=case_name,
            session_id=f"playground-eval-{component.id}-{case_name}",
            input=request.custom_input,
        )
    elif request.case_name is not None:
        lookup = _lookup_case_by_name(component, request.case_name)
        if lookup is None:
            user_total = len(_user_case_payloads(component.id))
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown case: {request.case_name} (component {component.id} "
                    f"has {len(component.cases())} canonical + {user_total} user cases)"
                ),
            )
        case = lookup
    else:
        raise HTTPException(
            status_code=400,
            detail="either case_name or custom_input is required",
        )

    evaluators = component.evaluator_instances()
    declarations = {decl.name: decl for decl in component.evaluators()}
    if not evaluators:
        return {
            "status": EVAL_STATUS_NO_EVALUATORS,
            "component_id": component.id,
            "case_name": case.name,
            "results": [],
            "overall_passed": False,
        }

    data = _build_evaluation_data(
        case, request.actual_output, request.actual_trajectory, request.custom_expected
    )

    results: list[dict[str, Any]] = []
    overall_passed = True
    for evaluator in evaluators:
        name = type(evaluator).__name__
        declaration = declarations.get(name)
        threshold = declaration.threshold if declaration else 0.0
        hard_gate = declaration.hard_gate if declaration else False
        try:
            outputs = evaluator.evaluate(data)
        except Exception as exc:  # noqa: BLE001 — surface per-evaluator error
            logger.exception(
                "evaluator %s raised on %s/%s", name, component.id, case.name
            )
            # Match the non-exception branch's gate semantics: only
            # a crashing hard-gate evaluator trips overall_passed.
            # A soft evaluator crash is surfaced in its row (status
            # EVALUATOR_ERROR, passed=False, error=…) but does not
            # fail the overall evaluation, the same way a soft
            # evaluator that returns a 0.0 score does not.
            if hard_gate:
                overall_passed = False
            results.append(
                {
                    "name": name,
                    "threshold": threshold,
                    "hard_gate": hard_gate,
                    "passed": False,
                    "status": EVAL_STATUS_EVALUATOR_ERROR,
                    "error": str(exc),
                    "outputs": [],
                    "mean_score": None,
                }
            )
            continue

        scores = [o.score for o in outputs if o.score is not None]
        mean_score = sum(scores) / len(scores) if scores else 0.0
        passed = mean_score >= threshold
        if hard_gate and not passed:
            overall_passed = False
        results.append(
            {
                "name": name,
                "threshold": threshold,
                "hard_gate": hard_gate,
                "passed": passed,
                "status": EVAL_STATUS_OK,
                "mean_score": mean_score,
                "outputs": [_serialise_evaluation_output(o) for o in outputs],
            }
        )

    return {
        "status": EVAL_STATUS_OK,
        "component_id": component.id,
        "case_name": case.name,
        "results": results,
        "overall_passed": overall_passed,
    }


def _user_cases_base_dir() -> Path | None:
    """Return the configured user-cases directory, or ``None`` for default.

    Tests point ``PLAYGROUND_USER_CASES_DIR`` at a temp dir so they
    never touch the repo tree. In production the env var is unset
    and :func:`load_user_cases` falls through to
    :data:`DEFAULT_USER_CASES_DIR` (the in-repo sidecar folder).
    """
    override = os.environ.get("PLAYGROUND_USER_CASES_DIR")
    return Path(override) if override else None


def _lookup_case_by_name(component: Component, name: str) -> Case[Any, Any] | None:
    """Look ``name`` up across canonical cases and user cases.

    Canonical cases win on collision — but the POST handler rejects
    collisions at save time so the lookup order is belt-and-braces.
    Returns ``None`` when the name matches neither corpus; callers
    surface the miss as a 400 with the size of each corpus included
    in the detail so the UI can distinguish "typo" from "wrong
    component".
    """
    for case in component.cases():
        if case.name == name:
            return case
    for user_case in load_user_cases(component.id, _user_cases_base_dir()):
        if user_case.name == name:
            return user_case.to_case()
    return None


def _user_case_payloads(component_id: str) -> list[dict[str, Any]]:
    """Return serialised user cases for ``component_id``.

    The shape mirrors :func:`_serialise_case` so the frontend can
    render canonical and user cases through the same renderer; the
    ``source`` field is the only cue distinguishing the two.
    """
    cases = load_user_cases(component_id, _user_cases_base_dir())
    out: list[dict[str, Any]] = []
    for case in cases:
        payload = case.model_dump(mode="json", exclude_none=True)
        payload["source"] = "user"
        # ``role`` is already present on UserCase (pass/neg/edge) so
        # we don't re-derive it from the name like canonical cases.
        out.append(payload)
    return out


class SaveUserCaseRequest(BaseModel):
    """Body for ``POST /playground/components/{id}/user-cases``.

    ``confirm=False`` (the default) returns a preview bundle with the
    unified diff the commit would produce; ``confirm=True`` writes the
    case to disk and returns the landed payload. Splitting the two
    into the same endpoint keeps the UI flow simple: render diff,
    click commit, re-POST with ``confirm=True``.
    """

    name: str = Field(
        ...,
        description=(
            "Unique case name within the component. Alphanumerics "
            "plus ``_``/``-``; 1-64 chars."
        ),
    )
    role: str = Field(
        default="pass",
        description="One of ``pass`` / ``neg`` / ``edge``.",
    )
    input: Any = Field(
        ...,
        description="Input payload for the component under test.",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional free-form metadata.",
    )
    notes: str | None = Field(
        default=None,
        description="Optional human-facing comment shown in the UI.",
    )
    created_by: str | None = Field(
        default=None,
        description="Optional attribution (e.g. CLI user).",
    )
    confirm: bool = Field(
        default=False,
        description=(
            "When ``True`` the case is written to disk. When ``False`` "
            "(default) only the diff preview is returned."
        ),
    )


@router.get("/components/{component_id}/user-cases", response_class=JSONResponse)
async def list_user_cases(component_id: str) -> dict[str, Any]:
    """Return just the user-authored cases for a component."""
    component = get_component(component_id)
    if component is None:
        raise HTTPException(
            status_code=404, detail=f"unknown component: {component_id}"
        )
    payloads = _user_case_payloads(component.id)
    return {
        "component_id": component.id,
        "user_cases": payloads,
        "total": len(payloads),
    }


@router.post("/components/{component_id}/user-cases", response_class=JSONResponse)
def save_user_case(component_id: str, request: SaveUserCaseRequest) -> dict[str, Any]:
    """Preview or commit a user-authored case.

    The endpoint enforces two disjoint pre-conditions:

    1. The requested name must not collide with a canonical case.
       Canonical cases are the source of truth for CI and we never
       let a user entry shadow one.
    2. The requested name must not collide with an existing user
       case. Append-only is a product decision: editing a case
       belongs in a PR review, not in the workbench.

    A violation of either rule returns ``409 Conflict`` before the
    preview / commit paths diverge so both modes produce the same
    rejection for the same input.
    """
    component = get_component(component_id)
    if component is None:
        raise HTTPException(
            status_code=404, detail=f"unknown component: {component_id}"
        )

    canonical_names = {c.name for c in component.cases() if c.name}
    if request.name in canonical_names:
        raise HTTPException(
            status_code=409,
            detail=(
                f"name collides with canonical case {request.name!r}; "
                "pick a different name or edit the canonical case "
                "upstream instead."
            ),
        )

    try:
        new_case = UserCase(
            name=request.name,
            role=request.role,
            input=request.input,
            metadata=request.metadata or {},
            notes=request.notes,
            created_by=request.created_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base_dir = _user_cases_base_dir()

    # Stamp the case once up front. ``UserCase.stamped`` is idempotent
    # when ``created_at`` is already set, so the inner calls inside
    # :func:`preview_diff` and :func:`append_user_case` now become
    # no-ops and both paths serialise the same timestamp. Without this
    # a ``confirm=True`` save returned a ``preview`` bundle whose
    # ``after`` / ``diff`` carried the preview-time timestamp while
    # the ``case`` payload (and the on-disk file) carried the commit-
    # time timestamp — contradictory ``created_at`` values in one
    # response body.
    new_case = new_case.stamped()

    try:
        preview = preview_diff(component.id, new_case, base_dir)
    except DuplicateCaseNameError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not request.confirm:
        return {
            "component_id": component.id,
            "committed": False,
            "preview": preview,
        }

    try:
        stamped = append_user_case(component.id, new_case, base_dir)
    except DuplicateCaseNameError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    payload = stamped.model_dump(mode="json", exclude_none=True)
    payload["source"] = "user"
    return {
        "component_id": component.id,
        "committed": True,
        "preview": preview,
        "case": payload,
    }


# --------------------------------------------------------------------------
# Worker fleet registry
#
# Workers self-register against `/playground/workers` on boot, heartbeat
# periodically, and pin (or have pinned) a single TTS voice when they
# are TTS workers. The playground consumes this registry to pre-flight
# VRAM before entering the Production stage -- see
# ``server/strands_agents/playground/worker_registry.py`` for the
# invariants enforced here.
# --------------------------------------------------------------------------


class _RegisterWorkerRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=64)
    role: str = Field(..., description="tts | ltx_render | assembly")
    endpoint_url: str = Field(..., min_length=1, max_length=512)
    vram_gb: int = Field(..., gt=0, le=4096)
    voice_id: str | None = Field(default=None, min_length=1, max_length=128)


class _HeartbeatRequest(BaseModel):
    free_vram_gb: int | None = Field(default=None, ge=0, le=4096)


class _PinVoiceRequest(BaseModel):
    voice_id: str = Field(..., min_length=1, max_length=128)


def _serialise_worker(worker: Worker, *, registry: WorkerRegistry) -> dict[str, Any]:
    stale = registry.is_stale(worker)
    last_probe = worker.last_probe
    return {
        "worker_id": worker.worker_id,
        "role": worker.role,
        "endpoint_url": worker.endpoint_url,
        "vram_gb": worker.vram_gb,
        "voice_id": worker.voice_id,
        "registered_at": worker.registered_at,
        "last_heartbeat_at": worker.last_heartbeat_at,
        "stale": stale,
        "last_probe": (
            None
            if last_probe is None
            else {
                "total_gb": last_probe.total_gb,
                "free_gb": last_probe.free_gb,
                "compute_capability": (
                    None
                    if last_probe.compute_capability is None
                    else list(last_probe.compute_capability)
                ),
                "probed_at": last_probe.probed_at,
            }
        ),
    }


def _workers_registry_error_response(err: WorkerRegistryError) -> HTTPException:
    """Map registry-raised errors to HTTP status codes with stable reason codes."""

    if isinstance(err, WorkerNotFoundError):
        return HTTPException(
            status_code=404, detail={"reason": "worker_not_found", "message": str(err)}
        )
    if isinstance(err, DuplicateWorkerError):
        return HTTPException(
            status_code=409, detail={"reason": "duplicate_worker", "message": str(err)}
        )
    if isinstance(err, VoiceAlreadyPinnedError):
        return HTTPException(
            status_code=409,
            detail={
                "reason": "voice_already_pinned",
                "voice_id": err.voice_id,
                "other_worker_id": err.other_worker_id,
                "message": str(err),
            },
        )
    if isinstance(err, WorkerAlreadyHasVoiceError):
        return HTTPException(
            status_code=409,
            detail={
                "reason": "worker_already_has_voice",
                "existing_voice_id": err.existing_voice_id,
                "new_voice_id": err.new_voice_id,
                "message": str(err),
            },
        )
    if isinstance(err, VoiceOnNonTtsWorkerError):
        return HTTPException(
            status_code=400,
            detail={
                "reason": "voice_on_non_tts_worker",
                "role": err.role,
                "message": str(err),
            },
        )
    return HTTPException(
        status_code=400, detail={"reason": "registry_error", "message": str(err)}
    )


@router.get("/workers", response_class=JSONResponse)
async def list_workers(role: str | None = None) -> dict[str, Any]:
    """Snapshot of the fleet. Used by the playground card to render the
    VRAM dot alongside the existing model-reachability dot.
    """

    registry = get_default_registry()
    role_filter: WorkerRole | None = None
    if role is not None:
        if role not in ("tts", "ltx_render", "assembly"):
            raise HTTPException(
                status_code=400,
                detail={"reason": "unknown_role", "role": role},
            )
        role_filter = role  # type: ignore[assignment]
    workers = [
        _serialise_worker(w, registry=registry)
        for w in registry.iter_workers(role=role_filter)
    ]
    return {
        "workers": workers,
        "total": len(workers),
        "by_role": {
            r: sum(1 for w in workers if w["role"] == r)
            for r in ("tts", "ltx_render", "assembly")
        },
    }


@router.post("/workers", response_class=JSONResponse)
async def register_worker(request: _RegisterWorkerRequest) -> dict[str, Any]:
    """Register a worker. Called by each worker VM's bootstrap script
    once its local router is listening on ``endpoint_url``.
    """

    registry = get_default_registry()
    if request.role not in ("tts", "ltx_render", "assembly"):
        raise HTTPException(
            status_code=400,
            detail={"reason": "unknown_role", "role": request.role},
        )
    try:
        worker = registry.register_worker(
            worker_id=request.worker_id,
            role=request.role,  # type: ignore[arg-type]
            endpoint_url=request.endpoint_url,
            vram_gb=request.vram_gb,
            voice_id=request.voice_id,
        )
    except WorkerRegistryError as err:
        raise _workers_registry_error_response(err) from err
    return _serialise_worker(worker, registry=registry)


@router.delete("/workers/{worker_id}", response_class=JSONResponse)
async def unregister_worker(worker_id: str) -> dict[str, Any]:
    registry = get_default_registry()
    try:
        registry.unregister_worker(worker_id)
    except WorkerRegistryError as err:
        raise _workers_registry_error_response(err) from err
    return {"worker_id": worker_id, "unregistered": True}


@router.post("/workers/{worker_id}/heartbeat", response_class=JSONResponse)
async def heartbeat_worker(
    worker_id: str, request: _HeartbeatRequest
) -> dict[str, Any]:
    registry = get_default_registry()
    try:
        registry.heartbeat(worker_id, free_vram_gb=request.free_vram_gb)
        worker = registry.get_worker(worker_id)
    except WorkerRegistryError as err:
        raise _workers_registry_error_response(err) from err
    return _serialise_worker(worker, registry=registry)


@router.post("/workers/{worker_id}/voice", response_class=JSONResponse)
async def pin_worker_voice(worker_id: str, request: _PinVoiceRequest) -> dict[str, Any]:
    registry = get_default_registry()
    try:
        registry.pin_voice(worker_id, request.voice_id)
        worker = registry.get_worker(worker_id)
    except WorkerRegistryError as err:
        raise _workers_registry_error_response(err) from err
    return _serialise_worker(worker, registry=registry)


@router.get("/models/health", response_class=JSONResponse)
async def all_models_health() -> dict[str, Any]:
    """Return reachability for every declared model across all components.

    Models shared between components are deduplicated by ``model_id``,
    so a single probe result is reported once even if two components
    declare the same model — keeps the sidebar's badge counts honest.
    """
    seen: dict[str, DeclaredModel] = {}
    for component in iter_components():
        for model in component.declared_models:
            seen.setdefault(model.id, model)
    statuses = probe_models(seen.values())
    return {
        "models": [_serialise_reachability(s) for s in statuses],
        "total": len(statuses),
        "all_reachable": all(s.reachable for s in statuses),
        "unreachable_sentinel": MODEL_UNREACHABLE,
    }


# --------------------------------------------------------------------------
# Event-driven run endpoints
#
# The sync ``POST /components/{id}/run`` above stays for programmatic /
# curl callers: one request in, one terminal JSON body out. The UI
# needs a different shape — a live feedback line backed by structured
# events + an LLM narrator — so it drives the run through the
# ``runs`` family below:
#
#   POST /components/{id}/runs   → {run_id} (immediate)
#   GET  /runs/{run_id}          → current state (polling)
#   GET  /runs/{run_id}/events   → Server-Sent Events stream
#
# The run itself is dispatched on a background task; the sync task
# adapter is hosted on a worker thread so it can emit ``emit_sync``
# events onto the main loop without blocking FastAPI.
# --------------------------------------------------------------------------


_EVENT_STREAM_HEARTBEAT_SECONDS: float = 3.0
_EVENT_STREAM_MAX_SECONDS: float = 900.0


def _serialise_event(event: Event) -> dict[str, Any]:
    return event.to_dict()


def _serialise_run_state(stream: RunStream) -> dict[str, Any]:
    trace_url = (
        langfuse_trace_url(stream.trace_id) if stream.trace_id is not None else None
    )
    return {
        "run_id": stream.run_id,
        "component_id": stream.component_id,
        "case_name": stream.case_name,
        "created_at": stream.created_at,
        "closed": stream.closed,
        "events": [_serialise_event(e) for e in stream.snapshot()],
        "terminal": stream.terminal,
        "trace_id": stream.trace_id,
        "trace_url": trace_url,
    }


def _start_run_root_span(stream: RunStream) -> Any:
    """Open the root OTel span for a run and pin its trace id onto ``stream``.

    Returns a context manager the dispatcher ``with``-blocks on, or
    ``None`` when OTel is not available. On entry the stream's
    ``trace_id`` is populated so ``_serialise_run_state`` can surface
    it to the frontend before the first event lands. On exit the
    span closes normally — subsequent Strands / ADK child spans still
    nest under this root via OTel's context propagation, which is
    what gives Langfuse a single trace tree per playground run.
    """
    tracer = playground_tracer()
    if tracer is None:
        return None
    span_cm = tracer.start_as_current_span(
        "playground.run",
        attributes={
            "playground.run_id": stream.run_id,
            "playground.component_id": stream.component_id,
            "playground.case_name": stream.case_name or "",
        },
    )
    span = span_cm.__enter__()
    try:
        ctx = span.get_span_context()
        stream.trace_id = format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001 — telemetry must never crash the run
        pass
    return span_cm


async def _dispatch_run(
    *,
    stream: RunStream,
    component: Component,
    case: Case[Any, Any],
) -> None:
    """Run the component and pump events into ``stream``.

    Executes the synchronous task adapter in a worker thread so the
    FastAPI loop keeps serving SSE / state reads. Terminates the
    stream with exactly one of ``run.ok`` / ``run.error`` /
    ``run.cancelled`` and closes it with a terminal payload.
    """
    loop = asyncio.get_running_loop()
    stream.attach_loop(loop)

    run_span_cm = _start_run_root_span(stream)

    narrator_task = asyncio.create_task(narrator_loop(stream))

    # Defensive default. If anything in the ``try`` body raises before we
    # assign a more specific terminal payload (e.g. ``probe_models``
    # itself blows up, or an ``await stream.emit`` fails under load),
    # the ``finally`` block still has a well-formed payload to close
    # the stream with — no ``UnboundLocalError`` and no SSE clients
    # left hanging on a stream that never closes.
    terminal: dict[str, Any] = {
        "status": RUN_STATUS_TASK_ERROR,
        "component_id": component.id,
        "case_name": case.name,
        "error": "run failed with an unexpected internal error",
        "error_class": "UnexpectedError",
        "output": None,
        "trajectory": None,
    }
    try:
        # Reachability gate.
        await stream.emit(
            "probe.start",
            f"probing {len(component.declared_models)} declared model(s)",
            detail={"count": len(component.declared_models)},
        )
        probe_start = time.perf_counter()
        reachability = await asyncio.to_thread(probe_models, component.declared_models)
        probe_elapsed_ms = int((time.perf_counter() - probe_start) * 1000)
        for status in reachability:
            await stream.emit(
                "probe.done",
                (
                    f"{'reachable' if status.reachable else 'unreachable'}: "
                    f"{status.model_id} ({status.reason})"
                ),
                detail={
                    "model_id": status.model_id,
                    "provider": status.provider,
                    "reachable": status.reachable,
                    "reason": status.reason,
                    "latency_ms": status.latency_ms,
                },
            )
        if reachability and not any(s.reachable for s in reachability):
            unreachable = [
                _serialise_reachability(s) for s in reachability if not s.reachable
            ]
            await stream.emit(
                "run.error",
                "all declared models unreachable",
                detail={
                    "status": RUN_STATUS_MODEL_UNREACHABLE,
                    "probe_elapsed_ms": probe_elapsed_ms,
                },
            )
            terminal = {
                "status": RUN_STATUS_MODEL_UNREACHABLE,
                "component_id": component.id,
                "case_name": case.name,
                "unreachable_models": unreachable,
                "output": None,
                "trajectory": None,
            }
            return

        # Task adapter gate.
        task = component.task()
        if task is None:
            await stream.emit(
                "run.error",
                "no task adapter registered for this component",
                detail={"status": RUN_STATUS_NO_TASK_ADAPTER},
            )
            terminal = {
                "status": RUN_STATUS_NO_TASK_ADAPTER,
                "component_id": component.id,
                "case_name": case.name,
                "output": None,
                "trajectory": None,
            }
            return

        # Dispatch.
        await stream.emit(
            "task.start",
            f"dispatching {component.id}",
            detail={"component_id": component.id, "case": case.name},
        )
        task_start = time.perf_counter()

        # Task adapters run on a worker thread via ``asyncio.to_thread``.
        # Copying the current context into the worker and binding the
        # stream inside it lets adapters discover the active stream
        # (via :func:`strands_agents.playground.events.get_active_stream`)
        # and register playground hooks against it — without widening
        # every adapter's signature. The binding is torn down
        # automatically when the copied context goes out of scope.
        def _run_task_with_stream() -> Any:
            token = set_active_stream(stream)
            try:
                return task(case)
            finally:
                reset_active_stream(token)

        ctx = contextvars.copy_context()
        try:
            result = await asyncio.to_thread(ctx.run, _run_task_with_stream)
        except Exception as exc:  # noqa: BLE001 — surface to UI
            elapsed_ms = int((time.perf_counter() - task_start) * 1000)
            logger.exception("component run failed: %s/%s", component.id, case.name)
            await stream.emit(
                "run.error",
                f"{type(exc).__name__}: {exc}",
                detail={
                    "status": RUN_STATUS_TASK_ERROR,
                    "error_class": type(exc).__name__,
                    "error": str(exc),
                    "elapsed_ms": elapsed_ms,
                },
            )
            terminal = {
                "status": RUN_STATUS_TASK_ERROR,
                "component_id": component.id,
                "case_name": case.name,
                "error": str(exc),
                "error_class": type(exc).__name__,
                "output": None,
                "trajectory": None,
            }
            return

        elapsed_ms = int((time.perf_counter() - task_start) * 1000)
        if isinstance(result, dict):
            output = result.get("output", result)
            trajectory = result.get("trajectory")
        else:
            output = result
            trajectory = None
        await stream.emit(
            "task.done",
            f"{component.id} completed in {elapsed_ms}ms",
            detail={
                "elapsed_ms": elapsed_ms,
                "trajectory_len": len(trajectory)
                if isinstance(trajectory, list)
                else 0,
            },
        )
        await stream.emit(
            "run.ok",
            "run completed",
            detail={"elapsed_ms": elapsed_ms},
        )
        terminal = {
            "status": RUN_STATUS_OK,
            "component_id": component.id,
            "case_name": case.name,
            "output": output,
            "trajectory": trajectory,
        }

        # Post-run LLM interpretation. Best effort — if the narrator
        # model is unreachable or the LLM call raises, we still close
        # the run cleanly.
        interpretation = await interpret_run(
            stream, output=output, evaluator_scores=None
        )
        if interpretation:
            terminal["interpretation"] = interpretation

    except asyncio.CancelledError:
        await stream.emit("run.cancelled", "run cancelled by client")
        terminal = {
            "status": "CANCELLED",
            "component_id": component.id,
            "case_name": case.name,
            "output": None,
            "trajectory": None,
        }
        raise
    finally:
        narrator_task.cancel()
        try:
            await narrator_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if run_span_cm is not None:
            try:
                run_span_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 — telemetry must never crash the run
                logger.debug("run span close failed", exc_info=True)
        # ``terminal`` is seeded with a defensive default at the top of
        # the function and overwritten on every normal control-flow
        # path, so close() always sees a well-formed payload.
        await stream.close(terminal=terminal)


class StartRunRequest(BaseModel):
    """Body for ``POST /components/{id}/runs``.

    Same semantics as :class:`RunRequest` — either a registered case
    name or a custom input. The split into a separate type leaves
    room for future async-only parameters (e.g. ``narrator: bool``)
    without disturbing the synchronous endpoint's shape.
    """

    case_name: str | None = Field(default=None)
    custom_input: dict[str, Any] | None = Field(default=None)


@router.post("/components/{component_id}/runs", response_class=JSONResponse)
async def start_run(
    component_id: str,
    request: StartRunRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Allocate a run_id, kick off the dispatcher, return immediately.

    The returned ``run_id`` is used by the frontend to subscribe to
    ``GET /runs/{run_id}/events`` for live narration and to
    ``GET /runs/{run_id}`` for a polling fallback.
    """
    component = get_component(component_id)
    if component is None:
        raise HTTPException(
            status_code=404, detail=f"unknown component: {component_id}"
        )

    if request.custom_input is not None:
        case_name = request.case_name or "custom_input"
        case: Case[Any, Any] = Case[Any, Any](
            name=case_name,
            session_id=f"playground-run-{component.id}-{case_name}",
            input=request.custom_input,
        )
    elif request.case_name is not None:
        found = _lookup_case_by_name(component, request.case_name)
        if found is None:
            user_total = len(_user_case_payloads(component.id))
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown case: {request.case_name} (component {component.id} "
                    f"has {len(component.cases())} canonical + {user_total} user cases)"
                ),
            )
        case = found
    else:
        raise HTTPException(
            status_code=400,
            detail="either case_name or custom_input is required",
        )

    registry = get_registry()
    stream = registry.new_run(component_id=component.id, case_name=case.name)
    await stream.emit(
        "run.dispatched",
        f"queued {component.id} / {case.name}",
        detail={"component_id": component.id, "case": case.name},
    )
    # BackgroundTasks runs after the response is sent. For a real
    # HTTP client this is immediate — the frontend can open the SSE
    # subscription as soon as the POST returns. For TestClient the
    # background task completes before the POST returns, which is
    # the pattern we lean on in unit tests.
    background_tasks.add_task(
        _dispatch_run, stream=stream, component=component, case=case
    )
    return {
        "run_id": stream.run_id,
        "component_id": component.id,
        "case_name": case.name,
        "events_url": f"/playground/runs/{stream.run_id}/events",
        "state_url": f"/playground/runs/{stream.run_id}",
    }


@router.get("/runs/{run_id}", response_class=JSONResponse)
async def get_run(run_id: str) -> dict[str, Any]:
    """Polling fallback: return the current state of a run."""
    stream = get_registry().get(run_id)
    if stream is None:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
    return _serialise_run_state(stream)


@router.get("/runs/{run_id}/events")
async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    """Server-Sent Events stream of one run's event bus.

    Emission format is one ``data: <json>\\n\\n`` per event. Heartbeats
    (``:heartbeat\\n\\n`` comments) are injected every
    ``_EVENT_STREAM_HEARTBEAT_SECONDS`` so intermediaries don't close
    an idle connection. Clients watch for the ``run.ok`` /
    ``run.error`` / ``run.cancelled`` event kinds to know the run is
    done; the server also closes the stream cleanly once the terminal
    event has been delivered.
    """
    stream = get_registry().get(run_id)
    if stream is None:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")

    async def event_iter() -> Any:
        # Replay any events that landed before the client connected.
        last_seq = 0
        for event in stream.snapshot():
            payload = json.dumps(_serialise_event(event))
            yield f"data: {payload}\n\n"
            last_seq = max(last_seq, event.seq)

        deadline = time.time() + _EVENT_STREAM_MAX_SECONDS
        while time.time() < deadline:
            if await request.is_disconnected():
                return
            tail = await stream.wait_for_after(
                last_seq, timeout=_EVENT_STREAM_HEARTBEAT_SECONDS
            )
            if tail:
                for event in tail:
                    payload = json.dumps(_serialise_event(event))
                    yield f"data: {payload}\n\n"
                    last_seq = max(last_seq, event.seq)
            else:
                # heartbeat comment — SSE comments start with ':' and
                # are never surfaced to the client as an event.
                yield ":heartbeat\n\n"
            if stream.closed and not tail:
                return

    return StreamingResponse(
        event_iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # nginx: don't buffer SSE
            "Connection": "keep-alive",
        },
    )


@router.get("/runs", response_class=JSONResponse)
async def list_recent_runs(
    limit: int = 20,
    component_id: str | None = None,
) -> dict[str, Any]:
    """Return the registry's most-recent runs as flat summaries.

    Powers the ``/pipeline`` page's recent-runs sidebar so a UI
    driver that lost their browser process (or just opened a fresh
    tab) can find an in-flight run and re-attach via
    ``?run_id=<id>``. Filters to ``component_id`` when supplied so
    the pipeline sidebar isn't polluted with c01..c15 component
    runs from the same registry.

    The summary intentionally lifts ``topic`` /
    ``target_duration_sec`` / ``language`` off the
    ``run.dispatched`` event detail so the sidebar can render
    something more useful than a 16-hex run id without an extra
    round-trip per row.
    """
    capped_limit = max(1, min(int(limit) if limit else 20, 64))
    #: When ``component_id`` is supplied, the registry's "last N" view
    #: would silently drop matching runs whenever non-matching ones
    #: occupied the head — e.g. a sidebar requesting 20 pipeline runs
    #: would see only 5 if the 15 most-recent registry entries were
    #: c01..c15 component runs. Fetch the registry's full retention
    #: window (64) and apply the trim *after* filtering so callers
    #: always get up to ``limit`` matching runs.
    fetch_limit = 64 if component_id is not None else capped_limit
    streams = list(get_registry().recent(limit=fetch_limit))
    streams.reverse()  # most-recent first
    runs: list[dict[str, Any]] = []
    for stream in streams:
        if component_id is not None and stream.component_id != component_id:
            continue
        if len(runs) >= capped_limit:
            break
        snapshot = stream.snapshot()
        last_event = snapshot[-1] if snapshot else None
        dispatched = next(
            (e for e in snapshot if e.kind == "run.dispatched"),
            None,
        )
        topic = None
        target_duration_sec = None
        language = None
        if dispatched is not None:
            detail = dispatched.detail or {}
            raw_topic = detail.get("topic")
            if isinstance(raw_topic, str):
                topic = raw_topic
            raw_duration = detail.get("target_duration_sec")
            if isinstance(raw_duration, (int, float)):
                target_duration_sec = int(raw_duration)
            raw_language = detail.get("language")
            if isinstance(raw_language, str):
                language = raw_language
        terminal_status = (
            stream.terminal.get("status") if stream.terminal else None
        )
        runs.append(
            {
                "run_id": stream.run_id,
                "component_id": stream.component_id,
                "case_name": stream.case_name,
                "created_at": stream.created_at,
                "closed": stream.closed,
                "terminal_status": terminal_status,
                "event_count": len(snapshot),
                "last_event_ts": last_event.ts if last_event else None,
                "last_event_kind": last_event.kind if last_event else None,
                "topic": topic,
                "target_duration_sec": target_duration_sec,
                "language": language,
            }
        )
    return {"runs": runs}


# --------------------------------------------------------------------------
# Pipeline runs — drive the documentary pipeline end-to-end from the
# same RunStream surface that powers /components. The dispatcher
# always invokes the real DeepAgent orchestrator + real worker tools
# + real LLM-backed QA gates. CI mocks the worker HTTP boundary; it
# does not substitute a different runner.
# --------------------------------------------------------------------------


# Minimum / maximum target durations the dispatcher accepts. Real
# documentaries fall in roughly the 30s..600s window; clamping at the
# API surface stops a 1-second probe or a 10-hour typo from reaching
# the stream.
_PIPELINE_MIN_DURATION_SEC: int = 30
_PIPELINE_MAX_DURATION_SEC: int = 600
_PIPELINE_TOPIC_MAX_LEN: int = 200
_PIPELINE_LANGUAGE_MAX_LEN: int = 16

# Slice 9f: scene-count clamp. Matches
# ``pipeline_live_demo._DEMO_MIN_SCENES`` / ``_DEMO_MAX_SCENES``.
_PIPELINE_MIN_NUM_SCENES: int = 1
_PIPELINE_MAX_NUM_SCENES: int = 6

#: Component id used for pipeline runs in the run registry. Distinct
#: from the per-component ids so ``GET /runs/<id>`` can disambiguate
#: a pipeline run from a c01..c15 / infra component run.
PIPELINE_RUN_COMPONENT_ID: str = "pipeline"


#: In-memory map of ``run_id`` -> ``master.mp4`` path on disk.
#: Populated by :func:`_dispatch_pipeline_run` once the assembly
#: leaf reports a master.mp4 and consumed by the
#: :func:`stream_master_mp4` route so the UI can play the master
#: directly without depending on a B2 round-trip.
_PIPELINE_MASTER_PATHS: dict[str, Path] = {}


@router.get("/runs/{run_id}/master.mp4")
async def stream_master_mp4(run_id: str) -> FileResponse:
    """Serve the master ``.mp4`` for a completed pipeline run.

    The orchestrator writes ``master.mp4`` into the run's temporary
    artifacts directory; this route exposes it under a stable
    same-origin URL so the ``/pipeline`` UI can drop the path
    straight into a ``<video>`` tag without depending on B2 being
    reachable.
    """
    path = _PIPELINE_MASTER_PATHS.get(run_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="master.mp4 not found")
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        filename="master.mp4",
        headers={"Accept-Ranges": "bytes"},
    )


class StartPipelineRunRequest(BaseModel):
    """Body for ``POST /playground/pipeline/runs``.

    Mirrors the documentary pipeline's user-prompt surface: topic,
    target duration, and language. Defaults match the orchestrator's
    own defaults so a frontend can submit an empty form and still
    get a sensible run.

    There is no "mode" toggle: the pipeline always drives the real
    DeepAgent orchestrator against real workers + real LLM-backed QA
    gates. Tests use httpx-mocked workers, never a code-level
    "simulator mode".
    """

    topic: str = Field(default="The Federal Reserve")
    target_duration_sec: int = Field(default=60)
    language: str = Field(default="en")
    # Optional scene-count override. When ``None``, the orchestrator
    # derives N from ``target_duration_sec`` (~12s/scene, clamped to
    # ``[1, 6]``).
    num_scenes: int | None = Field(default=None)


def _normalise_pipeline_request(
    request: StartPipelineRunRequest,
) -> tuple[str, int, str, int | None]:
    """Validate + clamp pipeline-run inputs, raising HTTPException on bad input.

    Returns the cleaned
    ``(topic, target_duration_sec, language, num_scenes)`` tuple
    ready to pass to :class:`PipelineRun`. ``num_scenes`` is ``None``
    when the caller didn't override it; the orchestrator derives N
    from duration in that case.
    """
    topic = (request.topic or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="topic is required")
    if len(topic) > _PIPELINE_TOPIC_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=(
                f"topic too long: {len(topic)} chars (max {_PIPELINE_TOPIC_MAX_LEN})"
            ),
        )
    duration = int(request.target_duration_sec)
    if duration < _PIPELINE_MIN_DURATION_SEC or duration > _PIPELINE_MAX_DURATION_SEC:
        raise HTTPException(
            status_code=400,
            detail=(
                f"target_duration_sec out of range: {duration} "
                f"(allowed {_PIPELINE_MIN_DURATION_SEC}.."
                f"{_PIPELINE_MAX_DURATION_SEC})"
            ),
        )
    language = (request.language or "en").strip() or "en"
    if len(language) > _PIPELINE_LANGUAGE_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=(
                f"language tag too long: {len(language)} chars "
                f"(max {_PIPELINE_LANGUAGE_MAX_LEN})"
            ),
        )
    num_scenes: int | None
    if request.num_scenes is None:
        num_scenes = None
    else:
        num_scenes = int(request.num_scenes)
        if (
            num_scenes < _PIPELINE_MIN_NUM_SCENES
            or num_scenes > _PIPELINE_MAX_NUM_SCENES
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"num_scenes out of range: {num_scenes} "
                    f"(allowed {_PIPELINE_MIN_NUM_SCENES}.."
                    f"{_PIPELINE_MAX_NUM_SCENES})"
                ),
            )
    return topic, duration, language, num_scenes


async def _dispatch_pipeline_run(
    *,
    stream: RunStream,
    topic: str,
    target_duration_sec: int,
    language: str,
    num_scenes: int | None = None,
) -> None:
    """Drive a pipeline run against ``stream`` and close it.

    Always drives the real :func:`build_orchestrator` agent through
    the :class:`PipelineRun` runner, observing every tool call +
    interrupt the orchestrator emits and translating them onto
    ``stream``. There is no scripted-replay fallback path: workers
    are mocked at the HTTP boundary in CI, never substituted at the
    code level.
    """
    from strands_agents.playground.pipeline_live_demo import (
        build_demo_live_agent,
    )
    from strands_agents.playground.pipeline_live_hitl import (
        maybe_build_pipeline_hitl_operator,
    )
    from strands_agents.playground.pipeline_live_runner import (
        LivePipelineRun,
    )

    loop = asyncio.get_running_loop()
    stream.attach_loop(loop)

    run_span_cm = _start_run_root_span(stream)

    terminal: dict[str, Any] = {
        "status": RUN_STATUS_TASK_ERROR,
        "component_id": stream.component_id,
        "case_name": stream.case_name,
        "error": "pipeline run failed with an unexpected internal error",
        "error_class": "UnexpectedError",
        "output": None,
        "trajectory": None,
    }
    run_dir: Path | None = None
    try:
        run_started = time.perf_counter()
        run_dir = Path(tempfile.mkdtemp(prefix="pipeline_run_"))
        agent = build_demo_live_agent(
            run_dir,
            topic=topic,
            target_duration_sec=target_duration_sec,
            language=language,
            num_scenes=num_scenes,
        )
        # When ``ENABLE_PIPELINE_HITL`` is set, swap the default
        # ``auto_accept_interrupt`` for a queue-backed operator handler
        # so the playground gates pause until the operator console
        # (``POST /playground/approval/resume/{run_id}/{interrupt_id}``)
        # submits a real decision. Unset env keeps the legacy
        # auto-accept demo for CI / unattended runs.
        operator_decision = maybe_build_pipeline_hitl_operator(
            run_id=stream.run_id,
            run_dir=run_dir,
        )
        live_runner = LivePipelineRun(
            topic=topic,
            target_duration_sec=target_duration_sec,
            language=language,
            agent=agent,
            run_dir=run_dir,
            # Small per-event delay so the UI sees a progress-shaped
            # timeline rather than a wall of events in one frame.
            per_event_delay_s=0.15,
            run_id=stream.run_id,
            operator_decision=operator_decision,
        )
        result = await live_runner.run(stream)
        elapsed_ms = int((time.perf_counter() - run_started) * 1000)
        # Register the master.mp4 location so
        # ``GET /playground/runs/{run_id}/master.mp4`` can stream it
        # back to the UI. We copy the file into a stable, run-id keyed
        # path under ``/tmp/pipeline_masters`` so the temp run-dir can
        # be cleaned up without taking the playable mp4 with it.
        master_src = run_dir / "artifacts" / "master.mp4"
        if master_src.exists():
            stable_dir = Path(tempfile.gettempdir()) / "pipeline_masters"
            stable_dir.mkdir(parents=True, exist_ok=True)
            stable_path = stable_dir / f"{stream.run_id}.mp4"
            try:
                shutil.copyfile(str(master_src), str(stable_path))
                _PIPELINE_MASTER_PATHS[stream.run_id] = stable_path
                logger.info(
                    "run_id=<%s>, src=<%s>, stable=<%s> | master.mp4 registered for streaming",
                    stream.run_id,
                    master_src,
                    stable_path,
                )
            except Exception:  # noqa: BLE001 — telemetry-only
                logger.exception(
                    "run_id=<%s>, src=<%s> | master.mp4 stable copy failed",
                    stream.run_id,
                    master_src,
                )
        await stream.emit(
            "run.ok",
            "pipeline run completed",
            detail={
                "elapsed_ms": elapsed_ms,
                "stage_count": result.get("stage_count"),
                "event_count": result.get("event_count"),
                "final_mp4_b2_url": result.get("final_mp4_b2_url"),
            },
        )
        terminal = {
            "status": RUN_STATUS_OK,
            "component_id": stream.component_id,
            "case_name": stream.case_name,
            "output": result,
            "trajectory": None,
        }
    except asyncio.CancelledError:
        await stream.emit("run.cancelled", "pipeline run cancelled")
        terminal = {
            "status": "CANCELLED",
            "component_id": stream.component_id,
            "case_name": stream.case_name,
            "output": None,
            "trajectory": None,
        }
        raise
    except Exception as exc:  # noqa: BLE001 — surface to UI
        logger.exception(
            "pipeline run failed: topic=%s duration=%d", topic, target_duration_sec
        )
        await stream.emit(
            "run.error",
            f"{type(exc).__name__}: {exc}",
            detail={
                "status": RUN_STATUS_TASK_ERROR,
                "error_class": type(exc).__name__,
                "error": str(exc),
            },
        )
        terminal = {
            "status": RUN_STATUS_TASK_ERROR,
            "component_id": stream.component_id,
            "case_name": stream.case_name,
            "error": str(exc),
            "error_class": type(exc).__name__,
            "output": None,
            "trajectory": None,
        }
    finally:
        if run_span_cm is not None:
            try:
                run_span_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 — telemetry must never crash the run
                logger.debug("pipeline run span close failed", exc_info=True)
        await stream.close(terminal=terminal)
        if run_dir is not None:
            if os.environ.get("KEEP_RUN_DIR", "").strip().lower() in ("1", "true", "yes", "on"):
                logger.info("run_dir=<%s> | KEEP_RUN_DIR set, preserving", run_dir)
            else:
                shutil.rmtree(run_dir, ignore_errors=True)


@router.post("/pipeline/runs", response_class=JSONResponse)
async def start_pipeline_run(
    request: StartPipelineRunRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Allocate a pipeline run and kick off the orchestrator.

    The frontend ``/pipeline`` page submits a topic / duration /
    language form here, then subscribes to the same SSE surface as
    ``/components`` runs:

    * ``GET /playground/runs/<run_id>/events`` — live event stream.
    * ``GET /playground/runs/<run_id>``        — polling fallback.

    There is no scripted-replay or simulator path: the dispatcher
    always drives the real DeepAgent orchestrator. Tests mock the
    HTTP boundary against worker endpoints; they do not substitute
    a different runner.
    """
    topic, duration, language, num_scenes = _normalise_pipeline_request(request)
    case_name = "pipeline_run"

    registry = get_registry()
    stream = registry.new_run(
        component_id=PIPELINE_RUN_COMPONENT_ID, case_name=case_name
    )
    await stream.emit(
        "run.dispatched",
        f"queued pipeline run: {topic!r} ({duration}s, {language})",
        detail={
            "component_id": PIPELINE_RUN_COMPONENT_ID,
            "topic": topic,
            "target_duration_sec": duration,
            "language": language,
            "num_scenes": num_scenes,
        },
    )
    background_tasks.add_task(
        _dispatch_pipeline_run,
        stream=stream,
        topic=topic,
        target_duration_sec=duration,
        language=language,
        num_scenes=num_scenes,
    )
    return {
        "run_id": stream.run_id,
        "component_id": PIPELINE_RUN_COMPONENT_ID,
        "case_name": case_name,
        "topic": topic,
        "target_duration_sec": duration,
        "language": language,
        "num_scenes": num_scenes,
        "events_url": f"/playground/runs/{stream.run_id}/events",
        "state_url": f"/playground/runs/{stream.run_id}",
    }


__all__ = ["router"]
