"""
Systemic problem detection — fleet-wide pattern analysis.

Runs alongside InfraAgent, analyzing patterns ACROSS VMs that indicate
a larger problem than individual failures.

Patterns detected:
1. Cascade failure — multiple VMs down within a short window
2. Common error signature — same error across multiple VMs
3. Performance degradation — fleet-wide slowdown
4. Poison clips — same clip failing on multiple workers
5. Budget burn anomaly — cost accumulating faster than projected
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from fleet.cost_tracker import CostTracker

logger = logging.getLogger(__name__)


@dataclass
class SystemicPattern:
    """A detected fleet-wide anomaly."""

    pattern_type: str
    severity: str  # "warning" or "critical"
    evidence: list[dict] = field(default_factory=list)
    hypothesis: str = ""
    recommended_action: str = ""
    detected_at: float = field(default_factory=time.time)


class SystemicDetector:
    """Analyzes fleet-wide patterns to detect systemic problems.

    Fed data by the fleet coordinator and InfraAgent.
    Call ``check_patterns()`` periodically (e.g. every poll cycle).
    """

    CASCADE_WINDOW_SEC = 300.0   # 5 min window for cascade detection
    CASCADE_MIN_VMS = 3          # 3+ distinct VMs failing = cascade
    COMMON_ERROR_MIN_VMS = 2     # error on 2+ VMs = common error
    COMMON_ERROR_WINDOW_SEC = 600.0  # 10 min window for common error detection
    PERF_DEGRADATION_FACTOR = 2.0  # 2× slower than baseline = degradation
    BUDGET_BURN_THRESHOLD = 1.3  # 30% over projection

    def __init__(
        self,
        cost_tracker: Optional[CostTracker] = None,
        baseline_gen_time: float = 180.0,
    ) -> None:
        self._lock = threading.Lock()
        self._failure_log: list[dict] = []
        self._gen_times: deque[float] = deque(maxlen=200)
        self._baseline_gen_time = baseline_gen_time
        self._cost_tracker = cost_tracker
        self._active_patterns: list[SystemicPattern] = []

    # ------------------------------------------------------------------
    # Data ingestion (called by coordinator / InfraAgent)
    # ------------------------------------------------------------------

    def record_failure(
        self,
        worker_id: str,
        error: str,
        category: str = "unknown",
    ) -> None:
        """Record a VM or clip failure for pattern analysis."""
        with self._lock:
            self._failure_log.append({
                "worker_id": worker_id,
                "error": error,
                "category": category,
                "timestamp": time.time(),
            })
            # Trim old entries (keep last 500)
            if len(self._failure_log) > 500:
                self._failure_log = self._failure_log[-500:]

    def record_generation(
        self,
        worker_id: str,
        gen_time: float,
        clip_id: str,
    ) -> None:
        """Record a successful clip generation for performance tracking."""
        with self._lock:
            self._gen_times.append(gen_time)

    def update_baseline(self, baseline_gen_time: float) -> None:
        """Update the baseline generation time from historical data."""
        self._baseline_gen_time = baseline_gen_time

    # ------------------------------------------------------------------
    # Pattern detection
    # ------------------------------------------------------------------

    def check_patterns(
        self,
        queue_clips: Optional[list] = None,
    ) -> list[SystemicPattern]:
        """Run all pattern detectors. Returns newly detected patterns."""
        patterns: list[SystemicPattern] = []

        with self._lock:
            failure_log = list(self._failure_log)
            gen_times = list(self._gen_times)

        # Pattern 1: Cascade failure
        cascade = self._detect_cascade_failure(failure_log)
        if cascade:
            patterns.append(cascade)

        # Pattern 2: Common error signature
        common = self._detect_common_error(failure_log)
        if common:
            patterns.append(common)

        # Pattern 3: Performance degradation
        perf = self._detect_performance_degradation(gen_times)
        if perf:
            patterns.append(perf)

        # Pattern 4: Poison clips
        if queue_clips:
            poison = self._detect_poison_clips(queue_clips)
            if poison:
                patterns.append(poison)

        # Pattern 5: Budget burn anomaly
        budget = self._detect_budget_burn()
        if budget:
            patterns.append(budget)

        with self._lock:
            self._active_patterns = patterns

        if patterns:
            for p in patterns:
                logger.warning(
                    "SystemicDetector: %s (%s) — %s",
                    p.pattern_type, p.severity, p.hypothesis,
                )

        return patterns

    def get_active_patterns(self) -> list[SystemicPattern]:
        """Return the most recently detected patterns."""
        with self._lock:
            return list(self._active_patterns)

    # ------------------------------------------------------------------
    # Individual detectors
    # ------------------------------------------------------------------

    def _detect_cascade_failure(
        self, failure_log: list[dict]
    ) -> Optional[SystemicPattern]:
        """Detect if multiple VMs failed within a short window."""
        now = time.time()
        recent = [
            f for f in failure_log
            if now - f["timestamp"] < self.CASCADE_WINDOW_SEC
        ]
        unique_vms = {f["worker_id"] for f in recent}

        if len(unique_vms) >= self.CASCADE_MIN_VMS:
            return SystemicPattern(
                pattern_type="cascade_failure",
                severity="critical",
                evidence=recent[-10:],
                hypothesis=(
                    f"Shared dependency failure — {len(unique_vms)} VMs "
                    f"down within {self.CASCADE_WINDOW_SEC:.0f}s"
                ),
                recommended_action="pause_provisioning",
            )
        return None

    def _detect_common_error(
        self, failure_log: list[dict]
    ) -> Optional[SystemicPattern]:
        """Find error substrings common across multiple VMs."""
        # Only consider recent failures to avoid persistent false alerts
        now = time.time()
        recent = [
            f for f in failure_log
            if now - f["timestamp"] < self.COMMON_ERROR_WINDOW_SEC
        ]

        # Group errors by VM
        vm_errors: dict[str, list[str]] = {}
        for f in recent:
            vm_id = f.get("worker_id", "")
            err = f.get("error", "")
            vm_errors.setdefault(vm_id, []).append(err)

        # Extract normalized error signatures
        signatures: dict[str, set[str]] = {}
        for vm_id, errors in vm_errors.items():
            for err in errors:
                sig = _normalize_error(err)[:100]
                if sig:
                    signatures.setdefault(sig, set()).add(vm_id)

        # Find signatures on multiple VMs
        for sig, vms in signatures.items():
            if len(vms) >= self.COMMON_ERROR_MIN_VMS:
                return SystemicPattern(
                    pattern_type="common_error",
                    severity="critical",
                    evidence=[{"common_signature": sig, "affected_vms": sorted(vms)}],
                    hypothesis=f"All VMs failing with same error: {sig}",
                    recommended_action="diagnose_shared_dependency",
                )
        return None

    def _detect_performance_degradation(
        self, gen_times: list[float]
    ) -> Optional[SystemicPattern]:
        """Detect if fleet is generating clips significantly slower than baseline."""
        if len(gen_times) < 5 or self._baseline_gen_time <= 0:
            return None

        avg = statistics.mean(gen_times[-20:])  # recent window
        if avg > self._baseline_gen_time * self.PERF_DEGRADATION_FACTOR:
            return SystemicPattern(
                pattern_type="performance_degradation",
                severity="warning",
                evidence=[{
                    "fleet_avg_sec": round(avg, 1),
                    "baseline_sec": round(self._baseline_gen_time, 1),
                    "ratio": round(avg / self._baseline_gen_time, 2),
                }],
                hypothesis=(
                    f"Fleet generating clips {avg / self._baseline_gen_time:.1f}× "
                    f"slower than baseline ({avg:.0f}s vs {self._baseline_gen_time:.0f}s)"
                ),
                recommended_action="monitor_and_alert",
            )
        return None

    def _detect_poison_clips(
        self, queue_clips: list,
    ) -> Optional[SystemicPattern]:
        """Find clips that failed on multiple different workers."""
        poison = []
        for clip in queue_clips:
            attempts = getattr(clip, "attempts", 0)
            error_history = getattr(clip, "error_history", [])
            if attempts >= 2:
                unique_workers = {
                    e.get("worker_id", "") for e in error_history if e.get("worker_id")
                }
                if len(unique_workers) >= 2:
                    poison.append({
                        "clip_id": getattr(clip, "clip_id", "?"),
                        "attempts": attempts,
                        "failed_workers": sorted(unique_workers),
                    })

        if poison:
            return SystemicPattern(
                pattern_type="poison_clip",
                severity="warning",
                evidence=poison,
                hypothesis=(
                    f"{len(poison)} clip(s) failing on multiple workers — "
                    f"likely a prompt issue, not infrastructure"
                ),
                recommended_action="dead_letter_and_escalate",
            )
        return None

    def _detect_budget_burn(self) -> Optional[SystemicPattern]:
        """Detect if cost is accumulating faster than projected."""
        if not self._cost_tracker:
            return None
        if self._cost_tracker.is_over_projection(self.BUDGET_BURN_THRESHOLD):
            return SystemicPattern(
                pattern_type="budget_burn",
                severity="warning",
                evidence=[self._cost_tracker.summary()],
                hypothesis=(
                    f"Cost accumulating {self.BUDGET_BURN_THRESHOLD - 1:.0%} "
                    f"faster than projected"
                ),
                recommended_action="reduce_fleet_or_alert",
            )
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_error(error: str) -> str:
    """Normalize an error string for signature matching.

    Strips memory addresses, file paths, timestamps, and other noise
    so that semantically identical errors match.
    """
    import re
    s = error.strip()
    # Remove hex addresses (0x7fff...)
    s = re.sub(r"0x[0-9a-fA-F]+", "0x...", s)
    # Remove file paths (/home/user/...)
    s = re.sub(r"/[\w/.-]+", "/...", s)
    # Remove timestamps
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "TIMESTAMP", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s.strip()
