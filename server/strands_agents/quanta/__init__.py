"""Quanta — the pure atoms underneath the 15 pipeline components.

The 15 component modules under ``server/strands_agents/`` each mix two
kinds of code: pure deterministic transformations (atoms) and
orchestration that chains them together (connectors — loops, retries,
SubAgent prompts, approval gates).

This package makes the atom layer explicit and filesystem-visible.
Every function exported here is a plain Python function with:

* dict (or primitive) in, dict (or primitive) out
* no network, no disk, no subprocess, no LLM call
* same input always produces the same output

Connectors live back in their original modules; they are intentionally
not re-exported here because the whole point of the split is to mark
which code is pure and which is not.

The atoms are grouped by the component they come from (01..15). The
grouping matches ``docs/strands-migration/components/``; see
``reports/COMPONENT_IO_REPORT.md`` for a plain-language input/output
reference.
"""

from __future__ import annotations

from strands_agents.quanta.approval import (
    allowed_decisions_for,
    resume_command_from_decision,
    validate_decision,
)
from strands_agents.quanta.artifact_qa import evaluate_visual_artifact_quality
from strands_agents.quanta.assembly import check_assembly_inputs
from strands_agents.quanta.coherence import compute_structural_violations
from strands_agents.quanta.escalation import decide_escalation_action
from strands_agents.quanta.recovery import (
    classify_failure,
    diff_concept,
    propose_revised_concept,
)
from strands_agents.quanta.refiner import (
    adjust_scene_durations,
    validate_pronunciation_hints,
)
from strands_agents.quanta.scenario import (
    derive_scenario_topic,
    evaluate_scenario_structural,
    sum_scenario_duration,
)
from strands_agents.quanta.style_lock import check_style_lock
from strands_agents.quanta.timing import compute_timing_report

__all__ = [
    "adjust_scene_durations",
    "allowed_decisions_for",
    "check_assembly_inputs",
    "check_style_lock",
    "classify_failure",
    "compute_structural_violations",
    "compute_timing_report",
    "decide_escalation_action",
    "derive_scenario_topic",
    "diff_concept",
    "evaluate_scenario_structural",
    "evaluate_visual_artifact_quality",
    "propose_revised_concept",
    "resume_command_from_decision",
    "sum_scenario_duration",
    "validate_decision",
    "validate_pronunciation_hints",
]
