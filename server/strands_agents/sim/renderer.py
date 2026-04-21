"""GPU video renderer + worker-health fake.

The production SubAgent (component 10) takes three helpers — an async
task pool, a :class:`~strands_agents.task_tools.VideoWorkerDispatch`,
and a :class:`~strands_agents.task_tools.WorkerHealthCheck`.
:class:`FakeRenderer` implements the dispatch and health-check halves.
The pool itself is real — :class:`~strands_agents.tools.task_pool.AsyncTaskPool`
is already in-process Python and deterministic under a small worker
count, so there is no benefit to faking it.

Scripting outcomes
------------------

Tests declare what each (scene_id, seed) pair should produce by
scripting :class:`RenderOutcome`:

* ``clean`` — the default; a realistic-shaped dispatch payload with
  ``black_frame_fraction=0.0``, ``frames`` computed from duration at
  24fps, a fake mp4 path written to the sim tmpdir.
* ``frozen_frames`` — the QA layer in component 10 must reject this.
* ``black_frames`` — high black-frame fraction, same behaviour.
* ``wrong_duration(actual_sec)`` — the clip comes back shorter or
  longer than requested, triggering QA's duration-mismatch path.
* ``dispatch_error(message)`` — the dispatch itself raises, forcing
  the pool to mark the task failed.
"""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass
from typing import Any

from strands_agents.sim.recorder import CallRecord, Recorder


@dataclass(frozen=True)
class RenderOutcome:
    """Scripted outcome for one render call.

    Attributes:
        kind: ``"clean"``, ``"frozen_frames"``, ``"black_frames"``,
            ``"wrong_duration"``, or ``"dispatch_error"``.
        actual_duration_sec: Only used when ``kind == "wrong_duration"``.
        error_message: Only used when ``kind == "dispatch_error"``.
        black_frame_fraction: Override for the ``black_frames`` case.
            Defaults to ``0.35``, well above any reasonable threshold.
    """

    kind: str = "clean"
    actual_duration_sec: float | None = None
    error_message: str | None = None
    black_frame_fraction: float = 0.35


class FakeRenderer:
    """Scripted GPU dispatch + health-check fake."""

    def __init__(
        self,
        *,
        recorder: Recorder | None = None,
        tmpdir: str | None = None,
        workers_total: int = 2,
    ) -> None:
        """Create a renderer fake.

        Args:
            recorder: Optional :class:`Recorder`.
            tmpdir: Directory to write fake mp4 files into. Defaults
                to a fresh tempdir.
            workers_total: Initial worker count returned by
                :meth:`health_check`. Tests can change this at any
                time via :meth:`set_health`.
        """
        self._lock = threading.Lock()
        self._recorder = recorder
        self._tmpdir = tmpdir or tempfile.mkdtemp(prefix="fake-renderer-")
        self._outcomes_by_scene: dict[str, list[RenderOutcome]] = {}
        self._default_outcome = RenderOutcome()
        self._workers_total = workers_total
        self._workers_available = workers_total
        self._queue_depth = 0
        self._health_error: str | None = None

    # ------------------------------------------------------------------
    # Scripting controls
    # ------------------------------------------------------------------

    def set_outcome(
        self, *, scene_id: str, outcomes: list[RenderOutcome] | RenderOutcome
    ) -> None:
        """Queue outcomes for ``scene_id``.

        A list is consumed in order across successive dispatches for
        the same scene; after exhausting the list the default outcome
        takes over. Passing a single :class:`RenderOutcome` is
        shorthand for a one-element list.
        """
        items = [outcomes] if isinstance(outcomes, RenderOutcome) else list(outcomes)
        with self._lock:
            self._outcomes_by_scene[scene_id] = items

    def set_default_outcome(self, outcome: RenderOutcome) -> None:
        """Change the fallback outcome used when a scene has no queue."""
        with self._lock:
            self._default_outcome = outcome

    def set_health(
        self,
        *,
        workers_total: int | None = None,
        workers_available: int | None = None,
        queue_depth: int | None = None,
        error: str | None = None,
    ) -> None:
        """Override the next :meth:`health_check` response.

        Setting ``error`` to a non-empty string makes the next call
        raise :class:`RuntimeError` with that message, exercising the
        production SubAgent's "worker unhealthy, escalate" branch.
        """
        with self._lock:
            if workers_total is not None:
                self._workers_total = workers_total
            if workers_available is not None:
                self._workers_available = workers_available
            if queue_depth is not None:
                self._queue_depth = queue_depth
            self._health_error = error

    # ------------------------------------------------------------------
    # Helper surfaces
    # ------------------------------------------------------------------

    def dispatch(
        self,
        *,
        scene_id: str,
        concept_id: str,
        prompt: str,
        style_lock: dict[str, Any],
        duration_sec: float,
        seed: int,
        audio_artifact_url: str,
    ) -> dict[str, Any]:
        """Return a scripted dispatch payload (or raise) for one scene."""
        with self._lock:
            queue = self._outcomes_by_scene.get(scene_id, [])
            outcome = queue.pop(0) if queue else self._default_outcome
            if not queue and scene_id in self._outcomes_by_scene:
                # Drop empty queues so later scenes with the same id
                # fall through cleanly to the default.
                del self._outcomes_by_scene[scene_id]

        if outcome.kind == "dispatch_error":
            if self._recorder is not None:
                self._recorder.record(
                    CallRecord(
                        channel="renderer",
                        op="dispatch",
                        kwargs={
                            "scene_id": scene_id,
                            "concept_id": concept_id,
                            "seed": seed,
                        },
                        result_summary=f"error={outcome.error_message!r}",
                    )
                )
            msg = outcome.error_message or f"fake renderer error for scene {scene_id}"
            raise RuntimeError(msg)

        actual_duration = (
            outcome.actual_duration_sec
            if outcome.kind == "wrong_duration" and outcome.actual_duration_sec is not None
            else duration_sec
        )
        frames = max(int(round(actual_duration * 24)), 1)
        artifact_path = os.path.join(
            self._tmpdir, f"{scene_id}_{concept_id}_{seed}.mp4"
        )
        # Write a small sentinel file so any downstream code that
        # stats/opens it succeeds. Content is irrelevant to the fake.
        with open(artifact_path, "wb") as fh:
            fh.write(f"fake-mp4|scene={scene_id}|seed={seed}".encode())

        black_frac = 0.0 if outcome.kind != "black_frames" else outcome.black_frame_fraction
        payload: dict[str, Any] = {
            "artifact_path": artifact_path,
            "frames": frames,
            "codec": "h264",
            "black_frame_fraction": black_frac,
            "duration_sec": actual_duration,
            "concept_id": concept_id,
            "scene_id": scene_id,
            "seed": seed,
            "style_lock_tokens": list(style_lock.get("tokens", [])),
            "prompt_hash": _short_hash(prompt),
            "audio_artifact_url": audio_artifact_url,
        }
        if outcome.kind == "frozen_frames":
            payload["frozen_frame_runs"] = [{"start_frame": 0, "end_frame": frames - 1}]
        if self._recorder is not None:
            self._recorder.record(
                CallRecord(
                    channel="renderer",
                    op="dispatch",
                    kwargs={
                        "scene_id": scene_id,
                        "concept_id": concept_id,
                        "seed": seed,
                        "duration_sec": duration_sec,
                    },
                    result_summary=f"kind={outcome.kind} frames={frames}",
                )
            )
        return payload

    def health_check(self) -> dict[str, Any]:
        """Return the scripted worker-pool health snapshot."""
        with self._lock:
            if self._health_error is not None:
                err = self._health_error
                self._health_error = None
                if self._recorder is not None:
                    self._recorder.record(
                        CallRecord(
                            channel="renderer",
                            op="health_check",
                            result_summary=f"error={err!r}",
                        )
                    )
                raise RuntimeError(err)
            snapshot = {
                "workers_total": self._workers_total,
                "workers_available": self._workers_available,
                "queue_depth": self._queue_depth,
                "per_worker": [
                    {"worker_id": f"w{i}", "status": "ready"}
                    for i in range(self._workers_total)
                ],
            }
        if self._recorder is not None:
            self._recorder.record(
                CallRecord(
                    channel="renderer",
                    op="health_check",
                    result_summary=(
                        f"total={snapshot['workers_total']} "
                        f"avail={snapshot['workers_available']} "
                        f"queue={snapshot['queue_depth']}"
                    ),
                )
            )
        return snapshot


def _short_hash(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode()).hexdigest()[:12]
