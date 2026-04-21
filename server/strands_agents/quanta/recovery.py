"""Component 12 atoms — failure classification + concept revision.

Three pure atoms extracted from ``subagents/recovery_agents.py``:

* :func:`classify_failure` — deterministic rule table over an error
  string, recent failure history, and the failing concept. Returns
  one of ``transient``, ``fixable``, ``persistent``, ``catastrophic``.
* :func:`propose_revised_concept` — deterministic prompt/negative
  revision that addresses the classifier's hint while preserving
  structural fields.
* :func:`diff_concept` — pure set comparison of changed vs preserved
  fields between two concept dicts.

The SubAgent that drives these (classifier agent + remanifester agent)
is a connector because it uses an LLM to sequence the calls.
"""

from __future__ import annotations

from typing import Any

from strands_agents.subagents.recovery_agents import (
    classify as _classify_tool,
)
from strands_agents.subagents.recovery_agents import (
    diff_concept as _diff_concept_tool,
)
from strands_agents.subagents.recovery_agents import (
    propose_revised_concept as _propose_revised_concept_tool,
)


def classify_failure(
    error: str,
    recent_history: list[dict[str, Any]],
    concept: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a failure event into a recovery class.

    Args:
        error: Raw error message from the failing stage.
        recent_history: List of prior failure entries
            (``{"error": str, "concept_id": str, ...}``) used to detect
            repeat failures.
        concept: The concept or artifact that failed. Optional; only
            read for logging.

    Returns:
        ``{"class": "transient|fixable|persistent|catastrophic",
           "hint": str, "signals": [...], "reasoning": str}``.
    """
    return _classify_tool.__wrapped__(error, recent_history, concept)


def propose_revised_concept(
    original_concept: dict[str, Any],
    error: str,
    hint: str,
    style_lock: dict[str, Any],
) -> dict[str, Any]:
    """Return a revised visual concept addressing the failure cause.

    Preserves ``phrase_id``, ``scene_id``, ``duration_sec``, and always
    sets ``style_lock_applied=True`` on the revised concept.

    Raises:
        ValueError: If ``original_concept`` is empty or missing
            ``phrase_id`` / ``scene_id``.
    """
    return _propose_revised_concept_tool.__wrapped__(
        original_concept, error, hint, style_lock
    )


def diff_concept(
    original: dict[str, Any],
    revised: dict[str, Any],
) -> dict[str, Any]:
    """Return the set of fields that changed between two concepts.

    Returns:
        ``{"changed_fields": [...], "preserved_fields": [...]}``, both
        sorted alphabetically for deterministic output.
    """
    return _diff_concept_tool.__wrapped__(original, revised)


__all__ = ["classify_failure", "diff_concept", "propose_revised_concept"]
