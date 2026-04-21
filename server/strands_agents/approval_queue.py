"""In-process pending-interrupt queue for the dev/test operator console.

Production installs a LangGraph ``AsyncPostgresSaver`` checkpointer and
a persistent HTTP dispatcher; the in-process queue here covers two
needs:

* unit tests that drive the :func:`run_documentary` loop deterministically,
* the local dev server (``server.py``) so a single machine can exercise
  the full pipeline without a Postgres instance.

The queue is intentionally simple: one ``asyncio.Future`` per
``(run_id, interrupt_id)`` pair. ``add`` registers a pending interrupt
and returns the future the caller awaits; ``resolve`` completes the
future with the operator's :class:`ApprovalDecision`. The FastAPI
router in :mod:`server.api.approval` is a thin wrapper around these
two methods.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .approval import ApprovalDecision, validate_decision

logger = logging.getLogger(__name__)


@dataclass
class PendingInterrupt:
    """One pending interrupt as surfaced to the operator console."""

    run_id: str
    interrupt_id: str
    tool_name: str
    payload: dict[str, Any]
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable envelope for the HTTP surface."""

        return {
            "run_id": self.run_id,
            "interrupt_id": self.interrupt_id,
            "tool_name": self.tool_name,
            "payload": self.payload,
            "created_at": self.created_at,
        }


class PendingInterruptQueue:
    """Async registry of ``(run_id, interrupt_id) → future`` pairs.

    One instance per process. The FastAPI router and the pipeline
    runner share the same instance via :func:`get_default_queue`.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], PendingInterrupt] = {}
        self._futures: dict[tuple[str, str], asyncio.Future[ApprovalDecision]] = {}
        self._lock = asyncio.Lock()

    async def add(
        self,
        run_id: str,
        interrupt_id: str,
        tool_name: str,
        payload: dict[str, Any],
    ) -> asyncio.Future[ApprovalDecision]:
        """Register a pending interrupt and return the future to await.

        Args:
            run_id: The pipeline run id (usually the run_dir name).
            interrupt_id: LangGraph interrupt id.
            tool_name: The intercepted tool.
            payload: Tool-call args + context the operator sees.

        Returns:
            An :class:`asyncio.Future` that completes with the
            operator's :class:`ApprovalDecision` when
            :meth:`resolve` is called.

        Raises:
            ValueError: If the same ``(run_id, interrupt_id)`` pair
                is registered twice without resolving the first.
        """

        key = (run_id, interrupt_id)
        async with self._lock:
            if key in self._futures:
                raise ValueError(
                    f"interrupt already pending: run_id={run_id!r} "
                    f"interrupt_id={interrupt_id!r}",
                )
            loop = asyncio.get_running_loop()
            future: asyncio.Future[ApprovalDecision] = loop.create_future()
            self._pending[key] = PendingInterrupt(
                run_id=run_id,
                interrupt_id=interrupt_id,
                tool_name=tool_name,
                payload=payload,
            )
            self._futures[key] = future
            logger.info(
                "run_id=<%s>, interrupt_id=<%s>, tool_name=<%s> | queued",
                run_id,
                interrupt_id,
                tool_name,
            )
            return future

    async def resolve(
        self,
        run_id: str,
        interrupt_id: str,
        decision: ApprovalDecision,
    ) -> None:
        """Complete the future for ``(run_id, interrupt_id)``.

        Args:
            run_id: Pipeline run id.
            interrupt_id: LangGraph interrupt id.
            decision: Operator decision payload.

        Raises:
            KeyError: If no matching interrupt is registered.
            ValueError: If the decision fails :func:`validate_decision`.
        """

        key = (run_id, interrupt_id)
        async with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                raise KeyError(
                    f"no pending interrupt for run_id={run_id!r} "
                    f"interrupt_id={interrupt_id!r}",
                )
            validate_decision(pending.tool_name, decision)
            future = self._futures.pop(key)
            self._pending.pop(key, None)
            if not future.done():
                future.set_result(decision)
            logger.info(
                "run_id=<%s>, interrupt_id=<%s>, decision_type=<%s> | resolved",
                run_id,
                interrupt_id,
                decision.get("type"),
            )

    async def cancel(self, run_id: str, interrupt_id: str, reason: str) -> None:
        """Drop a pending interrupt, failing its future.

        Used when the operator walks away or the run is aborted. The
        awaiting coroutine receives a :class:`RuntimeError` so the
        run loop can surface the abort to the orchestrator.
        """

        key = (run_id, interrupt_id)
        async with self._lock:
            self._pending.pop(key, None)
            future = self._futures.pop(key, None)
            if future is not None and not future.done():
                future.set_exception(RuntimeError(f"interrupt cancelled: {reason}"))
            logger.info(
                "run_id=<%s>, interrupt_id=<%s>, reason=<%s> | cancelled",
                run_id,
                interrupt_id,
                reason,
            )

    def list_pending(self, run_id: str | None = None) -> list[PendingInterrupt]:
        """Snapshot of pending interrupts.

        Args:
            run_id: When provided, restrict to this run. Otherwise
                return every pending interrupt across runs.

        Returns:
            List of :class:`PendingInterrupt` snapshots (sorted by
            ``created_at`` ascending).
        """

        items = (
            v for k, v in self._pending.items()
            if run_id is None or k[0] == run_id
        )
        return sorted(items, key=lambda item: item.created_at)


_default_queue: PendingInterruptQueue | None = None


def get_default_queue() -> PendingInterruptQueue:
    """Return the process-wide queue singleton.

    Tests that want isolation construct their own
    :class:`PendingInterruptQueue` instance directly and pass it
    through explicitly.
    """

    global _default_queue  # noqa: PLW0603  # module-level singleton, intentional
    if _default_queue is None:
        _default_queue = PendingInterruptQueue()
    return _default_queue


def reset_default_queue() -> None:
    """Discard the process-wide queue singleton.

    Exposed so tests can guarantee a clean slate between runs.
    """

    global _default_queue  # noqa: PLW0603  # module-level singleton, intentional
    _default_queue = None


__all__ = [
    "PendingInterrupt",
    "PendingInterruptQueue",
    "get_default_queue",
    "reset_default_queue",
]
