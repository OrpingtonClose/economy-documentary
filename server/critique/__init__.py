"""Unified artifact critique + QA record layer.

This package is the substrate for the pull-based escalation redesign: every
pipeline artifact (scenario, scene, visual concept, audio clip, video clip,
scene assembly, final cut) carries a single :class:`ArtifactCritiqueRecord`
aggregating:

* structured LLM critiques from per-stage critic squads, and
* deterministic QA verdicts from existing gates
  (``qa_jury``, ``gatekeeper``, ``timeline_guardian``, scenario evaluator,
  visual ``coherence_evaluator``, ``scenario_evaluator_checks``);

and a tail of :class:`EscalationRef` entries recording which canonical
:class:`orchestrator.escalation_menu.EscalationAction` was applied on that
artifact and what the outcome was.

The module is **intentionally additive** — nothing in the live pipeline
reads it yet.  Adapters in :mod:`critique.adapters` let existing evaluator
outputs be mirrored into the store *alongside* their current code paths so
PR-1 is safely mergeable with zero behaviour change.  Consumers (the
pull-based supervisor + deployment-planner tools) land in a follow-up PR.

Design rules:

* Dependency-free of ADK / litellm / google-genai so it can be imported
  from tests, telemetry and escalation tools without pulling the model
  stack in.
* Disk-first persistence under ``runs/<run_id>/critiques/<type>/<id>.json``
  matching the existing B2 checkpoint convention in
  :mod:`tools.b2_checkpoint`.  B2 upload is best-effort and non-fatal.
* Writes are append-style (``append_critique`` / ``append_qa`` /
  ``append_escalation``) so multiple agents can contribute to the same
  record without clobbering each other.
"""

from __future__ import annotations

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
from critique.critic_squad import (
    CriticSpec,
    CriticSquad,
    build_critic_squad,
    make_critic_squad_callback,
)
from critique.qa_storage import (
    mirror_coherence_evaluator_result,
    mirror_gatekeeper_check,
    mirror_gatekeeper_checks,
    mirror_jury_verdict,
    mirror_scenario_evaluator_result,
    mirror_timeline_guardian_result,
)
from critique.store import ArtifactCritiqueStore, get_critique_store

__all__ = [
    "ARTIFACT_TYPES",
    "ArtifactCritiqueRecord",
    "ArtifactCritiqueStore",
    "ArtifactType",
    "Critique",
    "CriticSpec",
    "CriticSquad",
    "CritiqueRating",
    "EscalationRef",
    "QA_VERDICTS",
    "QaVerdict",
    "QaVerdictStatus",
    "artifact_type_and_id",
    "build_critic_squad",
    "get_critique_store",
    "make_critic_squad_callback",
    "mirror_coherence_evaluator_result",
    "mirror_gatekeeper_check",
    "mirror_gatekeeper_checks",
    "mirror_jury_verdict",
    "mirror_scenario_evaluator_result",
    "mirror_timeline_guardian_result",
    "worst_status",
]
