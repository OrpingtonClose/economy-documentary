"""Component 03 atoms — scenario refiner pure helpers.

Two atoms extracted from ``scenario_refiner.py``:

* :func:`adjust_scene_durations` — rewrite per-scene
  ``target_duration_sec`` values. Deterministic, no LLM.
* :func:`validate_pronunciation_hints` — check that every scene retains
  its ``pronunciation_hints`` dict after a refinement pass.

The remaining tool in the component — ``tweak_voice_text`` — is a
connector because it delegates the actual rewrite to an injected
text-rewriter helper (LLM-backed in production). It is NOT exposed
here.
"""

from __future__ import annotations

from typing import Any

from strands_agents.scenario_refiner import (
    adjust_scene_durations as _adjust_scene_durations_tool,
)
from strands_agents.scenario_refiner import (
    validate_pronunciation_hints as _validate_pronunciation_hints_tool,
)


def adjust_scene_durations(
    scenes: list[dict[str, Any]],
    per_scene_targets: dict[str, float],
) -> dict[str, Any]:
    """Replace ``target_duration_sec`` per scene while preserving structure.

    Args:
        scenes: The current scenes list. Each entry must carry an
            ``id`` (or ``scene_num`` / ``scene_id``).
        per_scene_targets: Mapping of ``{scene_id: new_target_seconds}``.

    Returns:
        ``{"scenes": [...], "updated_scene_ids": [...]}``.

    Raises:
        ValueError: If ``scenes`` is empty or any target is
            non-positive.
    """
    return _adjust_scene_durations_tool.__wrapped__(scenes, per_scene_targets)


def validate_pronunciation_hints(scenes: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify every scene retains its ``pronunciation_hints`` dict.

    Args:
        scenes: The current scenes list.

    Returns:
        ``{"ok": bool, "missing_on": [scene_id, ...]}``.
    """
    return _validate_pronunciation_hints_tool.__wrapped__(scenes)


__all__ = ["adjust_scene_durations", "validate_pronunciation_hints"]
