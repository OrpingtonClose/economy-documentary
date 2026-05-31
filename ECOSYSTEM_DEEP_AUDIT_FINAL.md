> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# pydantic Ecosystem Deep Audit — FINAL

Date: 2026-05-27
Scope: All installed pydantic ecosystem packages + external provenance package + EventStoreDB macOS viability

---

## 1. AbstractCapability Patterns: What Exists vs. What We Build

### 1.1 Verified Real `AbstractCapability` Subclasses (installed in venv)

| Package | Class | Hooks Used | What It Does |
|---|---|---|---|
| `pydantic_ai_todo` | `TodoCapability` | `get_toolset()`, `get_instructions()` | Provides todo tools + dynamic system prompt injection showing current todos |
| `pydantic_ai_shields` | `CostTracking` | `before_run`, `after_run` | Budget check + token/cost tracking via genai-prices |
| `pydantic_ai_shields` | `ToolGuard` | `prepare_tools`, `before_tool_execute` | Hide blocked tools from model + require approval |
| `pydantic_ai_shields` | `InputGuard` | `before_run` | Custom guardrail function on user input |
| `pydantic_ai_shields` | `OutputGuard` | `after_run` | Custom guardrail function on model output |
| `pydantic_ai_shields` | `AsyncGuardrail` | `wrap_run` | Concurrent/blocking/monitoring guardrail modes |
| `pydantic_ai_shields` | `PromptInjection` | `before_run` | Regex-based prompt injection detection |
| `pydantic_ai_shields` | `PiiDetector` | `before_run` | Regex-based PII detection (email, SSN, etc.) |
| `pydantic_ai_shields` | `SecretRedaction` | `after_run` | Regex-based secret detection in output |
| `pydantic_ai_shields` | `BlockedKeywords` | `before_run` | Keyword blocking with regex support |
| `pydantic_ai_shields` | `NoRefusals` | `after_run` | Detect and block model refusal phrases |
| `pydantic_ai_summarization` | `ContextManagerCapability` | `before_model_request`, `after_tool_execute`, `get_toolset()`, `for_run()` | Token tracking, auto-compression, tool truncation, compact tool |
| `pydantic_ai_summarization` | `SummarizationCapability` | `before_model_request` | LLM-based history compression |
| `pydantic_ai_summarization` | `SlidingWindowCapability` | `before_model_request` | Zero-cost message trimming |
| `pydantic_ai_summarization` | `LimitWarnerCapability` | `before_model_request` | Inject warning before limits |
| `pydantic_deep` | `HooksCapability` | `before_tool_execute`, `after_tool_execute`, `on_tool_execute_error`, `before_run`, `after_run`, `on_run_error`, `before_model_request`, `after_model_request` | Claude Code-style hooks with command/handler execution |
| `pydantic_deep` | `StuckLoopDetection` | `after_tool_execute` | Detect repetitive patterns, raise ModelRetry |
| `pydantic_deep` | `PlanCapability` | (tools) | Interactive planning (ask_user, save_plan) |
| `pydantic_deep` | `MemoryCapability` | (tools) | Persistent memory across sessions |
| `pydantic_deep` | `BrowserCapability` | (tools) | Web browsing |
| `pydantic_deep` | `TeamCapability` | (tools) | Agent team management |
| `pydantic_deep` | `PeriodicReminderCapability` | `before_model_request` | Task reminders every N turns |
| `pydantic_deep` | `EvictionCapability` | `after_tool_execute` | Large tool output eviction |
| `pydantic_deep` | `PatchToolCallsCapability` | `after_tool_execute` | Fix orphaned tool calls |
| `pydantic_deep` | `SkillsCapability` | (tools) | Skill directory loading |
| `pydantic_deep` | `ContextFilesCapability` | (tools) | Context file injection |

### 1.2 External `AbstractCapability` (not installed, available)

| Package | Class | Hooks Used | What It Does |
|---|---|---|---|
| `pydantic-ai-provenance` | `ProvenanceCapability` | `before_run`, `after_run`, `before_tool_execute`, `after_tool_execute`, `before_model_request`, `after_model_request` | Full execution DAG, citation keys, multi-agent attribution, verification |

### 1.3 pydantic-ai `Hooks` (decorator-based, no subclassing)

```python
from pydantic_ai import Agent
from pydantic_ai.capabilities import Hooks

hooks = Hooks()

@hooks.on.before_model_request
async def log_request(ctx, request_context):
    print(f"[{ctx.agent.name}] {len(request_context.messages)} messages")
    return request_context

@hooks.on.after_tool_execute
async def log_tool(ctx, *, call, tool_def, args, result):
    print(f"Tool {call.tool_name}: {str(result)[:100]}")
    return result

agent = Agent("openrouter:deepseek/deepseek-v4-flash", capabilities=[hooks])
```

**Decorator events available:** `before_run`, `after_run`, `run` (wrap_run), `run_error`, `before_node_run`, `after_node_run`, `node_run` (wrap_node_run), `node_run_error`, `before_model_request`, `after_model_request`, `model_request` (wrap_model_request), `model_request_error`, `before_tool_validate`, `after_tool_validate`, `tool_validate` (wrap_tool_validate), `tool_validate_error`, `before_tool_execute`, `after_tool_execute`, `tool_execute` (wrap_tool_execute), `tool_execute_error`, `before_output_validate`, `after_output_validate`, `output_validate` (wrap_output_validate), `output_validate_error`, `before_output_process`, `after_output_process`, `output_process` (wrap_output_process), `output_process_error`, `prepare_tools`, `prepare_output_tools`, `deferred_tool_calls`, `run_event_stream`, `event`.

**Tool filtering:** `@hooks.on.before_tool_execute(tools=["execute"])` — only fires for matching tools.

**Timeouts:** `@hooks.on.before_tool_execute(timeout=5.0)` — raises `HookTimeoutError` if exceeded.

---

## 2. What We Should Use vs. Build

### 2.1 Use Off-the-Shelf (Zero Custom Code)

| Need | Package | Class | Why |
|---|---|---|---|
| **Todo tracking** | `pydantic_ai_todo` | `TodoCapability` | Already an `AbstractCapability`. Provides tools + dynamic instructions. |
| **Token management** | `pydantic_ai_summarization` | `ContextManagerCapability` | Auto-compression, tool truncation, token tracking. Battle-tested. |
| **Budget enforcement** | `pydantic_ai_shields` | `CostTracking` | Uses genai-prices. Budget check before run. Tracks cumulative cost. |
| **Tool blocking** | `pydantic_ai_shields` | `ToolGuard` | `prepare_tools` hides blocked tools from model. `before_tool_execute` enforces approval. |
| **Prompt injection guard** | `pydantic_ai_shields` | `PromptInjection` | 6 categories of injection patterns. Configurable sensitivity. |
| **Execution DAG / causal log** | `pydantic-ai-provenance` | `ProvenanceCapability` | Full DAG, citation keys, verification, visualization. |
| **Simple debug logging** | `pydantic-ai` core | `Hooks` decorator | One-liner decorators. No subclassing. |

### 2.2 Build Custom (Subclass AbstractCapability)

| Need | Why Custom | Hooks |
|---|---|---|
| **OTIO-aware context compaction** | Needs to query OTIO projection to determine focus | `before_model_request` or `wrap_model_request` |
| **Per-run_id todo persistence** | Todo state must persist across HTTP wake cycles | `for_run()` to initialize per-run, custom save after `agent.run()` |
| **Script reader tool** | Reads operator-authored YAML | Not a capability — a `Tool` |
| **Pipeline orchestrator logic** | Prompt-based rules for waking agents | Not a capability — agent system prompt + tools |

### 2.3 Build Custom (Subclass AbstractCapability) — ONLY IF...

Only subclass `AbstractCapability` when you need **middleware semantics** (wrap_*), **tool provision** (get_toolset), or **dynamic instructions** (get_instructions). For everything else, use decorator-based `Hooks` or existing capabilities.

---

## 3. EventStoreDB / KurrentDB: macOS Reality

### 3.1 Verdict: NO NATIVE macOS BINARY

Checked GitHub releases for EventStoreDB/KurrentDB (v24.10.14, v26.0.3, v26.1.0):

| Asset | Available? |
|---|---|
| Linux x64 tar.gz | Yes |
| Linux arm64 tar.gz | Yes |
| Windows x64 tar.gz | Yes |
| Debian/Ubuntu .deb | Yes |
| RPM packages | Yes |
| NuGet package | Yes |
| **macOS / Darwin** | **NO** |

No `.dmg`, no `.pkg`, no `darwin-x64`, no `osx` tarballs in any recent release.

### 3.2 Installation Options

| Method | Viability | Notes |
|---|---|---|
| **Homebrew** | `brew search eventstore` → no formula | Not available |
| **Official downloads** | eventstore.com → 301 to kurrent.io → 522 error | Website down |
| **Docker** | `docker run eventstore/eventstore` | Works. User wants to avoid. |
| **Build from source** | .NET project, complex | Hours of setup. Not practical. |
| **JSONL file** | Already implemented in `server/event_store.py` | **Recommended.** |

### 3.3 Python Client

| Package | Status | Notes |
|---|---|---|
| `esdbclient` | Listed in `pip list` (v1.1.7) but `import esdbclient` fails | May be in wrong Python env or broken install |
| `eventstoredb` | v0.9.0 on PyPI | Alternative client |

Even with a working client, there's no server to connect to on macOS without Docker.

### 3.4 Recommendation

**Keep JSONL file event store.** It satisfies all architectural requirements:
- Append-only ✓
- Immutable ✓
- Replayable ✓
- Single source of truth ✓
- No Docker ✓
- No external dependencies ✓

Update architecture doc §5 to state JSONL is the current implementation, with EventStoreDB as a future distributed deployment option.

---

## 4. Architecture Doc §8: Specific Fixes Needed

### 4.1 `ProvenanceCapability` (line 3741)

**Current:** `ProvenanceCapability(agent_name=role)` — references undefined class.

**Fix:** Add dependency + import:

```toml
# server/pyproject.toml
[project.dependencies]
"pydantic-ai-provenance" = ">=0.1.0"
```

```python
from pydantic_ai_provenance.capability import ProvenanceCapability

provenance = ProvenanceCapability(
    agent_name=role,
    source_tools=["read_script", "query_gsa"],
)
```

### 4.2 `HooksCapability` misattributed (lines 3660–3677)

**Current:** Describes `HooksCapability` as carrying `on_before_compress=otio_aware_compress`.

**Fix:** `on_before_compress` is a **direct parameter** to `create_deep_agent()`, not part of any capability:

```python
agent = create_deep_agent(
    model="...",
    on_before_compress=otio_aware_compress,  # Direct param
    capabilities=[
        ProvenanceCapability(agent_name=role),
        # NOT in HooksCapability
    ],
)
```

The architecture doc's layer stack diagram (§8.1) correctly shows `HooksCapability` as "OUR HOOK" but incorrectly describes the mechanism.

### 4.3 `PipelineDeps` undefined (line 3751, 3779)

**Current:** `deps_type=PipelineDeps` and `deps=PipelineDeps(...)` used without definition.

**Fix:** Add definition in §8.2 or §8.3:

```python
from pydantic_deep.deps import DeepAgentDeps
from dataclasses import dataclass, field

@dataclass
class PipelineDeps(DeepAgentDeps):
    projections: dict[str, Any] = field(default_factory=dict)
    agent_role: str = ""
    max_tokens: int = 128_000
    compaction_model: str = "openrouter:deepseek/deepseek-v4-flash"
```

### 4.4 `include_todo=False` (line 3728)

**Current:** Disables pydantic-deep's built-in todo toolset.

**Implication:** If todo tracking is needed, add `pydantic_ai_todo.TodoCapability` via `capabilities=`:

```python
from pydantic_ai_todo import TodoCapability

agent = create_deep_agent(
    # ...
    include_todo=False,  # Disable pydantic-deep default
    capabilities=[
        TodoCapability(),  # Add pydantic-ai-todo capability
    ],
)
```

The architecture doc should clarify this distinction.

---

## 5. Minimal Capabilities Stack for Pipeline Agents

```python
from pydantic_ai import Agent
from pydantic_ai.capabilities import Hooks
from pydantic_ai_todo import TodoCapability
from pydantic_ai_summarization import ContextManagerCapability
from pydantic_ai_shields import CostTracking, ToolGuard
from pydantic_ai_provenance import ProvenanceCapability  # external

# Simple logging via decorator
hooks = Hooks()
@hooks.on.after_tool_execute
async def log_tool(ctx, *, call, tool_def, args, result):
    # Log to pipeline tracing system
    return result

agent = Agent(
    "openrouter:deepseek/deepseek-v4-flash",
    capabilities=[
        hooks,                                    # Debug logging
        TodoCapability(),                         # Scene/task tracking
        ContextManagerCapability(max_tokens=128_000),  # Token management
        CostTracking(budget_usd=10.0),            # Budget enforcement
        ToolGuard(blocked=["execute"]),           # Safety
        ProvenanceCapability(agent_name="scenario", source_tools=["read_script"]),
    ],
)
```

---

## 6. Summary

| Finding | Impact |
|---|---|
| `TodoCapability` IS real (in `pydantic_ai_todo`) | Architecture doc should use it, not disable todos |
| `ProvenanceCapability` IS real (external package) | Add to dependencies, use instead of custom causal log |
| `Hooks` decorator (pydantic-ai core) is simplest for logging | Use for debug/observability; only subclass `AbstractCapability` for middleware |
| EventStoreDB has **no macOS binary** | JSONL file store is correct. Update §5. |
| `esdbclient` exists on PyPI but not importable in venv | If ESDB is needed later, fix the install |
| §8 has 4 specific issues | Provenance import, Hooks misattribution, PipelineDeps undefined, todo clarification |

Core architecture (§1–§7, §9) is **final and tight**. §8 needs the 4 fixes above. §5 needs JSONL clarification.
