# 15 — approval-gates (`HumanInTheLoopMiddleware` via `interrupt_on`)

Human-in-the-loop is **declarative** in deepagents. You don't write
approval-polling code; you list which tools must interrupt on
`create_deep_agent(..., interrupt_on={...})`, and the caller resumes
with a structured `Command(resume=...)`.

---

## Intent

The documentary pipeline gates three kinds of human decisions:

1. **First GPU dispatch of a run** — `launch_visual_production` pauses;
   operator accepts / edits prompt args / rejects.
2. **Final assembly** — `launch_assembly` pauses; operator accepts /
   responds (e.g. "hold for review"), no edit.
3. **Escalation-to-human** — `request_human_approval`, invoked by the
   `escalation` SubAgent (component 13) when it decides
   `action == "escalate_to_human"`. Operator responds with a decision
   (fix / retry / skip / abort) and a rationale.

Each gate must be:

- **Persistent** — if the server restarts mid-run, the interrupt state
  is restored from LangGraph's checkpointer (Postgres in prod, memory
  in tests).
- **Observable** — operator UI reads pending interrupts over the
  existing `server/api/approval.py` HTTP surface.
- **Auditable** — every resume writes an `ApprovalRecord` to disk
  (run_dir/`approvals/*.json`).

---

## Current implementation

`server/agents/pipeline.py` + `server/callbacks/approval_gate.py` +
`.approval_state.json` file polling on the VM. Operator clicks approve
in the UI → UI writes `.approval_state.json` → polling callback unblocks
the agent. Stateful, brittle, no checkpointing.

---

## Target implementation

### Declaring gates

See component 14. The relevant field on `create_deep_agent`:

```python
interrupt_on = {
    "launch_visual_production": {
        "allow_accept":  True,   # operator can accept as-is
        "allow_edit":    True,   # operator can edit args (prompt, seed)
        "allow_respond": True,   # operator can send free-form reply
        # "allow_reject" is always True
    },
    "launch_assembly": {
        "allow_accept":  True,
        "allow_edit":    False,  # no surgery on assembly args
        "allow_respond": True,
    },
    "request_human_approval": True,  # shorthand: accept + respond
}
```

### `request_human_approval` tool

```python
# server/strands_agents/approval.py
from typing import Any

from langchain_core.tools import tool

@tool
def request_human_approval(
    reason: str,
    summary: str,
    options: list[str] | None = None,
    context_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Pause the run for an operator decision.

    Args:
        reason: Short label for the UI (e.g. "escalation:skip_scene_s3").
        summary: One-paragraph human-readable summary for the operator.
        options: If provided, the UI renders these as clickable buttons.
        context_paths: Files the operator may want to inspect (timeline,
            failed frame, error log).

    Returns:
        The operator's response as a dict. Shape is determined by the
        resume payload (see `Command` examples below).
    """
    # The body is irrelevant: HumanInTheLoopMiddleware intercepts this
    # call before it executes and raises an interrupt. The tool never
    # actually runs; the interrupt resume payload becomes the tool
    # result.
    raise RuntimeError("request_human_approval must be intercepted by HumanInTheLoopMiddleware")
```

### Caller-side resume loop

```python
# server/strands_agents/run.py
from langgraph.types import Command

async def run_documentary(brief: str, run_dir: Path) -> dict[str, Any]:
    agent = build_orchestrator(run_dir)
    state = await agent.ainvoke({"messages": [("user", brief)]})
    while "__interrupt__" in state:
        interrupt = state["__interrupt__"][0]
        decision  = await get_operator_decision(interrupt, run_dir)
        state     = await agent.ainvoke(Command(resume=decision))
    return state
```

`get_operator_decision` is the HTTP handler shim that:

1. Writes the interrupt payload to `run_dir/approvals/pending_{id}.json`.
2. Serves it from `server/api/approval.py GET /pending`.
3. Blocks on a future/queue until the operator `POST /resume`.
4. Validates and returns the resume payload.

### Resume payload shapes

| Type | Payload | Tool result |
|------|---------|-------------|
| `accept` | `{"type": "accept"}` | Tool runs with original args |
| `edit`   | `{"type": "edit", "args": {...}}` | Tool runs with edited args |
| `reject` | `{"type": "reject", "reason": "..."}` | Tool raises; agent sees error |
| `respond`| `{"type": "respond", "content": "..."}` | Free-form content injected as tool result |

### Persistence

The `create_deep_agent` call in component 14 must pass a checkpointer so
interrupts survive restarts. Recommended:

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

checkpointer = AsyncPostgresSaver.from_conn_string(os.environ["APPROVAL_DB_URL"])
# ... then pass via create_deep_agent's graph compile step (see deepagents docs)
```

### Audit trail

After every resume, write:

```json
// run_dir/approvals/resume_{interrupt_id}.json
{
  "interrupt_id": "int-abc123",
  "tool_name": "launch_visual_production",
  "operator": "orpington.close@gmail.com",
  "decision": {"type": "edit", "args": {...}},
  "at": "2026-04-20T20:24:00Z"
}
```

Component 14's pipeline contract requires all resume records to be
present before the run can be marked complete.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ApprovalGateTrajectoryEvaluator(),       # custom: interrupt → resume reaches correct state
    ToolParameterAccuracyEvaluator(),        # edited args actually used
    ContractComplianceEvaluator(APPROVAL_CONTRACT),
    GoalSuccessRateEvaluator(),              # pipeline completes after resume
]
```

### Test cases (minimum 6)

| Case | Gate | Operator response | Expected outcome |
|------|------|-------------------|------------------|
| `accept_visual_dispatch` | `launch_visual_production` | `accept` | Dispatch proceeds, args unchanged |
| `edit_visual_prompt` | `launch_visual_production` | `edit` w/ new prompt | Dispatch proceeds with edited prompt; QA still runs on output |
| `reject_visual_dispatch` | `launch_visual_production` | `reject` | Production SubAgent sees tool error, falls back to escalation |
| `respond_assembly_hold` | `launch_assembly` | `respond: hold 24h` | Tool result carries response; orchestrator parks run |
| `escalation_decision` | `request_human_approval` | `respond: skip scene s3` | Escalation SubAgent returns `skip`; pipeline continues |
| `resume_after_restart` | Any | Operator resumes after simulated server restart | Checkpointer restores state; resume succeeds |

### Simulators

- `OperatorActorSimulator` — `ActorSimulator.from_case_for_user_simulator(...)` with seeded decisions per case.

### Thresholds

| Evaluator | Min score | Hard gate |
|-----------|-----------|-----------|
| `ApprovalGateTrajectoryEvaluator` | 1.00 | Yes |
| `ToolParameterAccuracyEvaluator` | 1.00 | Yes |
| `ContractComplianceEvaluator` | 1.00 | Yes |
| `GoalSuccessRateEvaluator` | 0.90 | Yes |

---

## File layout

```
server/strands_agents/
├── approval.py                   # request_human_approval @tool
├── run.py                        # Command-resume loop
└── evals/
    └── experiments/
        └── approval_experiment.json

server/api/
└── approval.py                   # HTTP surface: GET /pending, POST /resume
```

---

## Acceptance criteria

- [ ] `interrupt_on={...}` passed to `create_deep_agent` in component 14.
- [ ] `request_human_approval` is a no-op `@tool` — execution is fully
      intercepted by `HumanInTheLoopMiddleware`.
- [ ] Caller-side resume loop is in `run.py`; no polling.
- [ ] LangGraph checkpointer wired; restart-recovery case passes.
- [ ] Every resume writes an `ApprovalRecord` to disk.
- [ ] All 6 cases pass thresholds.
