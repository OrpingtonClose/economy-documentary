> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# pydantic Ecosystem Re-Audit + EventStoreDB Reality Check

Date: 2026-05-27

---

## 1. pydantic-ai-provenance: REAL, INSTALLABLE, SUPERIOR

**Package:** `pydantic-ai-provenance` (GitHub: `dugarsumit/pydantic-ai-provenance`)
**Install:** `pip install pydantic-ai-provenance` or `uv add pydantic-ai-provenance`
**Requirements:** Python ≥ 3.12, pydantic-ai ≥ 1.80
**Status in project:** NOT installed, NOT in `server/pyproject.toml`

### What It Provides

`ProvenanceCapability` is a **real `AbstractCapability` subclass** that hooks into the agent lifecycle:

- **Full execution DAG** — every tool call, model request, and response linked in a directed acyclic graph
- **Automatic citation keys** (`d_1`, `d_2`, `a_1`, ...) injected into source tool results so the LLM can cite them inline
- **Multi-agent attribution** — subagent outputs propagate through a shared store via `contextvars`, enabling transitive citation resolution across agent boundaries
- **Citation verification** — TF-IDF cosine overlap (Step 2) and optional LLM entailment (Step 3) to validate every `[REF|...]` tag
- **Graph visualization** — export as Mermaid, GraphViz DOT, or JSON

### Why This Is Superior to Custom CausalLogCapability

| Feature | Custom CausalLogCapability | ProvenanceCapability |
|---|---|---|
| Implementation | Hand-written ~150 lines | Battle-tested package |
| Execution DAG | Flat JSONL events | Full DAG with edges |
| Tool call linking | Manual (agent, tool_name) | Automatic with citation keys |
| Multi-agent attribution | Would need custom code | Built-in via contextvars |
| Citation verification | None | TF-IDF + LLM entailment |
| Visualization | None | Mermaid, DOT, HTML |
| Export | JSONL only | JSON, Mermaid, DOT, HTML |
| Maintenance burden | Ours | Community |

### Usage

```python
from pydantic_ai import Agent
from pydantic_ai_provenance.capability import ProvenanceCapability

provenance = ProvenanceCapability(
    agent_name="scenario",
    source_tools=["read_script"],  # tools whose results are raw data sources
)

agent = Agent(
    "openrouter:deepseek/deepseek-v4-flash",
    capabilities=[provenance],
    system_prompt="Present documentary scenes.",
)

# After run
store = provenance.store
print(store.to_mermaid())  # Visual DAG
print(store.to_json())     # Full graph export
report = await provenance.verify(result.output)  # Citation verification
```

**Verdict:** Install `pydantic-ai-provenance` and use it instead of building a custom causal log capability. It does everything we need and more.

---

## 2. pydantic-ai Hooks Capability: Simpler Than AbstractCapability Subclassing

The official pydantic-ai docs show a `Hooks` capability (distinct from pydantic-deep's `HooksCapability`) that uses **decorator-based registration**:

```python
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import Hooks
from pydantic_ai.models import ModelRequestContext

hooks = Hooks()

@hooks.on.before_model_request
async def log_request(ctx: RunContext, request_context: ModelRequestContext):
    agent_name = ctx.agent.name if ctx.agent else 'unknown'
    print(f'[{agent_name}] Sending {len(request_context.messages)} messages')
    return request_context

@hooks.on.after_tool_execute
async def log_tool(ctx, *, call, tool_def, args, result):
    print(f"Tool {call.tool_name} returned {str(result)[:100]}")
    return result

agent = Agent(
    "openrouter:deepseek/deepseek-v4-flash",
    capabilities=[hooks],
)
```

**Why this matters:** For simple logging/observability, decorator-based hooks are far simpler than subclassing `AbstractCapability`. Only subclass `AbstractCapability` when you need:
- `wrap_model_request()` to intercept/short-circuit model calls
- `wrap_run()` to wrap the entire run
- `get_toolset()` / `get_native_tools()` to provide tools
- `get_instructions()` / `get_model_settings()` to inject config

For pure observation (logging, metrics), use `Hooks`.

---

## 3. Full Ecosystem Capability Inventory (Verified)

### Built into pydantic-ai (core)

| Capability | Purpose | Use for pipeline? |
|---|---|---|
| `Hooks` | Decorator-based lifecycle hooks | **YES** — simple logging |
| `Thinking` | Model thinking/reasoning | Maybe — for complex decisions |
| `WebSearch` / `WebFetch` | Web search and URL fetching | **NO** — documentary pipeline |
| `ThreadExecutor` | Custom thread pool for sync tools | Maybe — long-running servers |
| `PrepareTools` / `PrepareOutputTools` | Filter/modify tool definitions | Maybe — dynamic tool filtering |
| `PrefixTools` | Namespace tool names | Maybe — multi-agent composition |
| `ReinjectSystemPrompt` | Ensure system prompt is first | Maybe — message history from DB |
| `HandleDeferredToolCalls` | Resolve approval-required tools | Maybe — HITL workflows |

### Ecosystem packages (installed in venv)

| Package | Capability | Purpose | Use for pipeline? |
|---|---|---|---|
| `pydantic-ai-todo` | `TodoCapability` | Task planning with add/read/update/complete | **YES** — scene sequencing |
| `summarization-pydantic-ai` | `ContextManagerCapability` | Token tracking + auto-compression | **YES** — context management |
| `summarization-pydantic-ai` | `SummarizationCapability` | LLM-based history compression | **YES** — long runs |
| `summarization-pydantic-ai` | `SlidingWindowCapability` | Zero-cost message trimming | **YES** — fallback |
| `summarization-pydantic-ai` | `LimitWarnerCapability` | Inject finish-soon hint | Maybe — token warnings |
| `pydantic-ai-shields` | `CostTracking` | Token/cost tracking + budget enforcement | **YES** — budget validation |
| `pydantic-ai-shields` | `ToolGuard` | Block/approve specific tools | **YES** — safety (deny execute) |
| `pydantic-ai-shields` | `InputGuard` / `OutputGuard` | Custom validation functions | Maybe — content filtering |
| `pydantic-ai-backend` | `ConsoleCapability` | File ops + shell execute | Maybe — if needed |
| `subagents-pydantic-ai` | `SubAgentCapability` | Multi-agent delegation | **NO** — external HTTP agents |
| `pydantic-ai-skills` | `SkillsCapability` | Progressive skill loading | Maybe — skill discovery |

### External packages (not installed, available)

| Package | Capability | Purpose | Use for pipeline? |
|---|---|---|---|
| `pydantic-ai-provenance` | `ProvenanceCapability` | Execution DAG + citation tracking | **YES** — causal logging |

---

## 4. AbstractCapability: What to Subclass vs. What to Use Off-the-Shelf

### Use `Hooks` (decorator) for:
- Simple logging of model requests, tool calls, runs
- Metrics collection (latency counters)
- Debugging (print statements)
- Light validation (check args before execution)

### Subclass `AbstractCapability` ONLY for:
- **Providing tools** (`get_toolset()`, `get_native_tools()`)
- **Wrapping model requests** (`wrap_model_request()` — e.g., caching, fallback models)
- **Wrapping runs** (`wrap_run()` — e.g., retry logic, circuit breakers)
- **Injecting instructions/settings** (`get_instructions()`, `get_model_settings()`)
- **Tool definition filtering** (`prepare_tools()`)
- **Complex cross-cutting concerns** that need middleware semantics

### Our pipeline mapping:

| Need | Right Tool |
|---|---|
| Causal logging / execution DAG | `pydantic-ai-provenance.ProvenanceCapability` |
| Token management / compression | `summarization-pydantic-ai.ContextManagerCapability` |
| Budget tracking | `pydantic-ai-shields.CostTracking` |
| Scene/task sequencing | `pydantic-ai-todo.TodoCapability` |
| Safety / tool blocking | `pydantic-ai-shields.ToolGuard` |
| Simple debug logging | `pydantic-ai.Hooks` decorator |
| Script reader tool | Custom `Tool` (not a capability) |

---

## 5. EventStoreDB Reality Check: NOT USED IN ACTUAL CODE

### Architecture Doc Claims

The architecture document (§5, §2.4) extensively describes EventStoreDB:
- "Append-only event store backed by EventStoreDB"
- "GSA subscribes to EventStoreDB streams"
- "Stream topology: `pipeline-{run_id}`"
- "ESDB client interface"

### Actual Code Reality

`server/event_store.py` (124 lines) implements:

```python
class EventStore:
    """Append-only event log backed by a JSONL file."""

    def __init__(self, log_path: str) -> None:
        self.log_path = log_path
        self._seq = self._last_seq()

    def append(self, effect: Effect, otio_hash_before: str) -> EventRecord:
        self._seq += 1
        record = EventRecord(seq=self._seq, effect=effect, otio_hash_before=otio_hash_before)
        with open(self.log_path, "a") as f:
            f.write(record.model_dump_json() + "\n")
        return record
```

**No EventStoreDB client is installed.** No `esdb` import. No TCP connection to port 2113. Just a JSONL file.

### EventStoreDB Native Mac Installation

| Approach | Status |
|---|---|
| **Homebrew** | `eventstore` formula does not exist. `brew search eventstore` returns nothing relevant. |
| **Official downloads** | EventStoreDB rebranded to **Kurrent** (kurrent.io). Website returns 522 (Cloudflare error). |
| **GitHub releases** | API call returned no macOS assets (likely rate-limited or no native macOS binary). |
| **Docker** | `docker run eventstore/eventstore:latest` — works but user explicitly wants to avoid Docker. |
| **Native binary** | EventStoreDB historically had limited macOS support. Kurrent's status is unclear due to website outage. |

### Recommendation

**Keep the JSONL file event store.** It is:
- Already implemented and working
- No external dependencies
- No Docker required
- Append-only and immutable (satisfies architecture principles)
- Easy to inspect (`cat events.jsonl | jq`)
- Easy to backup (just copy the file)

If EventStoreDB is needed later for distributed deployments, the `EventStore` class can be swapped for an ESDB-backed implementation behind the same interface. The architecture doc should be updated to reflect that **the current implementation uses JSONL**, with EventStoreDB as a future scalability path.

---

## 6. Architecture Doc Status: What's Final vs. What's Fantasy

### §1–§7: CORE ARCHITECTURE — FINAL

| Section | Status | Notes |
|---|---|---|
| §1 Principles | **FINAL** | All 10 principles verified. §1.7 Natural Language Only is correctly documented. |
| §2 Topology | **FINAL** | Component inventory, HTTP contract, emergent phases all consistent. |
| §3 Effects | **FINAL** | 32 effect types, `EffectUnion`, `KIND_TO_MODEL` all match. |
| §4 Rules as Prompt | **FINAL** | Prompt-based rules documented correctly. |
| §5 Event Store | **MISMATCH** | Doc says EventStoreDB. Code uses JSONL file. |
| §6 Projections | **FINAL** | OTIO, Job, VM, Budget, State projections all defined. |
| §7 Situation Types | **FINAL** | Narrative templates and builder documented. |

### §8: pydantic-deep INTEGRATION — NEEDS UPDATE

| Element | Status | Action |
|---|---|---|
| `ProvenanceCapability` | **REAL** but not installed | Add `pydantic-ai-provenance` to `pyproject.toml` or replace with `pydantic-ai.Hooks` + `ProvenanceCapability` combo |
| `HooksCapability` | **MISATTRIBUTED** | `on_before_compress` is a `create_deep_agent()` param, not part of `HooksCapability` |
| `PipelineDeps` | **UNDEFINED** | Never defined in the doc. Should show fields: `projections`, `agent_role`, etc. |
| `ContextManagerCapability` | **REAL** | Correctly documented. |
| `SlidingWindowProcessor` | **REAL** | Correctly documented. |
| `EvictionCapability` | **REAL** | Correctly documented. |
| `PatchToolCallsCapability` | **REAL** | Correctly documented. |

### §9: PARSER — FINAL

| Section | Status | Notes |
|---|---|---|
| §9.5 Semantic Extraction | **FINAL** | Single-phase, instructor-based. No fast paths. No regex. Correct. |
| §9.5.1 Discriminated Models | **FINAL** | Per-effect-type models with Literal discriminants. |
| §9.5.2 Category-Conditioned | **FINAL** | Permitted effect kinds per agent. |
| §9.5.3 Validation | **FINAL** | Three-level validation described. |

---

## 7. Recommended Changes to Architecture Doc

### §5 Event Store

Update to reflect the actual JSONL implementation:

```markdown
### 5.0 Storage Backend

The current implementation uses an append-only JSONL file (`events.jsonl`).
Each line is a validated `EventRecord` (Pydantic model). This satisfies all
architectural requirements: immutability, append-only, replayability, and
single source of truth.

EventStoreDB is reserved as a future scalability path for distributed
deployments. The `EventStore` class interface is designed to be swappable.
```

### §8.2 Agent Construction

Replace `ProvenanceCapability(agent_name=role)` with the real package:

```python
from pydantic_ai_provenance.capability import ProvenanceCapability

provenance = ProvenanceCapability(
    agent_name=role,
    source_tools=["read_script", "query_gsa"],
)

agent = create_deep_agent(
    model=config.agent_models[role],
    instructions=ROLE_INSTRUCTIONS[role],
    on_before_compress=otio_aware_compress,  # Direct param, NOT HooksCapability
    capabilities=[
        provenance,  # Execution DAG + causal logging
        CostTracking(budget_usd=config.max_run_budget_usd),
    ],
    # ... rest unchanged
)
```

### §8.3 PipelineDeps

Add definition:

```python
@dataclass
class PipelineDeps(DeepAgentDeps):
    """Extended deps for pipeline agents."""
    projections: dict[str, Any]  # otio, jobs, vm, budget, state
    agent_role: str
    max_tokens: int
    compaction_model: str
```

---

## 8. Dependency Update

Add to `server/pyproject.toml`:

```toml
[project.dependencies]
"pydantic-ai-provenance" = ">=0.1.0"
```

Or install directly:
```bash
pip install pydantic-ai-provenance
```

---

## 9. Summary

| Discovery | Impact |
|---|---|
| `pydantic-ai-provenance` is real and installable | **Use it** for causal logging instead of custom `AbstractCapability` |
| `pydantic-ai.Hooks` (decorator-based) exists | Use for simple logging; only subclass `AbstractCapability` for middleware |
| `EventStoreDB` not installed, code uses JSONL | **Update §5** to reflect JSONL implementation |
| `HooksCapability` misattributed in §8 | **Fix §8.2** — `on_before_compress` is a direct param |
| `PipelineDeps` undefined in §8 | **Add definition** in §8.2 or §8.3 |
| EventStoreDB no native Mac install path | JSONL is the right choice for this laptop |
