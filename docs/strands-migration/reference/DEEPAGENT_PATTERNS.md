# DEEPAGENT_PATTERNS — canonical snippets for the orchestrator layer

Every orchestrator-touching component (05, 09, 10, 13, 14, 15) cites this
file rather than restating the deepagents API. Line references point at
[`OrpingtonClose/deepagents`](https://github.com/OrpingtonClose/deepagents).

---

## 1. `create_deep_agent(...)` — the one call

Source: [`libs/deepagents/deepagents/graph.py:218-604`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/graph.py#L218).

```python
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_openai import ChatOpenAI

agent = create_deep_agent(
    model=ChatOpenAI(model="gpt-4o"),                  # or "openai/gpt-4o" (LiteLLM alias)
    system_prompt=DOCUMENTARY_ORCHESTRATOR_PROMPT,     # string, short; invariants live in AGENTS.md
    tools=[...],                                       # list[BaseTool | Callable]
    subagents=[...],                                   # list[SubAgent | AsyncSubAgent | CustomSubAgent]
    memory=[                                           # opt-in; triggers MemoryMiddleware
        "docs/strands-migration/AGENTS.md",
        ".deepagents/AGENTS.md",
    ],
    backend=FilesystemBackend(root_dir="/tmp/documentary-pipeline"),
    interrupt_on={                                     # triggers HumanInTheLoopMiddleware
        "launch_visual_production": {"allow_accept": True, "allow_edit": True, "allow_respond": True},
        "launch_assembly":          {"allow_accept": True, "allow_edit": False, "allow_respond": True},
        "request_human_approval":   True,
    },
    middleware=[],                                     # extra user middleware (placed before defaults)
)

# LangGraph CompiledStateGraph API
final_state = await agent.ainvoke({"messages": [("user", brief)]})
```

What the factory wires up automatically
([`graph.py:284-604`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/graph.py#L284)):

```
user middleware (if any)
  → TodoListMiddleware               # todo list tool
  → FilesystemMiddleware              # ls / read_file / write_file / edit_file
  → SubAgentMiddleware                # task() tool to delegate
  → SummarizationMiddleware           # context compaction
  → PatchToolCallsMiddleware
  → AsyncSubAgentMiddleware           # only if any AsyncSubAgent passed
  → AnthropicPromptCachingMiddleware  # only for anthropic models
  → MemoryMiddleware                  # only if memory=[...] passed
  → HumanInTheLoopMiddleware          # only if interrupt_on={...} passed
  → _PermissionMiddleware             # always last, tool-level permissions
```

---

## 2. `SubAgent` — delegating a cohesive domain to its own context

Source: [`libs/deepagents/deepagents/middleware/subagents.py:25-90`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py#L25).

```python
from deepagents.types import SubAgent

visual_subagent: SubAgent = {
    "name": "visual",
    "description": (
        "Visual production specialist. Use for any task involving content "
        "analysis, visual concept generation, or coherence evaluation "
        "across scenes. Call with the current scenes JSON and style_lock."
    ),
    "system_prompt": VISUAL_SUBAGENT_PROMPT,
    "tools": [                                # SubAgent's own tools
        analyze_content_phrases,              # Strands leaf (component 06)
        generate_visual_concepts,             # Strands leaf (component 07)
        evaluate_visual_coherence,            # Strands leaf (component 08)
        read_file, write_file,                # inherited from parent filesystem
    ],
    "model": "openai/gpt-4o",                 # may differ from parent
    "middleware": [SummarizationMiddleware(max_tokens_before_summary=8000)],
    # "interrupt_on": {...}                   # SubAgent-local HITL (optional)
}
```

The orchestrator delegates via the built-in `task` tool:

```
task(subagent_type="visual", description="Generate visual concepts for the current 5 scenes")
```

The SubAgent runs in an isolated message history (parent's filesystem is
shared), executes its own plan, and returns a summary that the orchestrator
sees as the `task` tool result.

---

## 3. `AsyncSubAgent` — long-running remote subagent on Agent Protocol

Source: [`libs/deepagents/deepagents/types/async_subagent.py`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/types/async_subagent.py).

Used for subagents that run on a separate server and take longer than the
parent's synchronous patience — **not** for our TTS/GPU jobs, which are
better modelled as task-pool tools (next section). `AsyncSubAgent` is
reserved for something like "deep research on the topic's economic context
that takes 20 minutes".

```python
from deepagents.types import AsyncSubAgent

research_async: AsyncSubAgent = {
    "name": "deep_research",
    "description": "Multi-hour deep research on the documentary topic.",
    "client": AgentClient(url=os.environ["RESEARCH_AGENT_URL"]),
    "input_builder": lambda state: {"topic": state["documentary_topic"]},
}
```

Orchestrator gets three tools: `launch_deep_research`, `check_tasks`,
`await_tasks`. The agent can fire-and-poll rather than block.

---

## 4. AsyncTaskPool — parallelism for local long jobs (MiroThinker pattern)

This is **not** an AsyncSubAgent. It's a custom tool set backed by a pool
of workers the orchestrator owns. Mirrors
[`MiroThinker apps/strands-agent/task_tools.py`](https://github.com/OrpingtonClose/MiroThinker/blob/main/apps/strands-agent/task_tools.py).

Shape:

```python
from langchain_core.tools import BaseTool, tool

pool = AsyncTaskPool(max_concurrent=4)

@tool
async def launch_audio_render(scene_id: str, script: str, voice: str) -> dict:
    """Queue TTS + WhisperX for a scene. Returns task_id immediately."""
    task_id = await pool.submit("audio", scene_id=scene_id, script=script, voice=voice)
    return {"task_id": task_id, "scene_id": scene_id, "status": "queued"}

@tool
async def check_tasks() -> list[dict]:
    """Snapshot of every running / completed task."""
    return [t.snapshot() for t in pool.all()]

@tool
async def await_tasks(task_ids: list[str], timeout: float = 600) -> list[dict]:
    """Block until given tasks complete or timeout. Returns result dicts."""
    return await pool.wait(task_ids, timeout=timeout)
```

Orchestrator usage pattern: launch many, plan other work, poll, await.

```
for scene in scenes:
    launch_audio_render(scene_id=scene["id"], ...)   # 8 concurrent TTS jobs

# delegate visual subagent while audio runs
task(subagent_type="visual", description="...")

# come back
await_tasks(task_ids=[...], timeout=300)
evaluate_timing(scenes=..., whisperx_alignment=...)
```

---

## 5. `MemoryMiddleware` — opt-in persistent memory

Source: [`libs/deepagents/deepagents/middleware/memory.py`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/middleware/memory.py).

Activated automatically when you pass `memory=[...]` to
`create_deep_agent` ([`graph.py:592-593`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/graph.py#L592)):

- **`before_agent` hook** reads every path in `memory` via the configured
  backend and caches the content ([`memory.py:238-270`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/middleware/memory.py#L238)).
- **`wrap_model_call → modify_request` hook** injects the cached content
  plus the `<memory_guidelines>` prompt into the system message on every
  model call ([`memory.py:306-320`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/middleware/memory.py#L306)).
- The agent is **instructed to `edit_file` the AGENTS.md files** when it
  learns something new ([`memory.py:97-156`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/middleware/memory.py#L97)).

It is **user-preference / project-context memory**, not self-critique.
To make memory influence planning, we seed
[`AGENTS.md`](../AGENTS.md) with planning heuristics directly ("launch
audio before visual concepts", "escalate persistent TTS failures after 2
retries").

---

## 6. `HumanInTheLoopMiddleware` — `interrupt_on={...}`

Source: [`libs/deepagents/deepagents/graph.py:363-420`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/graph.py#L363).

```python
create_deep_agent(
    ...,
    interrupt_on={
        "launch_visual_production": InterruptOnConfig(
            allow_accept=True, allow_edit=True, allow_respond=True
        ),
        "launch_assembly": InterruptOnConfig(
            allow_accept=True, allow_edit=False, allow_respond=True
        ),
        "request_human_approval": True,         # shorthand for {"allow_accept": True, "allow_respond": True}
    },
)
```

When the DeepAgent decides to call one of these tools, LangGraph raises
an `interrupt()` carrying the tool call payload. The caller resumes with:

```python
from langgraph.types import Command

result = await agent.ainvoke(Command(resume={"type": "accept"}))
result = await agent.ainvoke(Command(resume={"type": "edit",    "args": {...}}))
result = await agent.ainvoke(Command(resume={"type": "respond", "content": "..."}))
result = await agent.ainvoke(Command(resume={"type": "reject",  "reason": "..."}))
```

See component 15 for the caller-side resume protocol and persistence.

---

## 7. Backends — where files live

Source: [`libs/deepagents/deepagents/backends/`](https://github.com/OrpingtonClose/deepagents/tree/main/libs/deepagents/deepagents/backends).

- **`StateBackend()`** (default) — filesystem lives in LangGraph state.
  Ephemeral. Fine for evals / tests.
- **`FilesystemBackend(root_dir=...)`** — real filesystem. Required for
  production because:
  - OTIO timelines, audio files, and video segments are large binaries.
  - The `launch_audio_render` task pool needs a shared directory the
    workers can write into.
  - B2 sync (AGENTS.md invariant #4) needs real paths.

Use `FilesystemBackend(root_dir=f"/tmp/documentary-pipeline/{run_id}")` so
each run has an isolated tree.

---

## 8. Middleware stack — `AgentMiddleware` hooks we might add

Source: [`libs/deepagents/deepagents/middleware/`](https://github.com/OrpingtonClose/deepagents/tree/main/libs/deepagents/deepagents/middleware).

Project-specific middleware we may add to the orchestrator:

- **`ContractEnforcementMiddleware`** — after any `write_file` into
  `scenes.json` / `timeline.otio`, validate against
  [`contracts/CONTRACTS.md`](../contracts/CONTRACTS.md). On failure,
  raise a tool error so the orchestrator can react.
- **`B2CheckpointMiddleware`** — after any state transition that
  produces a new primary artifact, queue a background `launch_b2_sync`.
- **`OTelRootSpanMiddleware`** — open one root span per run at
  `before_agent`, close at `after_agent`, so LangGraph + Strands traces
  stitch cleanly.

Define these as subclasses of `AgentMiddleware` with `before_agent`,
`after_agent`, `wrap_model_call`, and/or `wrap_tool_call` hooks.
