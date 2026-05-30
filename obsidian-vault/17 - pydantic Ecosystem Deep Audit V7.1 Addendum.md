---
{
  "title": "pydantic Ecosystem Deep Audit (V7.1 Addendum)",
  "section": "17",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[16 - Traceability and Observability|Traceability and Observability]] | [[00 - Index|Index]] | [[18 - Authoring Workflow for Quasi-Deterministic Agents|Authoring Workflow for Quasi-Deterministic Agents]] ->

# pydantic Ecosystem Deep Audit (V7.1 Addendum)


This section documents the findings from a comprehensive source-code audit of all pydantic-related packages installed in the pipeline environment. The audit verified which capabilities are real, which are fantasy, and how they compose.

### 17.1 Installed Packages and Verified Capabilities

| Package | Version | Real `AbstractCapability` Subclasses | Status |
|---|---|---|---|
| `pydantic-deep` | latest | `HooksCapability`, `SlidingWindowProcessor` | ✅ Verified |
| `pydantic-ai-summarization` | latest | `ContextManagerCapability`, `SummarizationCapability`, `SlidingWindowCapability`, `LimitWarnerCapability` | ✅ Verified |
| `pydantic-ai-shields` | latest | `CostTracking`, `ToolGuard`, `InputGuard`, `OutputGuard`, `AsyncGuardrail` | ✅ Verified |
| `pydantic-ai-todo` | 0.2.1 | `TodoCapability` | ✅ Verified |
| `pydantic-ai-provenance` | NOT INSTALLED | `ProvenanceCapability` | ⚠️ Real but external; `pip install pydantic-ai-provenance` required |

### 17.2 Capability Details

#### 17.2.1 TodoCapability (`pydantic-ai-todo`)

- **Source:** `pydantic_ai_todo/capability.py`
- **Type:** Real `AbstractCapability` subclass
- **Provides:**
  - `get_toolset()` → `create_todo`, `update_todo`, `delete_todo`, `list_todos`
  - `get_instructions()` → Dynamic todo-list markdown injected into system prompt
- **Usage in pipeline:** Track scene/block progress per agent. The agent can create todos ("reconcile block A1:3:2"), update them ("measured: 3.2s"), and the system prompt automatically reflects current todo state on each turn.

#### 17.2.2 ContextManagerCapability (`pydantic-ai-summarization`)

- **Source:** `pydantic_ai_summarization/capability.py`
- **Type:** Real `AbstractCapability` subclass
- **Provides:** Token tracking, auto-compression trigger at threshold, tool output truncation
- **Usage in pipeline:** Primary context manager. Triggers `on_before_compress` callback when token budget exceeds 90%.

#### 17.2.3 HooksCapability (`pydantic-deep`)

- **Source:** `pydantic_deep/capabilities/hooks.py`
- **Type:** Real `AbstractCapability` subclass
- **Provides:** Decorator-style hooks for tool lifecycle: `on_before_tool_call`, `on_after_tool_call`, `on_tool_error`
- **V7.1 correction:** `HooksCapability` does NOT provide `on_before_compress`. That is a **direct parameter** to `create_deep_agent()`. `HooksCapability` is for logging and middleware around individual tool calls.

#### 17.2.4 ProvenanceCapability (`pydantic-ai-provenance`)

- **Source:** External package `dugarsumit/pydantic-ai-provenance`
- **Type:** Real `AbstractCapability` subclass (NOT installed by default)
- **Provides:**
  - Execution DAG construction (what called what)
  - Citation tracking (which source documents informed which output)
  - Multi-agent attribution (which agent produced which artifact)
  - Graph export (Mermaid, DOT, JSON)
- **Usage in pipeline:** Replace custom causal logging. Install: `pip install pydantic-ai-provenance`
- **Why not in default deps:** Discovered late in V7 development; requires separate package installation.

### 17.3 Hooks vs AbstractCapability Distinction

| Mechanism | Use for | Don't use for |
|---|---|---|
| `HooksCapability` (decorator) | Logging around tool calls, metrics, simple middleware | Context compaction, adding tools |
| `AbstractCapability` subclass | Middleware that wraps `wrap_*` methods, provides tools via `get_toolset()`, injects instructions via `get_instructions()` | One-off logging |
| Direct `create_deep_agent()` param | `on_before_compress`, `model`, `instructions`, `eviction_token_limit` | Complex middleware |

**Rule of thumb:** If you need to add tools or instructions, subclass `AbstractCapability`. If you just need to log tool calls, use `HooksCapability`. If it's a global agent behavior (model choice, compaction callback), use direct parameters.

### 17.4 Dependency Installation Commands

```bash
# Core pipeline deps (already installed)
pip install pydantic-deep pydantic-ai-summarization pydantic-ai-shields pydantic-ai-todo

# Optional: provenance tracking
pip install pydantic-ai-provenance
```

---

