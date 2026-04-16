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


class StateDictProxy(dict):
    """Dict proxy that delegates all operations to an underlying dict by reference.

    The old ADK state object had a .to_dict() method that deterministic_steps.py
    calls in 10 places. This proxy wraps the *original* dict without copying,
    so mutations flow through to the underlying object (e.g. invocation_state).

    Implementation: we override __init__ to skip the normal dict copy and instead
    store a reference. All dict methods delegate to that reference via __getattr__
    on the internal dict. We inherit from dict so isinstance checks pass and
    bracket syntax (state["key"]) works via our overridden dunder methods.
    """

    def __init__(self, target: dict[str, Any]) -> None:
        # Do NOT call super().__init__(target) — that would copy items.
        super().__init__()
        object.__setattr__(self, "_target", target)

    # -- Core dict protocol: delegate to _target --
    def __getitem__(self, key: str) -> Any:
        return object.__getattribute__(self, "_target")[key]

    def __setitem__(self, key: str, value: Any) -> None:
        object.__getattribute__(self, "_target")[key] = value

    def __delitem__(self, key: str) -> None:
        del object.__getattribute__(self, "_target")[key]

    def __contains__(self, key: object) -> bool:
        return key in object.__getattribute__(self, "_target")

    def __iter__(self):  # type: ignore[override]
        return iter(object.__getattribute__(self, "_target"))

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_target"))

    def __repr__(self) -> str:
        return repr(object.__getattribute__(self, "_target"))

    def get(self, key: str, default: Any = None) -> Any:
        return object.__getattribute__(self, "_target").get(key, default)

    def keys(self):  # type: ignore[override]
        return object.__getattribute__(self, "_target").keys()

    def values(self):  # type: ignore[override]
        return object.__getattribute__(self, "_target").values()

    def items(self):  # type: ignore[override]
        return object.__getattribute__(self, "_target").items()

    def update(self, *args: Any, **kwargs: Any) -> None:
        object.__getattribute__(self, "_target").update(*args, **kwargs)

    def pop(self, key: str, *args: Any) -> Any:
        return object.__getattribute__(self, "_target").pop(key, *args)

    def setdefault(self, key: str, default: Any = None) -> Any:
        return object.__getattribute__(self, "_target").setdefault(key, default)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict copy — ADK compatibility method."""
        return dict(object.__getattribute__(self, "_target"))


# Keep backward-compatible alias
StateDict = StateDictProxy


class CallbackContext:
    """Minimal replacement for google.adk.agents.callback_context.CallbackContext.

    Provides a .state dict that the deterministic callbacks read/write.
    """

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state: StateDictProxy = StateDictProxy(state) if state is not None else StateDictProxy({})
