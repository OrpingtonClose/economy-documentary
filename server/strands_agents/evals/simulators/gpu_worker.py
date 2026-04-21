"""GPU worker :class:`ToolSimulator` per ``SIMULATION.md`` §1.

Provides mocked ``dispatch_video_job`` / ``check_job_status`` /
``check_worker_health`` tools backed by a shared ``StateRegistry``.
Component PRs that exercise production orchestration (10, 12, 14) wire
this into their experiments; other components can ignore it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from strands_evals.simulation.tool_simulator import StateRegistry, ToolSimulator

_SHARE_STATE_ID = "video_pipeline"

_INITIAL_STATE_DESCRIPTION = (
    "GPU worker pool: 2 workers available, queue empty. Each worker has "
    "ltx capability loaded. Jobs take ~90 s on average, 10% fail "
    "transiently with 'CUDA OOM' (retry succeeds), 5% fail persistently "
    "with 'model checkpoint missing'. Worker health endpoints return "
    "{'status': 'ok', 'capabilities': ['ltx'], 'gpu_mem_free_mb': 12000}."
)


class DispatchResponse(BaseModel):
    """Result of dispatching a video render job."""

    job_id: str
    worker_url: str
    queued_at: float


class JobStatus(BaseModel):
    """Snapshot returned by the worker for a known job id."""

    job_id: str
    state: Literal["queued", "running", "succeeded", "failed"]
    progress: float = Field(ge=0.0, le=1.0)
    error: str | None = None
    artifact_url: str | None = None


class WorkerHealth(BaseModel):
    """Health report aggregated across the worker pool."""

    status: Literal["ok", "degraded", "down"]
    capabilities: list[str]
    gpu_mem_free_mb: int
    queue_depth: int


def build_gpu_worker_simulator(
    *,
    state_registry: StateRegistry | None = None,
    model: str | None = None,
) -> ToolSimulator:
    """Construct the GPU worker :class:`ToolSimulator`.

    Args:
        state_registry: Optional pre-initialised registry to share state
            with other simulators or a test harness. One is created if
            omitted.
        model: Model identifier for the LLM-backed simulator. ``None``
            defers to whatever the caller's :class:`ToolSimulator`
            default is, which is how experiments pin their own model.

    Returns:
        A :class:`ToolSimulator` with the three GPU tools registered.
    """
    sim = ToolSimulator(state_registry=state_registry, model=model)

    @sim.tool(
        output_schema=DispatchResponse,
        share_state_id=_SHARE_STATE_ID,
        initial_state_description=_INITIAL_STATE_DESCRIPTION,
    )
    def dispatch_video_job(
        scene_id: str,
        duration_sec: float,
        voice_locked_audio_url: str,
    ) -> DispatchResponse:
        """Dispatch an LTX video render for one scene.

        Args:
            scene_id: Scene identifier from the OTIO timeline.
            duration_sec: Target video length matching the narration
                slot (narration + tail silence).
            voice_locked_audio_url: B2 URL of the TTS audio the render
                must lip-sync / beat-sync to.
        """

    @sim.tool(
        output_schema=JobStatus,
        share_state_id=_SHARE_STATE_ID,
        initial_state_description=_INITIAL_STATE_DESCRIPTION,
    )
    def check_job_status(job_id: str) -> JobStatus:
        """Return the current state for a previously dispatched job."""

    @sim.tool(
        output_schema=WorkerHealth,
        share_state_id=_SHARE_STATE_ID,
        initial_state_description=_INITIAL_STATE_DESCRIPTION,
    )
    def check_worker_health() -> WorkerHealth:
        """Return an aggregated health snapshot of the worker pool."""

    return sim
