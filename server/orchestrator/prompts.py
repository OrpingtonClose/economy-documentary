"""
LLM prompts for the planning-heavy production orchestrator.

All prompts used by the ProductionOrchestrator for plan generation,
evaluation, replanning, and synthesis.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Plan optimizer — generates / refines the ProductionPlan
# ---------------------------------------------------------------------------

PLAN_OPTIMIZER_INSTRUCTION = """\
You are an expert video production planner for a documentary pipeline.
Given visual concepts (prompts, durations, LoRA styles) and available GPU workers,
create an optimal production plan that:
1. Groups clips into batches for parallel execution
2. Assigns clips to GPU workers based on load balancing
3. Orders batches to maximize style coherence (same LoRA in same batch)
4. Prioritizes clips that other clips depend on for style reference
5. Accounts for resume support (skip already-generated clips)
6. Estimates GPU time and identifies risks

Rules:
- Every clip duration MUST be ≤ 10.0 seconds (LTX-2.3 hardware limit)
- Every visual concept MUST have exactly one ClipTask
- No duplicate clip_ids
- Batch size should not exceed the number of available workers
- Already-generated clips with good QA MUST be marked skip=true, not regenerated
- clip_id format: "s{scene_num:03d}_p{phrase_idx:03d}"

Output a ProductionPlan JSON with batches, worker assignments, strategy,
estimated GPU minutes, and risk assessment.
"""

# ---------------------------------------------------------------------------
# Plan evaluator — rates the plan quality
# ---------------------------------------------------------------------------

PLAN_EVALUATOR_INSTRUCTION = """\
You are a video production plan evaluator. Evaluate the given production plan
against these criteria:

1. DURATION VALIDITY: Every clip duration ≤ 10.0 seconds (LTX-2.3 limit)
2. WORKER BALANCE: No worker has >2x the clips of another worker
3. BATCH COHERENCE: Clips in the same batch should share visual style or scene
4. ORDERING: Style-reference clips must come before clips that depend on them
5. COMPLETENESS: Every visual concept has exactly one clip task
6. RISK MITIGATION: Plan identifies what could go wrong and has fallbacks
7. RESUME AWARENESS: Already-generated clips are marked skip=true, not regenerated

Rating scale:
- EXCELLENT (3): All criteria pass, plan is production-ready
- GOOD (2): Minor issues that won't cause failures
- FAIR (1): Several improvements needed but structurally sound
- POOR (0): Duration limits violated, clips missing, or structural errors

Rate as EXCELLENT only if ALL criteria pass.
Rate as POOR if duration limits are violated or clips are missing.

Respond with a JSON object containing:
{
  "rating": 0-3,
  "feedback": "specific feedback",
  "needs_improvement": true/false,
  "focus_areas": ["area1", "area2"]
}
"""

# ---------------------------------------------------------------------------
# Replan prompt — adjusts plan after batch execution
# ---------------------------------------------------------------------------

REPLAN_PROMPT_TEMPLATE = """\
You are replanning a video production after batch {batch_idx} completed.

Previous batch results:
{batch_results}

Failed clips: {failed_clips}
QA reasons for failures: {qa_reasons}

Remaining batches in the original plan:
{remaining_batches}

Already completed clips (do NOT regenerate these):
{completed_clips}

Decide:
1. Should failed clips be retried with modified prompts? If so, adjust the prompt
   to avoid the QA failure reason.
2. Should the global style be adjusted based on QA patterns?
3. Should worker assignments be rebalanced based on timing data?
4. Are there cross-clip coherence issues visible from results so far?

Output an updated ProductionPlan JSON for the REMAINING work only.
Already-completed clips must NOT appear in the new plan.
"""

# ---------------------------------------------------------------------------
# Synthesis prompt — final summary after all batches
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """\
Summarize the production results for the pipeline log:

Plan strategy: {strategy}
Total clips: {total_clips}
Failed clips: {failed_clips}
Skipped clips (resume): {skipped_clips}
Replan events: {replan_count}
Workers used: {workers_used}
Batch results:
{batch_summaries}

Produce a concise 2-3 sentence summary suitable for the pipeline status display.
Include any quality concerns or notable patterns.
"""

# ---------------------------------------------------------------------------
# Plan generation user prompt — provides the visual concepts to the optimizer
# ---------------------------------------------------------------------------

PLAN_GENERATION_USER_TEMPLATE = """\
Generate a production plan for the following documentary video clips.

Visual concepts ({num_concepts} clips total):
{concepts_json}

Available GPU workers ({num_workers}):
{worker_list}

Already-generated clips (skip these — mark skip=true):
{existing_clips}

Visual style:
{visual_style}

Constraints:
- Maximum clip duration: 10.0 seconds (LTX-2.3 limit)
- Each worker processes one clip at a time
- Batch size should not exceed {num_workers} (the worker count)
- Group clips by LoRA style for coherence when possible

{feedback_section}

Output a valid ProductionPlan JSON.
"""

# ---------------------------------------------------------------------------
# Evaluator user prompt — provides the plan to evaluate
# ---------------------------------------------------------------------------

PLAN_EVALUATION_USER_TEMPLATE = """\
Evaluate this production plan:

{plan_json}

Visual concepts count: {num_concepts}
Available workers: {num_workers}
Already-generated clips: {num_existing}

Respond with a JSON evaluation result.
"""
