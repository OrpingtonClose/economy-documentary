"""
Active infrastructure agent — continuous health monitoring, auto-recovery,
and escalation.

The contracts in ``contracts.py`` are **passive gates** checked at stage
transitions.  This module is the **active watchdog** that runs continuously
alongside the pipeline:

1. **Worker health polling** — every ``poll_interval`` seconds, hit each
   worker's ``/health`` endpoint.  Track consecutive failures.
2. **Auto-recovery** — when a worker fails, wait and retry.  If the worker
   stays down, mark it degraded.
3. **Pipeline pause** — when a *critical* worker (TTS during audio stage,
   video during production stage) goes down, set a pause flag that
   callbacks can check before starting expensive work.
4. **Stage timing watchdog** — track how long the current stage has been
   running.  ``2×`` expected duration → warning; ``4×`` → critical alert.
5. **Escalation ladder** — log → retry → pause → alert operator.

Architecture invariants enforced:

- One model per VM — never share, never swap.
- Every required service must be confirmed healthy before pipeline start
  (pre-flight in ``run_pipeline.py``).  This agent enforces the same rule
  *continuously* throughout the run.
- Never silently degrade — if a critical worker dies, stop and report loud.

Usage::

    from infra_agent import InfraAgent

    agent = InfraAgent()
    # Start monitoring in a background asyncio task
    task = asyncio.create_task(agent.run())
    # ... run the pipeline ...
    agent.shutdown()
    await task
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class WorkerRole(str, Enum):
    """Role a GPU worker serves in the pipeline."""
    TTS = "tts"
    VIDEO = "video"


class WorkerStatus(str, Enum):
    """Health status of a single worker."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"      # responding but model not loaded / unhealthy status
    UNREACHABLE = "unreachable"  # connection failed
    UNKNOWN = "unknown"        # not yet checked


class Severity(str, Enum):
    """Escalation severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class WorkerSnapshot:
    """Point-in-time health snapshot for a single worker."""
    url: str
    role: WorkerRole
    status: WorkerStatus = WorkerStatus.UNKNOWN
    last_check: float = 0.0
    consecutive_failures: int = 0
    last_error: str = ""
    vram_used_gb: float = 0.0
    vram_total_gb: float = 0.0
    gpu_name: str = ""
    model_loaded: bool = False


@dataclass
class StageTimingEntry:
    """Timing data for a single pipeline stage."""
    stage: str
    started_at: float = 0.0
    expected_duration_sec: float = 0.0
    warning_emitted: bool = False
    critical_emitted: bool = False


@dataclass
class EscalationEvent:
    """A single escalation event for the operator log."""
    timestamp: float
    severity: Severity
    source: str  # e.g. "worker:tts", "stage:audio", "pipeline"
    message: str
    details: dict = field(default_factory=dict)


# Expected stage durations (seconds).  2× triggers warning, 4× triggers
# critical.  These are generous estimates for a typical 5-scene documentary.
_EXPECTED_STAGE_DURATIONS: dict[str, float] = {
    "scenario": 120.0,       # 2 min (LLM only)
    "audio": 600.0,          # 10 min (TTS for ~30 clips)
    "visual_direction": 300.0,  # 5 min (LLM only)
    "production": 3600.0,    # 60 min (video generation on GPU)
    "assembly": 300.0,       # 5 min (ffmpeg)
}


# ---------------------------------------------------------------------------
# InfraAgent
# ---------------------------------------------------------------------------


class InfraAgent:
    """Active infrastructure monitor that runs alongside the pipeline.

    Thread-safe: the pipeline (running in its own asyncio loop or thread)
    can call ``is_paused()``, ``get_status()``, ``notify_stage_start()``
    etc. at any time.

    Parameters
    ----------
    poll_interval:
        Seconds between health polls (default 30).
    max_consecutive_failures:
        How many consecutive failures before marking a worker degraded
        and escalating (default 3).
    """

    def __init__(
        self,
        poll_interval: float = 30.0,
        max_consecutive_failures: int = 3,
    ) -> None:
        self._poll_interval = poll_interval
        self._max_consecutive_failures = max_consecutive_failures

        # Worker registry — populated at startup from env vars
        self._workers: list[WorkerSnapshot] = []
        self._lock = threading.Lock()

        # Pipeline state
        self._current_stage: Optional[StageTimingEntry] = None
        self._paused = False
        self._pause_reason = ""
        self._shutdown_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Escalation log
        self._escalations: list[EscalationEvent] = []

        # Populate worker list from environment
        self._discover_workers()

    # ------------------------------------------------------------------
    # Worker discovery
    # ------------------------------------------------------------------

    def _discover_workers(self) -> None:
        """Read worker URLs from environment and register them."""
        workers: list[WorkerSnapshot] = []

        # TTS worker
        tts_url = os.environ.get("TTS_WORKER_URL", "")
        if tts_url:
            workers.append(WorkerSnapshot(url=tts_url.strip(), role=WorkerRole.TTS))

        # Video workers (comma-separated)
        video_urls_str = os.environ.get("VIDEO_WORKER_URLS", "")
        video_urls = [u.strip() for u in video_urls_str.split(",") if u.strip()] if video_urls_str else []
        gpu_url = os.environ.get("GPU_WORKER_URL", "")
        if gpu_url and gpu_url.strip() not in video_urls:
            video_urls.append(gpu_url.strip())

        for vurl in video_urls:
            workers.append(WorkerSnapshot(url=vurl, role=WorkerRole.VIDEO))

        with self._lock:
            self._workers = workers

        logger.info(
            "InfraAgent discovered %d workers: %s",
            len(workers),
            ", ".join(f"{w.role.value}@{w.url}" for w in workers),
        )

    # ------------------------------------------------------------------
    # Health polling
    # ------------------------------------------------------------------

    def _check_worker_health(self, worker: WorkerSnapshot) -> None:
        """Poll a single worker's /health endpoint (synchronous).

        Updates the worker snapshot in-place.
        """
        health_url = f"{worker.url.rstrip('/')}/health"
        try:
            req = Request(health_url)
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            worker.status = WorkerStatus.UNREACHABLE
            worker.consecutive_failures += 1
            worker.last_error = str(exc)
            worker.last_check = time.time()
            worker.model_loaded = False
            return

        worker.last_check = time.time()
        worker.gpu_name = data.get("gpu", "")
        worker.vram_used_gb = float(data.get("vram_used_gb", 0))
        worker.vram_total_gb = float(data.get("vram_total_gb", 0))

        if data.get("status") != "ok":
            worker.status = WorkerStatus.DEGRADED
            worker.consecutive_failures += 1
            worker.last_error = f"unhealthy status: {data.get('status')}"
            worker.model_loaded = False
            return

        # Check model loaded
        capability = "tts" if worker.role == WorkerRole.TTS else "ltx"
        loaded_key = f"{capability}_loaded"
        if not data.get(loaded_key, False):
            worker.status = WorkerStatus.DEGRADED
            worker.consecutive_failures += 1
            worker.last_error = f"{capability} model not loaded"
            worker.model_loaded = False
            return

        # All good
        if worker.consecutive_failures > 0:
            logger.info(
                "InfraAgent: %s worker at %s recovered after %d failures",
                worker.role.value, worker.url, worker.consecutive_failures,
            )
        worker.status = WorkerStatus.HEALTHY
        worker.consecutive_failures = 0
        worker.last_error = ""
        worker.model_loaded = True

    def _poll_all_workers(self) -> None:
        """Poll all registered workers and handle escalation."""
        with self._lock:
            workers = list(self._workers)

        for w in workers:
            self._check_worker_health(w)

            # Escalation logic
            if w.consecutive_failures == self._max_consecutive_failures:
                self._escalate(
                    severity=Severity.WARNING,
                    source=f"worker:{w.role.value}",
                    message=(
                        f"{w.role.value.upper()} worker at {w.url} has failed "
                        f"{w.consecutive_failures} consecutive health checks. "
                        f"Last error: {w.last_error}"
                    ),
                    details={"url": w.url, "failures": w.consecutive_failures},
                )

            if w.consecutive_failures == self._max_consecutive_failures * 2:
                # Double the threshold — escalate to critical
                self._escalate(
                    severity=Severity.CRITICAL,
                    source=f"worker:{w.role.value}",
                    message=(
                        f"{w.role.value.upper()} worker at {w.url} has been "
                        f"unreachable for {w.consecutive_failures} checks "
                        f"({w.consecutive_failures * self._poll_interval:.0f}s). "
                        f"Manual intervention required."
                    ),
                    details={"url": w.url, "failures": w.consecutive_failures},
                )

            # Auto-pause if a critical worker for the current stage is down
            if w.consecutive_failures >= self._max_consecutive_failures:
                self._maybe_pause_for_worker(w)

    def _maybe_pause_for_worker(self, worker: WorkerSnapshot) -> None:
        """Pause the pipeline if the downed worker is critical for the current stage."""
        stage = self._current_stage
        if stage is None:
            return

        # TTS is critical during audio stage
        if worker.role == WorkerRole.TTS and stage.stage == "audio":
            self._pause(
                f"TTS worker at {worker.url} is down during audio stage. "
                f"Pipeline paused to prevent generating with synthetic fallbacks."
            )
            return

        # Video workers are critical during production stage
        if worker.role == WorkerRole.VIDEO and stage.stage == "production":
            # Only pause if ALL video workers are down
            with self._lock:
                healthy_video = sum(
                    1 for w in self._workers
                    if w.role == WorkerRole.VIDEO and w.status == WorkerStatus.HEALTHY
                )
            if healthy_video == 0:
                self._pause(
                    f"All video workers are down during production stage. "
                    f"Pipeline paused to prevent generating with placeholder video."
                )

    # ------------------------------------------------------------------
    # Stage timing watchdog
    # ------------------------------------------------------------------

    def notify_stage_start(self, stage: str) -> None:
        """Called by pipeline callbacks when a stage begins.

        Thread-safe.
        """
        expected = _EXPECTED_STAGE_DURATIONS.get(stage, 600.0)
        with self._lock:
            self._current_stage = StageTimingEntry(
                stage=stage,
                started_at=time.time(),
                expected_duration_sec=expected,
            )
        logger.info(
            "InfraAgent: stage '%s' started (expected %.0fs)",
            stage, expected,
        )

    def notify_stage_complete(self, stage: str) -> None:
        """Called by pipeline callbacks when a stage completes.

        Thread-safe.
        """
        with self._lock:
            if self._current_stage and self._current_stage.stage == stage:
                elapsed = time.time() - self._current_stage.started_at
                logger.info(
                    "InfraAgent: stage '%s' completed in %.1fs (expected %.0fs, %.1f×)",
                    stage, elapsed,
                    self._current_stage.expected_duration_sec,
                    elapsed / max(self._current_stage.expected_duration_sec, 1),
                )
                self._current_stage = None

    def _check_stage_timing(self) -> None:
        """Check if the current stage is running longer than expected."""
        with self._lock:
            stage = self._current_stage
        if stage is None:
            return

        elapsed = time.time() - stage.started_at
        ratio = elapsed / max(stage.expected_duration_sec, 1)

        if ratio >= 4.0 and not stage.critical_emitted:
            stage.critical_emitted = True
            self._escalate(
                severity=Severity.CRITICAL,
                source=f"stage:{stage.stage}",
                message=(
                    f"Stage '{stage.stage}' has been running for {elapsed:.0f}s "
                    f"(4× expected {stage.expected_duration_sec:.0f}s). "
                    f"Possible hang — manual investigation required."
                ),
                details={
                    "stage": stage.stage,
                    "elapsed_sec": round(elapsed),
                    "expected_sec": round(stage.expected_duration_sec),
                    "ratio": round(ratio, 1),
                },
            )
        elif ratio >= 2.0 and not stage.warning_emitted:
            stage.warning_emitted = True
            self._escalate(
                severity=Severity.WARNING,
                source=f"stage:{stage.stage}",
                message=(
                    f"Stage '{stage.stage}' is running slowly: {elapsed:.0f}s "
                    f"(2× expected {stage.expected_duration_sec:.0f}s). "
                    f"May be normal for large documentaries, monitoring."
                ),
                details={
                    "stage": stage.stage,
                    "elapsed_sec": round(elapsed),
                    "expected_sec": round(stage.expected_duration_sec),
                    "ratio": round(ratio, 1),
                },
            )

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------

    def _pause(self, reason: str) -> None:
        """Pause the pipeline (set flag that callbacks check)."""
        if self._paused:
            return  # already paused
        self._paused = True
        self._pause_reason = reason
        self._escalate(
            severity=Severity.CRITICAL,
            source="pipeline",
            message=f"Pipeline PAUSED: {reason}",
        )

    def resume(self) -> None:
        """Resume the pipeline after manual intervention.

        Thread-safe.  Called by an operator (or future auto-recovery logic)
        after the root cause is fixed.
        """
        if not self._paused:
            return
        self._paused = False
        old_reason = self._pause_reason
        self._pause_reason = ""
        self._escalate(
            severity=Severity.INFO,
            source="pipeline",
            message=f"Pipeline RESUMED (was paused for: {old_reason})",
        )

    def is_paused(self) -> bool:
        """Check if the pipeline is currently paused.

        Thread-safe.  Pipeline callbacks should call this before starting
        expensive work (TTS generation, video generation).
        """
        return self._paused

    def get_pause_reason(self) -> str:
        """Return the reason the pipeline was paused, or empty string."""
        return self._pause_reason

    # ------------------------------------------------------------------
    # Escalation
    # ------------------------------------------------------------------

    def _escalate(
        self,
        severity: Severity,
        source: str,
        message: str,
        details: Optional[dict] = None,
    ) -> None:
        """Record an escalation event and log it."""
        event = EscalationEvent(
            timestamp=time.time(),
            severity=severity,
            source=source,
            message=message,
            details=details or {},
        )
        with self._lock:
            self._escalations.append(event)

        log_fn = {
            Severity.INFO: logger.info,
            Severity.WARNING: logger.warning,
            Severity.CRITICAL: logger.critical,
        }.get(severity, logger.warning)

        log_fn("INFRA-AGENT [%s] %s: %s", severity.value, source, message)

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return a JSON-serialisable status snapshot.

        Thread-safe.  Can be called from dashboard endpoints, logging,
        or operator queries.
        """
        with self._lock:
            workers = [
                {
                    "url": w.url,
                    "role": w.role.value,
                    "status": w.status.value,
                    "consecutive_failures": w.consecutive_failures,
                    "last_error": w.last_error,
                    "last_check": w.last_check,
                    "vram_used_gb": w.vram_used_gb,
                    "vram_total_gb": w.vram_total_gb,
                    "gpu_name": w.gpu_name,
                    "model_loaded": w.model_loaded,
                }
                for w in self._workers
            ]

            stage = None
            if self._current_stage:
                elapsed = time.time() - self._current_stage.started_at
                stage = {
                    "name": self._current_stage.stage,
                    "elapsed_sec": round(elapsed, 1),
                    "expected_sec": round(self._current_stage.expected_duration_sec),
                    "ratio": round(
                        elapsed / max(self._current_stage.expected_duration_sec, 1),
                        2,
                    ),
                }

            recent_escalations = [
                {
                    "timestamp": e.timestamp,
                    "severity": e.severity.value,
                    "source": e.source,
                    "message": e.message,
                }
                for e in self._escalations[-20:]  # last 20
            ]

        return {
            "paused": self._paused,
            "pause_reason": self._pause_reason,
            "workers": workers,
            "current_stage": stage,
            "recent_escalations": recent_escalations,
            "total_escalations": len(self._escalations),
        }

    def get_worker_summary(self) -> str:
        """Return a human-readable one-line worker summary."""
        with self._lock:
            workers = list(self._workers)

        if not workers:
            return "No workers registered"

        parts = []
        for w in workers:
            emoji = {
                WorkerStatus.HEALTHY: "OK",
                WorkerStatus.DEGRADED: "DEGRADED",
                WorkerStatus.UNREACHABLE: "DOWN",
                WorkerStatus.UNKNOWN: "?",
            }[w.status]
            parts.append(f"{w.role.value}@{w.url}: {emoji}")

        return " | ".join(parts)

    # ------------------------------------------------------------------
    # B2 status upload
    # ------------------------------------------------------------------

    def _upload_status_to_b2(self) -> None:
        """Upload current infra status to B2 for external monitoring.

        Non-critical — failure is logged but does not affect monitoring.
        """
        try:
            from tools.b2_checkpoint import upload_json
            status = self.get_status()
            upload_json(status, "infra/infra_status.json")
        except Exception as exc:
            logger.debug("InfraAgent: B2 status upload failed: %s", exc)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the monitoring loop on a daemon thread.

        The thread runs independently of the asyncio event loop, so
        ``time.sleep()`` in pipeline callbacks (``check_infra_pause()``)
        cannot deadlock the monitoring loop.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("InfraAgent: already running")
            return
        self._shutdown_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="infra-agent",
            daemon=True,
        )
        self._thread.start()

    def _run_loop(self) -> None:
        """Main monitoring loop (runs on a daemon thread).

        Uses ``threading.Event.wait(timeout)`` for interruptible sleep.
        """
        logger.info("InfraAgent: monitoring started (poll_interval=%.0fs)", self._poll_interval)

        # Initial health check
        self._poll_all_workers()
        logger.info("InfraAgent: initial health: %s", self.get_worker_summary())

        poll_count = 0
        while not self._shutdown_event.is_set():
            # Sleep for poll_interval, waking early if shutdown is requested
            if self._shutdown_event.wait(timeout=self._poll_interval):
                break  # shutdown was requested

            self._poll_all_workers()
            self._check_stage_timing()

            poll_count += 1

            # Upload status to B2 every 5 polls (reduce API calls)
            if poll_count % 5 == 0:
                self._upload_status_to_b2()

            # Auto-unpause if workers recovered
            if self._paused:
                self._check_auto_resume()

        logger.info("InfraAgent: monitoring stopped after %d polls", poll_count)

    def _check_auto_resume(self) -> None:
        """Auto-resume pipeline if the worker that caused the pause has recovered."""
        if not self._paused:
            return

        with self._lock:
            workers = list(self._workers)

        # Check if the pause was caused by TTS or video worker
        if "TTS worker" in self._pause_reason:
            tts_healthy = any(
                w.role == WorkerRole.TTS and w.status == WorkerStatus.HEALTHY
                for w in workers
            )
            if tts_healthy:
                logger.info("InfraAgent: TTS worker recovered — auto-resuming pipeline")
                self.resume()
                return

        if "video workers" in self._pause_reason.lower():
            healthy_video = sum(
                1 for w in workers
                if w.role == WorkerRole.VIDEO and w.status == WorkerStatus.HEALTHY
            )
            if healthy_video > 0:
                logger.info(
                    "InfraAgent: %d video worker(s) recovered — auto-resuming pipeline",
                    healthy_video,
                )
                self.resume()
                return

    def shutdown(self) -> None:
        """Signal the monitoring loop to stop and wait for it.

        Thread-safe.  The daemon thread will exit after the current
        poll cycle completes.
        """
        logger.info("InfraAgent: shutdown requested")
        self._shutdown_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                logger.warning("InfraAgent: thread did not stop within 5s")

    # ------------------------------------------------------------------
    # Worker management helpers
    # ------------------------------------------------------------------

    def add_worker(self, url: str, role: WorkerRole) -> None:
        """Register a new worker at runtime (e.g. after provisioning a new VM).

        Thread-safe.
        """
        with self._lock:
            # Avoid duplicates
            if any(w.url == url for w in self._workers):
                logger.warning("InfraAgent: worker %s already registered", url)
                return
            self._workers.append(WorkerSnapshot(url=url, role=role))
        logger.info("InfraAgent: added %s worker at %s", role.value, url)

    def remove_worker(self, url: str) -> None:
        """Unregister a worker (e.g. after terminating a VM).

        Thread-safe.
        """
        with self._lock:
            self._workers = [w for w in self._workers if w.url != url]
        logger.info("InfraAgent: removed worker at %s", url)

    def get_healthy_workers(self, role: Optional[WorkerRole] = None) -> list[str]:
        """Return URLs of all healthy workers, optionally filtered by role.

        Thread-safe.
        """
        with self._lock:
            return [
                w.url for w in self._workers
                if w.status == WorkerStatus.HEALTHY
                and (role is None or w.role == role)
            ]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_infra_agent: Optional[InfraAgent] = None
_infra_lock = threading.Lock()


def get_infra_agent() -> Optional[InfraAgent]:
    """Return the global InfraAgent singleton, or None if not started."""
    return _infra_agent


def start_infra_agent(
    poll_interval: float = 30.0,
    max_consecutive_failures: int = 3,
) -> InfraAgent:
    """Create and return the global InfraAgent singleton.

    The caller is responsible for running ``agent.run()`` in an asyncio task.
    """
    global _infra_agent
    with _infra_lock:
        if _infra_agent is not None:
            return _infra_agent
        _infra_agent = InfraAgent(
            poll_interval=poll_interval,
            max_consecutive_failures=max_consecutive_failures,
        )
    return _infra_agent


def check_infra_pause() -> None:
    """Check if the infra agent has paused the pipeline.

    Call this from pipeline callbacks before starting expensive work.
    If paused, blocks until resumed or raises after timeout.

    Raises ``RuntimeError`` if paused for longer than 10 minutes
    (operator has not intervened).
    """
    agent = get_infra_agent()
    if agent is None:
        return  # infra agent not running — no enforcement

    if not agent.is_paused():
        return

    reason = agent.get_pause_reason()
    logger.warning("Pipeline is PAUSED by infra agent: %s", reason)
    logger.warning("Waiting for worker recovery or manual resume...")

    # Wait up to 10 minutes for resume, checking every 10 seconds
    max_wait = 600  # 10 minutes
    waited = 0
    while agent.is_paused() and waited < max_wait:
        time.sleep(10)
        waited += 10
        if waited % 60 == 0:
            logger.warning(
                "Pipeline still paused (%ds / %ds). Reason: %s",
                waited, max_wait, agent.get_pause_reason(),
            )

    if agent.is_paused():
        raise RuntimeError(
            f"Pipeline has been paused for {max_wait}s without recovery. "
            f"Reason: {reason}. Manual intervention required."
        )

    logger.info("Pipeline resumed after %ds pause", waited)
