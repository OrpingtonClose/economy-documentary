"""
LoRA catalog tools -- query and retrieve LoRA style information.

The Visual Director's Content Analyst calls these tools to make LoRA
selection decisions based on what the narration communicates. LoRA choice
is a creative decision, not a config setting.
"""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "lora_catalog.json")
_catalog: Optional[dict] = None


def _load_catalog() -> dict:
    """Load the LoRA catalog from JSON. Cached after first load.

    Returns an empty dict (with a logged warning) if the catalog file is
    missing or contains invalid JSON, so downstream code never crashes.
    """
    global _catalog
    if _catalog is not None:
        return _catalog

    catalog: dict = {}
    try:
        with open(_CATALOG_PATH, "r") as f:
            catalog = json.load(f)
        logger.info("Loaded LoRA catalog: %d entries", len(catalog))
    except FileNotFoundError:
        logger.warning("LoRA catalog not found at %s — using empty catalog", _CATALOG_PATH)
    except json.JSONDecodeError as exc:
        logger.warning("LoRA catalog contains invalid JSON (%s) — using empty catalog", exc)
    _catalog = catalog
    return catalog


def _score_match(entry: dict, content_type: str, mood: str, tags: List[str]) -> float:
    """Score how well a LoRA entry matches the query criteria."""
    score = 0.0
    entry_tags = set(entry.get("tags", []))
    best_for = entry.get("best_for", "").lower()
    avoid_for = entry.get("avoid_for", "").lower()
    description = entry.get("description", "").lower()

    # Tag overlap
    query_tags = set(t.lower() for t in tags)
    overlap = entry_tags & query_tags
    score += len(overlap) * 2.0

    # Content type match against best_for
    if content_type:
        ct_lower = content_type.lower()
        if ct_lower in best_for:
            score += 3.0
        if ct_lower in description:
            score += 1.0
        if ct_lower in avoid_for:
            score -= 3.0

    # Mood match against description and tags
    if mood:
        mood_lower = mood.lower()
        if mood_lower in description:
            score += 2.0
        for tag in entry_tags:
            if mood_lower in tag or tag in mood_lower:
                score += 1.0

    return score


def query_lora_catalog(
    content_type: str = "",
    mood: str = "",
    tags: str = "",
    tool_context=None,
) -> str:
    """Query the LoRA catalog for matching styles.

    Args:
        content_type: Type of content (e.g., "historical", "technology",
                      "nature", "data", "urban").
        mood: Desired mood (e.g., "dramatic", "nostalgic", "mysterious").
        tags: Comma-separated tags to match against LoRA entries.

    Returns:
        JSON string with ranked LoRA matches.
    """
    catalog = _load_catalog()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    scored: list[tuple[str, float, dict]] = []
    if catalog is None:
        catalog = {}
    for lora_id, entry in catalog.items():
        score = _score_match(entry, content_type, mood, tag_list)
        scored.append((lora_id, score, entry))

    # Sort by score descending
    scored.sort(key=lambda x: x[1], reverse=True)

    results = []
    for lora_id, score, entry in scored[:5]:
        results.append(
            {
                "lora_id": lora_id,
                "relevance_score": round(score, 2),
                "description": entry["description"],
                "best_for": entry["best_for"],
                "default_weight": entry["default_weight"],
                "tags": entry["tags"],
                "transition_affinity": entry.get("transition_affinity", []),
            }
        )

    return json.dumps({"matches": results, "total_in_catalog": len(catalog)})


def get_lora_details(lora_id: str, tool_context=None) -> str:
    """Get full details for a specific LoRA entry.

    Args:
        lora_id: The LoRA identifier (e.g., "documentary-realism").

    Returns:
        JSON string with full LoRA details.
    """
    catalog = _load_catalog()
    if catalog is None:
        catalog = {}

    if lora_id not in catalog:
        return json.dumps(
            {
                "error": f"LoRA '{lora_id}' not found",
                "available": list(catalog.keys()),
            }
        )

    entry = catalog[lora_id]
    return json.dumps({"lora_id": lora_id, **entry})

