"""
Preference Ledger — scoped records of human decisions for surgical re-manifestation.

The Preference Ledger captures human decisions made during escalation (L4 HUMAN)
so they can be re-applied in future runs without re-escalating. Each record is
scoped to a specific context (stage, failure class, artifact type) so re-manifestation
is surgical — it only affects the same kind of decision, not the entire pipeline.

Design principles:
- Records are appended, never mutated (append-only journal)
- Scoped: (stage, failure_class, artifact_type) → preference
- Resolved: the most recent record wins (last-write-wins per scope)
- Re-manifestation: on pipeline start, the ledger is consulted and preferences
  are folded into the pipeline state before any agent runs
- Human decisions are captured during L4 escalation as structured JSON
- The ledger lives in the OTIO file under metadata["documentary"]["preference_ledger"]

Spec references:
    - Issue #147 (ARCH-E1)
    - docs/ARCHITECTURE_DIAGRAMS.md diagram 6 (dashed, deferred)
    - docs/IMPLEMENTATION_ROADMAP.md Stage 7
"""

from __future__ import annotations

import copy
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from tools.otio_file_ops import resolve_timeline_path, otio_read_modify_write
from tools.otio_metadata import read_pipeline_metadata, write_pipeline_metadata

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Preference record
# ---------------------------------------------------------------------------

@dataclass
class PreferenceRecord:
    """A single human decision captured during L4 escalation.

    Attributes:
        scope: The decision scope — (stage, failure_class, artifact_type).
        decision: What the human decided (e.g., "use_alternative_voice",
            "lower_resolution", "accept_degraded", "extend_duration").
        rationale: Human-readable explanation of why.
        context: Structured context of the failure that triggered L4.
        value: The concrete value to apply on re-manifestation
            (e.g., {"voice": "alloy"}, {"resolution": "720p"}).
        recorded_at: Unix timestamp when the decision was captured.
        run_id: The pipeline run ID where this decision was made.
    """
    scope: str  # "stage:failure_class:artifact_type" e.g. "audio:content:narration"
    decision: str
    rationale: str = ""
    context: dict = field(default_factory=dict)
    value: dict = field(default_factory=dict)
    recorded_at: float = 0.0
    run_id: str = ""

    def __post_init__(self):
        if self.recorded_at == 0.0:
            self.recorded_at = time.time()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PreferenceRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Ledger operations
# ---------------------------------------------------------------------------

def record_preference(
    timeline_path: str | None = None,
    record: PreferenceRecord | None = None,
    *,
    scope: str = "",
    decision: str = "",
    rationale: str = "",
    context: dict | None = None,
    value: dict | None = None,
    run_id: str = "",
) -> str:
    """Append a preference record to the ledger.

    Can be called with either a PreferenceRecord object or individual
    keyword arguments.  The record is appended to the OTIO file's
    ``metadata["documentary"]["preference_ledger"]`` list.

    Returns:
        JSON string confirming the write.
    """
    if record is None:
        if not scope or not decision:
            return json.dumps({"error": "scope and decision are required"})
        record = PreferenceRecord(
            scope=scope,
            decision=decision,
            rationale=rationale,
            context=context or {},
            value=value or {},
            run_id=run_id,
        )

    tp = timeline_path or resolve_timeline_path()

    def _append(timeline: Any) -> None:
        doc = timeline.metadata.setdefault("documentary", {})
        ledger = doc.setdefault("preference_ledger", [])
        # Append as a plain dict (OTIO compatible)
        ledger.append(record.to_dict())

    otio_read_modify_write(tp, _append)
    logger.info("Preference recorded: scope=%s decision=%s", record.scope, record.decision)
    return json.dumps({"recorded": True, "scope": record.scope, "decision": record.decision})


def resolve_preferences(
    timeline_path: str | None = None,
    scope: str = "",
) -> Optional[PreferenceRecord]:
    """Find the most recent preference for a given scope.

    The ledger is append-only; the most recent record for a scope wins.

    Args:
        timeline_path: Path to the OTIO file. Uses resolve_timeline_path() if None.
        scope: The scope to look up (e.g., "audio:content:narration").

    Returns:
        The most recent PreferenceRecord for the scope, or None.
    """
    tp = timeline_path or resolve_timeline_path()
    ledger = read_pipeline_metadata(tp, "preference_ledger")
    if not ledger:
        return None

    # Filter to scope, return most recent
    # ledger is already native Python (from read_pipeline_metadata + _to_native)
    matches = [r for r in ledger if isinstance(r, dict) and r.get("scope") == scope]
    if not matches:
        return None

    latest = max(matches, key=lambda r: r.get("recorded_at", 0))
    return PreferenceRecord.from_dict(latest)


def list_preferences(
    timeline_path: str | None = None,
    scope_prefix: str = "",
) -> list[PreferenceRecord]:
    """List all preference records, optionally filtered by scope prefix.

    Args:
        timeline_path: Path to the OTIO file.
        scope_prefix: Filter to scopes starting with this prefix
            (e.g., "audio:" for all audio preferences).

    Returns:
        List of PreferenceRecords, newest first.
    """
    tp = timeline_path or resolve_timeline_path()
    ledger = read_pipeline_metadata(tp, "preference_ledger")
    if not ledger:
        return []

    records = []
    for r in ledger:
        if isinstance(r, dict):
            if scope_prefix and not r.get("scope", "").startswith(scope_prefix):
                continue
            records.append(PreferenceRecord.from_dict(r))

    # Newest first
    records.sort(key=lambda r: r.recorded_at, reverse=True)
    return records


def re_manifest_preferences(
    timeline_path: str | None = None,
) -> dict[str, Any]:
    """Apply stored preferences to the pipeline state on run start.

    This is called once at pipeline startup. It reads the most recent
    preference for each unique scope and writes them into the pipeline
    metadata as ``"active_preferences"`` so agents can consult them.

    Returns:
        Dict of scope → value for all active preferences.
    """
    tp = timeline_path or resolve_timeline_path()
    ledger = read_pipeline_metadata(tp, "preference_ledger")
    if not ledger:
        return {}

    # Build last-write-wins map: scope → most recent record
    scope_map: dict[str, PreferenceRecord] = {}
    for r in ledger:
        if isinstance(r, dict):
            rec = PreferenceRecord.from_dict(r)
            if rec.scope not in scope_map or rec.recorded_at > scope_map[rec.scope].recorded_at:
                scope_map[rec.scope] = rec

    # Write active preferences to OTIO metadata
    active = {scope: rec.value for scope, rec in scope_map.items() if rec.value}
    write_pipeline_metadata(tp, "active_preferences", active, provenance={"agent": "preference_ledger"})

    logger.info("Re-manifested %d preferences", len(active))
    return active


def clear_preferences(
    timeline_path: str | None = None,
    scope: str = "",
) -> str:
    """Clear preference records. If scope is given, only that scope.

    This is a maintenance operation — normally preferences are append-only.
    Use this to clean up stale records.

    Returns:
        JSON string confirming the deletion.
    """
    tp = timeline_path or resolve_timeline_path()

    if scope:
        def _remove_scope(timeline: Any) -> None:
            doc = timeline.metadata.get("documentary", {})
            ledger = doc.get("preference_ledger", [])
            # OTIO AnyDictionary has .get() but is not isinstance(dict)
            doc["preference_ledger"] = [
                r for r in ledger
                if hasattr(r, "get") and r.get("scope") != scope
            ]
        otio_read_modify_write(tp, _remove_scope)
        return json.dumps({"cleared": True, "scope": scope})
    else:
        def _clear_all(timeline: Any) -> None:
            doc = timeline.metadata.setdefault("documentary", {})
            doc["preference_ledger"] = []
        otio_read_modify_write(tp, _clear_all)
        return json.dumps({"cleared": True, "scope": "all"})
