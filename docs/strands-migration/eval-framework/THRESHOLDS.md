> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# THRESHOLDS — per-stage regression thresholds

Single source of truth for CI gating.
[`CI_PIPELINE.md`](./CI_PIPELINE.md)'s `run_experiment.py` parses the table
below and enforces each row against the corresponding `EvaluationReport`.

Thresholds are **permissive at launch** (we want green builds while
migrating) and ratcheted monthly based on the trailing-30-day distribution.
See `docs/strands-migration/eval-framework/THRESHOLDS.md` git history for
the ratchet log.

---

## Threshold table

| Stage | Evaluator | Min Score | Hard Gate? | Rationale |
|-------|-----------|-----------|------------|-----------|
| scenario | `ScenarioQualityEvaluator` | 0.70 | Yes | Any `POOR` cap from the structural checks is a hard fail. |
| scenario | `CoherenceEvaluator` | 0.75 | No | LLM-as-judge; wiggle room. |
| scenario | `TrajectoryEvaluator` | 0.80 | Yes | Agent must exercise generate→evaluate→refine tool sequence. |
| scenario | `FaithfulnessEvaluator` | 0.70 | No | Groundedness in the research corpus. |
| scenario | `ContractComplianceEvaluator` | 1.00 | Yes | `SCENARIO_CONTRACT` preconditions + postconditions. |
| timing | `ContractComplianceEvaluator` | 1.00 | Yes | `AUDIO_CONTRACT` + `scenes[]` present. |
| timing | `Equals(timing_passed, expected)` | 1.00 | Yes | Deterministic per-case expectation. |
| audio | `AudioInvariantEvaluator` | 1.00 | Yes | All seven invariants must pass. |
| audio | `ContractComplianceEvaluator` | 1.00 | Yes | `AUDIO_CONTRACT` including B2 upload check. |
| audio | `CritiqueStoreEvaluator(artifact_type="audio")` | 0.75 | No | Aggregate over all audio clips. |
| visual_direction | `VisualCoherenceEvaluator` | 0.70 | No | LLM-as-judge on coherence across scenes. |
| visual_direction | `ToolSelectionAccuracyEvaluator` | 0.70 | No | Concepter picked right tool for phrase type. |
| visual_direction | `ContractComplianceEvaluator` | 1.00 | Yes | `VISUAL_DIRECTION_CONTRACT`. |
| production | `TimelineComplianceEvaluator` | 1.00 | Yes | OTIO timeline must be fully compliant. |
| production | `GoalSuccessRateEvaluator` | 0.80 | Yes | Each scene has a rendered clip uploaded to B2. |
| production | `ToolParameterAccuracyEvaluator` | 0.75 | No | Right prompt shape for GPU worker. |
| production | `EscalationDecisionEvaluator` | 0.70 | No | Supervisor's recovery choices. |
| assembly | `TimelineComplianceEvaluator` | 1.00 | Yes | Final assembly OTIO. |
| assembly | `ContractComplianceEvaluator` | 1.00 | Yes | `ASSEMBLY_CONTRACT`. |
| recovery | `EscalationDecisionEvaluator` | 0.70 | No | Recovery agents' fix/retry/skip calls. |
| escalation | `EscalationDecisionEvaluator` | 0.70 | No | Top-level escalation decisions. |
| escalation | `InteractionsEvaluator` | 0.65 | No | Multi-turn conversation quality. |
| pipeline | `InteractionsEvaluator` | 0.60 | No | End-to-end inter-agent quality. |
| pipeline | `GoalSuccessRateEvaluator` | 0.70 | No | End-to-end success rate (production canary). |

---

## Enforcement semantics

- **Min Score** rows use `EvaluationReport.overall_score`.
- **Hard Gate? = Yes** rows additionally require `EvaluationReport.test_passes is True`
  on **every** case (i.e. no single case regressed).
- Deterministic evaluators (`Contains`, `Equals`, `ToolCalled`,
  `StateEquals`) always hard-gate at `1.0` — we don't tolerate partial
  pass rates on them.
- LLM-as-judge evaluators (Coherence, Faithfulness, Helpfulness,
  VisualCoherence, EscalationDecision) are **never** hard gates. Their
  scores drift with model revisions; the nightly run tracks the drift.

---

## Ratchet policy

Every first of the month:

1. Pull trailing-30-day `overall_score` distribution for every row.
2. Set new min = `P25 - 0.05` (floor of 0.5 for LLM-as-judge).
3. If a hard-gate row had *any* regression in the window, leave it alone
   (don't loosen a hard gate).
4. Open a PR updating this file; the PR itself must pass CI against the
   tightened thresholds.

This is stricter than time-unbounded ratcheting because we'd otherwise
suffer a quality ratchet on a bad week.

---

## What is NOT in CI

- Subjective scoring ("does the documentary feel good?") — that's the job
  of the approval gates. Evals exist to make approval gates rare, not
  replace them.
- Cost regressions. Cost tracking lives on Phoenix dashboards; enforcing
  it in CI encourages gaming evaluator complexity.
- Flaky-worker regressions. We detect those via the nightly integration
  workflow, not PR CI.
