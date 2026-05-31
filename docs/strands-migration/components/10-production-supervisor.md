> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# 10 — production-supervisor (DeepAgent `SubAgent`)

The production supervisor is the **GPU-dispatch specialist**. It decides
when to launch `launch_visual_production` for each scene, monitors the
jobs, runs per-artifact QA, and drives tactical recovery (component 12).
It is a `SubAgent` — not a flat tool set on the main orchestrator —
because GPU dispatch has enough local state (per-scene retry count, per-
worker health, per-scene concept selection) to warrant its own context.

When tactical recovery fails or a situation exceeds its authority, the
production SubAgent emits a structured escalation payload and returns to
the parent orchestrator, which then delegates to the `escalation`
SubAgent (component 13).

---

## Intent

Given accepted scenes + `concepts_by_scene` + style_lock + audio
artifacts, produce per-scene video artifacts:

1. For each scene: pick the highest-scoring concept, call
   `launch_visual_production(scene_id, concept, style_lock, ...)`.
2. `await_tasks` on the launched batch (parallelism up to pool cap).
3. For each returned artifact: run `evaluate_visual_coherence` and/or
   `evaluate_visual_artifact_quality` (deterministic checks: frame count,
   duration, codec, black-frame fraction).
4. For failures:
   - Transient worker errors → `retry_scene` tool (component 12).
   - Prompt-level issues → `fix_scene` tool (regenerates prompt,
     re-launches).
   - Content-level issues unfixable tactically → `skip_scene` tool
     (marks the scene as degraded) OR request escalation.
5. Return per-scene artifacts + a production report. AGENTS.md invariant
   #6 forbids returning with any scene still marked `pending`.

---

## Current implementation

`server/agents/production_supervisor.py` — approximately 700 LOC. A
single `Agent` with tools for GPU dispatch, health checks, per-artifact
QA, and escalation hooks. Uses `SlidingWindowConversationManager` to
carry diagnostic context across scenes within a single run.

---

## Target implementation

### SubAgent declaration

```python
# server/strands_agents/subagents/production.py
from deepagents.types import SubAgent

PRODUCTION_SUPERVISOR_PROMPT = """\
You are the visual production supervisor. You dispatch GPU video jobs for
each scene, monitor their completion, run QA on each artifact, and apply
tactical recovery when needed.

Hard rules (AGENTS.md invariants):
- Never dispatch a scene whose audio artifact is missing.
- Never mark the stage complete with any scene still pending.
- Retry budget is 2 attempts per scene. After that, either call
  fix_scene (prompt-level) or escalate.
- Check worker health before dispatching. If fewer workers than scenes,
  dispatch in rolling batches rather than all at once.

Process:
1. Read scenes.json and concepts_by_scene.json.
2. Call check_worker_health.
3. Dispatch every scene's highest-scoring concept in parallel (up to
   worker count) via launch_visual_production.
4. await_tasks on the batch.
5. For each returned artifact, call evaluate_visual_artifact_quality.
6. For failures: apply retry / fix / skip per the rules.
7. Write production_report.json with per-scene status.

Only escalate via `request_escalation(payload)` after tactical recovery
is exhausted.
"""

production_subagent: SubAgent = {
    "name": "production",
    "description": (
        "GPU dispatch specialist. Invoke after visual concepts are "
        "written. Returns per-scene video artifacts + production_report."
    ),
    "system_prompt": PRODUCTION_SUPERVISOR_PROMPT,
    "tools": [
        check_worker_health,
        launch_visual_production,        # component 10's task-pool tool
        check_tasks,
        await_tasks,
        evaluate_visual_artifact_quality,  # deterministic @tool
        evaluate_visual_coherence,         # component 08 (optional re-check)
        retry_scene,                       # component 12
        fix_scene,                         # component 12
        skip_scene,                        # component 12
        request_escalation,                # payload builder; orchestrator handles delegation
        read_file,
        write_file,
    ],
    "model": os.environ.get("STRANDS_MODEL", "openai/gpt-4o"),
    "middleware": [
        SummarizationMiddleware(max_tokens_before_summary=12_000),
    ],
}
```

### `launch_visual_production` (AsyncTaskPool tool)

Lives in `server/strands_agents/task_tools.py`. Identical shape to
`launch_audio_render` (component 04) but dispatches to the GPU worker
pool. Payload:

```python
{
    "scene_id": "s3",
    "concept_id": "c12",
    "prompt": "...",
    "style_lock": {...},
    "duration_sec": 5.2,
    "seed": 42,
}
```

Returns immediately with a `task_id`. Completion payload carries
`{"artifact_path": "...", "frames": 126, "codec": "h264", "black_frame_fraction": 0.01}`.

### Interrupt wiring (component 15)

`launch_visual_production` is listed in the parent orchestrator's
`interrupt_on={...}` so that the first dispatch of every run is
human-approved. Subsequent dispatches within the same SubAgent run do
not re-interrupt (deepagents tracks interrupt history per tool call).

### Recovery surface (component 12)

- `retry_scene(scene_id)` — increments retry counter, re-launches.
  Orchestrator-invariant: max 2 retries per scene.
- `fix_scene(scene_id, reason)` — calls `generate_visual_concepts`
  with the failure reason, picks a different concept, re-launches.
  Budget 1 per scene.
- `skip_scene(scene_id)` — marks the scene as degraded, records the
  reason. Flagged in `production_report.json`.

### Why a SubAgent

- **Isolated turn budget.** Per-scene dispatch + QA + tactical recovery
  can easily take 40+ tool calls. Keeping that in the parent
  orchestrator's context would starve the planner of room for the
  finishing stages (assembly, approval).
- **Expensive model choice.** The supervisor benefits from a larger
  reasoning-capable model, independent of the orchestrator's choice.
- **Escalation boundary.** The SubAgent completing and returning is the
  natural handover point for the orchestrator to consult the
  `escalation` SubAgent.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    GoalSuccessRateEvaluator(),
    ProductionSupervisorTrajectoryEvaluator(),  # custom
    TimelineComplianceEvaluator(),              # artifact-level
    ToolParameterAccuracyEvaluator(),           # launch args correct
    ParallelLaunchEvaluator(tool="launch_visual_production"),
    ContractComplianceEvaluator(PRODUCTION_CONTRACT),
    EscalationDecisionEvaluator(),              # when escalation was requested
]
```

### Test cases (minimum 6)

| Case | Scenes | Seeded worker / artifact behaviour | Expected outcome |
|------|--------|-----------------------------------|------------------|
| `one_shot_success` | 5 | All 5 dispatches return valid artifacts | 1 dispatch batch, no recovery, no escalation |
| `transient_worker_error` | 5 | Scene 3 returns worker-500 | 1 retry succeeds, no escalation |
| `prompt_issue` | 5 | Scene 2 fails QA (style drift) | `fix_scene` applied, then succeeds |
| `persistent_failure` | 5 | Scene 4 fails after 2 retries + 1 fix | `skip_scene` applied OR escalation requested |
| `worker_starved` | 10 | Only 2 workers available | Rolling batches of 2, not a single mass dispatch |
| `budget_exhausted` | 5 | All scenes fail persistently | Escalation requested; no scene marked complete falsely |

### Simulators

- `GPUWorkerSimulator` (see [`eval-framework/SIMULATION.md`](../eval-framework/SIMULATION.md)): drives `launch_visual_production`, `check_tasks`, `await_tasks`, `check_worker_health` with seeded per-case behaviour.

### Thresholds

| Evaluator | Min score | Hard gate |
|-----------|-----------|-----------|
| `GoalSuccessRateEvaluator` | 0.80 | Yes |
| `ProductionSupervisorTrajectoryEvaluator` | 0.80 | Yes |
| `TimelineComplianceEvaluator` | 1.00 | Yes |
| `ToolParameterAccuracyEvaluator` | 0.90 | Yes |
| `ParallelLaunchEvaluator` | 0.70 | No |
| `ContractComplianceEvaluator` | 1.00 | Yes |
| `EscalationDecisionEvaluator` (on escalation cases) | 0.70 | No |

---

## File layout

```
server/strands_agents/
├── subagents/
│   └── production.py              # SubAgent declaration
├── task_tools.py                  # launch_visual_production (+ shared pool)
├── artifact_qa.py                 # evaluate_visual_artifact_quality
└── evals/
    └── experiments/
        └── production_experiment.json
```

---

## Acceptance criteria

- [ ] `production` SubAgent shipped as a `SubAgent` TypedDict.
- [ ] `launch_visual_production`, `check_tasks`, `await_tasks`,
      `check_worker_health` on the SubAgent's `tools`.
- [ ] Retry budget and rolling batching enforced by the system prompt;
      evals pin the trajectory.
- [ ] Escalation payload shape documented (see component 13 contract).
- [ ] All 6 cases pass thresholds.
- [ ] OTel trace shows a nested `task` span with per-scene sub-spans.
