"""Compatibility shim replacing google.adk and google.genai types.

Provides lightweight replacements for CallbackContext and genai_types
so that deterministic_steps.py and timeline_guardian.py can work without
the Google ADK dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Part:
    """Minimal replacement for google.genai.types.Part."""

    text: str | None = None


@dataclass
class Content:
    """Minimal replacement for google.genai.types.Content."""

    role: str = "model"
    parts: list[Part] = field(default_factory=list)


class _GenaiTypes:
    """Namespace mimicking google.genai.types."""

    Content = Content
    Part = Part


genai_types = _GenaiTypes()


class StateDict(dict):
    """Dict subclass with to_dict() for ADK compatibility.

    The old ADK state object had a .to_dict() method that deterministic_steps.py
    calls in 10 places (e.g. upload_pipeline_state(state.to_dict())).
    This subclass ensures those calls work on plain dicts.
    """

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict copy of the state."""
        return dict(self)


class CallbackContext:
    """Minimal replacement for google.adk.agents.callback_context.CallbackContext.

    Provides a .state dict that the deterministic callbacks read/write.
    """

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state: StateDict = StateDict(state) if state is not None else StateDict()
