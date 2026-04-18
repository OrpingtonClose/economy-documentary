"""Tests for the two-signal worker-bad rule (ARCH-C4, issue #143; absorbs #78).

Spec: diagram 8 in ``docs/ARCHITECTURE_DIAGRAMS.md`` —
*single-failure-doesn't-condemn*.

A worker is condemned **only** when both:
  (a) a job failure has been attributed to it, AND
  (b) an ``infra_agent`` telemetry anomaly has been recorded for it
  (CUDA error, OOM, process crash, GPU driver fault, etc.)
…and both signals fall within the configured corroboration window.

These tests cover the four canonical cases the issue spells out:
  1. single job failure → no condemnation
  2. single CUDA error  → no condemnation
  3. both within window → condemned
  4. both outside window → not condemned

Plus a few invariants: job-outcome alone never condemns regardless of count,
the rule is sticky once corroborated, generic non-anomaly messages are not
silently promoted to signal B, and the integration with ``FleetCoordinator``
exposes the rule on its public API.

Run with::

    cd server && poetry run pytest tests/test_worker_bad_rule.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from fleet.worker_bad_rule import (  # noqa: E402
    DEFAULT_CORROBORATION_WINDOW_SEC,
    WorkerHealthTracker,
    looks_like_infra_anomaly,
)


# ---------------------------------------------------------------------------
# Test helper: deterministic clock
# ---------------------------------------------------------------------------


class _Clock:
    """Monotonic test clock so tests don't depend on wall time."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def tracker(clock: _Clock) -> WorkerHealthTracker:
    """Tracker with a 60s window, fed by the test clock."""
    return WorkerHealthTracker(corroboration_window_sec=60.0, clock=clock)


# ---------------------------------------------------------------------------
# Required scenarios from issue #143
# ---------------------------------------------------------------------------


def test_single_job_failure_does_not_condemn(tracker: WorkerHealthTracker) -> None:
    """Case 1: single job failure → no condemnation.

    The prompt could be the problem; one bad clip is not evidence about
    the worker.  Job-outcome alone must never be sufficient.
    """
    verdict = tracker.record_job_failure(
        worker_id="vm-A",
        error="Generation produced black frames",
        clip_id="clip-7",
    )

    assert verdict.bad is False
    assert tracker.is_worker_bad("vm-A") is False
    assert verdict.job_signal_count == 1
    assert verdict.infra_signal_count == 0
    assert "single failure does not condemn" in verdict.reason


def test_repeated_job_failures_alone_still_dont_condemn(
    tracker: WorkerHealthTracker, clock: _Clock,
) -> None:
    """Reinforcement: many job failures with zero infra signals never condemn.

    The whole point of the rule is that job outcomes are never sufficient.
    """
    for i in range(5):
        clock.advance(1.0)
        tracker.record_job_failure(
            worker_id="vm-A",
            error=f"black frames on clip-{i}",
            clip_id=f"clip-{i}",
        )

    verdict = tracker.evaluate("vm-A")
    assert verdict.bad is False
    assert verdict.job_signal_count == 5
    assert verdict.infra_signal_count == 0
    assert tracker.condemned_workers() == []


def test_single_cuda_error_does_not_condemn(tracker: WorkerHealthTracker) -> None:
    """Case 2: single CUDA error / infra anomaly → no condemnation.

    Workers occasionally emit a CUDA error and recover; one telemetry
    spike without any job evidence is not enough.
    """
    verdict = tracker.record_infra_anomaly(
        worker_id="vm-A",
        kind="cuda_error",
        message="CUDA error: device-side assert triggered",
    )

    assert verdict.bad is False
    assert tracker.is_worker_bad("vm-A") is False
    assert verdict.infra_signal_count == 1
    assert verdict.job_signal_count == 0
    assert "awaiting corroboration" in verdict.reason


def test_both_signals_within_window_condemns(
    tracker: WorkerHealthTracker, clock: _Clock,
) -> None:
    """Case 3: job failure AND infra anomaly within window → condemned."""
    tracker.record_job_failure(
        worker_id="vm-A",
        error="LTX call returned non-zero",
        clip_id="clip-3",
    )
    clock.advance(15.0)  # well inside the 60s window
    verdict = tracker.record_infra_anomaly(
        worker_id="vm-A",
        kind="cuda_error",
        message="CUDA out of memory; process exited 137",
    )

    assert verdict.bad is True
    assert tracker.is_worker_bad("vm-A") is True
    assert verdict.signal_gap_sec == pytest.approx(15.0)
    assert verdict.window_sec == 60.0
    assert "two-signal corroboration" in verdict.reason
    assert tracker.condemned_workers() == ["vm-A"]


def test_both_signals_outside_window_does_not_condemn(
    tracker: WorkerHealthTracker, clock: _Clock,
) -> None:
    """Case 4: job failure and infra anomaly too far apart → not condemned.

    Stale infra signals from yesterday must not corroborate today's failure;
    the worker is presumed innocent until a fresh corroborated pair lands
    inside the window.
    """
    tracker.record_job_failure(
        worker_id="vm-A",
        error="LTX call returned non-zero",
        clip_id="clip-3",
    )
    clock.advance(120.0)  # 2× the 60s window — stale
    verdict = tracker.record_infra_anomaly(
        worker_id="vm-A",
        kind="driver_fault",
        message="GPU driver reset detected",
    )

    assert verdict.bad is False
    assert tracker.is_worker_bad("vm-A") is False
    assert verdict.signal_gap_sec == pytest.approx(120.0)
    assert "corroboration window" in verdict.reason
    assert tracker.condemned_workers() == []


# ---------------------------------------------------------------------------
# Independence and ordering invariants
# ---------------------------------------------------------------------------


def test_signals_on_different_workers_do_not_cross_corroborate(
    tracker: WorkerHealthTracker, clock: _Clock,
) -> None:
    """A job failure on vm-A and a CUDA error on vm-B must not condemn either.

    The two signals must be on the SAME worker; otherwise the rule would
    falsely condemn whichever worker happened to log a CUDA event nearby.
    """
    tracker.record_job_failure(worker_id="vm-A", error="black frames", clip_id="c1")
    clock.advance(5.0)
    tracker.record_infra_anomaly(
        worker_id="vm-B", kind="cuda_error", message="CUDA error",
    )

    assert tracker.is_worker_bad("vm-A") is False
    assert tracker.is_worker_bad("vm-B") is False
    assert tracker.condemned_workers() == []


def test_infra_then_job_within_window_also_condemns(
    tracker: WorkerHealthTracker, clock: _Clock,
) -> None:
    """Order doesn't matter: infra anomaly first, job failure second still condemns.

    The rule is symmetric — both signals are independent, neither is
    privileged as "first."
    """
    tracker.record_infra_anomaly(
        worker_id="vm-A", kind="oom", message="CUDA out of memory",
    )
    clock.advance(30.0)
    verdict = tracker.record_job_failure(
        worker_id="vm-A", error="LTX call returned non-zero", clip_id="c4",
    )

    assert verdict.bad is True
    assert verdict.signal_gap_sec == pytest.approx(30.0)


def test_condemnation_is_sticky_until_reset(
    tracker: WorkerHealthTracker, clock: _Clock,
) -> None:
    """Once condemned, the worker stays condemned until reset_worker.

    A subsequent passing clip does not exonerate (the worker may be
    about to die).  Only operator/coordinator action via reset clears it.
    """
    tracker.record_job_failure(worker_id="vm-A", error="fail", clip_id="c1")
    clock.advance(5.0)
    tracker.record_infra_anomaly(worker_id="vm-A", kind="cuda", message="CUDA error")
    assert tracker.is_worker_bad("vm-A") is True

    # Time passes well beyond the window; no new signals.
    clock.advance(10_000.0)
    assert tracker.is_worker_bad("vm-A") is True, (
        "Stickiness: once corroborated the verdict must persist regardless "
        "of how stale the original signals become."
    )

    tracker.reset_worker("vm-A")
    assert tracker.is_worker_bad("vm-A") is False
    assert tracker.condemned_workers() == []


def test_reset_unknown_worker_is_a_noop(tracker: WorkerHealthTracker) -> None:
    tracker.reset_worker("vm-never-seen")  # must not raise
    assert tracker.is_worker_bad("vm-never-seen") is False


# ---------------------------------------------------------------------------
# Boundary / config behavior
# ---------------------------------------------------------------------------


def test_signals_exactly_at_window_boundary_corroborate(
    tracker: WorkerHealthTracker, clock: _Clock,
) -> None:
    """Equal-to-window gap counts as inside (closed interval)."""
    tracker.record_job_failure(worker_id="vm-A", error="fail", clip_id="c1")
    clock.advance(60.0)  # exactly at 60s window
    verdict = tracker.record_infra_anomaly(
        worker_id="vm-A", kind="cuda", message="CUDA error",
    )
    assert verdict.bad is True


def test_signals_just_outside_window_do_not_corroborate(
    tracker: WorkerHealthTracker, clock: _Clock,
) -> None:
    """A hair past the window: no condemnation."""
    tracker.record_job_failure(worker_id="vm-A", error="fail", clip_id="c1")
    clock.advance(60.5)
    verdict = tracker.record_infra_anomaly(
        worker_id="vm-A", kind="cuda", message="CUDA error",
    )
    assert verdict.bad is False


def test_window_is_configurable() -> None:
    """The corroboration window is a knob; default comes from env via the
    module constant, but tests can override it per-instance."""
    clock = _Clock()
    tight = WorkerHealthTracker(corroboration_window_sec=5.0, clock=clock)

    tight.record_job_failure(worker_id="vm-A", error="fail", clip_id="c1")
    clock.advance(10.0)
    verdict = tight.record_infra_anomaly(
        worker_id="vm-A", kind="cuda", message="CUDA error",
    )
    assert verdict.bad is False, "10s gap exceeds tight 5s window"
    assert verdict.window_sec == 5.0


def test_zero_or_negative_window_rejected() -> None:
    with pytest.raises(ValueError):
        WorkerHealthTracker(corroboration_window_sec=0)
    with pytest.raises(ValueError):
        WorkerHealthTracker(corroboration_window_sec=-1)


def test_default_window_constant_is_positive() -> None:
    """The module-level default must be a sensible positive number; the
    pipeline relies on it as the fallback when no explicit window is set."""
    assert DEFAULT_CORROBORATION_WINDOW_SEC > 0


# ---------------------------------------------------------------------------
# Anomaly classification — the second signal must be a real telemetry signal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "CUDA error: device-side assert triggered",
        "torch.cuda.OutOfMemoryError: CUDA out of memory",
        "GPU driver reset detected",
        "process crashed with signal 9",
        "Segmentation fault (core dumped)",
        "NVML: Unknown error",
        "Xid 79: GPU has fallen off the bus",
        "CUDNN_STATUS_EXECUTION_FAILED",
    ],
)
def test_infra_anomaly_classifier_recognises_real_signals(msg: str) -> None:
    assert looks_like_infra_anomaly(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "",
        "scenario validation failed",
        "QA rejected clip for low motion",
        "approval timeout waiting for operator",
        "request returned 500",
        "narration mismatch",
    ],
)
def test_infra_anomaly_classifier_rejects_non_anomaly_messages(msg: str) -> None:
    assert looks_like_infra_anomaly(msg) is False


def test_ingest_infra_message_only_records_real_anomalies(
    tracker: WorkerHealthTracker, clock: _Clock,
) -> None:
    """Generic strings must not be promoted to signal B and silently condemn.

    This is the "fail loud" guarantee — only true telemetry anomalies
    qualify as the gating second signal.
    """
    tracker.record_job_failure(worker_id="vm-A", error="black frames", clip_id="c1")
    clock.advance(1.0)

    noise = tracker.ingest_infra_message(
        worker_id="vm-A",
        message="approval timeout waiting for operator",
    )
    assert noise is None
    assert tracker.is_worker_bad("vm-A") is False

    verdict = tracker.ingest_infra_message(
        worker_id="vm-A",
        message="CUDA out of memory; process exited 137",
    )
    assert verdict is not None
    assert verdict.bad is True


def test_record_signals_require_worker_id(tracker: WorkerHealthTracker) -> None:
    """Fail loud: silently dropping anonymous signals would let a bad worker
    hide behind missing attribution."""
    with pytest.raises(ValueError):
        tracker.record_job_failure(worker_id="", error="fail")
    with pytest.raises(ValueError):
        tracker.record_infra_anomaly(worker_id="", kind="cuda", message="CUDA error")


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def test_snapshot_reports_per_worker_state(
    tracker: WorkerHealthTracker, clock: _Clock,
) -> None:
    tracker.record_job_failure(worker_id="vm-A", error="fail", clip_id="c1")
    clock.advance(5.0)
    tracker.record_infra_anomaly(worker_id="vm-A", kind="cuda", message="CUDA error")
    tracker.record_infra_anomaly(worker_id="vm-B", kind="cuda", message="CUDA error")

    snap = tracker.snapshot()
    assert set(snap.keys()) == {"vm-A", "vm-B"}
    assert snap["vm-A"]["condemned"] is True
    assert snap["vm-A"]["job_failure_count"] == 1
    assert snap["vm-A"]["infra_anomaly_count"] == 1
    assert snap["vm-B"]["condemned"] is False
    assert snap["vm-B"]["job_failure_count"] == 0
    assert snap["vm-B"]["infra_anomaly_count"] == 1


# ---------------------------------------------------------------------------
# FleetCoordinator integration: report_failed alone must NOT condemn
# ---------------------------------------------------------------------------


def test_fleet_coordinator_report_failed_alone_does_not_condemn() -> None:
    """End-to-end-ish: ``FleetCoordinator.report_failed`` records signal A
    only; without an infra anomaly the worker stays uncondemned.

    This is the integration guarantee that job-outcome signals are never
    the sole input to worker-bad decisions.
    """
    from fleet.coordinator import FleetCoordinator

    coord = FleetCoordinator(worker_bad_window_sec=60.0)
    try:
        coord.report_failed(
            clip_id="clip-1",
            worker_id="vm-A",
            error="black frames",
            category="content",
        )
        coord.report_failed(
            clip_id="clip-2",
            worker_id="vm-A",
            error="motion below threshold",
            category="content",
        )

        assert coord.is_worker_bad("vm-A") is False
        assert coord.worker_health.condemned_workers() == []
    finally:
        coord.shutdown()


def test_fleet_coordinator_corroborated_signals_condemn() -> None:
    """``FleetCoordinator.record_infra_anomaly`` + ``report_failed`` within
    the window mark the worker bad via the public API."""
    from fleet.coordinator import FleetCoordinator

    coord = FleetCoordinator(worker_bad_window_sec=60.0)
    try:
        coord.report_failed(
            clip_id="clip-1",
            worker_id="vm-A",
            error="LTX call returned non-zero",
            category="infra",
        )
        verdict = coord.record_infra_anomaly(
            worker_id="vm-A",
            kind="cuda_error",
            message="CUDA out of memory; process exited 137",
        )
        assert verdict.bad is True
        assert coord.is_worker_bad("vm-A") is True
        summary = coord.get_summary()
        assert "vm-A" in summary["worker_health"]["condemned_workers"]
    finally:
        coord.shutdown()
