"""Approval gate compatibility shim for the Strands migration.

The original file-based approval gate was replaced by the ApprovalGatePlugin
(server/hooks/approval_gate.py) which uses Strands graph interrupts. This shim
provides the mark_stage_ready() function that deterministic_steps.py still calls
at line 1698 to signal that clips are ready for human review.
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip().lower() in ("1", "true", "yes")
_AUTO_APPROVE = _TEST_MODE or os.environ.get(
    "DOCUMENTARY_AUTO_APPROVE", ""
).strip().lower() in ("1", "true", "yes")

_APPROVAL_FILE = os.path.join(
    os.environ.get("PIPELINE_STATE_DIR", os.environ.get("PIPELINE_OUTPUT_DIR", "/tmp/documentary-pipeline")),
    ".approval_state.json",
)

# How often to poll for approval (seconds)
_POLL_INTERVAL = 5.0

# Maximum time to wait for approval before timing out (seconds)
# 2 hours — generous, human may step away
_MAX_WAIT = 7200.0


def _read_approval_state() -> dict:
    """Read the approval state file."""
    if os.path.exists(_APPROVAL_FILE):
        try:
            with open(_APPROVAL_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_approval_state(state: dict) -> None:
    """Write the approval state file."""
    os.makedirs(os.path.dirname(_APPROVAL_FILE), exist_ok=True)
    with open(_APPROVAL_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_stage_approved(stage: str) -> bool:
    """Check if a stage has been approved by the human.

    In test/auto-approve mode, all stages are auto-approved.
    """
    if _AUTO_APPROVE:
        return True
    state = _read_approval_state()
    return state.get(stage, {}).get("approved", False)


def mark_stage_ready(stage: str) -> None:
    """Mark a stage as ready for human review (but not yet approved).

    In test mode, stages are auto-approved -- skip disk I/O entirely.
    """
    if _AUTO_APPROVE:
        logger.info("stage=<%s> | auto-approved (test mode)", stage)
        return
    state = _read_approval_state()
    if stage not in state:
        state[stage] = {}
    state[stage]["ready"] = True
    state[stage]["ready_at"] = time.time()
    _write_approval_state(state)
    logger.info("stage=<%s> | marked ready for review", stage)


def approve_stage(stage: str) -> None:
    """Programmatically approve a stage (no human needed).

    Used by quick-test and other automated paths that skip the normal
    human-in-the-loop flow but still need downstream stages to proceed.
    """
    if _AUTO_APPROVE:
        logger.info("stage=<%s> | already auto-approved (test/auto-approve mode)", stage)
        return
    state = _read_approval_state()
    if stage not in state:
        state[stage] = {}
    state[stage]["ready"] = True
    state[stage]["approved"] = True
    state[stage]["approved_at"] = time.time()
    state[stage]["approved_by"] = "quick-test"
    _write_approval_state(state)
    logger.info("stage=<%s> | programmatically approved (quick-test)", stage)


def wait_for_approval(stage: str) -> bool:
    """Block until the human approves the given stage.

    Returns True if approved, False if timed out.
    In test mode, returns immediately.
    """
    if _AUTO_APPROVE:
        logger.info("stage=<%s> | auto-approved (test mode)", stage)
        return True
    start = time.time()
    logger.info("stage=<%s> | waiting for human approval", stage)

    while time.time() - start < _MAX_WAIT:
        if is_stage_approved(stage):
            elapsed = time.time() - start
            logger.info("stage=<%s>, elapsed=<%.1fs> | approved", stage, elapsed)
            return True
        time.sleep(_POLL_INTERVAL)

    logger.warning("stage=<%s>, max_wait=<%.0fs> | timed out waiting for approval", stage, _MAX_WAIT)
    return False
