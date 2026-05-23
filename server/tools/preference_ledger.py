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

import logging
import time
from dataclasses import asdict, dataclass, field

from tools.otio_file_ops import resolve_timeline_path
from tools.otio_metadata import read_pipeline_metadata

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
