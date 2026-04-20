# 14 — pipeline-graph

The full end-to-end `Graph`. Composes every component from 01-13 plus
the approval gates (15). Replaces the 1 111-line `pipeline.py`.

---

## Intent

```
[user: topic + target_duration]
        │
        ▼
scenario_agent (01)
        │
        ▼
timing_loop (05 = GraphBuilder[audio 04, timing 02, refiner 03])
        │
        ▼
visual_loop (09 = GraphBuilder[content_analyst 06, visual_concepter 07, coherence_evaluator 08])
        │
        ▼
production_supervisor (10)  ──┐
        │                     │ (escalation_request)
        ▼                     ▼
assembly_tool (11)       escalation_supervisor (13) ──► recovery_agents (12)
        │                     │
        ▼                     ▼
[final_output]          (decision routed back into production or abort)
```

Approval gates (15) insert as conditional interrupts at stage
boundaries: scenario → approval → timing; visual → approval → production.

---

## Current implementation

`server/agents/pipeline.py` (1 111 lines). `SequentialAgent` with 5
stages, wrapping every agent in monkey-patched callbacks for contracts,
approval gates, preference ledger, state manager, preview triggers,
strict assembler checks, and intent verification.

---

## Strands implementation

Single file: `server/strands_agents/pipeline_graph.py`. Target: under
**300 LOC** (vs current 1 111).

```python
from strands.multiagent import Graph, GraphBuilder
from strands.session import FileSessionManager
from .scenario_agent import build_scenario_agent
from .timing_loop import build_timing_loop
from .visual_loop import build_visual_loop
from .production_supervisor import build_production_supervisor
from .assembly_tool import assemble_final_cut
from .escalation_supervisor import build_escalation_supervisor
from .approval_gates import ApprovalGate
from strands import Agent

def build_pipeline_graph(
    output_dir: str = "/tmp/documentary-pipeline",
    session_path: str = "/tmp/documentary-pipeline/sessions",
) -> Graph:
    assembly_agent = Agent(
        model=None, tools=[assemble_final_cut],
        system_prompt="Call assemble_final_cut with state.", deterministic=True,
    )

    return (
        GraphBuilder()
        .set_graph_id("documentary_pipeline")
        .add_node(build_scenario_agent(), node_id="scenario")
        .add_node(build_timing_loop(), node_id="timing_loop")
        .add_node(build_visual_loop(), node_id="visual_loop")
        .add_node(build_production_supervisor(), node_id="production")
        .add_node(assembly_agent, node_id="assembly")
        .add_node(build_escalation_supervisor(), node_id="escalation")
        # Linear backbone
        .add_edge("scenario", "timing_loop")
        .add_edge("timing_loop", "visual_loop")
        .add_edge("visual_loop", "production")
        .add_edge("production", "assembly")
        # Escalation branch: production → escalation when request is set
        .add_edge(
            "production", "escalation",
            condition=lambda s: bool(s.results["production"].result.output.get("escalation_request")),
        )
        # Escalation may route back to production
        .add_edge(
            "escalation", "production",
            condition=lambda s: s.results["escalation"].result.output.get("action") in {"fix", "retry"},
        )
        # or straight to assembly (skip) or terminate (abort)
        .add_edge(
            "escalation", "assembly",
            condition=lambda s: s.results["escalation"].result.output.get("action") == "skip",
        )
        .set_entry_point("scenario")
        .set_max_node_executions(80)
        .set_execution_timeout(3600.0)
        .set_session_manager(FileSessionManager(base_path=session_path))
        .set_hook_providers([
            ApprovalGate(stage="scenario", after_node="scenario"),
            ApprovalGate(stage="visual", after_node="visual_loop"),
            DashboardEmitter(),
            PreferenceLedgerApplier(),
        ])
        .build()
    )
```

### Approval gates

`ApprovalGate` is a `HookProvider` listening to `AfterNodeCallEvent` on
the graph; it raises `InterruptException` after the specified node
unless `invocation_state["auto_approve"]` is set. Resume via the
standard Strands resume flow (see component 15).

### Escalation as a graph edge, not a subgraph

The escalation branch is plain conditional edges on the top-level
graph. No separate "recovery subgraph" — that introduces a state
boundary and re-invokes components unnecessarily. Keeping it flat makes
`recovery_log` trivial and the trace easy to read.

### Session manager

`FileSessionManager` persists interrupt state to disk, so an approval
gate or escalation consultation can span a process restart (e.g. the
user answers via dashboard hours later).

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(SCENARIO_CONTRACT),
    ContractComplianceEvaluator(AUDIO_CONTRACT),
    ContractComplianceEvaluator(VISUAL_DIRECTION_CONTRACT),
    ContractComplianceEvaluator(PRODUCTION_CONTRACT),
    ContractComplianceEvaluator(ASSEMBLY_CONTRACT),
    TrajectoryEvaluator(),
    GoalSuccessRateEvaluator(),
    InteractionsEvaluator(),
    TimelineComplianceEvaluator(),
]
```

### Test cases (minimum 4 — end-to-end, expensive)

| Case name | Input | Expected |
|-----------|-------|----------|
| `golden_path_inflation` | "5-scene inflation documentary", auto_approve=True, all sims clean | final_output.mp4 exists; test_pass=True |
| `golden_path_monetary_policy` | "10-scene monetary policy", auto_approve=True | final_output.mp4; longer trace |
| `escalation_branch` | clean input but simulator injects persistent error on clip 3 | escalation runs; final_output.mp4 still produced |
| `user_rejects_scenario` | scenario approval returns rejected | graph halts at scenario boundary; no downstream nodes invoked |

### Simulators

All three (`GPU`, `TTS`, `ESCALATION`) — this is the composition test.

### Thresholds

- Every `ContractComplianceEvaluator` = 1.00 (hard)
- `TrajectoryEvaluator` ≥ 0.70 (soft)
- `GoalSuccessRateEvaluator` ≥ 0.75 (hard)
- `InteractionsEvaluator` ≥ 0.60 (soft)
- `TimelineComplianceEvaluator` = 1.00 (hard)

---

## File layout

```
server/strands_agents/
└── pipeline_graph.py                             # target < 300 LOC
```

---

## Acceptance criteria

- [ ] LOC under 300 (vs 1 111 today).
- [ ] No monkey-patching anywhere in the module.
- [ ] `golden_path_inflation` end-to-end under 12 min wall-clock against simulators.
- [ ] Session resume tested: kill process mid-approval, restart, send user reply, run completes.
- [ ] OTel trace in Phoenix shows full graph structure.
- [ ] Experiment passes thresholds against simulators; nightly passes against real workers.
