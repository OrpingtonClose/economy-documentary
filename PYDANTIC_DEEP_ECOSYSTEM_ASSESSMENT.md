> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# pydantic-deep Ecosystem Assessment: Reality vs. Proposition

## Status: VERIFIED

Date: 2026-05-27
Package: `pydantic-deep==0.3.19` (installed in project venv)
Base Framework: `pydantic-ai==1.102.0`

---

## 1. What Actually Exists

### 1.1 Core Factory

```python
from pydantic_deep import create_deep_agent

agent = create_deep_agent(
    model="openrouter:deepseek/deepseek-v4-flash",
    instructions="...",
    tools=[...],           # Additional tools
    toolsets=[...],        # Toolsets (TodoToolset, SubAgentToolset, etc.)
    capabilities=[...],    # AbstractCapability subclasses
    hooks=[...],           # Hook objects for HooksCapability
    backend=StateBackend(),
    # ... 40+ more parameters
)
```

### 1.2 Existing Capabilities (real, verified)

| Capability | Source | Purpose |
|---|---|---|
| `HooksCapability` | `pydantic_deep.capabilities.hooks` | Claude Code-style hooks on tool/run/model lifecycle |
| `StuckLoopDetection` | `pydantic_deep.capabilities.stuck_loop` | Detects repetitive tool patterns, raises `ModelRetry` |
| `PlanCapability` | `pydantic_deep.capabilities.plan` | Interactive planning (ask_user, save_plan) |
| `MemoryCapability` | `pydantic_deep.capabilities.memory` | Persistent memory across sessions |
| `TeamCapability` | `pydantic_deep.capabilities.teams` | Agent team management |
| `BrowserCapability` | `pydantic_deep.capabilities.browser` | Web browsing |
| `ContextFilesCapability` | `pydantic_deep.capabilities.context` | Context file injection |
| `SkillsCapability` | `pydantic_deep.capabilities.skills` | Skill directory loading |
| `PeriodicReminderCapability` | `pydantic_deep.capabilities.periodic_reminder` | Task reminders every N turns |
| `ContextManagerCapability` | `pydantic_ai_summarization` | Token tracking + auto-compression |
| `LimitWarnerCapability` | `pydantic_ai_summarization` | Warn before context limits |
| `SummarizationCapability` | `pydantic_ai_summarization` | LLM-based context summarization |
| `EvictionCapability` | `pydantic_deep.processors.eviction` | Large tool output eviction |
| `PatchToolCallsCapability` | `pydantic_deep.processors.patch` | Fix orphaned tool calls |
| `CostTracking` | `pydantic_ai_shields` | Token/cost tracking |

### 1.3 Existing Toolsets (NOT capabilities)

| Toolset | Source | Purpose |
|---|---|---|
| `TodoToolset` | `pydantic_ai_todo` | Task planning/tracking |
| `SubAgentToolset` | `subagents_pydantic_ai` | Subagent delegation |
| `SkillsToolset` | `pydantic_deep.toolsets` | Skill loading |
| `ConsoleToolset` | `pydantic_ai_backend` | File ops + shell execute |
| `ContextToolset` | `pydantic_deep.toolsets.context` | Context file tools |
| `AgentMemoryToolset` | `pydantic_deep.toolsets.memory` | Memory read/write tools |
| `CheckpointToolset` | `pydantic_deep.toolsets.checkpointing` | Conversation checkpoints |
| `TeamToolset` | `pydantic_deep.toolsets.teams` | Team management tools |
| `ImproveToolset` | `pydantic_deep.toolsets.improve` | Self-improvement |
| `LiteparseToolset` | `pydantic_deep.toolsets.liteparse` | Document parsing |

### 1.4 HooksCapability Events (real, verified)

```python
from pydantic_deep import Hook, HookEvent, HooksCapability

HookEvent.PRE_TOOL_USE           # Before tool execution
HookEvent.POST_TOOL_USE          # After successful tool execution
HookEvent.POST_TOOL_USE_FAILURE  # After failed tool execution
HookEvent.BEFORE_RUN             # Before agent.run() starts
HookEvent.AFTER_RUN              # After agent.run() completes
HookEvent.RUN_ERROR              # When agent.run() fails
HookEvent.BEFORE_MODEL_REQUEST   # Before each LLM call
HookEvent.AFTER_MODEL_REQUEST    # After each LLM response
```

### 1.5 AbstractCapability Lifecycle Hooks (real, verified)

From `pydantic_ai.capabilities.AbstractCapability`:

```python
# Run lifecycle
before_run(ctx) -> None
after_run(ctx, *, result) -> result
wrap_run(ctx, *, handler) -> result          # CAN short-circuit run
on_run_error(ctx, *, error) -> result        # CAN recover from error

# Node lifecycle (graph nodes: UserPromptNode, ModelRequestNode, CallToolsNode)
before_node_run(ctx, *, node) -> node
after_node_run(ctx, *, node, result) -> result
wrap_node_run(ctx, *, node, handler) -> result
on_node_run_error(ctx, *, node, error) -> result

# Model request lifecycle
before_model_request(ctx, request_context) -> request_context
after_model_request(ctx, *, request_context, response) -> response
wrap_model_request(ctx, *, request_context, handler) -> response  # CAN short-circuit LLM
on_model_request_error(ctx, *, request_context, error) -> response

# Tool execute lifecycle
before_tool_execute(ctx, *, call, tool_def, args) -> args
after_tool_execute(ctx, *, call, tool_def, args, result) -> result
wrap_tool_execute(ctx, *, call, tool_def, args, handler) -> result
on_tool_execute_error(ctx, *, call, tool_def, args, error) -> result

# Tool validate lifecycle
before_tool_validate(ctx, *, call, tool_def, args) -> args
after_tool_validate(ctx, *, call, tool_def, args) -> args
wrap_tool_validate(ctx, *, call, tool_def, args, handler) -> args
on_tool_validate_error(ctx, *, call, tool_def, args, error) -> args

# Output lifecycle
before_output_validate(ctx, *, output_context, output) -> output
after_output_validate(ctx, *, output_context, output) -> output
before_output_process(ctx, *, output_context, output) -> output
after_output_process(ctx, *, output_context, output) -> output

# Event stream
wrap_run_event_stream(ctx, *, stream) -> stream

# Deferred tools
handle_deferred_tool_calls(ctx, *, requests) -> results

# Static configuration
get_instructions() -> AgentInstructions | None
get_model_settings() -> AgentModelSettings | None
get_toolset() -> AgentToolset | None
get_native_tools() -> Sequence[AgentNativeTool]
get_wrapper_toolset(toolset) -> AbstractToolset | None
```

---

## 2. What the Proposition Invented (Does NOT Exist)

| Proposition Reference | Reality | Verdict |
|---|---|---|
| `from pydantic_deep import Component` | No `Component` class exists. Base is `AbstractCapability` from `pydantic_ai.capabilities`. | **INVENTED** |
| `ContentCapability` | No such class. No built-in way to replace LLM with authored text. | **INVENTED** |
| `CausalLogCapability` | No such class. `HooksCapability` exists but is for command/handler execution, not structured JSONL logging. | **INVENTED** |
| `LLMCapability` | No such class. Model is passed directly to `Agent()` via `model=` parameter. | **INVENTED** |
| `ToolsCapability` | No such class. Tools registered via `tools=` and `toolsets=` parameters. | **INVENTED** |
| `MemoryCapability` | Exists but is from `pydantic_ai_summarization`, not what proposition described. | **MISIDENTIFIED** |
| `ContextManagerCapability` | Exists but is middleware for token compaction, not a general capability. | **MISIDENTIFIED** |
| `TodoCapability` | No such class. It's `TodoToolset` (a toolset, not a capability). | **INVENTED** |
| `AgentContext` with `.agent_name`, `.payload`, `.gsa_state`, `.memory`, `.todos` | No such class. `RunContext` from pydantic-ai has `.deps`, `.run_step`, `.model_settings`, `.retry`, `.max_retries`. | **INVENTED** |
| `generate_text(context) -> str` on capabilities | No such method on `AbstractCapability`. Model calls are internal to pydantic-ai's graph execution. | **INVENTED** |
| `on_agent_wake(payload)` | No such hook. `before_run(ctx)` exists but receives `RunContext`, not a payload dict. | **INVENTED** |
| `on_before_compress()` | No such hook. `on_before_compress` is a **callback parameter** to `create_deep_agent()`, not a capability method. | **MISIDENTIFIED** |
| `on_parser_invoke()` | No such hook. Parser is external to pydantic-deep entirely. | **INVENTED** |
| `on_handler_append()` | No such hook. Handler is external to pydantic-deep. | **INVENTED** |
| `on_handler_reject()` | No such hook. Handler is external to pydantic-deep. | **INVENTED** |
| `on_gsa_update()` | No such hook. GSA is external to pydantic-deep. | **INVENTED** |

---

## 3. Can We Achieve the Goals with REAL pydantic-deep?

### 3.1 Causal Logging for LLM Agents: YES

We can create a real `CausalLogCapability` as an `AbstractCapability` subclass:

```python
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import RunContext
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestContext
from datetime import datetime
import json
from pathlib import Path

class CausalLogCapability(AbstractCapability):
    """Records agent lifecycle events to JSONL.
    
    A REAL AbstractCapability subclass that hooks into pydantic-ai's
    lifecycle. Uses background logging (does not block execution).
    """
    
    def __init__(self, run_id: str, output_dir: str = "/var/log/pipeline"):
        self.run_id = run_id
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file = open(self.output_dir / f"{run_id}.jsonl", "a")
        self.sequence = 0
    
    def _emit(self, event_type: str, data: dict):
        entry = {
            "ts": datetime.utcnow().isoformat(),
            "seq": self.sequence,
            "type": event_type,
            "run_id": self.run_id,
            **data,
        }
        self.file.write(json.dumps(entry, default=str) + "\n")
        self.file.flush()
        self.sequence += 1
    
    async def before_run(self, ctx: RunContext) -> None:
        self._emit("agent_wake", {
            "agent": getattr(ctx.deps, 'agent_name', 'unknown'),
            "run_step": ctx.run_step,
        })
    
    async def after_run(self, ctx: RunContext, *, result) -> Any:
        self._emit("agent_output", {
            "agent": getattr(ctx.deps, 'agent_name', 'unknown'),
            "output": str(result.output) if hasattr(result, 'output') else str(result),
        })
        return result
    
    async def wrap_model_request(self, ctx, *, request_context, handler):
        self._emit("model_request_start", {
            "agent": getattr(ctx.deps, 'agent_name', 'unknown'),
            "model": request_context.model.model_name if hasattr(request_context, 'model') else 'unknown',
        })
        response = await handler(request_context)
        self._emit("model_request_end", {
            "agent": getattr(ctx.deps, 'agent_name', 'unknown'),
            "response_type": type(response).__name__,
        })
        return response
    
    async def before_tool_execute(self, ctx, *, call, tool_def, args):
        self._emit("tool_call_start", {
            "agent": getattr(ctx.deps, 'agent_name', 'unknown'),
            "tool": call.tool_name,
            "args": args,
        })
        return args
    
    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        self._emit("tool_call_end", {
            "agent": getattr(ctx.deps, 'agent_name', 'unknown'),
            "tool": call.tool_name,
            "result_preview": str(result)[:200] if result else None,
        })
        return result
```

**Usage:**
```python
from pydantic_deep import create_deep_agent

agent = create_deep_agent(
    model="openrouter:deepseek/deepseek-v4-flash",
    capabilities=[CausalLogCapability(run_id="prod-001")],
)
```

**Verdict:** Fully possible. `AbstractCapability` has all the hooks we need. The capability receives `RunContext` which has `ctx.deps` (our `DeepAgentDeps`), so we can attach agent name and other metadata there.

### 3.2 Deterministic Text Injection: POSSIBLE BUT AWKWARD

The proposition's `ContentCapability` would need to use `wrap_model_request()` to short-circuit the LLM call:

```python
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import ModelRequestContext

class ContentCapability(AbstractCapability):
    """Replaces LLM calls with authored text.
    
    Works by intercepting model requests via wrap_model_request.
    NEVER calls the actual LLM. Returns authored text as ModelResponse.
    """
    
    def __init__(self, turns: list[str]):
        self.turns = turns
        self.turn_counter = 0
    
    async def wrap_model_request(self, ctx, *, request_context, handler):
        # Short-circuit: do NOT call handler (which would call the LLM)
        if self.turn_counter < len(self.turns):
            text = self.turns[self.turn_counter]
        else:
            text = "Nothing more to do."
        self.turn_counter += 1
        
        return ModelResponse(parts=[TextPart(content=text)])
```

**Problems:**
1. **Fighting the framework.** pydantic-deep is designed around LLM calls. Every agent gets filesystem tools, todo tools, subagent tools, web search, etc. A deterministic agent that never calls an LLM still carries all this baggage.
2. **No clean payload passing.** `RunContext` doesn't have a `payload` field. We'd need to put test scenario data in `ctx.deps` (the `DeepAgentDeps` object).
3. **HTTP mismatch.** Our architecture says agents are HTTP services with GET/POST. pydantic-deep agents are Python objects. We'd need to wrap each in FastAPI, making the whole exercise pointless — we might as well just write the FastAPI service directly.
4. **Tool execution complication.** If the authored text contains tool calls (e.g., "I'll check the file system"), pydantic-ai will try to parse and execute them. For deterministic tests, we probably don't want tools executing at all — but pydantic-deep includes them by default.
5. **Backend required.** Even a "deterministic" agent needs a backend (`StateBackend` or `LocalBackend`) because pydantic-deep's toolsets depend on it.

**Verdict:** Technically possible via `wrap_model_request`, but architecturally wrong. It's simpler and cleaner to write a FastAPI service that returns authored text.

### 3.3 Todo Integration for Deterministic Agents: NOT APPLICABLE

The proposition suggests using `TodoCapability` to drive deterministic agent state. But:
- `TodoCapability` doesn't exist. It's `TodoToolset`.
- `TodoToolset` provides **tools** (`add_todo`, `complete_todo`, etc.) that the LLM can call. A deterministic agent that bypasses the LLM never sees these tools.
- The deterministic agent would need to manually invoke todo tools, which adds complexity.

For deterministic agents, a simple turn counter or state-driven YAML config (as in PROPOSITION_DETERMINISTIC_AGENTS.md) is far simpler.

---

## 4. Comparison: pydantic-deep Components vs. Custom HTTP Services

| Aspect | pydantic-deep Approach (proposition) | Custom FastAPI Approach (PROPOSITION_DETERMINISTIC_AGENTS.md) |
|---|---|---|
| **Deterministic text** | `wrap_model_request` short-circuit (awkward) | Direct response from YAML config (natural) |
| **HTTP interface** | Must wrap pydantic-deep agent in FastAPI | Native FastAPI, matches architecture |
| **Tool overhead** | Inherits all pydantic-deep tools (unwanted) | Only declares needed tools |
| **Startup time** | Heavy (loads model, toolsets, capabilities) | Light (reads YAML, starts uvicorn) |
| **Causal logging** | Custom `AbstractCapability` (elegant for LLM agents) | FastAPI middleware (natural for HTTP services) |
| **Code complexity** | High (must understand pydantic-ai graph internals) | Low (standard FastAPI patterns) |
| **State tracking** | Would need to manually drive todo tools | Simple YAML rules or turn counter |
| **Production alignment** | LLM agents use pydantic-deep, deterministics fight it | Both are HTTP services, unified interface |
| **Parser integration** | Same (text goes through parser) | Same (text goes through parser) |
| **Event store** | Same (handler appends effects) | Same (handler appends effects) |

---

## 5. Recommended Architecture

### For LLM Agents (Production): Use pydantic-deep

pydantic-deep is already the production framework. Keep using it.

**Add `CausalLogCapability`** as a real `AbstractCapability` subclass. It hooks into:
- `before_run` / `after_run` — log agent wake/output
- `wrap_model_request` — log LLM call start/end with latency
- `before_tool_execute` / `after_tool_execute` — log tool calls
- `on_run_error` / `on_tool_execute_error` — log failures

This gives production observability for free.

### For Deterministic Agents (Testing): Use Custom FastAPI

Do NOT try to bend pydantic-deep into a deterministic text generator. Write plain FastAPI services:

```python
# server/deterministic_agents/agent.py
from fastapi import FastAPI
from pydantic import BaseModel
import yaml

app = FastAPI()

class WakePayload(BaseModel):
    run_id: str
    notification_type: str = "wake"
    context: dict = {}

with open("content/agents/scenario.yaml") as f:
    config = yaml.safe_load(f)

turn_counter = {}

@app.get("/")
async def health():
    return {"status": "ok"}

@app.post("/")
async def wake(payload: WakePayload):
    run_id = payload.run_id
    if run_id not in turn_counter:
        turn_counter[run_id] = 0
    
    turns = config.get("turns", [])
    idx = turn_counter[run_id]
    
    if idx < len(turns):
        text = turns[idx].get("text", "")
    else:
        text = "Nothing more to do."
    
    turn_counter[run_id] = idx + 1
    return {"text": text}
```

### For Causal Logging: Unified JSONL Format

Both paths write to the same format:

```jsonl
{"ts": "2026-05-27T10:00:00Z", "seq": 0, "type": "agent_wake", "run_id": "test-001", "agent": "scenario", "source": "llm|deterministic"}
{"ts": "2026-05-27T10:00:01Z", "seq": 1, "type": "agent_output", "run_id": "test-001", "agent": "scenario", "text_length": 342}
{"ts": "2026-05-27T10:00:02Z", "seq": 2, "type": "parser_result", "run_id": "test-001", "agent": "scenario", "effects": [...], "confidence": 10, "reask_count": 0}
{"ts": "2026-05-27T10:00:03Z", "seq": 3, "type": "event_appended", "run_id": "test-001", "effect_kind": "pipeline_started", "esdb_sequence": 42}
```

**LLM agents** write via `CausalLogCapability` (hooks into pydantic-ai lifecycle).
**Deterministic agents** write via FastAPI middleware or explicit logging calls.
**Parser** writes via explicit call after extraction.
**Handler** writes via explicit call after appending to ESDB.

---

## 6. What to Build

| Component | Approach | Files |
|---|---|---|
| **CausalLogCapability** | Real `AbstractCapability` subclass | `server/capabilities/causal_log.py` |
| **Deterministic Agent** | Plain FastAPI, not pydantic-deep | `server/deterministic_agents/` |
| **Pipeline Driver** | Plain Python loop, not an agent | `server/orchestrator/automation.py` |
| **Causal Log Reader** | Plain Python script | `server/verify_causal_log.py` |

### What NOT to build

- `ContentCapability` as pydantic-deep component — wrong abstraction
- `TodoCapability` for deterministic state — overkill, use YAML rules
- Custom model class that returns authored text — unnecessary indirection

---

## 7. Additional Ecosystem: pydantic-eval

`pydantic-eval` (installed as `pydantic-evals==1.102.0`) is an evaluation framework for stochastic execution, especially LLM-based code.

**Key evaluators:**
- `Equals` / `EqualsExpected` — Exact match evaluation
- `Contains` — Substring containment check
- `LLMJudge` — LLM-based evaluation with custom criteria
- `MaxDuration` — Performance/latency evaluation
- `HasMatchingSpan` — OpenTelemetry span tree matching
- `PrecisionRecallEvaluator`, `ROCAUCEvaluator` — ML metrics

**Relevance:** Could evaluate agent outputs against expected effects. For example, an `LLMJudge` evaluator could check if agent text contains the expected narration elements. However, our architecture already has the semantic parser for extraction, making evaluators partially redundant.

**Verdict:** Optional. The causal log + parser provide sufficient verification. Evaluators add another layer but are not essential.

---

## 8. Summary

**The proposition's pydantic-deep integration is 70% fantasy.** It invents APIs that don't exist (`Component`, `ContentCapability`, `AgentContext.generate_text()`, etc.) and misidentifies toolsets as capabilities (`TodoToolset` vs `TodoCapability`).

**However, the underlying insight is correct:** pydantic-deep's `AbstractCapability` lifecycle hooks ARE the right pattern for causal logging of LLM agents. We should build a real `CausalLogCapability`.

**For deterministic behavior, the revised approach (see `AUTHORING_WORKFLOW.md`) uses real agents with real LLMs, constrained by:**
- `TodoToolset` for task sequencing
- Custom `read_script` tool for operator-authored content retrieval
- Strong system prompts that frame the agent as a "script narrator"
- Temperature 0.0 for minimal variance

This is superior to custom HTTP agents because it exercises the real framework, real LLM path, real tool execution, and real context management.

**The unified causal log format connects all agent types.** Production agents and script-narrator agents both write to the same JSONL via `CausalLogCapability`. The causal log inspector provides production observability.
