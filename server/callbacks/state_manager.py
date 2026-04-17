"""
Pipeline state initialization and utilities.

Provides ``build_pipeline_state()`` which returns the initial session state
dict for the documentary pipeline. All agents read and write to these keys
via ADK's session state (blackboard pattern).

Also provides ``safe_state_dict()`` — a defensive wrapper around
``state.to_dict()`` that handles both ADK ``State`` objects and plain
``dict`` instances without crashing.  This was identified during the
Strands migration where ``state.to_dict()`` on a plain dict raises
``AttributeError``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def safe_state_dict(state: Any) -> dict:
    """Convert pipeline state to a plain dict safely.

    ADK ``State`` objects expose ``.to_dict()``, but callbacks may also
    receive a plain ``dict`` (e.g. during testing, after B2 restore, or
    in simulation mode).  Calling ``.to_dict()`` on a plain dict raises
    ``AttributeError`` and crashes the pipeline silently.

    This helper tries ``.to_dict()`` first, falls back to ``dict(state)``,
    and returns ``{}`` as a last resort — never crashes.

    Args:
        state: ADK State object or plain dict.

    Returns:
        A plain dict snapshot of the state.
    """
    if state is None:
        return {}
    if hasattr(state, "to_dict"):
        try:
            return state.to_dict()
        except Exception:
            logger.debug("state.to_dict() failed, falling back to dict()")
    if isinstance(state, dict):
        return dict(state)
    try:
        return dict(state)
    except (TypeError, ValueError):
        logger.warning("Could not convert state to dict, returning empty")
        return {}


def build_pipeline_state() -> dict:
    """Return the initial pipeline session state."""
    return {
        "_pipeline_key": f"doc_{uuid.uuid4().hex[:8]}",
        "topic": "",
        "corpus_path": "",
        "language": "en",
        "scenes": "[]",
        "content_analysis": "(not yet analyzed)",
        "visual_concepts": "(not yet generated)",
        "coherence_evaluation": "(not yet evaluated)",
        "whisperx_alignment": "{}",
        "otio_mutations": "[]",
        "otio_violation": None,
        "pipeline_phase": "idle",
        "lora_selections": "{}",
        "quick_test": "",
        # Template variables for scenario_director instructions.
        # Defaults are for standard (non-quick-test) mode.
        # run_pipeline.py and _init_pipeline_state override these
        # when DOCUMENTARY_QUICK_TEST is set.
        "quick_test_rules": "",
        "max_scene_duration": "45",
        "max_words_per_scene": "112",
    }
