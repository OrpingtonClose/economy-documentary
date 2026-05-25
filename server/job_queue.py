"""Job Queue — SQLite-backed coordination between media agents and provisioner.

Architecture:
    Media agent → creates Job → queue → PENDING
    Provisioner → reads queue → assigns to worker → ASSIGNED/RUNNING
    Worker → executes → uploads to B2 → marks COMPLETED
    Media agent → polls COMPLETED → downloads from B2 → QA
    Media agent → QA fails → requeue with comments → NEEDS_RETRY

No VM URLs in agent conversations. No direct worker calls from domain agents.
The queue is the single source of coordination truth.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from models.job import Job, JobStatus, JobType, QAResult

logger = logging.getLogger(__name__)

_DB_PATH = Path("/tmp/documentary-pipeline/job_queue.db")
_LOCK = threading.Lock()


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            stage TEXT NOT NULL,
            scene_num INTEGER NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            qa_comments TEXT NOT NULL DEFAULT '[]',
            worker_id TEXT NOT NULL DEFAULT '',
            artifact_path TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0,
            started_at REAL NOT NULL DEFAULT 0,
            completed_at REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_stage_status ON jobs(stage, status);
        CREATE INDEX IF NOT EXISTS idx_worker ON jobs(worker_id);
        """
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=row["job_id"],
        job_type=JobType(row["job_type"]),
        stage=row["stage"],
        scene_num=row["scene_num"],
        payload=json.loads(row["payload"]),
        status=JobStatus(row["status"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        qa_comments=json.loads(row["qa_comments"]),
        worker_id=row["worker_id"],
        artifact_path=row["artifact_path"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


# ---------------------------------------------------------------------------
# Media agent API — create jobs, poll results, QA
# ---------------------------------------------------------------------------

def create_job(
    job_type: JobType,
    stage: str,
    scene_num: int,
    payload: dict[str, Any],
    max_attempts: int = 3,
) -> Job:
    """Create a new job and enqueue it.

    Idempotent: if a non-failed job for the same stage+scene already exists,
    returns the existing job instead of creating a duplicate.
    """
    with _LOCK, _conn() as conn:
        # Check for existing non-failed job
        row = conn.execute(
            """
            SELECT * FROM jobs
            WHERE stage = ? AND scene_num = ? AND status != 'failed'
            ORDER BY created_at DESC LIMIT 1
            """,
            (stage, scene_num),
        ).fetchone()
        if row:
            existing = _row_to_job(row)
            logger.info(
                "job deduplicated | stage=<%s> scene=<%s> existing_job=<%s> status=<%s>",
                stage, scene_num, existing.job_id, existing.status.value,
            )
            return existing

    job = Job(
        job_id=f"job_{uuid.uuid4().hex[:12]}",
        job_type=job_type,
        stage=stage,
        scene_num=scene_num,
        payload=payload,
        status=JobStatus.PENDING,
        max_attempts=max_attempts,
        created_at=time.time(),
    )

    with _LOCK, _conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs
            (job_id, job_type, stage, scene_num, payload, status,
             attempts, max_attempts, qa_comments, worker_id, artifact_path,
             created_at, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.job_type.value,
                job.stage,
                job.scene_num,
                json.dumps(job.payload),
                job.status.value,
                job.attempts,
                job.max_attempts,
                json.dumps(job.qa_comments),
                job.worker_id,
                job.artifact_path,
                job.created_at,
                job.started_at,
                job.completed_at,
            ),
        )
        conn.commit()

    logger.info(
        "Created %s job %s for stage=%s scene=%d",
        job.job_type.value, job.job_id, job.stage, job.scene_num,
    )
    return job


def get_completed_jobs(stage: str) -> list[Job]:
    """Get all completed jobs for a stage — media agent polls this."""
    with _LOCK, _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE stage = ? AND status = 'completed'
            ORDER BY scene_num
            """,
            (stage,),
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def get_failed_jobs(stage: str) -> list[Job]:
    """Get jobs that exceeded max attempts — permanent failures."""
    with _LOCK, _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE stage = ? AND status = 'failed'
            ORDER BY scene_num
            """,
            (stage,),
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def requeue_job_with_qa_comments(job_id: str, qa_result: QAResult) -> Job:
    """QA failed — put job back in queue with comments for worker to see."""
    with _LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Job {job_id} not found")

        job = _row_to_job(row)
        if job.attempts >= job.max_attempts:
            conn.execute(
                "UPDATE jobs SET status = 'failed' WHERE job_id = ?",
                (job_id,),
            )
            conn.commit()
            logger.warning("Job %s exceeded max attempts — marked failed", job_id)
            return _row_to_job(
                conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            )

        comments = json.loads(row["qa_comments"])
        comments.extend(qa_result.comments)
        if qa_result.suggested_fix:
            comments.append(f"SUGGESTED_FIX: {qa_result.suggested_fix}")

        conn.execute(
            """
            UPDATE jobs
            SET status = 'needs_retry',
                qa_comments = ?,
                worker_id = '',
                artifact_path = '',
                started_at = 0,
                completed_at = 0
            WHERE job_id = ?
            """,
            (json.dumps(comments), job_id),
        )
        conn.commit()

    logger.info(
        "Requeued job %s with QA comments (attempt %d/%d)",
        job_id, job.attempts + 1, job.max_attempts,
    )
    return get_job(job_id)


def get_job(job_id: str) -> Job:
    """Get a single job by ID."""
    with _LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"Job {job_id} not found")
    return _row_to_job(row)


def get_queue_summary(stage: str) -> dict[str, int]:
    """Return count of jobs by status for a stage."""
    with _LOCK, _conn() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) as cnt
            FROM jobs
            WHERE stage = ?
            GROUP BY status
            """,
            (stage,),
        ).fetchall()
    summary: dict[str, int] = {s.value: 0 for s in JobStatus}
    for r in rows:
        summary[r["status"]] = r["cnt"]
    return summary


# ---------------------------------------------------------------------------
# Provisioner agent API — read queue, assign jobs, mark completion
# ---------------------------------------------------------------------------

def claim_next_pending_job(stage: str) -> Job | None:
    """Provisioner calls this to get the next unassigned job for a stage."""
    with _LOCK, _conn() as conn:
        # Prefer pending, then needs_retry (failed QA, has comments)
        row = conn.execute(
            """
            SELECT * FROM jobs
            WHERE stage = ? AND (status = 'pending' OR status = 'needs_retry')
            ORDER BY
                CASE status WHEN 'needs_retry' THEN 0 ELSE 1 END,
                created_at
            LIMIT 1
            """,
            (stage,),
        ).fetchone()

        if row is None:
            return None

        job = _row_to_job(row)
        conn.execute(
            """
            UPDATE jobs
            SET status = 'assigned', attempts = attempts + 1, started_at = ?
            WHERE job_id = ?
            """,
            (time.time(), job.job_id),
        )
        conn.commit()

    logger.info(
        "Claimed %s job %s (attempt %d) for worker assignment",
        job.job_type.value, job.job_id, job.attempts + 1,
    )
    return get_job(job.job_id)


def mark_job_running(job_id: str, worker_id: str) -> None:
    """Provisioner calls this when a worker starts executing the job."""
    with _LOCK, _conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', worker_id = ? WHERE job_id = ?",
            (worker_id, job_id),
        )
        conn.commit()
    logger.info("Job %s now running on worker %s", job_id, worker_id)


def mark_job_completed(job_id: str, artifact_path: str) -> None:
    """Worker/Provisioner calls this when job is done and artifact is in B2."""
    with _LOCK, _conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'completed', artifact_path = ?, completed_at = ?
            WHERE job_id = ?
            """,
            (artifact_path, time.time(), job_id),
        )
        conn.commit()
    logger.info("Job %s completed, artifact at: %s", job_id, artifact_path)


def mark_job_failed(job_id: str, error_message: str) -> None:
    """Worker/Provisioner calls this on permanent failure."""
    with _LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return

        job = _row_to_job(row)
        if job.attempts >= job.max_attempts:
            conn.execute(
                "UPDATE jobs SET status = 'failed' WHERE job_id = ?",
                (job_id,),
            )
            logger.error(
                "Job %s permanently failed after %d attempts: %s",
                job_id, job.attempts, error_message,
            )
        else:
            conn.execute(
                "UPDATE jobs SET status = 'needs_retry' WHERE job_id = ?",
                (job_id,),
            )
            logger.warning(
                "Job %s failed (attempt %d/%d): %s — will retry",
                job_id, job.attempts, job.max_attempts, error_message,
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def clear_all_jobs() -> int:
    """Delete all jobs — call on successful pipeline completion."""
    with _LOCK, _conn() as conn:
        cur = conn.execute("DELETE FROM jobs")
        conn.commit()
    deleted = cur.rowcount
    logger.info("Cleared %d jobs from queue", deleted)
    return deleted
