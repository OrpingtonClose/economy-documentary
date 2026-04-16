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

_AUTO_APPROVE = os.environ.get("DOCUMENTARY_TEST_MODE", "").lower() in ("1", "true", "yes")

_APPROVAL_FILE = os.path.join(
    os.environ.get("PIPELINE_STATE_DIR", "/tmp/documentary-pipeline"),
    "approval_state.json",
)


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
