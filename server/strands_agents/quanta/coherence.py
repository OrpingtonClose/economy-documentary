"""Component 08 atom — structural coherence violations.

One pure atom: :func:`compute_structural_violations`. Deterministic
hard-invariant checks over a visual concept list (one concept per
content-analysis phrase_id, no forbidden style tokens in prompts, no
long runs of identical shot_type + camera_movement pairs).

The soft-scoring half of the component (``score_visual_coherence``) is
a connector — it calls an LLM-backed soft scorer, then merges its
opinion with the structural verdict. Only the structural check is
pure.
"""

from __future__ import annotations

from typing import Any

from strands_agents.coherence_evaluator import _structural_violations


def compute_structural_violations(
    visual_concepts: list[dict[str, Any]],
    style_lock: dict[str, Any],
    content_analysis: dict[str, Any],
) -> list[str]:
    """Return the list of HARD-invariant violations.

    A non-empty list forces the final coherence rating to ``POOR``
    regardless of the soft scorer's opinion.

    Args:
        visual_concepts: Concept list to check.
        style_lock: Movie-level style lock (for forbidden styles).
        content_analysis: Per-scene content analysis carrying the
            ``phrase_id`` set that every concept must map onto 1:1.

    Returns:
        Ordered list of human-readable violation strings. Empty when
        the structural contract is satisfied.
    """
    return _structural_violations(visual_concepts, style_lock, content_analysis)


__all__ = ["compute_structural_violations"]
