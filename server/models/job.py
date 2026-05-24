"""Pydantic models for the job queue — the coordination layer between agents.

Media agents (audio, video) create jobs and put them in the queue.
The provisioner agent reads the queue, provisions workers, assigns jobs.
Workers execute jobs and upload results to B2.
Media agents poll the queue for completed jobs, download from B2, QA.
If QA fails, the job goes back into the queue with comments.

No VM URLs in agent conversations. No direct worker calls from domain agents.
The queue is the single coordination mechanism.
"""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """Lifecycle of a job in the queue."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_RETRY = "needs_retry"


class JobType(str, Enum):
    """What kind of work this job represents."""

    NARRATION = "narration"
    VIDEO_RENDER = "video_render"
    MUX = "mux"
    LOUDNORM = "loudnorm"


class Job(BaseModel):
    """A single unit of work in the pipeline queue."""

    job_id: str = Field(description="Unique job identifier (UUID)")
    job_type: JobType = Field(description="narration or video_render")
    stage: str = Field(description="'audio' or 'video'")
    scene_num: int = Field(description="1-based scene index")
    payload: dict = Field(
        default_factory=dict,
        description="Type-specific payload (narration text, visual description, etc.)",
    )
    status: JobStatus = Field(default=JobStatus.PENDING)
    attempts: int = Field(default=0, description="How many times this job has been tried")
    max_attempts: int = Field(default=3, description="Max retry attempts before permanent failure")
    qa_comments: list[str] = Field(
        default_factory=list,
        description="QA feedback that caused retry — worker sees this on requeue",
    )
    worker_id: str = Field(default="", description="Which worker/instance is handling this job")
    artifact_path: str = Field(
        default="",
        description="B2 key where the result artifact is stored after completion",
    )
    created_at: float = Field(default=0.0)
    started_at: float = Field(default=0.0)
    completed_at: float = Field(default=0.0)


class JobResult(BaseModel):
    """Result of a completed job — what the worker returns."""

    job_id: str
    status: str = Field(description="'success', 'error', 'timeout'")
    artifact_path: str = Field(default="", description="Local file path of the output artifact")
    output_path: str = Field(default="", description="Local path if worker reported one")
    duration_sec: float = Field(default=0.0)
    error_message: str = Field(default="")
    worker_logs: str = Field(default="", description="Tail of worker logs for diagnostics")


class QAResult(BaseModel):
    """QA decision on a completed job — extracted by the media agent."""

    job_id: str
    passed: bool
    verdict: str = Field(description="'pass', 'fail', 'needs_retry', 'escalate'")
    comments: list[str] = Field(default_factory=list)
    suggested_fix: str = Field(default="", description="What to change on retry")
