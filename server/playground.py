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

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from strands_evals.case import Case
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.playground import (
    Component,
    DeclaredModel,
    EvaluatorDeclaration,
    MODEL_UNREACHABLE,
    ReachabilityStatus,
    get_component,
    get_default_cache,
    iter_components,
    probe_models,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/playground", tags=["playground"])


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


@router.get("/components", response_class=JSONResponse)
async def list_components() -> dict[str, Any]:
    """Return the 15 components with metadata suitable for a sidebar."""
    components = [_component_summary(c) for c in iter_components()]
    return {"components": components, "total": len(components)}


@router.get("/components/{component_id}", response_class=JSONResponse)
async def get_component_detail(component_id: str) -> dict[str, Any]:
    """Return one component's full metadata, including its cases."""
    component = get_component(component_id)
    if component is None:
        raise HTTPException(status_code=404, detail=f"unknown component: {component_id}")
    detail = _component_summary(component)
    detail["cases"] = [_serialise_case(c) for c in component.cases()]
    return detail


@router.get("/components/{component_id}/cases", response_class=JSONResponse)
async def list_component_cases(component_id: str) -> dict[str, Any]:
    """Return just the case list for a component."""
    component = get_component(component_id)
    if component is None:
        raise HTTPException(status_code=404, detail=f"unknown component: {component_id}")
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


@router.get(
    "/components/{component_id}/models/health", response_class=JSONResponse
)
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
        raise HTTPException(status_code=404, detail=f"unknown component: {component_id}")
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
        raise HTTPException(status_code=404, detail=f"unknown component: {component_id}")
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
    2. Probe every declared model. Any unreachable → return
       ``MODEL_UNREACHABLE`` with the unreachable set surfaced. The
       plan pins this as a hard-gate failure, not a degradation.
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
        raise HTTPException(status_code=404, detail=f"unknown component: {component_id}")

    reachability = probe_models(component.declared_models)
    unreachable = [s for s in reachability if not s.reachable]
    if unreachable:
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
        cases_by_name = {c.name: c for c in component.cases()}
        case = cases_by_name.get(request.case_name)
        if case is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown case: {request.case_name} (component {component.id} "
                    f"has {len(cases_by_name)} cases)"
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
def evaluate_component(
    component_id: str, request: EvaluateRequest
) -> dict[str, Any]:
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
        raise HTTPException(status_code=404, detail=f"unknown component: {component_id}")

    if request.custom_input is not None:
        case_name = request.case_name or "custom_input"
        case: Case[Any, Any] = Case[Any, Any](
            name=case_name,
            session_id=f"playground-eval-{component.id}-{case_name}",
            input=request.custom_input,
        )
    elif request.case_name is not None:
        cases_by_name = {c.name: c for c in component.cases()}
        lookup = cases_by_name.get(request.case_name)
        if lookup is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown case: {request.case_name} (component {component.id} "
                    f"has {len(cases_by_name)} cases)"
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


__all__ = ["router"]
