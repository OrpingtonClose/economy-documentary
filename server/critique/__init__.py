"""Unified artifact critique + QA record layer and stylistic QA agents.

This package combines two concerns:

1. **Artifact critique substrate (PR-1 / #117).**  Every pipeline artifact
   (scenario, scene, visual concept, audio clip, video clip, scene
   assembly, final cut) carries a single :class:`ArtifactCritiqueRecord`
   aggregating:

   * structured LLM critiques from per-stage critic squads, and
   * deterministic QA verdicts from existing gates
     (``qa_jury``, ``gatekeeper``, ``timeline_guardian``, scenario
     evaluator, visual ``coherence_evaluator``,
     ``scenario_evaluator_checks``);

   plus a tail of :class:`EscalationRef` entries recording which
   canonical :class:`orchestrator.escalation_menu.EscalationAction`
   was applied on that artifact and what the outcome was.

   Adapters in :mod:`critique.adapters` let existing evaluator outputs
   be mirrored into the store alongside their current code paths.
   Disk-first persistence under ``runs/<run_id>/critiques/<type>/<id>.json``
   matches the existing B2 checkpoint convention.  Writes are
   append-style so multiple agents can contribute without clobbering
   each other.

2. **Stylistic QA invariants (ARCH-E3 / #149, absorbing #91 and #98).**
   Every emitted narration block is checked against stylistic
   invariants (uniform LUFS, voice continuity, character voice
   consistency, peak limiter, clicks, truncated plosives, hiss floor)
   regardless of which audio-ladder tier produced it.  A block that
   passes timing but fails a stylistic invariant re-enters the audio
   ladder with the invariant violation as the failure signal.
   Spec: ``docs/ARCHITECTURE_DIAGRAMS.md`` diagram 2.

Both layers are dependency-free of ADK / litellm / google-genai at the
package surface so they can be imported from tests, telemetry, and
escalation tools without pulling the model stack in.
"""

from __future__ import annotations

from critique.audio_invariants import (
    InvariantResult,
    InvariantVerdict,
    InvariantViolation,
    NarrationBlock,
    check_character_voice_consistency,
    check_clicks,
    check_hiss_floor_continuity,
    check_peak_limiter,
    check_plosive_truncation,
    check_uniform_lufs,
    check_voice_continuity,
    run_all_invariants,
)
from critique.ledger_override import is_lufs_override_active
from critique.record import (
    ArtifactCritiqueRecord,
    ArtifactType,
    ARTIFACT_TYPES,
    Critique,
    CritiqueRating,
    EscalationRef,
    QA_VERDICTS,
    QaVerdict,
    QaVerdictStatus,
    artifact_type_and_id,
    worst_status,
)
from critique.store import ArtifactCritiqueStore, get_critique_store
from critique.stylistic_qa_agent import (
    STYLISTIC_QA_OPERATION,
    StylisticInvariantFailure,
    build_stylistic_qa_agent,
    stylistic_qa_after_agent_callback,
)

__all__ = [
    "ARTIFACT_TYPES",
    "ArtifactCritiqueRecord",
    "ArtifactCritiqueStore",
    "ArtifactType",
    "Critique",
    "CritiqueRating",
    "EscalationRef",
    "InvariantResult",
    "InvariantVerdict",
    "InvariantViolation",
    "NarrationBlock",
    "QA_VERDICTS",
    "QaVerdict",
    "QaVerdictStatus",
    "STYLISTIC_QA_OPERATION",
    "StylisticInvariantFailure",
    "artifact_type_and_id",
    "build_stylistic_qa_agent",
    "check_character_voice_consistency",
    "check_clicks",
    "check_hiss_floor_continuity",
    "check_peak_limiter",
    "check_plosive_truncation",
    "check_uniform_lufs",
    "check_voice_continuity",
    "get_critique_store",
    "is_lufs_override_active",
    "run_all_invariants",
    "stylistic_qa_after_agent_callback",
    "worst_status",
]
