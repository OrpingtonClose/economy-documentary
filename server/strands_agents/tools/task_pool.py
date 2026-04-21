"""AsyncTaskPool — primitive for parallel background work.

The DeepAgent orchestrator launches long-running work (TTS synthesis,
LTX video rendering, WhisperX alignment) via ``launch_*`` tools that
return immediately with a ``task_id``. It then calls ``check_tasks`` to
poll and ``await_tasks`` to block until a set of tasks reaches a
terminal state. This matches the miro pattern (see
``MiroThinker/apps/strands-agent/task_pool.py``) and the
``AGENTS.md`` guideline "launch TTS before LTX, await both before
assembly".

This module provides the *primitive* — a thread-backed registry with
idempotent launches. Component PRs (04-audio-agent, 10-production-supervisor)
bind concrete ``@tool``-decorated ``launch_tts`` / ``launch_video`` /
... wrappers onto a pool instance via :func:`make_task_tools`.

Idempotency
-----------
A launch is keyed by ``(task_type, identity)``. Calling
:meth:`AsyncTaskPool.launch` twice with the same identity returns the
original ``task_id`` and does **not** re-submit work. This lets the
orchestrator retry safely after a transient failure or a restored
session.

Thread safety
-------------
The registry is guarded by a lock; worker threads mutate only their own
:class:`TaskState`. The pool is designed for one orchestrator thread
calling ``launch`` / ``check`` / ``await_all`` concurrently with worker
threads writing terminal status.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled"})


@dataclass
class TaskState:
    """Snapshot of a single background task."""

    task_id: str
    task_type: str
    identity: str
    status: str = "pending"  # pending | running | complete | failed | cancelled
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for tool output."""
        return asdict(self)

    @property
    def is_terminal(self) -> bool:
        """True once the task has reached a terminal status."""
        return self.status in _TERMINAL_STATUSES


class AsyncTaskPool:
    """Thread-backed registry of background tasks with idempotent launches."""

    def __init__(self, *, max_workers: int = 4) -> None:
        """Create a pool bounded by ``max_workers`` concurrent executors.

        Args:
            max_workers: Upper bound on worker threads. Defaults to 4 —
                sized for the typical scene-count of one documentary
                (4–10 concurrent TTS/LTX jobs).
        """
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="strands-task-pool",
        )
        self._tasks: dict[str, TaskState] = {}
        self._identity_index: dict[tuple[str, str], str] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._lock = threading.Lock()
        self._shutdown = False

    def launch(
        self,
        *,
        task_type: str,
        identity: str,
        fn: Callable[[], dict[str, Any]],
    ) -> TaskState:
        """Submit ``fn`` for background execution; idempotent on identity.

        Args:
            task_type: Logical kind of work (``"tts"``, ``"ltx"``, etc.).
            identity: Stable key that uniquely identifies this unit of
                work (e.g. ``f"scene-{scene_id}-rev{revision}-{voice}"``).
                Re-launching with the same ``(task_type, identity)``
                returns the existing task unchanged.
            fn: Zero-arg callable executed on a worker thread. Must
                return a JSON-serializable dict.

        Returns:
            Snapshot of the task. New launches start in ``"pending"``;
            duplicates return the current status of the existing task.
        """
        key = (task_type, identity)
        with self._lock:
            if self._shutdown:
                raise RuntimeError("task pool is shut down")
            existing_id = self._identity_index.get(key)
            if existing_id is not None:
                logger.debug(
                    "task_type=<%s>, identity=<%s>, task_id=<%s> | launch is idempotent, returning existing task",
                    task_type,
                    identity,
                    existing_id,
                )
                return self._tasks[existing_id]

            task_id = uuid.uuid4().hex
            state = TaskState(task_id=task_id, task_type=task_type, identity=identity)
            self._tasks[task_id] = state
            self._identity_index[key] = task_id

        future = self._executor.submit(self._run, task_id, fn)
        with self._lock:
            self._futures[task_id] = future
        logger.debug(
            "task_type=<%s>, identity=<%s>, task_id=<%s> | task launched",
            task_type,
            identity,
            task_id,
        )
        return state

    def _run(self, task_id: str, fn: Callable[[], dict[str, Any]]) -> None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                return
            state.status = "running"
            state.started_at = time.time()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 — surface any worker error as task failure
            with self._lock:
                state.status = "failed"
                state.error = f"{type(exc).__name__}: {exc}"
                state.finished_at = time.time()
            logger.warning(
                "task_id=<%s>, error=<%s> | task failed", task_id, state.error
            )
            return
        with self._lock:
            state.status = "complete"
            state.result = result if isinstance(result, dict) else {"value": result}
            state.finished_at = time.time()

    def check(self, task_ids: list[str]) -> list[dict[str, Any]]:
        """Return current snapshots for the given task ids.

        Unknown ids produce a ``"not_found"`` status record so the caller
        can distinguish a missing task from a stale one.
        """
        with self._lock:
            out: list[dict[str, Any]] = []
            for tid in task_ids:
                state = self._tasks.get(tid)
                if state is None:
                    out.append({"task_id": tid, "status": "not_found"})
                else:
                    out.append(state.to_dict())
            return out

    def await_all(
        self,
        task_ids: list[str],
        *,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Block until every task is terminal or ``timeout`` elapses.

        Args:
            task_ids: Tasks to wait for. Unknown ids are skipped.
            timeout: Max seconds to wait. ``None`` waits indefinitely.

        Returns:
            The final status snapshots in the same order as ``task_ids``.
            Tasks still running when the timeout fires are returned in
            whatever status they happen to hold.
        """
        with self._lock:
            futures = [
                f for f in (self._futures.get(tid) for tid in task_ids) if f is not None
            ]
        if futures:
            wait(futures, timeout=timeout)
        return self.check(task_ids)

    def shutdown(self, *, wait_for_completion: bool = True) -> None:
        """Stop accepting new launches; optionally block on in-flight tasks."""
        with self._lock:
            self._shutdown = True
        self._executor.shutdown(wait=wait_for_completion)


def make_task_tools(pool: AsyncTaskPool) -> dict[str, Callable[..., Any]]:
    """Bind ``check_tasks`` / ``await_tasks`` tools to ``pool``.

    Returns a dict of plain callables. Component PRs wrap these with
    ``@tool`` (supplying the task-type-specific ``launch_*`` themselves)
    so we don't couple the primitive to the Strands runtime here.

    Args:
        pool: The pool to bind. Callers are responsible for its lifetime.

    Returns:
        Mapping with keys ``"check_tasks"`` and ``"await_tasks"``.
    """

    def check_tasks(task_ids: list[str]) -> list[dict[str, Any]]:
        """Return status snapshots for ``task_ids``; never blocks."""
        return pool.check(task_ids)

    def await_tasks(
        task_ids: list[str], timeout: float | None = None
    ) -> list[dict[str, Any]]:
        """Block until every task in ``task_ids`` is terminal or ``timeout`` expires."""
        return pool.await_all(task_ids, timeout=timeout)

    return {"check_tasks": check_tasks, "await_tasks": await_tasks}
