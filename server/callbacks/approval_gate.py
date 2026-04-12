"""
Approval Gate — human-in-the-loop checkpoints between pipeline stages.

The pipeline pauses after each stage completes, waits for human approval
via the dashboard, then proceeds to the next stage.

Approval state is persisted to disk so it survives restarts.

Flow:
    Stage completes → after_agent_callback signals "ready for review"
    Dashboard shows data + "Approve" button
    Human clicks "Approve" → POST /agui/approve
    Next stage's before_agent_callback polls until approved → proceeds
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

_OUTPUT_DIR = os.environ.get("PIPELINE_OUTPUT_DIR", "/workspace/documentary-output")
_APPROVAL_FILE = os.path.join(_OUTPUT_DIR, ".approval_state.json")

# Auto-approve all stages in test mode (no human needed)
_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip().lower() in ("1", "true")

# How often to poll for approval (seconds)
_POLL_INTERVAL = 5.0

# Maximum time to wait for approval before timing out (seconds)
# 2 hours — generous, human may step away
_MAX_WAIT = 7200.0


def _read_approval_state() -> dict:
    """Read approval state from disk."""
    if not os.path.exists(_APPROVAL_FILE):
        return {}
    try:
        with open(_APPROVAL_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_approval_state(state: dict) -> None:
    """Write approval state to disk."""
    os.makedirs(os.path.dirname(_APPROVAL_FILE), exist_ok=True)
    with open(_APPROVAL_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_stage_approved(stage: str) -> bool:
    """Check if a stage has been approved by the human.

    In test mode, all stages are auto-approved.
    """
    if _TEST_MODE:
        return True
    state = _read_approval_state()
    return state.get(stage, {}).get("approved", False)


def mark_stage_ready(stage: str) -> None:
    """Mark a stage as ready for human review (but not yet approved).

    In test mode, stages are auto-approved — skip disk I/O entirely.
    """
    if _TEST_MODE:
        logger.info("Stage '%s' auto-approved (test mode)", stage)
        return
    state = _read_approval_state()
    if stage not in state:
        state[stage] = {}
    state[stage]["ready"] = True
    state[stage]["ready_at"] = time.time()
    _write_approval_state(state)
    logger.info("Stage '%s' marked ready for review", stage)


def wait_for_approval(stage: str) -> bool:
    """Block until the human approves the given stage.

    Returns True if approved, False if timed out.
    In test mode, returns immediately.
    """
    if _TEST_MODE:
        logger.info("Stage '%s' auto-approved (test mode)", stage)
        return True
    start = time.time()
    logger.info("Waiting for human approval of stage '%s'...", stage)

    while time.time() - start < _MAX_WAIT:
        if is_stage_approved(stage):
            elapsed = time.time() - start
            logger.info("Stage '%s' approved after %.1fs", stage, elapsed)
            return True
        time.sleep(_POLL_INTERVAL)

    logger.warning("Timed out waiting for approval of stage '%s' (%.0fs)", stage, _MAX_WAIT)
    return False


# ---------------------------------------------------------------------------
# Callback factories — create before/after callbacks for each stage
# ---------------------------------------------------------------------------

def make_after_stage_callback(stage: str):
    """Create an after_agent_callback that marks a stage ready for review.

    This wraps any existing after_agent_callback so both run.
    """
    def _after_callback(callback_context: CallbackContext) -> Optional[genai_types.Content]:
        mark_stage_ready(stage)
        return None
    return _after_callback


def make_before_stage_callback(requires_stage: str):
    """Create a before_agent_callback that waits for a prerequisite stage approval.

    The callback blocks (polls) until the required stage is approved.
    If timed out, it returns Content to skip the agent with an error.
    """
    def _before_callback(callback_context: CallbackContext) -> Optional[genai_types.Content]:
        if not is_stage_approved(requires_stage):
            logger.info(
                "Stage requires '%s' approval — waiting...", requires_stage
            )
            approved = wait_for_approval(requires_stage)
            if not approved:
                return genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(
                        text=f"ERROR: Timed out waiting for '{requires_stage}' approval. "
                             f"Please approve the {requires_stage} stage on the dashboard."
                    )],
                )
        return None
    return _before_callback
