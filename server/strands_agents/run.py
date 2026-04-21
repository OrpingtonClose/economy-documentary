"""Async entrypoint for the documentary pipeline — components 14 + 15.

:func:`run_documentary` is the single function the CLI shim
(``run_pipeline.py --pipeline=strands``) calls once the orchestrator
lands. It builds the DeepAgent, invokes it, and yields control back to
the operator whenever an ``interrupt_on`` tool triggers a LangGraph
interrupt. The operator decision is resumed via ``Command(resume=...)``
until the graph runs to completion.

Component 15 supplies two concrete resume handlers:

* :func:`queue_operator_decision` — awaits the pending-interrupt
  queue (:mod:`server.strands_agents.approval_queue`). The FastAPI
  operator console (:mod:`server.api.approval`) resolves the queue
  when the operator submits a decision. Every resolved decision is
  audited to disk (``run_dir/approvals/resume_{id}.json``).
* :func:`_auto_reject_interrupt` — the safe CI fallback. Rejects
  every interrupt so a run with no operator attached still
  terminates.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.types import Command

from .approval import (
    ApprovalDecision,
    ApprovalRecord,
    _allowed_decisions,
    new_interrupt_id,
    resume_command_from_decision,
    write_approval_record,
    write_pending_envelope,
)
from .approval_queue import PendingInterruptQueue, get_default_queue
from .pipeline import build_documentary_orchestrator

logger = logging.getLogger(__name__)


# A resume handler sees the full graph state (including
# ``state["__interrupt__"]``) and returns the :class:`Command` that
# drives the next ``agent.ainvoke(...)``.
OperatorDecision = Callable[[dict[str, Any]], Awaitable[Command]]


_DEFAULT_OPERATOR = "ci-auto"


async def _auto_reject_interrupt(state: dict[str, Any]) -> Command:
    """Safe default: decline every interrupt so CI smoke runs terminate.

    Picks the most decline-like decision the gate permits —
    ``reject`` where allowed, otherwise ``respond`` (since
    ``request_human_approval`` only accepts ``accept`` / ``respond``).
    This keeps the payload valid under
    :func:`validate_decision` for every gate in
    :data:`INTERRUPT_GATE_CONFIG`.
    """

    interrupts = state.get("__interrupt__", [])
    try:
        _, tool_name, _ = _extract_interrupt_metadata(state)
    except RuntimeError:
        tool_name = "unknown"

    allowed = _allowed_decisions(tool_name)
    logger.warning(
        "interrupt_count=<%d>, tool=<%s> | auto-declining (no operator attached)",
        len(interrupts),
        tool_name,
    )

    if "reject" in allowed:
        return Command(
            resume={"type": "reject", "reason": "no operator attached"},
        )
    return Command(
        resume={"type": "respond", "content": "no operator attached"},
    )


def _extract_interrupt_metadata(
    state: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Return ``(interrupt_id, tool_name, payload)`` from ``__interrupt__``.

    LangGraph stashes pending interrupts at ``state["__interrupt__"]``
    as a list of :class:`langgraph.types.Interrupt` objects or dicts.
    We operate on whichever shape is there (tests hand-craft dicts;
    the real graph passes dataclass-like instances).
    """

    interrupts = state.get("__interrupt__", [])
    if not interrupts:
        raise RuntimeError("run loop saw no pending interrupt")
    interrupt = interrupts[0]

    value = (
        interrupt.get("value")
        if isinstance(interrupt, dict)
        else getattr(interrupt, "value", {}) or {}
    )
    if not isinstance(value, dict):
        value = {}

    interrupt_id: str | None = (
        interrupt.get("id")
        if isinstance(interrupt, dict)
        else getattr(interrupt, "id", None)
    )
    if not interrupt_id:
        interrupt_id = new_interrupt_id()

    tool_name = (
        value.get("tool_name")
        or value.get("action_request", {}).get("action")
        or "unknown"
    )

    payload: dict[str, Any] = {
        "args": value.get("tool_input") or value.get("args") or {},
        "description": value.get("description") or value.get("summary"),
        "allowed_decisions": value.get("allowed_decisions"),
    }
    return interrupt_id, str(tool_name), payload


def queue_operator_decision(
    run_id: str,
    run_dir: Path,
    queue: PendingInterruptQueue | None = None,
    *,
    operator: str = _DEFAULT_OPERATOR,
) -> OperatorDecision:
    """Build a resume handler backed by the pending-interrupt queue.

    Each pending interrupt is mirrored to disk (pending envelope)
    **and** registered on the queue. The coroutine awaits the queue
    future; when the operator console submits a decision via
    ``POST /approval/resume/...`` the future completes and the
    handler writes the audit record before returning the
    corresponding :class:`Command`.

    Args:
        run_id: Pipeline run id the operator console uses.
        run_dir: Run filesystem root; audit records land here.
        queue: Pending-interrupt queue. Defaults to the process-wide
            singleton (:func:`get_default_queue`).
        operator: Recorded on :class:`ApprovalRecord`. Production
            should pass the authenticated operator's email.

    Returns:
        A coroutine function suitable as ``get_operator_decision``
        in :func:`run_documentary`.
    """

    actual_queue = queue if queue is not None else get_default_queue()

    async def _handler(state: dict[str, Any]) -> Command:
        interrupt_id, tool_name, payload = _extract_interrupt_metadata(state)
        write_pending_envelope(run_dir, interrupt_id, tool_name, payload)
        future = await actual_queue.add(
            run_id=run_id,
            interrupt_id=interrupt_id,
            tool_name=tool_name,
            payload=payload,
        )
        decision: ApprovalDecision = await future
        record = ApprovalRecord(
            interrupt_id=interrupt_id,
            tool_name=tool_name,
            operator=operator,
            decision=decision,
        )
        write_approval_record(run_dir, record)
        return resume_command_from_decision(tool_name, decision)

    return _handler


def replay_operator_decisions(
    decisions: Sequence[ApprovalDecision],
    run_dir: Path | None = None,
    *,
    operator: str = "test-replay",
) -> OperatorDecision:
    """Build a resume handler that replays a pre-scripted decision list.

    Used by unit tests and experiment cases to drive the interrupt
    loop without an operator console. The ``i``-th interrupt gets the
    ``i``-th decision. Running out of decisions raises
    :class:`RuntimeError` so under-specified test cases fail loudly.

    Args:
        decisions: Ordered list of operator decisions.
        run_dir: When provided, each replay still writes an audit
            record so trajectory evaluators can assert on it.
        operator: Recorded on the audit record.

    Returns:
        A coroutine function suitable as ``get_operator_decision``
        in :func:`run_documentary`.
    """

    iterator = iter(list(decisions))

    async def _handler(state: dict[str, Any]) -> Command:
        try:
            decision = next(iterator)
        except StopIteration as exc:
            raise RuntimeError(
                "replay_operator_decisions: pre-scripted list exhausted",
            ) from exc
        interrupt_id, tool_name, _ = _extract_interrupt_metadata(state)
        if run_dir is not None:
            record = ApprovalRecord(
                interrupt_id=interrupt_id,
                tool_name=tool_name,
                operator=operator,
                decision=decision,
            )
            write_approval_record(run_dir, record)
        return resume_command_from_decision(tool_name, decision)

    return _handler


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
            :func:`_auto_reject_interrupt`. Pass
            :func:`queue_operator_decision` (or
            :func:`replay_operator_decisions` in tests) to surface
            interrupts to an operator.
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
        command = await get_operator_decision(state)
        state = await agent.ainvoke(command)

    logger.info("rounds=<%d> | pipeline complete", rounds)
    return state


__all__ = [
    "OperatorDecision",
    "queue_operator_decision",
    "replay_operator_decisions",
    "run_documentary",
]
