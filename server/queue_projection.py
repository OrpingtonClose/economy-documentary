"""Job queue as a read model projected from the event log.

The queue is NOT a database. It is rebuilt from events every time.
This is pure event sourcing — the event log is the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from effects import (
    Effect,
    GenerateNarrationAudio,
    JobCompleted,
    JobFailed,
    JobRequeued,
    JobStarted,
    QAFailed,
    RenderVideoSegment,
)


@dataclass
class Job:
    """A job in the projected queue."""

    job_id: str
    job_type: str  # "NARRATION" or "VIDEO_RENDER"
    stage: str  # "audio" or "video"
    scene_num: int
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending, running, completed, failed, needs_retry
    worker_id: str = ""
    artifact_path: str = ""
    qa_comments: list[str] = field(default_factory=list)
    suggested_fix: str = ""


def _audio_job_id(scene_num: int, voice: str) -> str:
    """Deterministic job ID for audio jobs."""
    return f"audio_{scene_num}_{voice}"


def _video_job_id(scene_num: int) -> str:
    """Deterministic job ID for video jobs."""
    return f"video_{scene_num}"


def project_queue(effects: list[Effect]) -> dict[str, Job]:
    """Build queue state from a list of effects.

    Returns: {job_id: Job} mapping.
    """
    jobs: dict[str, Job] = {}

    for effect in effects:
        if isinstance(effect, GenerateNarrationAudio):
            job_id = _audio_job_id(effect.scene_num, effect.voice)
            if job_id not in jobs:
                jobs[job_id] = Job(
                    job_id=job_id,
                    job_type="NARRATION",
                    stage="audio",
                    scene_num=effect.scene_num,
                    payload={"voice": effect.voice, "text": effect.text},
                )

        elif isinstance(effect, RenderVideoSegment):
            job_id = _video_job_id(effect.scene_num)
            if job_id not in jobs:
                jobs[job_id] = Job(
                    job_id=job_id,
                    job_type="VIDEO_RENDER",
                    stage="video",
                    scene_num=effect.scene_num,
                    payload={"prompt": effect.prompt, "lora_id": effect.lora_id, "duration_sec": effect.duration_sec},
                )

        elif isinstance(effect, JobStarted):
            if effect.job_id in jobs:
                jobs[effect.job_id].status = "running"
                jobs[effect.job_id].worker_id = effect.worker_id

        elif isinstance(effect, JobCompleted):
            if effect.job_id in jobs:
                jobs[effect.job_id].status = "completed"
                jobs[effect.job_id].artifact_path = effect.artifact_path

        elif isinstance(effect, JobFailed):
            if effect.job_id in jobs:
                job = jobs[effect.job_id]
                job.status = "failed"
                # Check if we should retry
                attempts = sum(1 for e in effects if isinstance(e, JobStarted) and e.job_id == effect.job_id)
                if attempts < 3:
                    job.status = "needs_retry"
                    job.qa_comments.append(f"Failed: {effect.error_message}")

        elif isinstance(effect, QAFailed):
            if effect.job_id in jobs:
                job = jobs[effect.job_id]
                job.qa_comments.extend(effect.comments)
                if effect.suggested_fix:
                    job.suggested_fix = effect.suggested_fix

        elif isinstance(effect, JobRequeued):
            if effect.job_id in jobs:
                job = jobs[effect.job_id]
                job.status = "needs_retry"
                job.qa_comments.extend(effect.comments)
                if effect.suggested_fix:
                    job.suggested_fix = effect.suggested_fix

    return jobs


def get_queue_summary(jobs: dict[str, Job], stage: str) -> dict[str, int]:
    """Return count of jobs by status for a stage."""
    summary = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "needs_retry": 0}
    for job in jobs.values():
        if job.stage == stage and job.status in summary:
            summary[job.status] += 1
    return summary


def get_pending_jobs(jobs: dict[str, Job], stage: str) -> list[Job]:
    """Get pending or needs_retry jobs for a stage."""
    result = []
    for job in jobs.values():
        if job.stage == stage and job.status in ("pending", "needs_retry"):
            result.append(job)
    return result


def get_completed_jobs(jobs: dict[str, Job], stage: str) -> list[Job]:
    """Get completed jobs for a stage."""
    return [j for j in jobs.values() if j.stage == stage and j.status == "completed"]
