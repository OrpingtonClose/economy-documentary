"""
Infrastructure overseer — reads VM agent status, processes escalations,
makes lifecycle decisions.

Architecture (VM-side agent model):

    Each GPU VM runs its own **VMAgent** (in ``scripts/gpu_worker.py``)
    that manages the full lifecycle: bootstrap, model loading,
    self-monitoring (GPU health, disk, VRAM, temperature), local
    recovery, and structured escalation.

    This module is the **central overseer** that runs on the backend
    server.  It does NOT duplicate the monitoring the VM agents already
    perform.  Instead it:

    1. **Reads VM agent status** — every ``poll_interval`` seconds, hit
       each VM's ``/status`` endpoint to get rich data (bootstrap phase,
       health snapshot, escalation log, task tracking).
    2. **Processes escalations** — reads unacked escalation events from
       each VM agent and acts on them (log, pause pipeline, alert).
    3. **Acknowledges escalations** — once processed, POSTs back to
       ``/escalations/ack`` so the VM agent can prune its log.
    4. **Pipeline pause** — when a critical VM agent reports errors or
       unrecoverable failures, pauses the pipeline.
    5. **Stage timing watchdog** — tracks how long the current stage has
       been running.  ``2×`` expected duration → warning; ``4×`` → critical.
    6. **Lifecycle decisions** — based on VM agent status, decides whether
       to keep waiting, restart a VM, or alert the operator.

Architecture invariants enforced:

- One model per VM — never share, never swap.
- Every required service must be confirmed healthy before pipeline start.
- Never silently degrade — if a critical worker dies, stop and report loud.
- The overseer READS from VM agents; it does NOT do the work they do.

Usage::

    from infra_agent import start_infra_agent

    infra = start_infra_agent(poll_interval=30.0)
    infra.start()          # launches a daemon thread
    # ... run the pipeline ...
    infra.shutdown()        # joins the thread
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
    """Central overseer that reads VM agent status and makes lifecycle decisions.

    Each VM runs its own VMAgent (in scripts/gpu_worker.py) that handles
    bootstrap, self-monitoring, local recovery, and escalation.  This
    overseer reads the rich /status endpoint from each VM agent and:

    - Processes escalation events forwarded by VM agents
    - Pauses the pipeline when critical workers go down
    - Tracks stage timing and alerts on slow stages
    - Auto-resumes when workers recover
    - Uploads status to B2 for external dashboards

    Thread-safe: the pipeline (running in its own asyncio loop or thread)
    can call ``is_paused()``, ``get_status()``, ``notify_stage_start()``
    etc. at any time.

    Parameters
    ----------
    poll_interval:
        Seconds between reading VM agent status (default 30).
    max_consecutive_failures:
        How many consecutive failures before escalating (default 3).
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
            "Overseer discovered %d workers: %s",
            len(workers),
            ", ".join(f"{w.role.value}@{w.url}" for w in workers),
        )

    # ------------------------------------------------------------------
    # Health polling
    # ------------------------------------------------------------------

    def _check_worker_health(self, worker: WorkerSnapshot) -> None:
        """Probe worker via GET / (plain text).  No /status, no /health fallback.

        Plain-text protocol: ``ok {gpu} tts={yes|no} ltx={yes|no} vram={used}/{total}GB mode={mode}``
        Updates the worker snapshot in-place.
        """
        health_url = f"{worker.url.rstrip('/')}/"
        try:
            req = Request(health_url)
            with urlopen(req, timeout=10) as resp:
                text = resp.read().decode().strip()
        except Exception as exc:
            worker.status = WorkerStatus.UNREACHABLE
            worker.consecutive_failures += 1
            worker.last_error = str(exc)
            worker.last_check = time.time()
            worker.model_loaded = False
            return

        worker.last_check = time.time()
        parts = text.split()
        if not parts or parts[0] != "ok":
            worker.status = WorkerStatus.DEGRADED
            worker.consecutive_failures += 1
            worker.last_error = f"unhealthy status: {parts[0] if parts else 'empty'}"
            worker.model_loaded = False
            return

        # Parse VRAM
        vram_used = 0.0
        vram_total = 0.0
        gpu_name = ""
        for part in parts[1:]:
            if part.startswith("vram="):
                vram_str = part.split("=", 1)[1]
                if "/" in vram_str:
                    used_str, total_str = vram_str.split("/", 1)
                    vram_used = float(used_str)
                    vram_total = float(total_str.replace("GB", ""))
            elif not part.startswith(("tts=", "ltx=", "mode=")) and not "=" in part:
                gpu_name = part

        worker.gpu_name = gpu_name
        worker.vram_used_gb = vram_used
        worker.vram_total_gb = vram_total

        capability = "tts" if worker.role == WorkerRole.TTS else "ltx"
        if f"{capability}=yes" not in text:
            worker.status = WorkerStatus.DEGRADED
            worker.consecutive_failures += 1
            worker.last_error = f"{capability} not loaded"
            worker.model_loaded = False
            return

        # All good
        if worker.consecutive_failures > 0:
            logger.info(
                "Overseer: %s worker at %s recovered after %d failures",
                worker.role.value, worker.url, worker.consecutive_failures,
            )
        worker.status = WorkerStatus.HEALTHY
        worker.consecutive_failures = 0
        worker.last_error = ""
        worker.model_loaded = True

    def _process_vm_escalations(self, worker: WorkerSnapshot, status_data: dict) -> None:
        """Process escalation events from a VM agent.

        Reads unacked escalations from the /status response and forwards
        critical ones to the overseer's own escalation log.  Then acknowledges
        them on the VM agent.
        """
        escalations_data = status_data.get("escalations", {})
        recent = escalations_data.get("recent", [])
        if not recent:
            return

        max_ts = 0.0
        for esc in recent:
            if esc.get("acked"):
                continue
            ts = esc.get("timestamp", 0.0)
            max_ts = max(max_ts, ts)
            sev_str = esc.get("severity", "info")
            severity = {
                "info": Severity.INFO,
                "warning": Severity.WARNING,
                "critical": Severity.CRITICAL,
            }.get(sev_str, Severity.WARNING)

            # Forward to overseer's escalation log with source prefix
            self._escalate(
                severity=severity,
                source=f"vm:{worker.role.value}:{esc.get('source', 'unknown')}",
                message=f"[VM Agent] {esc.get('message', '')}",
                details=esc.get("details", {}),
            )

        # Acknowledge processed escalations on the VM agent
        if max_ts > 0:
            try:
                ack_url = f"{worker.url.rstrip('/')}/escalations/ack?before_ts={max_ts}"
                ack_req = Request(ack_url, method="POST", data=b"")
                with urlopen(ack_req, timeout=5) as resp:
                    resp.read()  # consume response
            except Exception as exc:
                logger.debug(
                    "Overseer: failed to ack escalations on %s: %s",
                    worker.url, exc,
                )

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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            return self._paused

    def get_pause_reason(self) -> str:
        """Return the reason the pipeline was paused, or empty string."""
        with self._lock:
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

        log_fn("OVERSEER [%s] %s: %s", severity.value, source, message)

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

            paused = self._paused
            pause_reason = self._pause_reason
            total_escalations = len(self._escalations)

        return {
            "paused": paused,
            "pause_reason": pause_reason,
            "workers": workers,
            "current_stage": stage,
            "recent_escalations": recent_escalations,
            "total_escalations": total_escalations,
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
        logger.info("Overseer: monitoring started (poll_interval=%.0fs)", self._poll_interval)

        # Initial health check
        self._poll_all_workers()
        logger.info("Overseer: initial health: %s", self.get_worker_summary())

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
            if self.is_paused():
                self._check_auto_resume()

        logger.info("Overseer: monitoring stopped after %d polls", poll_count)

    def _check_auto_resume(self) -> None:
        """Auto-resume pipeline if the worker that caused the pause has recovered."""
        with self._lock:
            if not self._paused:
                return
            pause_reason = self._pause_reason
            workers = list(self._workers)

        # Check if the pause was caused by TTS or video worker
        if "TTS worker" in pause_reason:
            tts_healthy = any(
                w.role == WorkerRole.TTS and w.status == WorkerStatus.HEALTHY
                for w in workers
            )
            if tts_healthy:
                logger.info("InfraAgent: TTS worker recovered — auto-resuming pipeline")
                self.resume()
                return

        if "video workers" in pause_reason.lower():
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
        logger.info("Overseer: added %s worker at %s", role.value, url)

    def remove_worker(self, url: str) -> None:
        """Unregister a worker (e.g. after terminating a VM).

        Thread-safe.
        """
        with self._lock:
            self._workers = [w for w in self._workers if w.url != url]
        logger.info("Overseer: removed worker at %s", url)

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

    The caller should call ``agent.start()`` to launch the daemon thread.
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
