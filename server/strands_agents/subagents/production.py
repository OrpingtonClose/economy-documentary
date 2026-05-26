"""Production SubAgent declaration for Component 10.

The production supervisor is the GPU-dispatch specialist. It decides
when to launch ``launch_visual_production`` for each scene, monitors
the jobs, runs deterministic per-artifact QA, and drives tactical
recovery (component 12). When recovery fails it builds a structured
escalation payload for the parent orchestrator to forward to the
``escalation`` SubAgent (component 13).

This module ships only the declarative SubAgent spec consumed by
Component 14's ``create_deep_agent(subagents=[...])`` call. The runtime
trajectory is evaluated via
``strands_agents.evals.experiments.production`` using trajectory
fixtures — no execution happens inside this module.

See ``docs/strands-migration/components/10-production-supervisor.md``.
"""

from __future__ import annotations

from typing import Any

from strands_agents.artifact_qa import evaluate_visual_artifact_quality
from strands_agents.coherence_evaluator import score_visual_coherence
from strands_agents.recovery import (
    FIX_BUDGET,
    RETRY_BUDGET,
    fix_scene,
    request_escalation,
    retry_scene,
    skip_scene,
)
from strands_agents.task_tools import (
    await_tasks,
    check_tasks,
    check_worker_health,
    launch_visual_production,
)

#: Environment variable consulted to override the SubAgent's model.
#: Production dispatch benefits from the vision-capable reasoning tier
#: (e.g. ``openai/gpt-4o``) so a dedicated env var is exposed.
PRODUCTION_SUBAGENT_MODEL_ENV: str = "STRANDS_VISION_MODEL"

#: Fallback model when :data:`PRODUCTION_SUBAGENT_MODEL_ENV` is not set.
PRODUCTION_SUBAGENT_DEFAULT_MODEL: str = "openai/gpt-4o"

#: Hard ceiling on retry attempts per scene. Re-exported from
#: :mod:`strands_agents.recovery` for evaluator convenience.
PRODUCTION_RETRY_BUDGET: int = RETRY_BUDGET

#: Hard ceiling on prompt-level fixes per scene.
PRODUCTION_FIX_BUDGET: int = FIX_BUDGET


_PRODUCTION_SUBAGENT_PROMPT_TEMPLATE: str = """\
You are the visual production supervisor for a documentary pipeline.
You dispatch GPU video-render jobs, monitor completion, run QA on every
returned artifact, and drive tactical recovery. You are a SubAgent —
the parent orchestrator owns delegation to the escalation SubAgent.

Hard rules (AGENTS.md invariants):
- Never dispatch a scene whose audio artifact is missing. Every
  ``launch_visual_production`` call MUST pass a non-empty
  ``audio_artifact_url`` sourced from ``narration_blocks``.
- Never mark the stage complete with any scene still pending. Every
  scene must finish as ``rendered``, ``skipped``, or ``escalated``.
- Retry budget is __RETRY_BUDGET__ per scene. After that you MUST
  either call ``fix_scene`` (prompt-level) or request escalation.
- Fix budget is __FIX_BUDGET__ per scene. After that you MUST either
  call ``skip_scene`` or request escalation.
- Check worker health before the first dispatch. If the number of
  available workers is less than the number of scenes, dispatch in
  rolling batches of size ``workers_available`` rather than a single
  mass launch.

Process:
1. Read the current state: ``scenes``, ``concepts_by_scene``,
   ``style_lock``, and ``narration_blocks``. Build a per-scene
   dispatch plan: ``scene_id``, highest-scoring ``concept_id``, the
   rendered ``prompt``, ``duration_sec``, ``seed``, and the matching
   ``audio_artifact_url``. Refuse to proceed if any scene lacks
   audio — surface the gap and stop.
2. Call ``check_worker_health`` once. Record
   ``workers_available``. If it is less than the dispatch plan size,
   plan rolling batches.
3. For each batch, call ``launch_visual_production`` for every scene
   in the batch IN PARALLEL. Do not wait for the first before
   launching the second. Collect the returned ``task_id`` values.
4. Call ``await_tasks`` on the batch's task_ids. When a batch
   finishes, continue with the next batch until all scenes have a
   completion payload.
5. For each completion payload, call
   ``evaluate_visual_artifact_quality`` with the completion dict and
   the scene's ``duration_sec`` target. Inspect the verdict:

   - ``pass`` — record the artifact as ``rendered`` and continue.
   - ``warn`` — record the warning but treat the artifact as
     accepted (do NOT retry on warnings).
   - ``fail`` — decide between retry / fix / skip per the rules
     below.

6. Recovery decision tree (for every failed artifact):

   - Worker-transient failure (``worker_500``, ``timeout``,
     ``pool_starved``) AND retry budget not exhausted for this
     scene: call ``retry_scene`` and re-launch via
     ``launch_visual_production`` with ``revision=next_revision``.
     Add the new task to the active batch.
   - Prompt-level failure (``frame_count_mismatch``,
     ``black_frame_ceiling_exceeded``, ``duration_mismatch``,
     style drift) AND fix budget not exhausted: call
     ``fix_scene`` with the failure reason, regenerate the concept
     (via ``score_visual_coherence`` on the scene plus a fresh
     ``propose_concept`` if available), then re-launch production
     with ``revision=next_revision``.
   - Retry or fix budget exhausted AND the failure is localised
     (single scene): call ``skip_scene`` with the reason — the
     assembly stage will handle the gap.
   - Retry or fix budget exhausted AND the failure is systemic
     (e.g. every dispatch fails, or worker pool unhealthy): call
     ``request_escalation`` with scene_id=``"_global"`` and the
     accumulated evidence.

7. When all scenes are terminal (``rendered``, ``skipped``, or
   ``escalated``), write ``production_report.json`` via
   ``write_file`` with:

   - ``per_scene`` — list of ``{scene_id, status, task_id,
     artifact_path, retry_count, fix_count, qa_verdict}``.
   - ``workers_available_at_start`` — the snapshot from step 2.
   - ``rolling_batches`` — number of batches executed.
   - ``escalation_requested`` — bool; set True only if you called
     ``request_escalation``.

8. Return a brief summary to the parent including
   ``rendered_count``, ``skipped_count``, ``escalation_requested``,
   and the full per-scene ledger.

You MUST NOT write the final timeline or assembly — that is
component 11. You MUST NOT delegate directly; escalation goes
through the parent via ``request_escalation``. You MUST NOT call
``launch_visual_production`` for any scene after you have called
``skip_scene`` for it.
"""


PRODUCTION_SUBAGENT_PROMPT: str = _PRODUCTION_SUBAGENT_PROMPT_TEMPLATE.replace(
    "__RETRY_BUDGET__", str(PRODUCTION_RETRY_BUDGET)
).replace("__FIX_BUDGET__", str(PRODUCTION_FIX_BUDGET))


#: Tools the production SubAgent is allowed to call. Ordered so
#: health-check / dispatch / polling appear before QA / recovery /
#: escalation. The list intentionally contains one ``launch_*`` tool
#: (``launch_visual_production``) — this SubAgent is the only one in
#: the documentary graph that is permitted to dispatch GPU work.
PRODUCTION_SUBAGENT_TOOLS: tuple[Any, ...] = (
    check_worker_health,
    launch_visual_production,
    check_tasks,
    await_tasks,
    evaluate_visual_artifact_quality,
    score_visual_coherence,
    retry_scene,
    fix_scene,
    skip_scene,
    request_escalation,
)

#: Tool names a production trajectory should call. Exposed so tests
#: and :class:`ProductionSupervisorTrajectoryEvaluator` can assert
#: against the declared toolset without importing the callables.
PRODUCTION_SUBAGENT_TOOL_NAMES: tuple[str, ...] = (
    "check_worker_health",
    "launch_visual_production",
    "check_tasks",
    "await_tasks",
    "evaluate_visual_artifact_quality",
    "score_visual_coherence",
    "retry_scene",
    "fix_scene",
    "skip_scene",
    "request_escalation",
)

#: Tools that must appear exactly once at the start of every run
#: (before any ``launch_*`` call).
PRODUCTION_BOOTSTRAP_TOOLS: frozenset[str] = frozenset({"check_worker_health"})

#: Tools that signal the end of a recovery leg. Each appearance in
#: the trajectory consumes budget or transfers control out.
PRODUCTION_RECOVERY_TOOLS: frozenset[str] = frozenset(
    {"retry_scene", "fix_scene", "skip_scene", "request_escalation"}
)

#: Tools invoked per-dispatch (one per scene, potentially multiple per
#: scene across retries/fixes).
PRODUCTION_DISPATCH_TOOLS: frozenset[str] = frozenset(
    {"launch_visual_production", "check_tasks", "await_tasks"}
)


__all__ = []
