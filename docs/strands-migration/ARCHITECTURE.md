# ARCHITECTURE — current ADK vs. target Strands

This document maps every construct in the current ADK pipeline to its
Strands equivalent. It exists so implementers never have to guess whether a
given ADK feature has a Strands analogue.

Line references point at the current `HEAD` of
[`OrpingtonClose/economy-documentary`](https://github.com/OrpingtonClose/economy-documentary)
and [`OrpingtonClose/sdk-python`](https://github.com/OrpingtonClose/sdk-python).

---

## 1. Current ADK architecture

`server/agents/pipeline.py` (1 111 lines) composes a
`SequentialAgent("documentary_pipeline")` with five stages:

```
SequentialAgent("documentary_pipeline")
├── LoopAgent("scenario_with_gate", max_iterations=N)
│   └── scenario_director (LoopAgent max_iterations=3)
│       ├── scenario_generator (Agent + create_timeline_tool + 6 callbacks)
│       └── scenario_evaluator (Agent + exit_loop + structural checks + 6 callbacks)
│       └── [APPROVAL GATE: .approval_state.json polling]
├── LoopAgent("timing_loop", max_iterations=10)
│   ├── audio_agent           # TTS + WhisperX (deterministic callback)
│   ├── timing_evaluator      # deterministic duration check
│   └── scenario_refiner      # LLM; skips itself when timing_passed
├── LoopAgent("visual_director", max_iterations=5)
│   ├── content_analyst
│   ├── visual_concepter
│   └── coherence_evaluator
│   └── [APPROVAL GATE]
├── Agent("production_supervisor")   # GPU dispatch, escalation ladder
│   └── [APPROVAL GATE]
└── Agent("assembler_agent")         # OTIO → ffmpeg
```

### Why it hurts

- **22 callback files** in `server/callbacks/` are monkey-patched onto
  sub-agents from `pipeline.py` (see lines 181–600, the `_orig_*` /
  `_scenario_after_postconditions` / `_preflight_gate_before` dance). The
  composition is not discoverable by reading any single file.
- **Blackboard state** via `callback_context.state` means every cross-stage
  contract is implicit. `server/contracts.py` patches this up after the
  fact with `StageContract` + `validate_preconditions` /
  `validate_postconditions` called from the callbacks.
- **Approval gates poll `.approval_state.json`** on disk
  (`server/callbacks/approval_gate.py` line 30), which blocks the
  `LoopAgent` thread for up to 2 hours (line 50) — no first-class
  interrupt primitive.
- **Loops are external** — `scenario_generator` → `scenario_evaluator` is a
  `LoopAgent` of two agents exchanging state, rather than a single agent
  reasoning over when to re-generate.

---

## 2. Target Strands architecture

```
Graph(id="documentary_pipeline")
├── node "scenario_agent"           # strands.Agent with 4 tools, loops internally
├── edge (scenario_agent → timing_loop) with condition state.results["scenario"].approved
├── subgraph "timing_loop"          # Graph: audio → timing → refiner (cycle)
│   ├── node "audio_tool"           # deterministic @tool, not an Agent
│   ├── node "timing_tool"          # deterministic @tool, not an Agent
│   ├── node "scenario_refiner"     # strands.Agent
│   └── cycle edge (refiner → audio) condition=lambda s: not s.results["timing"]["timing_passed"]
├── subgraph "visual_loop"          # Graph: content_analyst → visual_concepter → coherence_evaluator (cycle)
├── node "production_supervisor"    # strands.Agent with GPU-dispatch tools + SlidingWindowConversationManager
├── node "assembly_tool"            # deterministic @tool
├── nodes "recovery_*"              # strands.Agents used by production's on-failure cycle edge
└── node "escalation_supervisor"    # strands.Agent with SlidingWindowConversationManager
```

All 22 current callback files become **`HookProvider` classes**
(one provider, one file, in `server/strands_agents/hooks/`). Approval gates
become `Interrupt`s (`strands/interrupt.py`). The blackboard becomes a
combination of `agent.state` (per-agent) and `invocation_state` (shared
across a graph invocation).

---

## 3. ADK → Strands construct mapping

| ADK | Strands | Notes |
|-----|---------|-------|
| `google.adk.agents.Agent` | `strands.Agent` | 1:1. Strands `Agent` is in [`sdk-python/src/strands/agent/agent.py`](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/agent/agent.py). |
| `LoopAgent(max_iterations=N, sub_agents=[a, b, c])` | `GraphBuilder` with a conditional cycle edge + `set_max_node_executions(N)` | The `condition: Callable[[GraphState], bool]` on `add_edge` ([graph.py:272](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/multiagent/graph.py#L272)) replaces `exit_loop`. `set_max_node_executions` ([graph.py:319](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/multiagent/graph.py#L319)) replaces `max_iterations`. |
| `SequentialAgent(sub_agents=[a, b])` | `GraphBuilder` with linear edges: `add_edge("a", "b")` | No special construct. |
| `exit_loop` tool (`google.adk.tools.exit_loop_tool.exit_loop`) | Conditional edge returns `False` | The agent doesn't need a tool to break the loop; the graph does. |
| `before_agent_callback` | `BeforeInvocationEvent` hook | [sdk-python/src/strands/hooks/events.py:38](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/hooks/events.py#L38). Can mutate `messages`. |
| `after_agent_callback` | `AfterInvocationEvent` hook | [hooks/events.py:66](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/hooks/events.py#L66). Can set `resume` to autonomously re-invoke. |
| `before_model_callback` | `BeforeModelCallEvent` | [hooks/events.py:225](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/hooks/events.py#L225). |
| `after_model_callback` | `AfterModelCallEvent` | [hooks/events.py:244](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/hooks/events.py#L244). |
| `before_tool_callback` | `BeforeToolCallEvent` (set `cancel_tool`) | [hooks/events.py:134](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/hooks/events.py#L134). `cancel_tool: bool \| str` short-circuits without calling the tool. |
| `after_tool_callback` | `AfterToolCallEvent` (set `retry=True` to retry) | [hooks/events.py:173](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/hooks/events.py#L173). |
| `google.adk.tools.FunctionTool(fn)` | `@tool` decorator | `from strands import tool`. |
| `callback_context.state` (blackboard) | `agent.state` + `invocation_state` | Agent-local state persists across turns; invocation_state is shared across a graph run. |
| `output_key="scenes"` (auto state write) | Manual state write in `AfterInvocationEvent` hook OR a `@tool` that writes to `agent.state` | No equivalent magic; be explicit. |
| `actions.escalate=True` | Conditional edge that returns `False`, or `cancel_node` via `BeforeNodeCallEvent` hook | Escalation is a graph-level decision in Strands. |
| Returning `Content` to skip agent LLM call | `cancel_node` on `BeforeNodeCallEvent` hook | Also useful for the "refiner skips when `timing_passed`" pattern. |
| `.approval_state.json` file polling (`server/callbacks/approval_gate.py`) | `Interrupt` + `InterruptException` | [sdk-python/src/strands/interrupt.py:32](https://github.com/OrpingtonClose/sdk-python/blob/main/src/strands/interrupt.py#L32). Agent raises `InterruptException(Interrupt(...))`; caller resumes with a list of `interruptResponse` content blocks. |
| `StageContract` (preconditions/postconditions) | `ContractEnforcer(HookProvider)` on `BeforeInvocationEvent` / `AfterInvocationEvent` | See [`contracts/CONTRACTS.md`](./contracts/CONTRACTS.md). |
| OpenTelemetry instrumentation (manual in `server/callbacks/*.py`) | Built-in via `strands.telemetry` | Zero config; `get_tracer()` already wired into `Experiment` and `Agent`. |
| Tool-calling with `parallel_tool_calls=True` (`server/agents/model_config.py:115`) | `tool_executors.concurrent.ConcurrentToolExecutor` | Pass to `Agent(tool_executor=...)`. Same semantics. |

---

## 4. Model routing

### Current

`server/agents/model_config.py` exposes `build_model()` which returns:

- a bare string (e.g. `"gemini-2.5-pro"`) for native Gemini, OR
- a `google.adk.models.LiteLlm(model=name, extra_body=...)` for everything
  else.

Four roles, four env vars:

| Role | Env var | Used by |
|------|---------|---------|
| Primary | `ADK_MODEL` (default `litellm/openai/gpt-4o`) | Tool-capable agents (scenario generator, production supervisor) |
| Synthesis | `ADK_SYNTHESIS_MODEL` | Evaluators, refiners |
| Thinker | `ADK_THINKER_MODEL` | Content analyst (deep reasoning) |
| Vision | `ADK_VISION_MODEL` | Coherence evaluator (video frames) |

Vendor-specific params (e.g. Venice `venice_parameters`) come through
`extra_body`.

### Target (Strands)

Strands accepts:

1. **A model string** like `"openai/gpt-4o"` — LiteLLM-style routing is the
   default. `strands.Agent(model="openai/gpt-4o")`.
2. **A `strands.models.openai.OpenAIModel` instance** (or `BedrockModel`,
   `AnthropicModel`, …) for explicit configuration:

    ```python
    from strands.models.openai import OpenAIModel

    primary = OpenAIModel(
        model_id=os.environ["STRANDS_MODEL"],        # e.g. "openai/gpt-4o"
        base_url=os.environ.get("OPENAI_API_BASE"),  # venice.ai, etc.
        api_key=os.environ["OPENAI_API_KEY"],
        params={"parallel_tool_calls": True, "extra_body": {"venice_parameters": {...}}},
    )
    ```

Keep the four roles, rename the env vars to the `STRANDS_*` prefix, and keep
`STRANDS_MODEL_FALLBACK` for role-level fallback. A single
`server/strands_agents/models.py` module exports `primary()`, `synthesis()`,
`thinker()`, `vision()` factory functions mirroring the shape of
`build_model()`.

| Role | New env var | Fallback to |
|------|-------------|-------------|
| Primary | `STRANDS_MODEL` | — |
| Synthesis | `STRANDS_SYNTHESIS_MODEL` | `STRANDS_MODEL` |
| Thinker | `STRANDS_THINKER_MODEL` | `STRANDS_SYNTHESIS_MODEL` |
| Vision | `STRANDS_VISION_MODEL` | `STRANDS_MODEL` |

This is a 1:1 port; keep the same vendor/proxy configuration. The only
behavioural change is we stop silently dropping `extra_body` when the role
has its own `*_API_BASE` (`model_config.py:130–138`) and always forward it.

---

## 5. Invocation shape

Current (ADK):

```python
runner = InMemoryRunner(app=app, agent=pipeline)
events = runner.run(user_id="u", session_id="s", new_message=content)
```

Target (Strands):

```python
from strands.multiagent import GraphBuilder

graph = (
    GraphBuilder()
    .add_node(scenario_agent, node_id="scenario")
    .add_node(timing_loop, node_id="timing_loop")
    ...
    .set_entry_point("scenario")
    .set_session_manager(FileSessionManager(base_path="/tmp/documentary-pipeline/sessions"))
    .set_hook_providers([ContractEnforcer(), RevisionTagger(), DashboardEmitter()])
    .build()
)
result = await graph.invoke_async(topic, invocation_state={"corpus_path": ..., "target_duration_sec": 420})
```

Key behavioural differences:

- `invocation_state` is passed explicitly at invoke time, not seeded via a
  `run_start_seed` callback (`server/callbacks/run_start_seed.py`).
- `FileSessionManager` handles persistence instead of ad-hoc JSON in
  `/tmp/documentary-pipeline/`.
- Hooks are a first-class `list[HookProvider]` passed to the graph, not
  monkey-patched after construction.
