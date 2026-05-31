> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# ARCHITECTURE — current ADK vs. target DeepAgent + Strands

This document maps every construct in the current ADK pipeline to its
equivalent in the target architecture: a **DeepAgent orchestrator** driving
a roster of **Strands-agent leaves**. It exists so implementers never have
to guess whether a given ADK feature has an analogue.

Line references point at the current `HEAD` of
[`OrpingtonClose/economy-documentary`](https://github.com/OrpingtonClose/economy-documentary),
[`OrpingtonClose/deepagents`](https://github.com/OrpingtonClose/deepagents),
[`OrpingtonClose/sdk-python`](https://github.com/OrpingtonClose/sdk-python),
and [`OrpingtonClose/MiroThinker`](https://github.com/OrpingtonClose/MiroThinker).

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
- **No cross-run memory.** Nothing learns. Every run re-discovers the same
  failure modes.

---

## 2. Target architecture: DeepAgent orchestrator + Strands leaves

The pipeline is **one DeepAgent** with a flat roster of tools and
subagents. There is no top-level graph; the DeepAgent plans its own
trajectory using its model + `TodoListMiddleware` + `MemoryMiddleware`.

```
create_deep_agent(
    model="openai/gpt-4o",                                          # or claude-sonnet-4-6
    memory=["docs/strands-migration/AGENTS.md",
            ".deepagents/AGENTS.md"],                               # MemoryMiddleware
    system_prompt=DOCUMENTARY_ORCHESTRATOR_PROMPT,                  # short; the invariants live in AGENTS.md
    backend=FilesystemBackend(root_dir="/tmp/documentary-pipeline"),
    tools=[
        # Strands leaves invoked directly (no subagent boundary)
        generate_scenario,            # component 01 (Strands agent → @tool wrapper)
        evaluate_scenario_structural, # component 01
        evaluate_timing,              # component 02 (deterministic @tool)
        refine_scenario,              # component 03 (Strands agent → @tool wrapper)
        validate_otio_timeline,       # assembly helper
        checkpoint_to_b2,             # persistence helper

        # AsyncTaskPool launch tools (MiroThinker pattern)
        launch_audio_render,          # component 04 — TTS + WhisperX per scene
        launch_visual_production,     # component 10 — LTX GPU job per scene
        launch_assembly,              # component 11 — OTIO → ffmpeg
        check_tasks,                  # poll status
        await_tasks,                  # block until done

        # Post-artifact QA
        evaluate_audio_invariants,    # component 04 (deterministic @tool)
        evaluate_visual_coherence,    # component 08 (calls vision model)

        # Worker pool health
        check_worker_health,
    ],
    subagents=[
        scenario_subagent,            # component 01 as SubAgent for isolated context
        visual_subagent,              # components 06+07+08 (content_analyst + visual_concepter + coherence_evaluator in one context)
        production_subagent,          # component 10 — GPU dispatch specialist
        escalation_subagent,          # component 13 — escalation decisions
    ],
    interrupt_on={                    # HumanInTheLoopMiddleware
        "launch_visual_production": {"allow_accept": True, "allow_edit": True, "allow_respond": True},
        "launch_assembly":          {"allow_accept": True, "allow_edit": False, "allow_respond": True},
        "request_human_approval":    True,
    },
)
```

### Leaf layer (Strands)

Every box in the `tools=[...]` and `subagents=[...]` lists is implemented
using the Strands Agents SDK:

- **`@tool` wrappers** around a small `strands.Agent` for LLM-backed leaves
  (scenario generator, refiner, concepter).
- **`@tool` wrappers** around plain Python for deterministic leaves
  (timing evaluator, audio invariant QA, OTIO validator, B2 checkpoint).
- **`HookProvider`** classes replace all 22 ADK callback files. Each
  Strands leaf carries the hooks it needs (contract enforcement, revision
  tagging, OTel spans).
- **`SlidingWindowConversationManager`** on the few Strands leaves that
  need turn memory (scenario refiner, production supervisor Strands
  agent). Most leaves are stateless.

### AsyncTaskPool layer (MiroThinker pattern)

TTS and LTX video renders are long-running (seconds to minutes). Calling
them synchronously from the DeepAgent blocks the planner. Instead:

- `launch_audio_render(scene_id, voice, script)` → returns immediately
  with a `task_id`, queues the job on the pool.
- `launch_visual_production(scene_id, concept_id, ...)` → same, for GPU
  jobs.
- `check_tasks()` → snapshot of all running/completed tasks.
- `await_tasks(task_ids=[...], timeout=600)` → block on specific tasks.

This mirrors [`MiroThinker apps/strands-agent/task_tools.py`](https://github.com/OrpingtonClose/MiroThinker/blob/main/apps/strands-agent/task_tools.py)
lines 56–162 and lets the orchestrator launch every scene's audio render
in parallel, then work on other planning (e.g. spinning up the visual
subagent) while they run.

### Middleware stack (automatic from `create_deep_agent`)

Per [`deepagents/graph.py:284-604`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/graph.py#L284):

```
TodoListMiddleware                  # todo list tool, agent-managed
FilesystemMiddleware                # ls, read_file, write_file, edit_file
SubAgentMiddleware                  # task() tool to delegate to subagents
SummarizationMiddleware             # context compaction
PatchToolCallsMiddleware
AsyncSubAgentMiddleware             # launch_* async subagents (if configured)
AnthropicPromptCachingMiddleware
MemoryMiddleware                    # loads + injects AGENTS.md, instructs edit_file
HumanInTheLoopMiddleware            # interrupt_on gates
_PermissionMiddleware               # tool-level permissions (always last)
```

The orchestrator gets planning + filesystem + subagents + memory + HITL
**for free**. The only thing the migration has to build is:

1. the leaf Strands agents (15 components),
2. the `launch_*` task tools (component 04/10/11 + infra),
3. the seeded `AGENTS.md`,
4. the system prompt for the DeepAgent.

---

## 3. ADK → DeepAgent+Strands construct mapping

| ADK | New home | Notes |
|-----|----------|-------|
| `google.adk.agents.Agent` (LLM-backed) | `strands.Agent` wrapped in `@tool`, OR a `SubAgent` TypedDict passed to `create_deep_agent(subagents=[...])` | If the agent has isolated context and a cohesive domain (scenario, visual, production, escalation), use `SubAgent`. Otherwise wrap as `@tool`. See [`deepagents/middleware/subagents.py:25`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py#L25). |
| `google.adk.agents.Agent` (deterministic) | Plain function with `@tool` decorator | No LLM. E.g. `evaluate_timing`, `validate_otio_timeline`, `checkpoint_to_b2`. |
| `LoopAgent(max_iterations=N, sub_agents=[a, b, c])` | DeepAgent planning loop | The orchestrator's model + `TodoListMiddleware` chooses when to call which tool. Termination is the orchestrator's decision (typically: "I've got an accepted scenario" or "timing_passed"). Safety cap via max turns in the graph config. |
| `SequentialAgent(sub_agents=[a, b])` | DeepAgent planning | Same — the orchestrator sequences by default because the AGENTS.md invariants forbid parallelism across stages. |
| `exit_loop` tool | Not needed | Loop exit is the orchestrator deciding the next tool call is no longer a retry. |
| `before_agent_callback` | DeepAgent `AgentMiddleware.before_agent` OR Strands `BeforeInvocationEvent` on the leaf | Which side owns the concern dictates which layer. Orchestrator-wide concerns (OTel root span, session init) → DeepAgent middleware. Leaf-local concerns (contract precondition) → Strands hook on the leaf. |
| `after_agent_callback` | DeepAgent `AgentMiddleware.after_agent` OR Strands `AfterInvocationEvent` | Same rule. Post-condition checks live on the Strands leaf that produces the artifact. |
| `before_model_callback` | Strands `BeforeModelCallEvent` on the leaf | Per-leaf concern. |
| `after_model_callback` | Strands `AfterModelCallEvent` on the leaf | Per-leaf concern. |
| `before_tool_callback` | Strands `BeforeToolCallEvent` (set `cancel_tool`) on the leaf | Per-leaf concern. Orchestrator-level tool gating is `interrupt_on`. |
| `after_tool_callback` | Strands `AfterToolCallEvent` (set `retry=True`) on the leaf | Transient worker errors are retried here, then escalated to the orchestrator if they persist. |
| `google.adk.tools.FunctionTool(fn)` | `@tool` decorator on the DeepAgent's `tools=[...]` list OR on a Strands leaf | Tools exposed to the orchestrator are the ones it plans with. Tools private to a Strands leaf stay on that leaf. |
| `callback_context.state` (blackboard) | **Two layers**: DeepAgent filesystem-backed state (via `FilesystemBackend(root_dir=...)`) for artifacts (scenes JSON, OTIO, audio paths), plus Strands `agent.state` / `invocation_state` for intra-leaf bookkeeping | The DeepAgent's `write_file` / `read_file` tools are how the orchestrator and subagents exchange large artifacts. See [`deepagents/middleware/filesystem.py`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py). |
| `output_key="scenes"` | Orchestrator calls `write_file("scenes.json", ...)` after the leaf returns | No magic. Explicit. |
| `actions.escalate=True` | DeepAgent calls `escalation` SubAgent via the `task` tool | See component 13. |
| Returning `Content` to skip the agent | Strands `cancel_node` via `BeforeInvocationEvent` hook on the leaf | Still useful for "refiner skips when `timing_passed`" — but the orchestrator also just … doesn't call the refiner when timing passed. |
| `.approval_state.json` file polling | `interrupt_on={tool_name: InterruptOnConfig}` on `create_deep_agent` | LangGraph's `interrupt()` primitive. Graph pauses; caller resumes with `accept`/`edit`/`respond`/`reject`. See [`deepagents/graph.py:363`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/graph.py#L363). Component 15. |
| `StageContract` (preconditions/postconditions) | Still `StageContract`, enforced by a Strands `ContractEnforcer(HookProvider)` on the producing leaf | Orchestrator reads the produced artifacts via `read_file`. Pre-condition failures on a leaf bubble up as tool errors and the orchestrator decides how to handle them. |
| OpenTelemetry instrumentation (manual in `server/callbacks/*.py`) | Built-in: LangGraph traces the DeepAgent; Strands traces each leaf | Zero config, two OTel trees that are stitched by a root span the orchestrator sets at run start. |
| Tool-calling with `parallel_tool_calls=True` | Two places: `launch_*` tools use AsyncTaskPool for *real* parallelism; within a Strands leaf, `tool_executors.concurrent.ConcurrentToolExecutor` for intra-turn parallelism | The orchestrator's own tool calls are serial inside a single LangGraph tick, but it can launch many `launch_*` tasks that run concurrently on the pool. |
| ADK no-op "worker VM manager" logic | Orchestrator's `check_worker_health` tool + AGENTS.md invariant #2 | The orchestrator asks before dispatching. No implicit degradation. |

### Recovery & escalation

- Component 12 (recovery agents: fix, retry, skip) are Strands `@tool`s
  the `production` SubAgent calls directly. They're tactical, in-context.
- Component 13 (escalation supervisor) is a `SubAgent` the orchestrator
  delegates to when tactical recovery fails or AGENTS.md rules say the
  situation is beyond the production SubAgent's authority.

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

### Target

Five roles: the four leaf roles above, plus **orchestrator**.

| Role | New env var | Fallback to | Binding |
|------|-------------|-------------|---------|
| Orchestrator | `DEEPAGENT_MODEL` (default `openai/gpt-4o` or `claude-sonnet-4-6`) | — | `create_deep_agent(model=...)` — either a `str` LiteLLM alias or a `BaseChatModel` instance. See [`graph.py:230`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/graph.py#L230). |
| Primary | `STRANDS_MODEL` | `DEEPAGENT_MODEL` | `strands.Agent(model=...)` on tool-capable leaves. |
| Synthesis | `STRANDS_SYNTHESIS_MODEL` | `STRANDS_MODEL` | Structural evaluators, refiners. |
| Thinker | `STRANDS_THINKER_MODEL` | `STRANDS_MODEL` | Content analyst. |
| Vision | `STRANDS_VISION_MODEL` | `STRANDS_MODEL` | Coherence evaluator. |

Strands leaves accept either a model string (`"openai/gpt-4o"` — LiteLLM
routing, the default) or a `strands.models.*` instance:

```python
from strands.models.openai import OpenAIModel

primary = OpenAIModel(
    model_id=os.environ["STRANDS_MODEL"],
    base_url=os.environ.get("OPENAI_API_BASE"),   # venice.ai, etc.
    api_key=os.environ["OPENAI_API_KEY"],
    params={"parallel_tool_calls": True, "extra_body": {"venice_parameters": {...}}},
)
```

The orchestrator uses the LangChain `ChatOpenAI` / `ChatAnthropic` instance
(or a string alias for LiteLLM). Keep a single
`server/strands_agents/models.py` module that exports
`orchestrator()`, `primary()`, `synthesis()`, `thinker()`, `vision()`
factory functions mirroring the shape of the current `build_model()`.

---

## 5. Where the spec lives

- [`AGENTS.md`](./AGENTS.md) — the seeded memory loaded by MemoryMiddleware. Hard invariants + planning heuristics.
- [`components/14-pipeline-graph.md`](./components/14-pipeline-graph.md) — the full `create_deep_agent(...)` call for the top-level orchestrator.
- [`components/15-approval-gates.md`](./components/15-approval-gates.md) — `interrupt_on` configuration, caller-side resume protocol.
- [`reference/DEEPAGENT_PATTERNS.md`](./reference/DEEPAGENT_PATTERNS.md) — copy-paste snippets for `create_deep_agent`, `SubAgent`, `AsyncSubAgent`, middleware.
- [`reference/STRANDS_SDK_PATTERNS.md`](./reference/STRANDS_SDK_PATTERNS.md) — copy-paste snippets for the Strands leaves.
- [`contracts/STATE_SCHEMA.md`](./contracts/STATE_SCHEMA.md) — what lives in the DeepAgent filesystem backend vs. per-leaf `agent.state`.
