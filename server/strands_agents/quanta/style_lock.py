"""Component 07 atom — style lock checker.

One pure atom: :func:`check_style_lock`. Deterministic rule-based
validation that every visual concept adheres to the movie-level style
lock (positive fragment present, forbidden styles absent,
shot_type/camera_movement in allow-list, ``style_lock_applied`` true).

The rest of the component — ``propose_visual_concept`` — is a
connector because it calls an injected LLM helper.
"""

from __future__ import annotations

from typing import Any

from strands_agents.visual_concepter import (
    check_style_lock as _check_style_lock_tool,
)


def check_style_lock(
    concepts: list[dict[str, Any]],
    style_lock: dict[str, Any],
) -> dict[str, Any]:
    """Run the deterministic style-lock checks on ``concepts``.

    Args:
        concepts: List of visual concept dicts to validate.
        style_lock: The movie-level style lock. Must carry
            ``positive_fragment`` (list[str]) and ``forbidden_styles``
            (list[str]); may carry ``allowed_shot_types`` and
            ``allowed_camera_movements`` allow-lists.

    Returns:
        ``{"ok": bool, "violations": [str, ...]}``. ``ok`` is true iff
        ``violations`` is empty.
    """
    return _check_style_lock_tool.__wrapped__(concepts, style_lock)


__all__ = ["check_style_lock"]
