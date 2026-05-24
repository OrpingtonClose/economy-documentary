"""Agent-facing tools for media agents to interact with the job queue.

Domain agents (audio/video) use these to:
  1. Create jobs for the provisioner to pick up
  2. Poll for completed jobs
  3. QA results and requeue with comments if unsatisfactory

They NEVER talk to VMs directly. B2 is the only artifact source.
"""

from __future__ import annotations

import json
from typing import Any

from strands import tool

from models.job import JobStatus, JobType, QAResult
from job_queue import (
    create_job,
    get_completed_jobs,
    get_failed_jobs,
    get_queue_summary,
    requeue_job_with_qa_comments,
)


# ---------------------------------------------------------------------------
# Media agent tools
# ---------------------------------------------------------------------------

@tool
def submit_render_job(
    stage: str,
    scene_num: int,
    job_type: str,
    payload: str,
    max_attempts: int = 3,
) -> str:
    """Submit a render job to the queue. Payload is a JSON string with type-specific parameters.

    job_type: "video_render" or "narration" or "mux" or "loudnorm"
    payload: JSON string. Examples:
      - video: {"model_name":"...","prompt":"...","width":...,"height":...,"duration":...}
      - audio: {"model_name":"...","text":"...","voice_id":"..."}
      - mux: {"audio_b2_key":"...","video_b2_key":"..."}
      - loudnorm: {"audio_b2_key":"..."}
    """
    try:
        jt = JobType(job_type)
    except ValueError:
        return (
            f"Invalid job_type '{job_type}'. "
            f"Must be one of: {[t.value for t in JobType]}"
        )

    try:
        parsed_payload: dict[str, Any] = json.loads(payload)
    except json.JSONDecodeError as exc:
        return f"Payload is not valid JSON: {exc}"

    job = create_job(
        job_type=jt,
        stage=stage,
        scene_num=scene_num,
        payload=parsed_payload,
        max_attempts=max_attempts,
    )
    return (
        f"Job {job.job_id} submitted. Type={jt.value}, "
        f"scene={scene_num}, status={job.status.value}"
    )


@tool
def poll_completed_jobs(stage: str) -> str:
    """Poll for completed jobs in a stage. Returns JSON with artifact locations."""
    jobs = get_completed_jobs(stage)
    if not jobs:
        return "No completed jobs yet."

    lines: list[str] = []
    for j in jobs:
        lines.append(
            f"  scene {j.scene_num}: job={j.job_id} "
            f"b2_key={j.b2_artifact_key} attempts={j.attempts}"
        )
    return "Completed jobs:\n" + "\n".join(lines)


@tool
def qa_completed_job(
    job_id: str,
    passed: bool,
    verdict: str,
    comments_json: str = "[]",
    suggested_fix: str = "",
) -> str:
    """QA a completed job. If failed, it goes back to queue with comments.

    passed: True if the artifact meets quality standards.
    verdict: 'pass', 'fail', 'needs_retry', or 'escalate'.
    comments_json: JSON array of comment strings.
    suggested_fix: If failed, what the worker should do differently.
    """
    try:
        from job_queue import get_job

        job = get_job(job_id)
    except Exception as exc:
        return f"Cannot QA job {job_id}: {exc}"

    if job.status != JobStatus.COMPLETED:
        return (
            f"Cannot QA job {job_id}: status is {job.status.value}, "
            f"expected 'completed'"
        )

    if passed:
        return f"Job {job_id} passed. Verdict: {verdict}"

    try:
        comments: list[str] = json.loads(comments_json)
    except json.JSONDecodeError:
        comments = []

    qa = QAResult(
        job_id=job_id,
        passed=False,
        verdict=verdict,
        comments=comments,
        suggested_fix=suggested_fix,
    )
    requeued = requeue_job_with_qa_comments(job_id, qa)
    return (
        f"Job {job_id} REJECTED. Verdict: {verdict}\n"
        f"Requeued with status={requeued.status.value}, "
        f"attempt={requeued.attempts}/{requeued.max_attempts}\n"
        f"Suggested fix: {suggested_fix}"
    )


@tool
def check_queue_status(stage: str) -> str:
    """Get a summary of job statuses for a stage."""
    summary = get_queue_summary(stage)
    total = sum(summary.values())
    lines = [f"Queue summary for {stage} (total={total}):"]
    for status, count in summary.items():
        lines.append(f"  {status}: {count}")
    return "\n".join(lines)


@tool
def get_failed_job_details(stage: str) -> str:
    """Get details of permanently failed jobs so agent can decide what to do."""
    jobs = get_failed_jobs(stage)
    if not jobs:
        return "No permanently failed jobs."

    lines: list[str] = []
    for j in jobs:
        lines.append(
            f"  scene {j.scene_num}: job={j.job_id} "
            f"attempts={j.attempts} comments={json.dumps(j.qa_comments)}"
        )
    return "Failed jobs:\n" + "\n".join(lines)


@tool
def download_b2_artifact(b2_key: str, local_path: str) -> str:
    """Download a completed artifact from B2 to local disk.

    Call this after poll_completed_jobs returns a b2_key.
    The local_path should be in the pipeline working directory.
    """
    from tools.b2_checkpoint import download_file

    ok = download_file(b2_key, local_path)
    if ok:
        return f"Downloaded {b2_key} -> {local_path}"
    return f"FAILED to download {b2_key} -> {local_path}"



