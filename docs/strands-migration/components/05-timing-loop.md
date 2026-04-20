# 05 — timing-loop

First `GraphBuilder` composition. Combines the audio tool (04), the
timing tool (02), and the scenario refiner (03) into a cycle: if timing
fails, refiner runs, audio re-runs, timing re-checks.

---

## Intent

Given `scenes`, `target_duration_sec`, and any upstream state, loop:

1. Render audio (`audio_tool.render_audio`).
2. Evaluate timing (`timing_tool.evaluate_timing`).
3. If `timing_passed = False`: refine scenes (`scenario_refiner`) and go
   back to step 1.
4. If `timing_passed = True`: exit with final `scenes`,
   `whisperx_alignment`, `timing_report`.

Bounded at 10 iterations (the current `TIMING_LOOP_MAX_ITERATIONS`).

---

## Current implementation

`server/agents/pipeline.py` lines ~139-171 assemble a
`LoopAgent("timing_loop", max_iterations=10, sub_agents=[audio_agent, timing_evaluator, scenario_refiner])`.
Loop exits when `scenario_refiner` sees `state["timing_passed"] is True`
and returns a cancelling content block via `_skip_if_timing_passed`.

---

## Strands implementation

Single module: `server/strands_agents/timing_loop.py`.

```python
from strands import Agent
from strands.multiagent import Graph, GraphBuilder
from .audio_tool import render_audio
from .timing_tool import evaluate_timing
from .scenario_refiner import build_scenario_refiner

def build_timing_loop() -> Graph:
    # Thin agent-shell around the audio tool so the graph can invoke it as a node.
    audio_agent = Agent(
        model=None,                             # no LLM required
        tools=[render_audio],
        system_prompt="Call render_audio with the scenes in invocation_state.",
        deterministic=True,
    )
    timing_agent = Agent(
        model=None,
        tools=[evaluate_timing],
        system_prompt="Call evaluate_timing with scenes + whisperx_alignment + target_duration_sec.",
        deterministic=True,
    )
    refiner = build_scenario_refiner()

    return (
        GraphBuilder()
        .set_graph_id("timing_loop")
        .add_node(audio_agent, node_id="audio")
        .add_node(timing_agent, node_id="timing")
        .add_node(refiner, node_id="refiner")
        .add_edge("audio", "timing")
        # Conditional cycle: if timing failed, refiner runs
        .add_edge(
            "timing", "refiner",
            condition=lambda s: not s.results["timing"].result.output.get("timing_passed", False),
        )
        .add_edge("refiner", "audio")
        .set_entry_point("audio")
        .set_max_node_executions(30)   # 10 loops * 3 nodes, safety margin
        .set_hook_providers([ContractEnforcer(AUDIO_CONTRACT)])
        .build()
    )
```

### Why two deterministic "agents" (audio + timing)

`GraphBuilder` currently expects `AgentBase | MultiAgentBase` nodes
([`graph.py:257`](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/multiagent/graph.py#L257)).
Wrapping a pure tool in `Agent(model=None, tools=[fn], deterministic=True)`
is the cheapest way to satisfy that interface without introducing an LLM
call. If the SDK grows a `@graph_node` decorator for bare tools, switch
to that — but the contract above doesn't change.

### Cycle-edge condition

```python
lambda s: not s.results["timing"].result.output.get("timing_passed", False)
```

Reads the most recent `timing` node's result. When the node hasn't run
yet, `"timing"` won't be in `s.results` — but that path isn't reachable
because the edge source is the timing node itself.

### Termination

- `set_max_node_executions(30)` is a safety net; the cycle condition
  should be the real terminator.
- Every iteration writes `timing_passed` via `@tool(context=True)` so the
  condition always reads the fresh value.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(AUDIO_CONTRACT),
    TrajectoryEvaluator(),                     # validates the node execution order
    Equals("timing_passed", True),             # final state must be True for success cases
]
```

### Test cases (minimum 5)

| Case name | Initial scenes | Expected node sequence | Expected final `timing_passed` |
|-----------|----------------|------------------------|-------------------------------|
| `one_shot_pass` | scenes already within tolerance | `audio → timing` | True |
| `one_refinement_pass` | scenes total 10% long | `audio → timing → refiner → audio → timing` | True |
| `two_refinements_pass` | one scene wildly off | `audio → timing → refiner → audio → timing → refiner → audio → timing` | True |
| `never_converges` | synthetic case that can't pass | graph hits `max_node_executions`, exits with `timing_passed=False` | False |
| `refiner_hook_skip` | scenes already within tolerance after first pass | `refiner` never invoked | True |

### Simulators

`TTS_WORKER_SIMULATOR` for audio. No LLM simulator needed; scenario
refiner talks to the real primary model during evals.

### Thresholds

- `ContractComplianceEvaluator` = 1.00 (hard)
- `TrajectoryEvaluator` ≥ 0.80 (hard)
- `Equals("timing_passed", expected)` = 1.00 (hard per case)

---

## File layout

```
server/strands_agents/
└── timing_loop.py                             # ~150 LOC
```

---

## Acceptance criteria

- [ ] `build_timing_loop()` returns a `Graph` that passes `graph.validate_acyclic()` check disabled (cyclic is intentional).
- [ ] Test `never_converges` exits cleanly without wedging.
- [ ] OTel trace shows the exact cycle order for `one_refinement_pass`.
- [ ] Refiner `SkipIfTimingPassed` hook fires on `refiner_hook_skip` — visible in trace as cancelled tool call.
- [ ] Component 14 can compose this graph as a sub-graph node.
