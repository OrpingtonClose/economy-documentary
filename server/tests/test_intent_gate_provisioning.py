"""INTENT-05 (#269): worker-provisioner gated on INTENT-02 signal.

The lazy-GPU contract is:

1. Pipeline start spawns a background thread that blocks on
   :func:`wait_for_intent_gate` before calling
   :meth:`WorkerProvisioner.start_provisioning`.
2. :data:`INTENT_GATE_PASSED` is ONLY set by
   :func:`run_preflight_gate` on a passing verdict.
3. If the gate never passes (timeout), the provisioner is never asked
   to boot VMs — the run costs zero GPU-seconds.
4. If the gate passes, the blocked thread wakes up and provisioning
   proceeds normally.

These tests exercise the signal plumbing in isolation — the real
``WorkerProvisioner.start_provisioning`` is not exercised (it hits
Vast.ai + SSH), but the gating code path is identical to what
:func:`_start_provisioning_bg` uses in ``agents.pipeline``.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from agents.intent_extractor import BriefIntent, BRIEF_INTENT_KEY  # noqa: E402
from callbacks.intent_gate import (  # noqa: E402
    INTENT_GATE_PASSED,
    reset_intent_gate,
    run_preflight_gate,
    wait_for_intent_gate,
)


def _passing_state() -> dict:
    intent = BriefIntent(
        duration_sec=420.0,
        tolerance_sec=30.0,
        audience="adhd-friendly",
        tone=["curious"],
        corpus_paths=[],
        required_topics=["PAG", "opioid chemistry"],
        forbidden_topics=[],
        format_hints={},
        confidence={"duration_sec": 0.98},
    )
    return {
        BRIEF_INTENT_KEY: intent.to_json(),
        "scenes": [
            {
                "scene_num": 1,
                "title": "intro",
                "duration_sec": 210.0,
                "narration": "PAG opens the story.",
            },
            {
                "scene_num": 2,
                "title": "chemistry",
                "duration_sec": 210.0,
                "narration": "opioid chemistry explained.",
            },
        ],
    }


@pytest.fixture(autouse=True)
def _reset_gate():
    reset_intent_gate()
    yield
    reset_intent_gate()


def test_provisioner_waits_for_gate_signal():
    """Provisioner thread blocks on wait_for_intent_gate until pass.

    Simulates the exact wait/set handoff used in
    :func:`agents.pipeline._start_provisioning_bg`.
    """
    provision_called = threading.Event()
    timed_out = threading.Event()

    def fake_provisioner_thread():
        fired = wait_for_intent_gate(timeout_sec=5.0)
        if fired:
            # This is where start_provisioning() would run in prod.
            provision_called.set()
        else:
            timed_out.set()

    t = threading.Thread(target=fake_provisioner_thread, daemon=True)
    t.start()

    # Give the thread a moment to enter wait_for_intent_gate.
    time.sleep(0.05)
    assert not provision_called.is_set(), (
        "provisioning must NOT start before the intent gate passes"
    )
    assert not timed_out.is_set()

    # Run the gate — a passing verdict signals the event.
    run_preflight_gate(_passing_state())

    t.join(timeout=1.0)
    assert provision_called.is_set(), (
        "provisioning should start immediately after the gate passes"
    )
    assert not timed_out.is_set()


def test_zero_gpu_cost_on_pre_gate_cancellation():
    """Cancelling a run before the gate passes must not invoke provisioning.

    We spin up the gated wait, never signal it, and assert that the
    "start_provisioning" hook is never called during the timeout
    window.
    """
    provision_called = threading.Event()

    def fake_provisioner_thread():
        fired = wait_for_intent_gate(timeout_sec=0.1)
        if fired:
            provision_called.set()

    t = threading.Thread(target=fake_provisioner_thread, daemon=True)
    t.start()
    t.join(timeout=1.0)

    assert not provision_called.is_set(), (
        "provisioning must not run when the gate never signalled — "
        "this is the zero GPU-seconds invariant"
    )


def test_gate_failed_run_never_signals_provisioner():
    """A failing-then-halting gate must never set INTENT_GATE_PASSED."""
    from callbacks.intent_gate import IntentGateHalt

    intent = BriefIntent(
        duration_sec=420.0,
        tolerance_sec=30.0,
        audience="general",
        tone=[],
        corpus_paths=[],
        required_topics=["PAG"],
        forbidden_topics=[],
        format_hints={},
        confidence={},
    )
    state = {
        BRIEF_INTENT_KEY: intent.to_json(),
        "scenes": [{"scene_num": 1, "duration_sec": 10.0, "narration": "PAG"}],
    }

    # Exhaust all attempts.  Every attempt fails; the last one halts.
    from callbacks.intent_gate import MAX_GATE_ATTEMPTS

    for _ in range(MAX_GATE_ATTEMPTS - 1):
        run_preflight_gate(state)
    with pytest.raises(IntentGateHalt):
        run_preflight_gate(state)

    assert not INTENT_GATE_PASSED.is_set(), (
        "INTENT_GATE_PASSED must stay unset on halt — lazy GPU "
        "provisioner must not proceed on a halted run"
    )
    # A subsequent wait call should time out.
    assert wait_for_intent_gate(timeout_sec=0.01) is False


def test_gate_pass_wakes_multiple_waiters():
    """Any number of provisioner threads all wake once gate passes."""
    wakeups = []
    wake_lock = threading.Lock()

    def waiter(idx: int):
        fired = wait_for_intent_gate(timeout_sec=2.0)
        with wake_lock:
            wakeups.append((idx, fired))

    threads = [threading.Thread(target=waiter, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    run_preflight_gate(_passing_state())
    for t in threads:
        t.join(timeout=1.0)

    assert len(wakeups) == 4
    assert all(fired for _idx, fired in wakeups)
