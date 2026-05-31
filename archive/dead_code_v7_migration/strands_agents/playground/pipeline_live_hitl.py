"""HITL (human-in-the-loop) wiring for ``/pipeline?mode=live`` — slice 9i.

The slice-9a :class:`LivePipelineRun` defaults to
:func:`auto_accept_interrupt`, which auto-approves every gate so the
playground demo runs end-to-end without an operator. That auto-approval
hides the fact that the orchestrator's hard invariant 9 ("approval
gates are binding") only holds when a real operator handler is wired
in.

This module bridges that gap. When the playground dispatcher detects
:func:`is_pipeline_hitl_enabled` it builds a queue-backed operator
decision callable via :func:`build_pipeline_hitl_operator` and passes
it to :class:`LivePipelineRun`. Each gate then:

1. Mirrors the pending interrupt onto :class:`PendingInterruptQueue`
   (process-wide singleton via :func:`get_default_queue`).
2. Persists a pending envelope under ``run_dir/approvals/`` so a
   restarted operator console can re-surface it.
3. Awaits the queue future. The operator console resolves the future
   by ``POST /playground/approval/resume/{run_id}/{interrupt_id}``;
   the resume payload is validated against
   :data:`INTERRUPT_GATE_CONFIG` before the future completes.
4. Writes an :class:`ApprovalRecord` audit line and returns the
   resume :class:`Command` to the runner.

The env gate :envvar:`ENABLE_PIPELINE_HITL` keeps the default
behaviour (auto-accept) for CI and the local demo. Production /
staging dispatchers set the gate so gates are binding for real.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from strands_agents.approval_queue import (
    PendingInterruptQueue,
    get_default_queue,
)
from strands_agents.run import queue_operator_decision

if TYPE_CHECKING:
    from strands_agents.playground.pipeline_live_runner import OperatorDecision


logger = logging.getLogger(__name__)


PIPELINE_HITL_ENV_VAR = "ENABLE_PIPELINE_HITL"
"""Env var the dispatcher reads to decide between auto-accept and queue."""


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_pipeline_hitl_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return ``True`` when the queue-backed HITL should fire.

    The env var is read case-insensitively. Empty / unset / any value
    not in :data:`_TRUTHY` returns ``False`` so the playground demo
    keeps its auto-accept default.

    Args:
        env: Optional override mapping. Defaults to :data:`os.environ`
            so production paths read live env. Tests pass a dict to
            isolate from process state.

    Returns:
        ``True`` iff the env var is set to a truthy literal.
    """

    source = env if env is not None else os.environ
    raw = source.get(PIPELINE_HITL_ENV_VAR, "")
    return raw.strip().lower() in _TRUTHY


def build_pipeline_hitl_operator(
    run_id: str,
    run_dir: Path,
    *,
    queue: PendingInterruptQueue | None = None,
    operator: str = "playground-operator",
) -> "OperatorDecision":
    """Construct the queue-backed operator decision for a live run.

    Thin wrapper around :func:`queue_operator_decision` that also
    pre-creates the ``approvals/`` subdirectory under ``run_dir`` so
    the first gate does not race the directory creation in
    :func:`write_pending_envelope`.

    Args:
        run_id: The playground :class:`RunStream` id. The frontend
            uses this id when posting to
            ``POST /playground/approval/resume/{run_id}/{interrupt_id}``;
            it must match the id surfaced on the
            ``pipeline.approval_gate`` SSE event.
        run_dir: Filesystem root the live runner already owns. The
            operator handler writes pending envelopes + audit
            records under ``{run_dir}/approvals/``.
        queue: Pending-interrupt queue. Defaults to
            :func:`get_default_queue` so the dispatcher and the
            ``/approval`` HTTP router share one process-wide
            instance. Tests pass an isolated queue.
        operator: Recorded on every :class:`ApprovalRecord`.
            Production should pass an authenticated identity; the
            playground default is fine for the demo + tests.

    Returns:
        An async callable matching
        :data:`pipeline_live_runner.OperatorDecision`.
    """

    actual_queue = queue if queue is not None else get_default_queue()
    approvals_dir = run_dir / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "run_id=<%s>, run_dir=<%s> | pipeline hitl operator built",
        run_id,
        run_dir,
    )
    return queue_operator_decision(
        run_id=run_id,
        run_dir=run_dir,
        queue=actual_queue,
        operator=operator,
    )


def maybe_build_pipeline_hitl_operator(
    run_id: str,
    run_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    queue: PendingInterruptQueue | None = None,
    operator: str = "playground-operator",
) -> "OperatorDecision | None":
    """Return a queue-backed operator only when the env gate is set.

    The dispatcher passes the result straight to
    :class:`LivePipelineRun`'s ``operator_decision`` parameter:
    ``None`` falls through to the default ``auto_accept_interrupt``
    so CI and the local demo keep their happy-path behaviour.

    Args:
        run_id: Playground run id (see
            :func:`build_pipeline_hitl_operator`).
        run_dir: Run scratch root.
        env: Optional env mapping for tests.
        queue: Optional queue for tests.
        operator: Audit-record identity.

    Returns:
        An :data:`OperatorDecision` or ``None``.
    """

    if not is_pipeline_hitl_enabled(env):
        logger.info(
            "run_id=<%s> | pipeline hitl env gate off, auto-accept active",
            run_id,
        )
        return None
    return build_pipeline_hitl_operator(
        run_id,
        run_dir,
        queue=queue,
        operator=operator,
    )


def hitl_run_id_header(run_id: str) -> dict[str, str]:
    """Build the HTTP response header that surfaces the playground run id.

    The frontend uses ``X-Pipeline-Run-Id`` to associate a pending
    gate (received over SSE on the same run id) with the
    ``POST /playground/approval/resume/{run_id}/{interrupt_id}`` URL.
    Surfacing the id on the start response keeps the frontend from
    having to parse SSE events for it.

    Args:
        run_id: Playground :class:`RunStream` id.

    Returns:
        A header dict suitable for ``Response.headers.update(...)``.
    """

    return {"X-Pipeline-Run-Id": run_id}


__all__ = [
    "PIPELINE_HITL_ENV_VAR",
    "build_pipeline_hitl_operator",
    "hitl_run_id_header",
    "is_pipeline_hitl_enabled",
    "maybe_build_pipeline_hitl_operator",
]
