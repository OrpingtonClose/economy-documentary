"""
Two-signal worker-bad rule (ARCH-C4, issue #143; absorbs #78).

Spec: diagram 8 in ``docs/ARCHITECTURE_DIAGRAMS.md`` — *single-failure-doesn't-
condemn*.  The fleet coordinator must require **two independent signals**
before marking a worker bad:

    a) a job failure on that worker, AND
    b) an ``infra_agent`` telemetry anomaly on that same worker
       (CUDA error, OOM, process crash, GPU driver fault, NVML/Xid event,
       segfault, ``cuda is unavailable``, etc.)

A single bad clip does not condemn — the prompt could be the problem.
A single passing clip does not exonerate — the worker could be about to die.
Both signals must occur on the same worker within a configurable
``corroboration_window_sec`` (default 300s, env ``WORKER_BAD_SIGNAL_WINDOW_SEC``).

Job-outcome signals are NEVER the sole input.  ``infra_agent`` telemetry is the
gating second signal.  This module is intentionally a plain-callable health
tracker — no LLM reasoning — wired into ``FleetCoordinator`` and fed by both
job results (``record_job_failure``) and ``infra_agent`` escalations
(``record_infra_anomaly``).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Window in which a job failure and an infra anomaly must overlap to
# corroborate each other.  Outside this window the signals are stale and
# a fresh corroborated pair is required before the worker is condemned.
DEFAULT_CORROBORATION_WINDOW_SEC = float(
    os.environ.get("WORKER_BAD_SIGNAL_WINDOW_SEC", "300")
)

# How many recent signals to retain per worker.  Old signals beyond this are
# evicted to keep memory bounded across long pipeline runs.
MAX_SIGNALS_PER_WORKER = 64


# Substrings that indicate an infra-class telemetry anomaly when scanning
# free-form error / escalation strings.  Matched case-insensitively against
# the normalized message.  Kept narrow on purpose: a generic "error" must
# never qualify as the gating second signal.
_INFRA_ANOMALY_PATTERNS = (
    r"\bcuda\b",
    r"cudnn",
    r"\bnvml\b",
    r"\bxid\b",
    r"\boom\b",
    r"out\s+of\s+memory",
    r"gpu\s+driver",
    r"driver\s+reset",
    r"driver\s+fault",
    r"process\s+crash(?:ed)?",
    r"process\s+exit(?:ed)?",
    r"core\s+dump(?:ed)?",
    r"segfault",
    r"segmentation\s+fault",
    r"signal\s+(?:9|11|15|kill|sigkill|sigsegv|sigterm)",
    r"vram\s+exhaust",
    r"thermal\s+throttle",
    r"ecc\s+error",
    r"hardware\s+fault",
)
_INFRA_ANOMALY_RE = re.compile("|".join(_INFRA_ANOMALY_PATTERNS), re.IGNORECASE)


def looks_like_infra_anomaly(message: str) -> bool:
    """Return True iff *message* contains an infra-class anomaly signature.

    Used to classify free-form ``infra_agent`` escalation messages into the
    "telemetry anomaly" bucket that gates worker condemnation.  Job-outcome
    error strings are *not* fed through this — the rule deliberately keeps
    the two signals on independent ingest paths.
    """
    if not message:
        return False
    return bool(_INFRA_ANOMALY_RE.search(message))


# ---------------------------------------------------------------------------
# Signal data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Signal:
    """A single observation about a worker.

    ``kind`` is either ``"job_failure"`` (signal A) or ``"infra_anomaly"``
    (signal B).  ``detail`` carries a short, free-form description used for
    operator-facing diagnostics; it is never re-classified by this module.
    """

    kind: str
    timestamp: float
    detail: str = ""
    clip_id: str = ""


@dataclass
class WorkerVerdict:
    """Outcome of ``WorkerHealthTracker.evaluate(worker_id)``."""

    worker_id: str
    bad: bool
    reason: str = ""
    job_signal: Optional[Signal] = None
    infra_signal: Optional[Signal] = None
    signal_gap_sec: Optional[float] = None
    window_sec: float = 0.0
    job_signal_count: int = 0
    infra_signal_count: int = 0


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class WorkerHealthTracker:
    """Two-signal worker-bad health bookkeeping.

    Thread-safe.  Holds two short ring buffers per worker (job failures and
    infra anomalies) and exposes :meth:`evaluate` / :meth:`is_worker_bad`
    that returns True iff there is at least one signal of each kind whose
    timestamps are within ``corroboration_window_sec`` of each other.

    Job-outcome signals alone never condemn; infra signals alone never
    condemn.  This is the entire point — see diagram 8.

    Once a worker is condemned, the verdict is sticky for the lifetime of
    this tracker (the coordinator decides what to do next: recycle, cordon,
    redispatch).  Use :meth:`reset_worker` to clear after the worker is
    replaced.
    """

    def __init__(
        self,
        corroboration_window_sec: float = DEFAULT_CORROBORATION_WINDOW_SEC,
        clock: Optional[Any] = None,
    ) -> None:
        if corroboration_window_sec <= 0:
            raise ValueError(
                f"corroboration_window_sec must be positive, got {corroboration_window_sec!r}"
            )
        self._window = float(corroboration_window_sec)
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._job_failures: dict[str, deque[Signal]] = defaultdict(
            lambda: deque(maxlen=MAX_SIGNALS_PER_WORKER)
        )
        self._infra_anomalies: dict[str, deque[Signal]] = defaultdict(
            lambda: deque(maxlen=MAX_SIGNALS_PER_WORKER)
        )
        self._condemned: dict[str, WorkerVerdict] = {}

    # ------------------------------------------------------------------
    # Config introspection
    # ------------------------------------------------------------------

    @property
    def corroboration_window_sec(self) -> float:
        return self._window

    # ------------------------------------------------------------------
    # Signal ingest
    # ------------------------------------------------------------------

    def record_job_failure(
        self,
        worker_id: str,
        error: str = "",
        clip_id: str = "",
        timestamp: Optional[float] = None,
    ) -> WorkerVerdict:
        """Record signal A: a clip-generation failure attributed to *worker_id*.

        Returns the current verdict after the new signal is folded in.  This
        signal alone NEVER condemns the worker — the verdict is only ``bad``
        if a corroborating infra anomaly also sits within the window.
        """
        if not worker_id:
            # Fail loud: callers must attribute failures to a worker.
            raise ValueError("record_job_failure: worker_id is required")
        ts = self._clock() if timestamp is None else float(timestamp)
        sig = Signal(kind="job_failure", timestamp=ts, detail=error or "", clip_id=clip_id or "")
        with self._lock:
            self._job_failures[worker_id].append(sig)
        verdict = self.evaluate(worker_id)
        if verdict.bad:
            logger.warning(
                "WorkerHealthTracker: %s CONDEMNED via job_failure corroboration "
                "(gap=%.1fs, window=%.0fs)",
                worker_id, verdict.signal_gap_sec or 0.0, self._window,
            )
        return verdict

    def record_infra_anomaly(
        self,
        worker_id: str,
        kind: str = "",
        message: str = "",
        timestamp: Optional[float] = None,
    ) -> WorkerVerdict:
        """Record signal B: an ``infra_agent`` telemetry anomaly for *worker_id*.

        ``kind`` is a short tag (``"cuda_error"``, ``"oom"``, ``"driver_fault"``,
        ``"process_crash"`` …) used for diagnostics.  This signal alone NEVER
        condemns the worker; corroboration requires a job failure within the
        same window.
        """
        if not worker_id:
            raise ValueError("record_infra_anomaly: worker_id is required")
        ts = self._clock() if timestamp is None else float(timestamp)
        detail = kind.strip() if kind else ""
        if message:
            detail = f"{detail}: {message}".strip(": ").strip() if detail else message
        sig = Signal(kind="infra_anomaly", timestamp=ts, detail=detail or "")
        with self._lock:
            self._infra_anomalies[worker_id].append(sig)
        verdict = self.evaluate(worker_id)
        if verdict.bad:
            logger.warning(
                "WorkerHealthTracker: %s CONDEMNED via infra_anomaly corroboration "
                "(gap=%.1fs, window=%.0fs)",
                worker_id, verdict.signal_gap_sec or 0.0, self._window,
            )
        return verdict

    def ingest_infra_message(
        self,
        worker_id: str,
        message: str,
        timestamp: Optional[float] = None,
    ) -> Optional[WorkerVerdict]:
        """Convenience hook: classify *message* and, if it looks like an infra
        anomaly, record it.  Returns the verdict if recorded, else ``None``.

        This lets ``FleetCoordinator`` scan ``infra_agent`` escalation logs
        without duplicating the keyword classifier.  Generic strings that
        don't match the anomaly signatures are ignored — they cannot become
        the gating second signal.
        """
        if not worker_id or not message:
            return None
        if not looks_like_infra_anomaly(message):
            return None
        return self.record_infra_anomaly(
            worker_id=worker_id,
            kind="telemetry",
            message=message,
            timestamp=timestamp,
        )

    # ------------------------------------------------------------------
    # Verdict
    # ------------------------------------------------------------------

    def evaluate(self, worker_id: str) -> WorkerVerdict:
        """Return the current two-signal verdict for *worker_id*.

        ``bad`` is True iff:
        - at least one ``job_failure`` AND at least one ``infra_anomaly``
          have been recorded for this worker, AND
        - some pair of these signals (one of each kind) has timestamps
          within ``corroboration_window_sec`` of each other.

        A worker that was previously condemned stays condemned (sticky)
        until :meth:`reset_worker` is called.
        """
        with self._lock:
            jobs = list(self._job_failures.get(worker_id, ()))
            infras = list(self._infra_anomalies.get(worker_id, ()))
            sticky = self._condemned.get(worker_id)

        if sticky is not None:
            # Re-emit sticky verdict but refresh counts.
            return WorkerVerdict(
                worker_id=worker_id,
                bad=True,
                reason=sticky.reason,
                job_signal=sticky.job_signal,
                infra_signal=sticky.infra_signal,
                signal_gap_sec=sticky.signal_gap_sec,
                window_sec=self._window,
                job_signal_count=len(jobs),
                infra_signal_count=len(infras),
            )

        if not jobs or not infras:
            reason = self._explain_insufficient(jobs, infras)
            return WorkerVerdict(
                worker_id=worker_id,
                bad=False,
                reason=reason,
                window_sec=self._window,
                job_signal_count=len(jobs),
                infra_signal_count=len(infras),
            )

        # Find the closest cross-kind pair.  Both lists are short (bounded by
        # MAX_SIGNALS_PER_WORKER) so the O(n*m) scan is fine.
        best_gap = float("inf")
        best_pair: Optional[tuple[Signal, Signal]] = None
        for j in jobs:
            for i in infras:
                gap = abs(j.timestamp - i.timestamp)
                if gap < best_gap:
                    best_gap = gap
                    best_pair = (j, i)

        if best_pair is None or best_gap > self._window:
            reason = (
                f"have {len(jobs)} job_failure + {len(infras)} infra_anomaly signal(s) "
                f"but closest pair is {best_gap:.1f}s apart "
                f"(> corroboration window {self._window:.0f}s)"
            )
            return WorkerVerdict(
                worker_id=worker_id,
                bad=False,
                reason=reason,
                job_signal=best_pair[0] if best_pair else None,
                infra_signal=best_pair[1] if best_pair else None,
                signal_gap_sec=best_gap if best_pair else None,
                window_sec=self._window,
                job_signal_count=len(jobs),
                infra_signal_count=len(infras),
            )

        reason = (
            f"two-signal corroboration: job_failure '{best_pair[0].detail[:80]}' + "
            f"infra_anomaly '{best_pair[1].detail[:80]}' within {best_gap:.1f}s "
            f"(window {self._window:.0f}s)"
        )
        verdict = WorkerVerdict(
            worker_id=worker_id,
            bad=True,
            reason=reason,
            job_signal=best_pair[0],
            infra_signal=best_pair[1],
            signal_gap_sec=best_gap,
            window_sec=self._window,
            job_signal_count=len(jobs),
            infra_signal_count=len(infras),
        )
        with self._lock:
            self._condemned[worker_id] = verdict
        return verdict

    def is_worker_bad(self, worker_id: str) -> bool:
        """Convenience: ``evaluate(worker_id).bad``."""
        return self.evaluate(worker_id).bad

    def condemned_workers(self) -> list[str]:
        """Return the sorted list of condemned worker ids."""
        with self._lock:
            return sorted(self._condemned.keys())

    # ------------------------------------------------------------------
    # Lifecycle / reset
    # ------------------------------------------------------------------

    def reset_worker(self, worker_id: str) -> None:
        """Clear all signals and any condemnation for *worker_id*.

        Called by the coordinator after a worker is recycled/replaced so
        its replacement is judged on its own evidence rather than inheriting
        the dead worker's history.
        """
        with self._lock:
            self._job_failures.pop(worker_id, None)
            self._infra_anomalies.pop(worker_id, None)
            self._condemned.pop(worker_id, None)

    def snapshot(self) -> dict[str, dict]:
        """Return a JSON-serialisable snapshot of all tracked workers.

        Useful for the dashboard / status endpoint.
        """
        with self._lock:
            workers = sorted(
                set(self._job_failures.keys()) | set(self._infra_anomalies.keys())
            )
            out: dict[str, dict] = {}
            for w in workers:
                jobs = list(self._job_failures.get(w, ()))
                infras = list(self._infra_anomalies.get(w, ()))
                cond = self._condemned.get(w)
                out[w] = {
                    "job_failure_count": len(jobs),
                    "infra_anomaly_count": len(infras),
                    "condemned": cond is not None,
                    "condemnation_reason": cond.reason if cond else "",
                    "last_job_failure_ts": jobs[-1].timestamp if jobs else 0.0,
                    "last_infra_anomaly_ts": infras[-1].timestamp if infras else 0.0,
                }
            return out

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _explain_insufficient(jobs, infras) -> str:
        if not jobs and not infras:
            return "no signals recorded"
        if jobs and not infras:
            return (
                f"have {len(jobs)} job_failure signal(s) but zero infra_anomaly signals — "
                f"single failure does not condemn (the prompt could be the problem)"
            )
        if infras and not jobs:
            return (
                f"have {len(infras)} infra_anomaly signal(s) but zero job_failure signals — "
                f"awaiting corroboration before condemning"
            )
        return "insufficient signals"
