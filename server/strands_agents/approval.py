"""Operator approval tool — component 15 placeholder.

Component 15 will flesh this out into a first-class LangGraph interrupt
surface with a FastAPI operator console. For now this tool returns a
structured ``pending`` envelope so the orchestrator (component 14) can
wire it through ``interrupt_on``. The DeepAgent's
``HumanInTheLoopMiddleware`` turns the call into a LangGraph interrupt
before this body ever runs in production — the body below is only
reached in unit tests that bypass the middleware.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool
def request_human_approval(
    summary: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Request operator review before a sensitive action proceeds.

    Args:
        summary: One-sentence description the operator sees verbatim.
            Must be non-empty; the orchestrator is responsible for
            writing a human-readable summary (see the
            ``EscalationDecision.human_summary`` contract).
        payload: Structured context the operator can drill into
            (diagnostic dict, scene ids, cost estimates, etc.).

    Returns:
        Envelope with ``status="pending"`` plus the summary and
        payload. Component 15 will replace this with a real interrupt
        gated on operator response.
    """

    logger.info(
        "summary=<%s>, payload_keys=<%s> | request_human_approval placeholder",
        summary,
        list((payload or {}).keys()),
    )
    return {
        "status": "pending",
        "summary": summary,
        "payload": payload or {},
    }


__all__ = ["request_human_approval"]
