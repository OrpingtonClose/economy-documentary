"""Custom :class:`strands_evals.Evaluator` subclasses.

See ``docs/strands-migration/eval-framework/CUSTOM_EVALUATORS.md`` for
the full list and ``EVAL_ARCHITECTURE.md`` §7 for orchestration-layer
evaluators.

PR 1/3 (this file): deterministic evaluators only. LLM-as-judge
evaluators (VisualCoherence, EscalationDecision) and orchestration
evaluators land in PR 2/3.
"""

from __future__ import annotations

from .audio_invariant import AudioInvariantEvaluator
from .contract_compliance import ContractComplianceEvaluator
from .critique_store import CritiqueStoreEvaluator
from .scenario_quality import ScenarioQualityEvaluator
from .timeline_compliance import TimelineComplianceEvaluator

__all__ = [
    "AudioInvariantEvaluator",
    "ContractComplianceEvaluator",
    "CritiqueStoreEvaluator",
    "ScenarioQualityEvaluator",
    "TimelineComplianceEvaluator",
]
