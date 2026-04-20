# 14 — pipeline orchestrator (`create_deep_agent`)

The full end-to-end orchestrator. Not a graph. One `create_deep_agent`
call wiring together tools (leaves), SubAgents (cohesive domains),
memory (AGENTS.md), and `interrupt_on` (approval gates). Replaces the
1 111-line `pipeline.py`.

---

## Intent

```
[user: topic + target_duration]
        │
        ▼
┌───────────────────────────────────────────────┐
│  Documentary DeepAgent orchestrator           │
│  (LangGraph CompiledStateGraph)               │
│                                               │
│  system_prompt: SHORT — just the stage outline│
│  memory: [AGENTS.md]   ← invariants + plans   │
│                                               │
│  tools:                                       │
│    @tool generate_scenario        (01)        │
│    @tool evaluate_scenario        (01)        │
│    @tool refine_scenario          (03)        │
│    @tool evaluate_timing          (02)        │
│    @tool launch_audio_render      (04)        │
│    @tool launch_visual_production (10 pool)   │
│    @tool launch_assembly          (11 pool)   │
│    @tool launch_b2_sync           (infra)     │
│    @tool check_tasks                          │
│    @tool await_tasks                          │
│    @tool request_human_approval   (15)        │
│                                               │
│  subagents:                                   │
│    visual       (09)  SubAgent                │
│    production   (10)  SubAgent                │
│    escalation   (13)  SubAgent                │
│                                               │
│  interrupt_on:                                │
│    launch_visual_production                   │
│    launch_assembly                            │
│    request_human_approval                     │
│                                               │
│  backend: FilesystemBackend(root_dir=run_dir) │
└───────────────────────────────────────────────┘
```

---

## Current implementation

`server/agents/pipeline.py` — 1 111 lines. `SequentialAgent` with 5
stages, wrapping every child agent in monkey-patched callbacks for
contracts, approval gates, preference ledger, state manager, preview
triggers, strict assembler checks, and intent verification.

---

## Target implementation

Single file: `server/strands_agents/pipeline.py`. Target: **≤ 300 LOC**
(vs current 1 111).

```python
# server/strands_agents/pipeline.py
import os
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware import HumanInTheLoopMiddleware  # noqa: F401
from langchain_core.tools import BaseTool

from .leaves import (
    generate_scenario,
    evaluate_scenario,
    refine_scenario,
    evaluate_timing,
)
from .task_tools import (
    launch_audio_render,
    launch_visual_production,
    launch_assembly,
    launch_b2_sync,
    check_tasks,
    await_tasks,
)
from .subagents.visual import visual_subagent
from .subagents.production import production_subagent
from .subagents.escalation import escalation_subagent
from .approval import request_human_approval

ORCHESTRATOR_PROMPT = """\
You are the documentary pipeline orchestrator. Your job is to turn a user
brief into a final video, going through five stages:

1. Scenario : call generate_scenario, then evaluate_scenario, then
   refine_scenario until the scenario passes structural checks.
2. Audio + timing : launch_audio_render in parallel per scene, await,
   evaluate_timing, and loop back to refine_scenario when needed (see
   AGENTS.md "Timing stage").
3. Visual : delegate to the `visual` SubAgent via the task tool.
4. Production : delegate to the `production` SubAgent via the task tool.
5. Assembly : launch_assembly, await, then launch_b2_sync.

Approval gates (handled by interrupt_on): launch_visual_production,
launch_assembly, request_human_approval. Do not try to bypass them.

When anything fails: first try tactical recovery inside the owning
SubAgent. If that exhausts, delegate to the `escalation` SubAgent and
follow its decision. Never mark a stage complete with unresolved
failures (see AGENTS.md invariants).
"""


def build_orchestrator(run_dir: Path) -> Any:
    tools: list[BaseTool] = [
        generate_scenario,
        evaluate_scenario,
        refine_scenario,
        evaluate_timing,
        launch_audio_render,
        launch_visual_production,
        launch_assembly,
        launch_b2_sync,
        check_tasks,
        await_tasks,
        request_human_approval,
    ]

    return create_deep_agent(
        model=os.environ.get("STRANDS_MODEL", "openai/gpt-4o"),
        system_prompt=ORCHESTRATOR_PROMPT,
        tools=tools,
        subagents=[visual_subagent, production_subagent, escalation_subagent],
        memory=[
            "docs/strands-migration/AGENTS.md",
            ".deepagents/AGENTS.md",
        ],
        backend=FilesystemBackend(root_dir=str(run_dir)),
        interrupt_on={
            "launch_visual_production": {"allow_accept": True, "allow_edit": True, "allow_respond": True},
            "launch_assembly":          {"allow_accept": True, "allow_edit": False, "allow_respond": True},
            "request_human_approval":   True,
        },
    )
```

Run entrypoint:

```python
# server/strands_agents/run.py
from langgraph.types import Command

async def run_documentary(brief: str, run_dir: Path) -> dict[str, Any]:
    agent = build_orchestrator(run_dir)
    state = await agent.ainvoke({"messages": [("user", brief)]})
    while "__interrupt__" in state:
        decision = await get_operator_decision(state)   # blocking; component 15
        state = await agent.ainvoke(Command(resume=decision))
    return state
```

### What the orchestrator does NOT do

- **No GraphBuilder.** Stage order is implicit in the system prompt +
  AGENTS.md heuristics. The DeepAgent plans each step; no hand-coded
  edges.
- **No monkey-patched callbacks.** All cross-cutting logic is middleware
  (`AgentMiddleware` subclasses) or hooks inside leaves.
- **No direct GPU dispatch.** The orchestrator delegates to the
  `production` SubAgent; it never calls `evaluate_visual_artifact_quality`
  itself.
- **No approval state polling.** `interrupt_on={...}` triggers a
  first-class LangGraph interrupt; the caller resumes with `Command`.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    GoalSuccessRateEvaluator(),
    InteractionsEvaluator(),
    PipelineTrajectoryEvaluator(),                # custom — stage ordering + SubAgent delegation
    ContractComplianceEvaluator(PIPELINE_CONTRACT),
    TimelineComplianceEvaluator(),                # final OTIO
    AudioInvariantEvaluator(),                    # final audio
    MemoryHonoringEvaluator(),                    # custom — was the AGENTS.md invariant respected?
]
```

### Test cases (minimum 5, all end-to-end)

| Case | Brief | Simulators | Expected trajectory |
|------|-------|------------|---------------------|
| `happy_path_5min` | "5-minute documentary about inflation, 5 scenes" | TTS, GPU, Assembly | Scenario → audio (1 iter) → visual (1 iter) → production (1 batch) → assembly → B2 |
| `timing_refine_once` | "3-minute explainer, 3 scenes" | TTS seeded off by +6s first pass | Extra scenario-refiner pass |
| `visual_revise` | "7-minute, 7 scenes" | GPU seeded to return style-drifted scene 4 | `fix_scene` applied in production |
| `escalation_path` | "5-minute, 5 scenes" | GPU seeded to fail persistently on scene 2 | Production requests escalation, escalation decides `skip`, pipeline completes |
| `operator_approval_edit` | "5-minute, 5 scenes" | GPU responds normally | Operator edits visual prompts at the first gate; pipeline resumes with edited args |

### Simulators

- `TTSToolSimulator` (component 04)
- `GPUWorkerSimulator` (component 10)
- `AssemblyToolSimulator` (component 11)
- `OperatorActorSimulator` for approval gates (component 15)

### Thresholds

| Evaluator | Min score | Hard gate |
|-----------|-----------|-----------|
| `GoalSuccessRateEvaluator` | 0.80 | Yes |
| `InteractionsEvaluator` | 0.60 | No |
| `PipelineTrajectoryEvaluator` | 0.80 | Yes |
| `ContractComplianceEvaluator` | 1.00 | Yes |
| `TimelineComplianceEvaluator` | 1.00 | Yes |
| `AudioInvariantEvaluator` | 1.00 | Yes |
| `MemoryHonoringEvaluator` | 0.90 | Yes |

---

## File layout

```
server/strands_agents/
├── pipeline.py                   # create_deep_agent call (≤ 300 LOC)
├── run.py                        # async entrypoint with Command resume loop
├── leaves/                       # scenario / timing / refiner leaves (01-03)
├── task_tools.py                 # launch_* + check_tasks + await_tasks
├── subagents/
│   ├── visual.py                 # 09
│   ├── production.py             # 10
│   └── escalation.py             # 13
├── approval.py                   # request_human_approval (15)
└── evals/
    └── experiments/
        └── pipeline_experiment.json
```

---

## Acceptance criteria

- [ ] Single `create_deep_agent` call with `memory=[...]`,
      `subagents=[...]`, `interrupt_on={...}`, `backend=FilesystemBackend(...)`.
- [ ] No GraphBuilder imports anywhere in the module.
- [ ] `server/strands_agents/pipeline.py` ≤ 300 LOC.
- [ ] All 5 cases pass thresholds.
- [ ] Langfuse trace shows: root span → nested SubAgent spans →
      per-leaf tool spans.
- [ ] `AGENTS.md` is loaded at startup (verified via
      `MemoryHonoringEvaluator` injecting a test invariant).
