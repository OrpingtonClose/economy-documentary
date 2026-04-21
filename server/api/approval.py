"""FastAPI operator console for the approval-gate surface — component 15.

Two JSON endpoints:

* ``GET /approval/pending`` — list pending interrupts (optionally
  scoped to a single ``run_id`` via query string).
* ``POST /approval/resume/{run_id}/{interrupt_id}`` — operator submits
  an :class:`ApprovalDecision`.

Both endpoints operate against a
:class:`server.strands_agents.approval_queue.PendingInterruptQueue`.
Production servers can wire a custom queue via
:func:`build_router`; the module-level :func:`router` factory uses the
process-wide singleton so ``server.py`` can ``include_router(router())``
without plumbing.

Resume payload format mirrors the spec (``type`` discriminator):

* ``{"type": "accept"}``
* ``{"type": "edit", "args": {...}}``
* ``{"type": "reject", "reason": "..."}``
* ``{"type": "respond", "content": ...}``

``validate_decision`` (from :mod:`server.strands_agents.approval`) is
called inside the queue's ``resolve``; a :class:`ValueError` there is
surfaced as an HTTP 400.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from strands_agents.approval import ApprovalDecision
from strands_agents.approval_queue import (
    PendingInterruptQueue,
    get_default_queue,
)

logger = logging.getLogger(__name__)


def build_router(queue: PendingInterruptQueue) -> APIRouter:
    """Construct the operator-console FastAPI router.

    Args:
        queue: The queue the router reads from / resolves against.
            Tests pass an isolated instance; production shares the
            singleton from :func:`get_default_queue`.

    Returns:
        A :class:`fastapi.APIRouter` prefixed ``/approval``.
    """

    router = APIRouter(prefix="/approval", tags=["approval"])

    @router.get("/pending")
    def list_pending(run_id: str | None = None) -> dict[str, Any]:
        """List pending interrupts.

        Args:
            run_id: Optional query param restricting to one run.

        Returns:
            ``{"pending": [...]}`` envelope where each item is the
            serialized :class:`PendingInterrupt` dict.
        """

        items = queue.list_pending(run_id)
        return {"pending": [item.to_dict() for item in items]}

    @router.post("/resume/{run_id}/{interrupt_id}")
    async def resume(
        run_id: str,
        interrupt_id: str,
        decision: ApprovalDecision,
    ) -> dict[str, Any]:
        """Submit an operator decision for a pending interrupt.

        Args:
            run_id: Pipeline run id.
            interrupt_id: LangGraph interrupt id.
            decision: Operator decision payload
                (:class:`ApprovalDecision`).

        Returns:
            ``{"status": "resolved", "decision_type": ...}``.

        Raises:
            HTTPException: 404 when no matching interrupt exists,
                400 when the decision fails validation.
        """

        try:
            await queue.resolve(run_id, interrupt_id, decision)
        except KeyError as exc:
            logger.warning(
                "run_id=<%s>, interrupt_id=<%s> | resume 404",
                run_id,
                interrupt_id,
            )
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            logger.warning(
                "run_id=<%s>, interrupt_id=<%s>, reason=<%s> | resume 400",
                run_id,
                interrupt_id,
                exc,
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "status": "resolved",
            "decision_type": decision.get("type"),
        }

    return router


def router() -> APIRouter:
    """Convenience factory wiring the process-wide queue.

    Returns:
        A router backed by :func:`get_default_queue`.
    """

    return build_router(get_default_queue())


__all__ = ["build_router", "router"]
