"""Component 10 — GPU-dispatch task tools.

The production SubAgent drives per-scene LTX video rendering via an
``AsyncTaskPool`` (``tools/task_pool.py``). This module exposes the
three Strands ``@tool`` callables the SubAgent uses to:

* launch a per-scene GPU job (``launch_visual_production``),
* check worker-pool health (``check_worker_health``),
* poll / block on in-flight tasks (``check_tasks`` / ``await_tasks``).

The real LTX worker dispatch, health endpoint, and B2 upload are
injected via :func:`set_production_helpers` — production wiring lands
in component 14. Unit tests inject deterministic fakes.

Design invariants (carried over from
``server/agents/production_supervisor.py``):

1. **Idempotent launches.** ``(scene_id, revision)`` is the identity;
   re-launching returns the existing ``task_id`` and does not double-
   submit work. Matches the miro AsyncTaskPool contract.
2. **Fail loud.** Dispatch, health-check, and polling failures raise
   :class:`RuntimeError` — never return a placeholder ``task_id`` or a
   fake-healthy status.
3. **No silent dispatch without audio.** The SubAgent prompt enforces
   the audio precondition; this module validates that the caller
   passed a non-empty ``audio_artifact_url`` to guard against prompt
   drift.
4. **Helpers injected.** Production wiring in component 14 calls
   :func:`set_production_helpers` once at startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from strands import tool

from strands_agents.tools.task_pool import AsyncTaskPool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper registry — injected by production wiring (component 14) or tests.
# ---------------------------------------------------------------------------


class VideoWorkerDispatch(Protocol):
    """Synthesize one LTX video clip.

    Args:
        scene_id: Stable identifier for the scene (e.g. ``"s3"``).
        concept_id: Identifier of the chosen visual concept.
        prompt: Fully-rendered LTX prompt.
        style_lock: Movie-level style-lock dict.
        duration_sec: Target clip duration in seconds.
        seed: RNG seed for deterministic generation.
        audio_artifact_url: URL of the aligned narration clip. Used so
            the LTX worker can hint cadence; passed through unchanged
            when the worker does not consume audio.

    Returns:
        Completion payload matching the spec — ``artifact_path``,
        ``frames``, ``codec``, ``black_frame_fraction``, and any
        worker-specific diagnostics.

    Raises:
        RuntimeError: On any dispatch failure. The pool records the
            task as ``failed``.
    """

    def __call__(
        self,
        *,
        scene_id: str,
        concept_id: str,
        prompt: str,
        style_lock: dict[str, Any],
        duration_sec: float,
        seed: int,
        audio_artifact_url: str,
    ) -> dict[str, Any]: ...


class WorkerHealthCheck(Protocol):
    """Return the current GPU worker-pool health snapshot.

    Returns:
        Dict with ``workers_total`` (int), ``workers_available`` (int),
        ``queue_depth`` (int), and ``per_worker`` (list of per-worker
        status dicts). Extra fields are preserved as-is.

    Raises:
        RuntimeError: On health-endpoint failure.
    """

    def __call__(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _ProductionHelpers:
    pool: AsyncTaskPool
    dispatch: VideoWorkerDispatch
    health_check: WorkerHealthCheck


_HELPERS: _ProductionHelpers | None = None


class ProductionHelpersNotConfigured(RuntimeError):
    """Raised when production tools are invoked before helpers are registered."""


def _get_helpers() -> _ProductionHelpers:
    if _HELPERS is None:
        raise ProductionHelpersNotConfigured(
            "production tools invoked before set_production_helpers was called"
        )
    return _HELPERS


# ---------------------------------------------------------------------------
# Strands @tool surface consumed by the production SubAgent.
# ---------------------------------------------------------------------------


def _identity(scene_id: str, revision: int) -> str:
    return f"{scene_id}-rev{revision}"


@tool
def launch_visual_production(
    scene_id: str,
    concept_id: str,
    prompt: str,
    style_lock: dict[str, Any],
    duration_sec: float,
    seed: int,
    audio_artifact_url: str,
    revision: int = 1,
) -> dict[str, Any]:
    """Submit a GPU video-render job for one scene; returns a ``task_id``.

    The launch is idempotent on ``(scene_id, revision)`` — re-invoking
    with the same pair returns the existing task unchanged. Increment
    ``revision`` to force a fresh dispatch (e.g. for retries after a
    ``fix_scene``).

    Args:
        scene_id: Stable scene identifier (e.g. ``"s3"``). Required.
        concept_id: Chosen visual concept identifier.
        prompt: Fully-rendered LTX prompt for the scene.
        style_lock: Movie-level style-lock dict (shot discipline,
            palette, motion profile). Forwarded unchanged to the
            worker.
        duration_sec: Target clip duration in seconds.
        seed: RNG seed for deterministic LTX sampling.
        audio_artifact_url: URL of the aligned narration clip. Must be
            non-empty — the SubAgent prompt forbids dispatch without
            audio.
        revision: Launch revision for this scene. Increment to bypass
            the idempotency guard on retries. Defaults to 1.

    Returns:
        Dict with ``task_id``, ``status`` (``"pending"`` | ``"running"`` |
        terminal on re-launch), ``scene_id``, and ``identity``.

    Raises:
        ValueError: On empty ``scene_id`` or missing ``audio_artifact_url``.
        ProductionHelpersNotConfigured: When :func:`set_production_helpers`
            has not been called.
    """
    if not scene_id:
        raise ValueError("launch_visual_production requires a non-empty scene_id")
    if not audio_artifact_url:
        raise ValueError(
            "launch_visual_production refuses to dispatch without an "
            "audio_artifact_url — AGENTS.md invariant #6"
        )
    if duration_sec <= 0:
        raise ValueError(
            f"launch_visual_production requires duration_sec > 0, got {duration_sec}"
        )
    if revision < 1:
        raise ValueError(
            f"launch_visual_production requires revision >= 1, got {revision}"
        )

    helpers = _get_helpers()

    # Snapshot dispatch args so the worker thread closure doesn't hold
    # references that the orchestrator might later mutate.
    dispatch_args: dict[str, Any] = {
        "scene_id": scene_id,
        "concept_id": concept_id,
        "prompt": prompt,
        "style_lock": dict(style_lock),
        "duration_sec": float(duration_sec),
        "seed": int(seed),
        "audio_artifact_url": audio_artifact_url,
    }

    def _worker() -> dict[str, Any]:
        return helpers.dispatch(**dispatch_args)

    identity = _identity(scene_id, revision)
    state = helpers.pool.launch(
        task_type="ltx", identity=identity, fn=_worker
    )
    logger.debug(
        "scene_id=<%s>, revision=<%d>, task_id=<%s>, status=<%s> | launched visual production",
        scene_id,
        revision,
        state.task_id,
        state.status,
    )
    return {
        "task_id": state.task_id,
        "status": state.status,
        "scene_id": scene_id,
        "identity": identity,
    }


@tool
def check_worker_health() -> dict[str, Any]:
    """Return a snapshot of GPU worker-pool health.

    Returns:
        Dict with ``workers_total``, ``workers_available``,
        ``queue_depth``, ``per_worker``. The SubAgent uses
        ``workers_available`` to decide whether to dispatch all scenes
        at once or in rolling batches.

    Raises:
        ProductionHelpersNotConfigured: When helpers are not
            registered.
        RuntimeError: On health-endpoint failure (re-raised from the
            helper).
    """
    helpers = _get_helpers()
    snapshot = helpers.health_check()
    if not isinstance(snapshot, dict):
        raise RuntimeError(
            f"check_worker_health helper returned non-dict: {type(snapshot).__name__}"
        )
    return snapshot


@tool
def check_tasks(task_ids: list[str]) -> list[dict[str, Any]]:
    """Return status snapshots for ``task_ids``; never blocks.

    Unknown ids produce ``{"task_id": ..., "status": "not_found"}``
    records so the SubAgent can distinguish a missing task from a
    stale one.
    """
    helpers = _get_helpers()
    return helpers.pool.check(list(task_ids))


@tool
def await_tasks(
    task_ids: list[str], timeout: float | None = None
) -> list[dict[str, Any]]:
    """Block until every task in ``task_ids`` is terminal or ``timeout`` expires.

    Args:
        task_ids: Tasks to wait for.
        timeout: Max seconds to wait. ``None`` waits indefinitely.

    Returns:
        Final status snapshots in the same order as ``task_ids``.
    """
    helpers = _get_helpers()
    return helpers.pool.await_all(list(task_ids), timeout=timeout)


__all__ = ["ProductionHelpersNotConfigured",
    "VideoWorkerDispatch",
    "WorkerHealthCheck",
    "await_tasks",
    "check_tasks",
    "check_worker_health",
    "launch_visual_production",]
