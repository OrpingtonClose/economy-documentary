# STRANDS_SDK_PATTERNS — canonical snippets

Copy-paste reference for every SDK construct this migration uses. All
examples are correct against the current `HEAD` of
[`OrpingtonClose/sdk-python`](https://github.com/OrpingtonClose/sdk-python).

---

## 1. Minimal `Agent`

```python
from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager

@tool
def generate_scenario(topic: str, num_scenes: int) -> dict:
    """Generate an initial scenes list for a documentary."""
    ...

agent = Agent(
    model="openai/gpt-4o",
    tools=[generate_scenario],
    system_prompt="You are the Scenario Director for an ADHD-friendly documentary pipeline.",
    conversation_manager=SlidingWindowConversationManager(window_size=20),
)

result = await agent.invoke_async("7 minute documentary about inflation")
```

---

## 2. `@tool` with state access

```python
from strands import Agent, tool
from strands.types.tools import ToolContext

@tool(context=True)
async def persist_scenes(context: ToolContext, scenes: list[dict]) -> dict:
    """Persist scenes JSON to the agent's state and the invocation state."""
    context.agent.state.set("scenes", scenes)
    context.invocation_state["scenes"] = scenes
    return {"ok": True, "count": len(scenes)}
```

Use this whenever a tool must publish cross-node data. Don't rely on
return values alone — downstream nodes read `invocation_state`.

---

## 3. Hook provider

```python
from dataclasses import dataclass
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeInvocationEvent, AfterInvocationEvent

@dataclass
class ContractEnforcer(HookProvider):
    contract_name: str

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeInvocationEvent, self._before)
        registry.add_callback(AfterInvocationEvent, self._after)

    def _before(self, event: BeforeInvocationEvent) -> None:
        # may mutate event.messages
        ...

    def _after(self, event: AfterInvocationEvent) -> None:
        # may set event.resume to re-invoke automatically
        ...
```

Pass to `Agent(hooks=[ContractEnforcer(...)])` or
`GraphBuilder().set_hook_providers([...])`.

---

## 4. Before/After ToolCall hooks (cancel / retry)

```python
from strands.hooks.events import BeforeToolCallEvent, AfterToolCallEvent

class SkipIfTimingPassed(HookProvider):
    def register_hooks(self, registry):
        registry.add_callback(BeforeToolCallEvent, self._maybe_cancel)

    def _maybe_cancel(self, event: BeforeToolCallEvent) -> None:
        if event.invocation_state.get("timing_passed"):
            event.cancel_tool = "timing already passed; no refinement needed"


class RetryTransientWorkerErrors(HookProvider):
    _TRANSIENT = ("CUDA OOM", "model reload in progress", "connection reset")

    def register_hooks(self, registry):
        registry.add_callback(AfterToolCallEvent, self._maybe_retry)

    def _maybe_retry(self, event: AfterToolCallEvent) -> None:
        err = event.result.get("content", [{}])[0].get("text", "")
        if any(t in err for t in self._TRANSIENT):
            event.retry = True
```

---

## 5. `GraphBuilder` with cycle edge and conditional edge

```python
from strands.multiagent import GraphBuilder

graph = (
    GraphBuilder()
    .add_node(audio_tool, node_id="audio")
    .add_node(timing_tool, node_id="timing")
    .add_node(scenario_refiner, node_id="refiner")
    # linear: audio → timing
    .add_edge("audio", "timing")
    # conditional cycle: refiner → audio iff timing failed
    .add_edge(
        "timing",
        "refiner",
        condition=lambda s: not s.results["timing"]["output"].get("timing_passed", False),
    )
    .add_edge("refiner", "audio")
    .set_entry_point("audio")
    .set_max_node_executions(30)     # 3x expected loop count as safety net
    .build()
)
result = await graph.invoke_async(topic, invocation_state={"scenes": scenes, "target_duration_sec": 420})
```

`add_edge(condition=...)` is at [`graph.py:272`](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/multiagent/graph.py#L272).
`set_max_node_executions` is at [`graph.py:319`](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/multiagent/graph.py#L319).

---

## 6. Interrupt (human-in-the-loop)

```python
from strands.interrupt import Interrupt, InterruptException
from strands.hooks.events import AfterInvocationEvent

class ScenarioApproval(HookProvider):
    def register_hooks(self, registry):
        registry.add_callback(AfterInvocationEvent, self._maybe_interrupt)

    def _maybe_interrupt(self, event: AfterInvocationEvent) -> None:
        if event.invocation_state.get("auto_approve"):
            return
        raise InterruptException(Interrupt(
            id="scenario-approval",
            name="scenario_approval",
            reason={"stage": "scenario", "scenes": event.invocation_state["scenes"]},
        ))
```

Resume the graph with the user's response:

```python
from strands.types.interrupt import InterruptResponseContent

resume_input: list[InterruptResponseContent] = [{
    "interruptResponse": {"interruptId": "scenario-approval", "response": {"approved": True}},
}]
result = await graph.invoke_async(resume_input)  # no topic arg; resumes
```

See [`interrupt.py:72-106`](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/interrupt.py#L72-L106).

---

## 7. Experiment (`strands-evals`)

```python
from strands_evals import Case, Experiment

experiment = Experiment[str, dict](
    cases=[
        Case(name="economics_basics", input="5-scene documentary about inflation",
             expected_trajectory=["generate_scenario", "evaluate_scenario", "refine_scenario", "evaluate_scenario"],
             metadata={"target_duration_sec": 300}),
    ],
    evaluators=[
        ScenarioQualityEvaluator(),
        CoherenceEvaluator(),
        FaithfulnessEvaluator(),
        TrajectoryEvaluator(),
        ContractComplianceEvaluator(SCENARIO_CONTRACT),
    ],
)

async def task(case):
    agent = build_scenario_agent()
    r = await agent.invoke_async(case.input, invocation_state=case.metadata)
    return {"output": {"scenes": r.state.get("scenes")}, "trajectory": [t.name for t in r.tool_uses]}

reports = await experiment.run_evaluations_async(task)
experiment.to_file("server/strands_agents/evals/experiments/scenario_experiment.json")
```

---

## 8. ToolSimulator

```python
from strands_evals.simulation import ToolSimulator

gpu_sim = ToolSimulator(
    tools=["dispatch_video_job", "check_job_status", "check_worker_health"],
    share_state_id="video_pipeline",
    initial_state_description="GPU worker pool: 2 workers available, queue empty...",
    tool_output_schemas={...},  # pydantic classes
)

agent = Agent(model=..., tools=gpu_sim.tools + [...])  # sim tools drop in unchanged
```

See [`tool_simulator.py`](https://github.com/OrpingtonClose/strands-evals/blob/main/src/strands_evals/simulation/tool_simulator.py).

---

## 9. ActorSimulator

```python
from strands_evals.simulation import ActorSimulator

actor = ActorSimulator.from_case_for_user_simulator(case, max_turns=8)
agent = Agent(model=..., tools=[...])
transcript = await actor.run_interaction_async(agent)
```

Returns a list of `Interaction` rows suitable for `InteractionsEvaluator`.

---

## 10. SessionManager

```python
from strands.session import FileSessionManager

session_manager = FileSessionManager(base_path="/tmp/documentary-pipeline/sessions")
graph = GraphBuilder()....set_session_manager(session_manager).build()
```

Used by 14 (pipeline-graph) and 15 (approval-gates) so interrupts survive
process restarts.

---

## 11. Telemetry (OTel)

No snippet required — every `Agent`, `Tool`, and `Graph` is instrumented
by default. Set `OTEL_EXPORTER_OTLP_ENDPOINT` and traces flow to Phoenix.
The `Experiment` class wires `get_tracer()` at import time
(`experiment.py:26`), so eval runs land in the same traces.

---

## Anti-patterns (don't do this)

- `agent.callback_handler.add_callback(...)` — monkey-patches. Use
  `HookProvider`.
- Sharing a single `SlidingWindowConversationManager` across agents — it's
  stateful. One per agent.
- Writing to `invocation_state` from a pure helper — always go through a
  `@tool(context=True)` so the write is attributable.
- `await asyncio.sleep(...)` in a hook — hooks are fast-path. Spawn a
  background task if you must.
- Bundling two concerns into one `HookProvider` — split into two
  providers even if they register for the same event.
