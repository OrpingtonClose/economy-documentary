"""Async entrypoint for the documentary pipeline — component 14.

:func:`run_documentary` is the single function the CLI shim
(``run_pipeline.py --pipeline=strands``) calls once the orchestrator
lands. It builds the DeepAgent, invokes it, and yields control back to
the operator whenever an ``interrupt_on`` tool triggers a LangGraph
interrupt. The operator decision is resumed via ``Command(resume=...)``
until the graph runs to completion.

The resume handler (``get_operator_decision``) is injected; component 15
(approval gates) supplies the production implementation. The default
(:func:`_auto_reject_interrupt`) rejects every interrupt, which is safe
for CI smoke runs — real runs must override it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.types import Command

from .pipeline import build_documentary_orchestrator

logger = logging.getLogger(__name__)


OperatorDecision = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def _auto_reject_interrupt(state: dict[str, Any]) -> dict[str, Any]:
    """Safe default: reject every interrupt so CI smoke runs terminate."""

    interrupts = state.get("__interrupt__", [])
    logger.warning(
        "interrupt_count=<%d> | auto-rejecting (component 15 not wired yet)",
        len(interrupts),
    )
    return {"decision": "reject"}


async def run_documentary(
    brief: str,
    run_dir: Path,
    *,
    model: str | BaseChatModel | None = None,
    get_operator_decision: OperatorDecision = _auto_reject_interrupt,
    max_interrupt_rounds: int = 32,
) -> dict[str, Any]:
    """Drive the orchestrator to completion, resuming through interrupts.

    Args:
        brief: The user's natural-language request.
        run_dir: Filesystem root passed to the DeepAgent.
        model: Optional chat model override (id or instance).
        get_operator_decision: Async handler invoked with the current
            graph state every time an interrupt fires. Defaults to
            :func:`_auto_reject_interrupt` — component 15 replaces it
            with a real operator console.
        max_interrupt_rounds: Safety cap so a misbehaving agent cannot
            loop on interrupts forever.

    Returns:
        The final graph state dict.

    Raises:
        RuntimeError: If the interrupt loop exceeds
            ``max_interrupt_rounds``.
    """

    agent = build_documentary_orchestrator(run_dir, model=model)
    state = await agent.ainvoke({"messages": [("user", brief)]})

    rounds = 0
    while "__interrupt__" in state:
        rounds += 1
        if rounds > max_interrupt_rounds:
            raise RuntimeError(
                f"interrupt loop exceeded max rounds ({max_interrupt_rounds})",
            )
        logger.info("round=<%d> | resolving interrupt", rounds)
        decision = await get_operator_decision(state)
        state = await agent.ainvoke(Command(resume=decision))

    logger.info("rounds=<%d> | pipeline complete", rounds)
    return state


__all__ = [
    "OperatorDecision",
    "run_documentary",
]
