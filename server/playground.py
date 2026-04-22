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

from strands_agents.playground import (
    Component,
    DeclaredModel,
    EvaluatorDeclaration,
    get_component,
    iter_components,
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


__all__ = ["router"]
