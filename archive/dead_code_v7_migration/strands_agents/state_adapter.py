"""
State adapter — bridges ADK CallbackContext to Strands agent state.

The existing deterministic callbacks in ``server/callbacks/`` all take
``CallbackContext`` objects. During the Strands migration, we need to
call these callbacks from Strands tools. Rather than rewriting 3300+
lines of callback code, we create a thin adapter that satisfies the
``CallbackContext`` interface using a plain dict.

This is a temporary bridge. Once all callbacks are ported to Strands
hooks, this adapter goes away.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CallbackContextAdapter:
    """Minimal CallbackContext implementation backed by a plain dict.

    Provides the ``state`` attribute and ``_event`` creation that the
    deterministic callbacks expect. Does NOT provide the full ADK
    CallbackContext surface — only what the callbacks actually use.

    Usage::

        state = {"scenes": [...], "pipeline_phase": "audio"}
        ctx = CallbackContextAdapter(state)
        # Now pass ctx to deterministic_audio_callback(ctx)
    """

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self._invocation_id = "strands-adapter"

    def __repr__(self) -> str:
        return f"CallbackContextAdapter(phase={self.state.get('pipeline_phase', 'unknown')})"


class GenaiTypesAdapter:
    """Minimal genai.types adapter for returning Content from callbacks.

    The ADK callbacks return ``genai_types.Content`` objects. The Strands
    equivalent is a plain dict or string. This adapter creates objects
    that have the same interface as ``genai_types.Content`` and
    ``genai_types.Part`` without requiring the google-genai package.
    """

    class Part:
        def __init__(self, text: str = "") -> None:
            self.text = text

    class Content:
        def __init__(self, role: str = "model", parts: list | None = None) -> None:
            self.role = role
            self.parts = parts or []

    @classmethod
    def make_content(cls, text: str, role: str = "model") -> "GenaiTypesAdapter.Content":
        """Create a Content object with a single text part."""
        return cls.Content(role=role, parts=[cls.Part(text=text)])


def make_callback_context(state: dict[str, Any]) -> CallbackContextAdapter:
    """Create a CallbackContext adapter from a state dict.

    This is the main entry point for calling ADK callbacks from Strands.
    """
    return CallbackContextAdapter(state)


def make_genai_content(text: str, role: str = "model") -> GenaiTypesAdapter.Content:
    """Create a genai_types.Content-compatible object without google-genai."""
    return GenaiTypesAdapter.make_content(text, role)
