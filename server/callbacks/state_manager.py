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
    receive a plain ``dict`` (e.g. after B2 restore).  Calling
    ``.to_dict()`` on a plain dict raises ``AttributeError`` and crashes
    the pipeline silently.

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
