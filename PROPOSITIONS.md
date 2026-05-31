> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Propositions for V7 — Cross-Cutting Features Evaluation

> Goal: Reduce complexity by adding well-chosen components. Not adding complexity to an already complex project.
>
> Date: 2026-05-27
> Status: Propositions under evaluation. Not yet in ARCHITECTURE_V7.md.

---

## P1. pydantic-ai-provenance — Execution DAG & Citation Tracking

**Package:** `pip install pydantic-ai-provenance` (github.com/dugarsumit/pydantic-ai-provenance). Real, Python ≥3.12, pydantic-ai ≥1.80.

**What it does.** Attach `ProvenanceCapability` to any pydantic-ai agent. Automatically builds a full execution DAG of every tool call, model request, and response. Injects citation keys (`d_1`, `a_1`, ...) into source tool results so the LLM can cite inline. Multi-agent attribution via shared store + `contextvars`. Citation verification via TF-IDF cosine overlap + optional LLM entailment. Exports to Mermaid, GraphViz DOT, JSON, or interactive HTML.

**Cross-cutting value for V7:**

| Current V7 pain point | Provenance solves | Integration cost |
|---|---|---|
| §16 traceability is hand-rolled (`run_id`, `effect_id`, `sequence`) | Full execution DAG auto-generated: every model call, every tool call, every claim linked to its source | One line per agent: `capabilities=[ProvenanceCapability(agent_name="audio")]` |
| No visibility into *why* an agent produced a particular effect | `attribute_output(store).summary()` shows the full path from sources to output | Zero architectural change |
| Operator debugging: grep logs, read raw EventStoreDB events | `store.to_mermaid()` or `store.to_html()` — visual graph of agent reasoning | Post-run export only; no runtime dependency |
| No citation verification on agent claims (e.g., "duration is 5.12s" — did the agent hallucinate?) | `verify_citations()` checks every `[REF\|...]` tag against source data | Optional; agent must be prompted to emit `[REF\|key]` tags |

**Complexity analysis:**
- **Lines of code removed from V7:** Custom traceability boilerplate in §16 (~30 lines of prose + any custom tracing code).
- **Lines added:** One `ProvenanceCapability` instantiation per agent. ~5 lines total.
- **New failure modes:** None. If provenance fails, the agent still produces text; the DAG is incomplete but the pipeline continues.
- **Performance impact:** Negligible. Graph building is in-memory; serialization is post-run only.

**Why it does not violate V7 principles:**
- No timeouts. No stubs. No mocks.
- No environment variables.
- Does not constrain agent reasoning — runs underneath.
- Does not add new endpoints. Does not require structured output from agents.

**Proposed integration point:**
```python
# In agent_base.py, per-agent construction:
from pydantic_ai_provenance.capability import ProvenanceCapability

provenance = ProvenanceCapability(
    agent_name=agent_role,  # "scenario", "audio", "video", "assembly"
    source_tools=[],  # In V7, "sources" are projection state, not file reads
)
agent = create_deep_agent(
    capabilities=[provenance, ...],
)

# After run, export for operator inspection:
# store.to_mermaid() → saved to /provenance/run_{run_id}/audio_{timestamp}.md
```

**Verdict: ADOPT** — Low integration cost, high debugging value, no architectural conflict.

---

## P2. CostTracking (from pydantic-ai-shields) — Budget Enforcement

**Package:** Built into pydantic-deep via `pydantic-ai-shields`. Enabled by default.

**What it does.** Tracks token usage and USD cost per model call. Cumulative across runs. Raises `BudgetExceededError` when `budget_usd` is exceeded. Callback `on_cost_update` fires after every run with `CostInfo` (run cost, cumulative cost, token counts).

**Cross-cutting value for V7:**

| Current V7 pain point | CostTracking solves | Integration cost |
|---|---|---|
| `max_run_budget_usd` in Config (§14.1.1) — no enforcement mechanism | Automatic budget enforcement: raises `BudgetExceededError` when exceeded | Already enabled by default in `create_deep_agent()` |
| Budget tracking is manual via `VASTGlobalStateObserved.credit_balance_usd` | Real-time cost tracking per agent turn, with `on_cost_update` callback | Hook the callback to emit `BudgetExceeded` effect to EventStoreDB |
| No per-agent cost visibility | `CostInfo.run_cost_usd` + `total_cost_usd` per agent | Already emitted by capability; just read it |

**Complexity analysis:**
- **Lines removed:** Custom budget accumulator logic in `BudgetProjection` (if any exists; currently V7 has no active budget enforcement — it was removed with `CostIncurred`).
- **Lines added:** Zero. Already enabled by default.
- **New failure modes:** `BudgetExceededError` on over-budget run. This is correct behavior — the pipeline should abort.

**Proposed integration point:**
```python
# In agent_base.py:
from pydantic_ai_shields import CostTracking

cost_cap = CostTracking(
    budget_usd=config.max_run_budget_usd,
    on_cost_update=lambda info: _emit_budget_effect(info),  # Emit to EventStoreDB
)
agent = create_deep_agent(
    capabilities=[cost_cap, ...],
)

# _emit_budget_effect writes a BudgetObserved effect to EventStoreDB
# if info.total_cost_usd > config.max_run_budget_usd:
#     await append_effect(run_id, BudgetExceeded(....))
```

**Verdict: ADOPT** — Already present in pydantic-deep. Wire the callback to EventStoreDB; remove any custom budget logic.

---

## P3. HooksCapability — Safety Gates & Audit Logging

**Package:** Built into pydantic-deep. `HooksCapability` is an `AbstractCapability` that dispatches on tool lifecycle events.

**What it does.** Register `Hook` objects for events: `PRE_TOOL_USE`, `POST_TOOL_USE`, `POST_TOOL_USE_FAILURE`, `BEFORE_RUN`, `AFTER_RUN`, `RUN_ERROR`, `BEFORE_MODEL_REQUEST`, `AFTER_MODEL_REQUEST`. Each hook can be a shell command or an async Python handler. Hooks run in order; first deny wins for `PRE_TOOL_USE`. Matchers (regex on tool name) filter which hooks fire.

**Cross-cutting value for V7:**

| Current V7 pain point | Hooks solve | Integration cost |
|---|---|---|
| `ALLOWLISTED_COMMANDS` (§14.1.4) — manual security list | `PRE_TOOL_USE` hook with safety gate: block any `execute` tool call whose command is not in ALLOWLISTED_COMMANDS | One hook registration |
| No audit trail of what tools an agent invoked | `POST_TOOL_USE` hook logs every tool call to EventStoreDB as an `AgentToolInvoked` effect | One hook registration |
| No rate limiting enforcement per-agent | `BEFORE_MODEL_REQUEST` hook increments a counter; blocks if rate exceeded | One hook registration |
| No way to redact secrets from tool results before they enter agent memory | `POST_TOOL_USE` hook with `redact_secrets` handler | One hook registration |

**Complexity analysis:**
- **Lines removed:** Custom security gate code in agent handlers. Custom audit logging. Custom rate limit logic.
- **Lines added:** ~10 lines of hook registrations in `agent_base.py`.
- **New failure modes:** Hook denial raises `ModelRetry` — the agent retries with a different approach. This is correct behavior.

**Proposed integration point:**
```python
from pydantic_deep import Hook, HookEvent
from pydantic_deep.capabilities.hooks import HookInput, HookResult

async def security_gate(hook_input: HookInput) -> HookResult:
    if hook_input.tool_name == "execute":
        cmd = hook_input.tool_input.get("command", "")
        basename = cmd.split()[0]
        if basename not in ALLOWLISTED_COMMANDS:
            return HookResult(allow=False, reason=f"Command '{basename}' not allowlisted")
    return HookResult(allow=True)

async def audit_logger(hook_input: HookInput) -> HookResult:
    # Emit AgentToolInvoked effect to EventStoreDB
    await append_effect(run_id, AgentToolInvoked(...))
    return HookResult(allow=True)

agent = create_deep_agent(
    hooks=[
        Hook(event=HookEvent.PRE_TOOL_USE, handler=security_gate, matcher="execute"),
        Hook(event=HookEvent.POST_TOOL_USE, handler=audit_logger),
    ],
)
```

**Verdict: ADOPT** — Replaces scattered security/audit/rate-limit code with declarative hooks. Centralizes cross-cutting concerns.

---

## P4. ContextManagerCapability — Token Tracking & Auto-Compression

**Package:** Built into pydantic-deep (via `pydantic-ai-summarization`). Enabled by default.

**What it does.** Tracks token usage in conversation history. When history exceeds `context_manager_max_tokens`, auto-compresses by summarizing older messages. Calls `on_context_update` callback with `(pct_used, current_tokens, max_tokens)`.

**Cross-cutting value for V7:**

| Current V7 pain point | ContextManager solves | Integration cost |
|---|---|---|
| §1.8 pydantic-deep compaction: "OTIO-aware pre-processing before agent.run()" | Auto-compression is built-in; V7's custom compaction logic may be redundant | Verify V7's OTIO-aware compaction adds value beyond built-in summarization |
| No visibility into token budget exhaustion | `on_context_update` callback fires at configurable thresholds | Hook callback to emit `TokenBudgetWarning` effect |
| Agents may exceed context window on long runs | Automatic summarization prevents overflow | Already enabled by default |

**Complexity analysis:**
- **Lines potentially removed:** Custom `on_before_compress` hook in V7 (§1.8) if it is redundant with built-in summarization.
- **Lines added:** Zero. Already enabled by default.
- **Risk:** V7's OTIO-aware compaction is domain-specific (preserves task-relevant details). Built-in summarization is generic. May lose task-critical context.

**Proposed evaluation:**
1. Run a test: agent with long message history, compare output quality with (a) V7 custom compaction, (b) built-in ContextManager only, (c) both.
2. If built-in is sufficient, remove V7 custom compaction. If not, keep V7 custom compaction and disable built-in (`context_manager=False`).

**Verdict: EVALUATE** — Already present. Determine if V7's custom compaction is redundant. If yes, remove it; if no, disable built-in to avoid conflict.

---

## P5. EvictionCapability — Large Output Interception

**Package:** Built into pydantic-deep. Enabled by default with `eviction_token_limit=20_000`.

**What it does.** Intercepts large tool results via `after_tool_execute` hook. Saves full output to backend file (`/large_tool_results/{id}`). Replaces result in conversation history with a preview (head/tail lines + file path). Agent can use `read_file` with offset/limit to access full output.

**Cross-cutting value for V7:**

| Current V7 pain point | Eviction solves | Integration cost |
|---|---|---|
| `ffmpeg` or `vastai` CLI output can be thousands of lines, bloating agent context | Automatically intercepts and files large outputs; agent sees only preview | Already enabled by default |
| No mechanism to page through large outputs | Agent calls `read_file(path="/large_tool_results/...", offset=0, limit=100)` | Already available |

**Complexity analysis:**
- **Lines removed:** Any custom large-output handling in V7 agent tools.
- **Lines added:** Zero.
- **Risk:** V7 agents do not use file-system tools (`read_file`, etc.) — they produce text and effects. Eviction is relevant only if agents use tools that return large text results. In V7, the only "tool" is the LLM itself; there are no `execute` or `read_file` tools inside the agent. Eviction may be irrelevant.

**Verdict: DEFER** — V7 agents are HTTP services that produce text, not file-system agents. Eviction solves a problem V7 does not have. If V7 ever adds internal tools (e.g., `read_event_stream`), then enable.

---

## P6. Checkpointing — Conversation Rewind & Session Forking

**Package:** Built into pydantic-deep. `include_checkpoints=True`.

**What it does.** Saves conversation snapshots at configurable frequency (`every_tool`, `every_turn`, `manual_only`). `save_checkpoint`, `list_checkpoints`, `rewind_to` tools. `RewindRequested` exception for app-level rewind. Session forking via `fork_from_checkpoint`. Stores: `InMemoryCheckpointStore` (default) or `FileCheckpointStore`.

**Cross-cutting value for V7:**

| Current V7 pain point | Checkpointing solves | Integration cost |
|---|---|---|
| Agent produces a bad effect; operator must issue `HumanInstruction` to correct | Operator can `rewind_to` a previous checkpoint and retry from a known-good state | Requires checkpoint store per run |
| No way to experiment with different agent approaches on the same state | `fork_from_checkpoint` creates parallel sessions from a snapshot | Low |
| No conversation-level recovery | Auto-save after every tool call prevents total loss of progress | Already built-in |

**Complexity analysis:**
- **Lines removed:** None. V7 has no conversation-level recovery.
- **Lines added:** Checkpoint store initialization. ~5 lines per agent startup.
- **Risk:** Checkpoints are conversation history snapshots, not EventStoreDB state. Rewinding an agent's conversation does NOT rewind the event stream. This could cause divergence: agent rewinds to before it emitted `QueueJob`, but `QueueJob` is still in EventStoreDB. The Provisioner may have already acted on it.

**Critical conflict with V7 architecture:**
V7's source of truth is EventStoreDB, not agent conversation history. Checkpointing rewinds conversation history but cannot rewind the event stream. This creates a **split-brain scenario**: the agent thinks it's at turn N, but the event stream is at turn N+3.

**Possible resolution:** Checkpointing could be adapted to save/restore `message_history` + `last_sequence` for the GSA. The agent would rewind its conversation AND its GSA query position. But EventStoreDB effects are immutable — they cannot be undone.

**Verdict: REJECT for V7** — Fundamental conflict with event-sourced architecture. EventStoreDB is the source of truth; conversation history is ephemeral. Checkpointing assumes conversation history is the state. Incompatible without major redesign.

---

## P7. Subagents — Task Delegation with Chiseled Context

**Package:** Built into pydantic-deep. `include_subagents=True`.

**What it does.** Main agent delegates to focused subagents via `task(description="...", subagent_type="audio-measurer")`. Subagents are isolated deep agents with fresh TODO lists, custom instructions, and optional custom models. Support sync/async execution modes, nested delegation (configurable depth), and `ask_parent` for clarification.

**Corrected understanding for V7:**
Subagents are **in-process task performers**, not HTTP services. They solve the "drowned in context" problem by receiving only a **chiseled subset** of global state. The main agent ALWAYS retains full capability to perform any task itself — subagents are optional helpers. If a subagent produces garbage, the main agent ignores it and does the work directly. This is a fool-proof fallback.

**Architecture:**

```
HTTP POST / → Main Agent (port 8001/8002/8003/8005)
    ├─ GET /?run_id=... → Global State Agent (receives full projections)
    ├─ Main agent reads full state, decides which situation applies
    ├─ May call: task(description="measure block A1:1:1", subagent_type="audio-measurer")
    │   → Subagent receives: chiseled context (audio_job_state + measurement_rules)
    │   → Subagent produces: natural language text (measurement reasoning)
    ├─ Main agent evaluates subagent output
    │   → If good: incorporates into its own reasoning
    │   → If bad: discards and performs the task itself
    ├─ Main agent produces final natural language text
    └─ Parser extracts effects from main agent's final text
```

**Key invariants:**
- Only the **main agent's** output is parsed for effects
- Subagents are invisible to the network and EventStoreDB
- Main agent can always bypass subagents and do the work itself
- Subagents are dev-time optimizations, not runtime requirements

**Cross-cutting value for V7:**

| Current V7 pain point | Subagents solve | Integration cost |
|---|---|---|
| Single agent must hold ALL projections + ALL effect schemas + ALL rules in context | Each subagent holds only its focused slice. Main agent only needs routing logic. | SubAgentConfig per task type |
| Audio Agent needs to reason about audio measurement, job queue, reconciliation, AND VM state | "audio-measurer" subagent only sees measurement state. "audio-reconciler" only sees reconciliation state. | ~3-5 subagents per main agent |
| Changing one agent's behavior risks breaking others | Subagents are isolated; changing "audio-measurer" instructions doesn't affect "audio-reconciler" | Low |
| Complex situations require the agent to hold multiple competing priorities | Main agent delegates each priority to a focused subagent, then synthesizes | Natural fit with task() tool |

**Subagent design for V7 agents:**

| Main Agent | Subagent | Chiseled Context | Purpose |
|---|---|---|---|
| Scenario | script-drafter | Unfilled OTIO slots, style_tags, narrator_voice | Draft script text for empty slots |
| Scenario | voice-tagger | OTIO speaker assignments, voice mismatches | Fix voice tag mismatches only |
| Audio | audio-measurer | Dirty audio blocks, measurement history | Produce measurement reasoning |
| Audio | audio-reconciler | Clean audio blocks, target durations, tolerance | Produce reconciliation reasoning |
| Video | video-judger | Dirty video blocks, LTX output previews | Judge video quality |
| Assembly | final-muxer | All slots filled, artifact URIs, duration targets | Produce muxing reasoning |
| Assembly | duration-validator | Measured durations vs targets | Validate duration tolerance |

**Why this does NOT violate V7 architecture:**
1. HTTP surface unchanged: only main agent has GET / and POST /
2. Subagents are in-process, invisible to the network
3. Effects still extracted from main agent text only
4. EventStoreDB still sole source of truth
5. GSA still sole read path
6. Fool-proof fallback: main agent can always do the task itself

**Complexity analysis:**
- **Lines removed:** Complex conditional logic in agent handlers for "which situation applies?" — routing is now explicit via subagent selection
- **Lines added:** SubAgentConfig definitions per agent (~10 lines each)
- **Context window reduction:** Main agent: ~2K tokens (routing logic). Subagent: ~4K tokens (focused state + rules). vs. Current single agent: ~10K+ tokens (all state + all rules)
- **Risk:** Near-zero. Subagents are optional; main agent fallback guarantees pipeline never breaks.

**Verdict: ADOPT** — Fundamental to solving V7's context-drowning problem. Subagents are in-process task performers with chiseled state. Main agent routes; subagents execute. Fool-proof fallback to main agent. Parser extracts from main agent output only.

---

## P8. Agent Teams — Multi-Agent Collaboration

**Package:** Built into pydantic-deep. `include_teams=True`.

**What it does.** Flat groups of agents with shared `SharedTodoList` (async-safe task tracker with claiming and dependencies) and `TeamMessageBus` (peer-to-peer messaging). `spawn_team`, `assign_task`, `check_teammates`, `message_teammate`, `dissolve_team` tools.

**Cross-cutting value for V7:**

| V7 concept | Teams equivalent | Fit |
|---|---|---|
| Audio + Video agents running in parallel | `spawn_team` with audio_member and video_member | **Mismatch** — V7 agents are HTTP services, not in-process team members |
| Shared job queue | `SharedTodoList` with claiming and dependencies | **Partial fit** — but JobProjection already handles this |
| Agent-to-agent messaging | `TeamMessageBus` with `message_teammate` | **Mismatch** — V7 uses HTTP POST wake notifications |

**Critical conflict with V7 architecture:**
Teams assume all members are in-process subagents registered on a `DynamicAgentRegistry`. V7 agents are independent processes. The `SharedTodoList` and `TeamMessageBus` are in-memory/asyncio objects that cannot span process boundaries.

**Verdict: REJECT for V7** — Same reason as subagents. Incompatible with V7's distributed process model.

---

## P9. Persistent Memory — MEMORY.md

**Package:** Built into pydantic-deep. `include_memory=True`.

**What it does.** Each agent gets a persistent `MEMORY.md` file stored in the backend. Auto-loaded into system prompt (first 200 lines). Writable via `read_memory`, `write_memory`, `update_memory` tools.

**Cross-cutting value for V7:**

| Current V7 pain point | Persistent Memory solves | Integration cost |
|---|---|---|
| Agent "memory" is rebuilt from projections on every turn; no learning across runs | Agent can save insights to MEMORY.md (e.g., "WhisperX on RTX 4090 takes ~12s per 30s clip") | Requires backend storage for MEMORY.md files |
| No way to accumulate domain knowledge across pipeline runs | Persistent memory survives process restarts | Low |

**Complexity analysis:**
- **Lines removed:** None. V7 has no persistent memory.
- **Lines added:** Backend storage for memory files. ~5 lines per agent.
- **Risk:** MEMORY.md is agent-local. V7's state is global (EventStoreDB). An agent's memory may drift from reality if it saves incorrect assumptions. Need a "memory validation" step where the agent checks its MEMORY.md against current projections.
- **V7 conflict:** V7 principle says "Agent memory does not persist" (§1.9 Principle 8). Persistent memory contradicts this.

**Possible resolution:** Relax Principle 8 to "Agent memory does not persist *session state* — but domain knowledge can be saved across runs." This is a semantic distinction: session state (what happened this run) is always rebuilt from EventStoreDB; domain knowledge (how to do things better) can be persisted.

**Verdict: CONSIDER** — Useful for accumulating operational knowledge (VM performance, model quirks). But contradicts V7 Principle 8. Requires principle amendment.

---

## P10. Human-in-the-Loop — interrupt_on

**Package:** Built into pydantic-deep. `interrupt_on={"execute": True, "write_file": True}`.

**What it does.** When a deferred tool is called, the agent pauses and returns `DeferredToolRequests`. Human reviews pending calls, builds approvals dict, resumes with `DeferredToolResults`. Web app integration pattern included.

**Cross-cutting value for V7:**

| Current V7 pain point | interrupt_on solves | Integration cost |
|---|---|---|
| `ClarificationRequest` effect for non-allowlisted bash commands | Direct tool-level approval: agent pauses, human approves/denies specific tool call | Replaces custom `ClarificationRequest` flow |
| No fine-grained approval per command | `ToolApproved` / `ToolDenied` per `tool_call_id` | Already built-in |

**Critical conflict with V7 architecture:**
V7 agents do not have tools. They produce text; the parser extracts effects. There is no `execute` tool inside the agent. The agent does not invoke bash commands — it produces text from which the parser may extract `ExecuteRawBash` effects. The effects are then handled by the agent handler (appended to EventStoreDB), not executed by the agent.

`interrupt_on` interrupts *tool calls inside the agent*. V7 agents have no such tools.

**Possible adaptation:** If V7 ever adds internal tools (e.g., a `query_gsa` tool that the agent uses to fetch state), `interrupt_on` could approve/deny those. But currently irrelevant.

**Verdict: REJECT for V7** — V7 agents produce text, not tool calls. `interrupt_on` solves a problem V7 does not have.

---

## P11. Streaming — Real-Time Progress

**Package:** Built into pydantic-deep. `agent.iter()` returns async generator of `UserPromptNode`, `ModelRequestNode`, `CallToolsNode`, `End`.

**Cross-cutting value for V7:**

| Current V7 pain point | Streaming solves | Integration cost |
|---|---|---|
| Operator has no visibility into agent progress during a long turn | Stream nodes show "Thinking...", "Executing tool X...", "Completed!" | SSE endpoint on each agent |
| No way to cancel a long-running agent | `asyncio.cancel()` on the streaming task | Already supported |

**Complexity analysis:**
- **Lines added:** SSE endpoint per agent. ~20 lines.
- **Performance impact:** Negligible.
- **Conflict with V7?** None. Does not change agent behavior. Pure observability.

**Proposed integration:**
```python
# In agent_base.py, alongside GET / and POST /:
@app.get("/stream")
async def stream_agent(run_id: str, prompt: str):
    async def event_generator():
        async with agent.iter(prompt, deps=deps) as run:
            async for node in run:
                yield f"data: {json.dumps({'node': type(node).__name__})}\n\n"
            yield f"data: {json.dumps({'output': run.result.output})}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**BUT:** V7's architecture guard says "Only GET / and POST / endpoints." Adding `/stream` violates this.

**Possible resolution:** `/stream` is a variant of `GET /` — it returns state. Or the guard could be relaxed to "Only GET /, POST /, and streaming variants." But the user explicitly said "Only GET / and POST / endpoints."

**Verdict: REJECT for V7** — Violates architecture guard. Operator observability is already provided by `GET /` on the GSA and EventStoreDB event stream. Streaming is nice-to-have, not essential.

---

## P12. Plan Mode — Planning Subagent

**Package:** Built into pydantic-deep. `include_plan=True`.

**What it does.** Planner subagent reads code, asks clarifying questions, creates step-by-step implementation plans. `ask_user` tool for human questions. `save_plan` tool writes markdown plan to `/plans/`.

**Cross-cutting value for V7:**

| V7 pain point | Plan mode solves | Integration cost |
|---|---|---|
| Complex repairs (Maintainer Agent) require operator to describe problem in detail | Planner can analyze event stream + projections and generate repair plan | Low |

**Critical conflict with V7 architecture:**
Plan mode is designed for code modification tasks ("add authentication to the app"). V7 is a pipeline, not a codebase. The "plans" would be pipeline repair plans, not code changes. The planner's tools (`read_file`, `ls`, `grep`) are filesystem tools; V7's state is in EventStoreDB, not files.

**Verdict: REJECT for V7** — Plan mode is a code-planning tool. V7's "planning" is emergent from projection state. No code to plan.

---

## P13. Context Files — AGENTS.md Auto-Discovery

**Package:** Built into pydantic-deep. `context_discovery=True`.

**What it does.** Auto-discovers `AGENTS.md`, `CLAUDE.md`, `SOUL.md`, `.cursorrules`, etc. from backend root. Injects into system prompt. `AGENTS.md` and `CLAUDE.md` shared with subagents; others main-agent-only.

**Cross-cutting value for V7:**

| Current V7 pain point | Context files solve | Integration cost |
|---|---|---|
| System prompts are hardcoded in Python strings | Project-level instructions in `AGENTS.md` auto-loaded; no code changes needed for prompt tweaks | Move prompt content to `AGENTS.md` |
| No per-project personality/tone configuration | `SOUL.md` for agent personality | One file |

**Complexity analysis:**
- **Lines removed:** Hardcoded system prompt strings in `agents/scenario.py`, `agents/audio.py`, etc.
- **Lines added:** `AGENTS.md` and `SOUL.md` files in project root.
- **Conflict with V7?** None. V7 already has the concept of system prompts with RULES blocks. Moving them to `AGENTS.md` is a file organization change, not an architecture change.

**Proposed integration:**
```python
# In agent_base.py:
agent = create_deep_agent(
    context_discovery=True,  # Auto-load AGENTS.md, SOUL.md
    # Or explicit:
    # context_files=["/project/AGENTS.md", "/project/SOUL.md"],
)
```

**BUT:** V7 agents are HTTP services, not file-system agents. The "backend" in pydantic-deep is a file storage abstraction (`StateBackend`, `LocalBackend`). V7 agents don't have a backend — they query the GSA via HTTP.

**Possible resolution:** Create a `PipelineBackend` that reads `AGENTS.md` from the project root (static file) and serves it to the agent at construction time.

**Verdict: CONSIDER** — Low value for V7. System prompts are already centralized in `agents/*.py`. Moving them to markdown files adds a layer of indirection without reducing complexity. Useful if the user wants non-coders to edit prompts.

---

## P14. Stuck Loop Detection

**Package:** Built into pydantic-deep. Enabled by default. `stuck_loop_detection=False` to disable.

**What it does.** Detects repetitive agent behavior via `after_tool_execute` hook. Three patterns:
- **Repeated identical calls:** Same tool with same args N times in a row
- **Alternating A-B-A-B:** Two tools alternating back and forth
- **No-op calls:** Same tool returning same result repeatedly

Actions: `action="warn"` (raises `ModelRetry`, model self-corrects) or `action="error"` (raises `StuckLoopError`, run aborts).

**Cross-cutting value for V7:**

| Current V7 pain point | Stuck Loop Detection solves | Integration cost |
|---|---|---|
| `AgentLoopDetected` effect (§3.8.2) — custom detection logic | Built-in detection with three patterns, no custom code | Already enabled by default |
| `loop_detection_threshold` (§14.1.3) — manual counter | Automatic detection via capability hooks | Remove custom counter |
| No detection of A-B-A-B oscillation (e.g., measure→requeue→measure→requeue) | Detects alternating patterns automatically | Already enabled |

**How it fits with subagents (P7):**
Stuck loop detection runs on the **main agent's** tool calls. Since subagents are invoked via `task()` (a tool call from the main agent's perspective), stuck loop detection catches:
- Main agent repeatedly spawning the same subagent with the same chiseled context
- Main agent oscillating between two subagents (e.g., measurer → reconciler → measurer → reconciler)

This is **more powerful** than V7's current `AgentLoopDetected` which only checks for duplicate effects.

**Configuration for V7:**
```python
agent = create_deep_agent(
    stuck_loop_detection=True,
    capabilities=[
        StuckLoopDetection(
            max_repeated=3,
            action="error",  # Hard abort on loop — operator intervenes
            detect_repeated=True,
            detect_alternating=True,
            detect_noop=True,
        )
    ],
)
```

**Why action="error" not "warn":**
V7 principle: "No automatic recovery — operator monitors via GET / and intervenes manually." A warning lets the model self-correct, which may loop again. An error aborts the run, surfacing the problem to the operator.

**Verdict: ADOPT** — Replaces custom `AgentLoopDetected` effect and `loop_detection_threshold`. More comprehensive (3 patterns vs 1). Already present in pydantic-deep.

---

## P15. Periodic Reminder — Focus Preservation

**Package:** Built into pydantic-deep. `PeriodicReminderCapability` with configurable generators.

**What it does.** Injects a "what are you supposed to be doing" nudge into the conversation every N model-request turns. Five generator modes:
- `None` (default): Extracts first user message verbatim, zero-cost
- Static string: Fixed reminder text, zero-cost
- Async callable: Dynamic content based on runtime state, zero-cost
- Compact transcript: Summarized view of last N turns, zero-cost
- `LLMReminderGenerator`: LLM-generated two-sentence nudge, low cost

Render styles: `system_reminder_tag` (default), `user_prompt`, `developer_note`.

**Cross-cutting value for V7:**

| Current V7 pain point | Periodic Reminder solves | Integration cost |
|---|---|---|
| Long agent turns may drift from original task (e.g., agent starts optimizing ffmpeg flags instead of measuring audio) | Injects original goal reminder every N turns | Already present; just configure |
| Subagents (P7) may lose sight of their specific task within chiseled context | Reminder re-anchors subagent to its delegated goal | One config per subagent |
| No mechanism to keep agent focused on highest-priority situation | Reminder references original user prompt / goal | Zero cost with default generator |

**How it fits with subagents (P7):**
When the main agent spawns a subagent via `task(description="measure block A1:1:1", subagent_type="audio-measurer")`, the subagent receives the task description as its "first user message." The periodic reminder extracts this and injects it every N turns:

```
<system-reminder>
The original request was: "measure block A1:1:1"
Check that your next action advances this goal.
</system-reminder>
```

This prevents the subagent from drifting (e.g., starting to reason about VM provisioning when it should only measure audio).

**Configuration for V7:**
```python
# Main agent: remind every 10 turns with original POST payload context
agent = create_deep_agent(
    periodic_reminder=PeriodicReminderConfig(
        every_n_turns=10,
        first_after=5,
        generator=None,  # Zero-cost: uses first user message
    )
)

# Subagent: remind more frequently (shorter tasks)
subagent_config = SubAgentConfig(
    name="audio-measurer",
    instructions="...",
    extra={
        "periodic_reminder": PeriodicReminderConfig(
            every_n_turns=5,
            first_after=3,
            generator=None,
        )
    }
)
```

**Why zero-cost default is sufficient:**
The default generator (`None`) extracts the first user message and wraps it in a `<system-reminder>` tag. No LLM call. No token cost. Just a text injection. For V7's subagents, the "first user message" is the task description from `task()`, which is exactly the goal the subagent should stay focused on.

**Verdict: ADOPT** — Zero-cost focus preservation. Critical for subagent reliability. Already present in pydantic-deep.

---

## Summary Table

| # | Proposition | Verdict | Complexity Delta | V7 Conflict? |
|---|---|---|---|---|
| P1 | pydantic-ai-provenance | **ADOPT** | Low (1 line per agent) | None |
| P2 | CostTracking | **ADOPT** | Zero (already present) | None |
| P3 | HooksCapability | **ADOPT** | Low (hook registrations) | None |
| P4 | ContextManagerCapability | **EVALUATE** | Zero (already present) | Possible redundancy with V7 custom compaction |
| P5 | EvictionCapability | **DEFER** | Zero (already present) | V7 agents have no file-system tools |
| P6 | Checkpointing | **REJECT** | Medium | Fundamental: rewinds conversation, not event stream |
| P7 | Subagents | **ADOPT** | Medium | None — subagents are in-process, main agent has HTTP only |
| P8 | Agent Teams | **REJECT** | High | Architectural: in-process vs. network-separated |
| P9 | Persistent Memory | **CONSIDER** | Low | Contradicts V7 Principle 8 (agent memory does not persist) |
| P10 | Human-in-the-Loop (interrupt_on) | **REJECT** | Zero | V7 agents have no internal tools to interrupt |
| P11 | Streaming | **REJECT** | Low | Violates "Only GET / and POST /" guard |
| P12 | Plan Mode | **REJECT** | Medium | Designed for code planning, not pipeline orchestration |
| P13 | Context Files | **CONSIDER** | Low | Low value; V7 prompts are already centralized |
| P14 | Stuck Loop Detection | **ADOPT** | Zero | Already present; replaces custom loop detection |
| P15 | Periodic Reminder | **ADOPT** | Zero | Already present; keeps subagents focused on task |

**Recommended immediate actions:**
1. **Adopt P1, P2, P3, P7, P14, P15** — wire into `agent_base.py` and subagent configs. Remove custom equivalents (loop detection, situation routing).
2. **Evaluate P4** — test if built-in context management replaces V7 custom compaction.
3. **Reject P6, P8, P10–P12** — incompatible with V7's event-sourced, distributed architecture.
4. **Consider P9, P13** — optional enhancements; not complexity reducers.

---

*Propositions document — not part of ARCHITECTURE_V7.md. For evaluation only.*

---

## E1. Server-Side Filtering on `subscribe_to_all` — GSA Live Updates

**Feature:** EventStoreDB catch-up subscription with `filter_include`/`filter_exclude` regex patterns on event type or stream name. Built into `esdbclient.subscribe_to_all()`.

**Current V7 approach:** GSA reads full stream via `read_since(run_id, last_sequence)`, then client-side filters and deserializes. On every `GET /` request, the GSA re-reads the stream.

**What it does:** `subscribe_to_all(filter_include=["QueueJob", "JobCompleted", "JobFailed"], filter_by_stream_name=False)` returns ONLY events matching the filter. Server does the filtering; client receives a filtered stream. Combined with `from_end=True`, the GSA can maintain a live subscription that pushes new events as they arrive.

**Cross-cutting value:**

| Current V7 pain point | Server-side filtering solves | Integration cost |
|---|---|---|
| GSA re-reads entire stream on every `GET /` | Live subscription pushes events; GSA holds in-memory cache | Replace polling with subscription |
| Client-side filtering deserializes 32 event types when only 5 are relevant | Server filters by `filter_include=["kind1", "kind2"]` | Zero — pass regex to subscription |
| No "live" mode — GSA always polls | `from_end=True` + catch-up subscription = push model | Architecture change in GSA |

**Proposed integration:**
```python
# In global_state_agent.py:
async def _gsa_event_loop():
    # Subscribe to all events, but only for streams matching "run-.*"
    # and only event types matching V7 effect kinds
    effect_pattern = "|".join(KIND_TO_MODEL.keys())
    async with client.subscribe_to_all(
        filter_include=[effect_pattern],
        filter_by_stream_name=False,
        include_caught_up=True,
    ) as subscription:
        async for item in subscription:
            if isinstance(item, Checkpoint):
                continue
            if isinstance(item, CaughtUp):
                _gsa_state.is_live = True
                continue
            # Update in-memory projection
            _update_projections(item)
```

**Verdict: ADOPT** — Eliminates polling code. Server-side filtering reduces network traffic. `CaughtUp` signal tells the GSA when it's processing live events vs catching up.

---

## E2. Stream Metadata — `$maxCount` for Automatic Per-Run Truncation

**Feature:** `set_stream_metadata(stream_name, metadata={"$maxCount": N})`. EventStoreDB automatically truncates old events when the stream exceeds N events.

**Current V7 approach:** No event retention policy. Streams grow unbounded.

**What it does:** Set `$maxCount` on each `run-{run_id}` stream. When the stream exceeds the limit, EventStoreDB soft-deletes the oldest events. The stream still exists, but old events are scavenged.

**Cross-cutting value:**

| Current V7 pain point | `$maxCount` solves | Integration cost |
|---|---|---|
| EventStoreDB disk grows unbounded with every run | Automatic truncation per stream | One metadata call at stream creation |
| Manual cleanup required | Server handles it | None |
| Long replays for old runs | Old events are truncated; replay is bounded | Set appropriate limit per run |

**Proposed integration:**
```python
# When creating a run (PipelineStarted effect):
client.set_stream_metadata(
    stream_name=f"run-{run_id}",
    metadata={"$maxCount": 10000},  # Keep last 10K events per run
)
```

**Why not `$maxAge`:** V7 has no concept of "this run is done, delete after 30 days." Events are the source of truth; deleting them loses history. `$maxCount` is safer — it keeps recent events, drops oldest.

**Verdict: ADOPT** — One metadata call per stream. Prevents unbounded growth. No code needed for cleanup.

---

## E3. Catch-Up Subscription `include_caught_up` — GSA Readiness Signal

**Feature:** `subscribe_to_stream(include_caught_up=True)` emits a `CaughtUp` sentinel object when the subscription transitions from historical replay to live events.

**Current V7 approach:** GSA has no way to know if its projections are "live" or still catching up. `GET /` returns whatever state is in memory, which may be incomplete.

**What it does:** The `CaughtUp` sentinel tells the GSA: "you have now processed all historical events and are receiving new events as they are appended." The GSA can expose this in its `GET /` response.

**Cross-cutting value:**

| Current V7 pain point | `CaughtUp` solves | Integration cost |
|---|---|---|
| Operator doesn't know if GSA projections are complete | `GET /` returns `is_live: true` after `CaughtUp` | One boolean field in response |
| Agent queries GSA before it's ready | Agent can check `is_live` and retry if false | One field check |
| No visibility into GSA initialization progress | `CaughtUp` is a definitive signal | Subscription architecture change |

**Proposed integration:**
```python
class GlobalStateResponse(BaseModel):
    run_id: str
    timestamp: float
    is_live: bool  # True after CaughtUp received
    otio: OTIOProjection
    # ...
```

**Verdict: ADOPT** — Zero cost (already in esdbclient). Adds operational visibility.

---

## E4. Persistent Subscriptions — Server-Side Checkpointing for GSA

**Feature:** `create_subscription_to_stream()` + `read_subscription_to_stream()`. Server maintains checkpoint position. Multiple consumers can connect. `ack()`/`nack()` with retry/park/stop actions.

**Current V7 approach:** GSA tracks `last_sequence` in-memory. If GSA restarts, it replays from sequence 0.

**What it does:** EventStoreDB server tracks the last acknowledged event position. If GSA restarts, it reconnects to the persistent subscription and receives only unacknowledged events. No in-memory `last_sequence` needed.

**Cross-cutting value:**

| Current V7 pain point | Persistent subscriptions solve | Integration cost |
|---|---|---|
| GSA loses `last_sequence` on restart → full replay | Server maintains checkpoint; resume from ack position | Create subscription group per run |
| No retry mechanism for failed projection updates | `nack(action="retry")` — server retries delivery | Handle nack in GSA |
| Only one GSA instance can read a stream | `DispatchToSingle` strategy allows failover consumer | Multiple GSA instances for HA |

**Proposed integration:**
```python
# At pipeline startup:
client.create_subscription_to_stream(
    group_name=f"gsa-{run_id}",
    stream_name=f"run-{run_id}",
    from_end=False,
    consumer_strategy="DispatchToSingle",
)

# In GSA event loop:
async with client.read_subscription_to_stream(
    group_name=f"gsa-{run_id}",
    stream_name=f"run-{run_id}",
) as subscription:
    async for event in subscription:
        try:
            _update_projections(event)
            await subscription.ack(event)
        except Exception:
            await subscription.nack(event, action="retry")
```

**Conflict with V7:** V7 says "no timeouts." Persistent subscriptions have `message_timeout` (default 30s). If the GSA doesn't ack within 30s, the server retries. This is server-side timeout, not code timeout.

**Resolution:** Set `message_timeout=None` or a very large value. The GSA acks after successful projection update. If the GSA hangs, the operator intervenes.

**Verdict: CONSIDER** — Powerful for HA and checkpointing, but adds server-side timeout semantics. Full replay on restart is acceptable for V7's scale. Persistent subscriptions shine in multi-consumer, high-availability scenarios that V7 may not need.

---

## E5. System Projections (`$et-`, `$ce-`) — Effect-Type Indexing

**Feature:** EventStoreDB system projections create link-event streams: `$et-EventType` contains all events of a given type, `$ce-Category` contains all events in a category. These are maintained automatically when `--run-projections=System` is enabled.

**Current V7 approach:** To find all `QueueJob` events across all runs, you must `read_all()` and filter client-side. Or maintain a separate index.

**What it does:** `$et-QueueJob` is a stream that contains link events pointing to every `QueueJob` event in the database. Reading `$et-QueueJob` with `resolve_links=True` gives you all `QueueJob` events without scanning unrelated events.

**Cross-cutting value:**

| Current V7 pain point | System projections solve | Integration cost |
|---|---|---|
| Provisioner needs all `QueueJob` events → must scan all streams | Read `$et-QueueJob` directly — only QueueJob events | Enable system projections at startup |
| Audit: "show me all PipelineAborted events" | Read `$et-PipelineAborted` | Zero — automatic |
| No cross-stream querying | `$ce-run` contains all events from all `run-*` streams | Stream naming convention |

**Proposed integration:**
```python
# Provisioner reads only QueueJob events:
async with client.subscribe_to_stream(
    stream_name="$et-QueueJob",
    resolve_links=True,
    from_end=False,
) as subscription:
    async for event in subscription:
        _handle_queue_job(event)
```

**Verdict: ADOPT** — Enable `--run-projections=System` at EventStoreDB startup. Zero code changes. Massive reduction in scan overhead for the Provisioner and any audit queries.

---

## E6. Secondary Indexes (`read_index`) — Direct Event-Type Lookup

**Feature:** KurrentDB 25.1+ supports `read_index(index_name="et-EventType")` for direct event-type lookups without projections.

**Current V7 approach:** Same as E5 — scan and filter.

**What it does:** Secondary indexes are built automatically for event types. `read_index(index_name="et-QueueJob")` returns all `QueueJob` events directly, without link-event resolution.

**Cross-cutting value:**

| Current V7 pain point | Secondary indexes solve | Integration cost |
|---|---|---|
| `$et-` streams require `resolve_links=True` (extra lookup) | `read_index` returns events directly | Requires KurrentDB 25.1+ |
| Link event resolution adds latency | Index is pre-resolved | None |

**Verdict: DEFER** — Requires KurrentDB 25.1+. System projections (`$et-`) in E5 work today. Upgrade to secondary indexes when upgrading EventStoreDB.

---

## E7. Multi-Append — Atomic Cross-Stream Writes

**Feature:** `multi_append_to_stream()` (KurrentDB 25.1+). Atomically appends events to multiple streams in one operation.

**Current V7 approach:** Each effect is appended to one stream (`run-{run_id}`). If multiple runs need coordination, multiple append calls.

**What it does:** Append `PipelineStarted` to `run-{run_id}` AND `BudgetSet` to `budget-{run_id}` atomically. Both succeed or both fail.

**Cross-cutting value:**

| Current V7 pain point | Multi-append solves | Integration cost |
|---|---|---|
| No atomic cross-stream operations | Atomic multi-stream append | Requires KurrentDB 25.1+ |
| Inconsistent state if one append fails | All-or-nothing semantics | Low |

**Verdict: DEFER** — V7 uses one stream per run (`run-{run_id}`). All effects for a run go to one stream. Multi-append is useful for cross-run coordination, which V7 doesn't do. Revisit if architecture ever splits effects across multiple streams.

---

## E8. Idempotent Appends — Built-In Deduplication

**Feature:** `append_to_stream()` with `current_version` + unique `event.id` (UUID) provides idempotent appends. Retrying a successful append does not create duplicates.

**Current V7 approach:** V7 already uses `effect_id` (UUIDv7) as the event ID. But does V7's `append_effect()` use `StreamState.ANY` or a specific `current_version`?

**What it does:** If `append_effect()` uses `current_version=StreamState.ANY` + unique `effect_id`, retries are idempotent. If it uses an integer position, retries may fail with `WrongCurrentVersion`.

**Cross-cutting value:**

| Current V7 pain point | Idempotent appends solve | Integration cost |
|---|---|---|
| Network retry could create duplicate effects | `StreamState.ANY` + UUID = idempotent | Verify `append_effect()` implementation |
| `WrongCurrentVersion` on concurrent writes | `StreamState.ANY` disables optimistic concurrency | May weaken consistency guarantees |

**Proposed integration:**
```python
# In event_store.py:
async def append_effect(run_id: str, effect: Effect) -> int:
    event = NewEvent(
        type=effect.kind,
        data=effect.model_dump_json().encode(),
        id=effect.effect_id,  # UUIDv7 — unique per effect
    )
    return client.append_to_stream(
        stream_name=f"run-{run_id}",
        current_version=StreamState.ANY,  # Idempotent, no concurrency check
        events=[event],
    )
```

**Trade-off:** `StreamState.ANY` disables optimistic concurrency control. Two agents appending simultaneously could interleave events. EventStoreDB guarantees ordering within a single append call, but not across concurrent appends.

**Alternative:** Use `current_version` = last known position. But this requires reading the stream before each append, adding latency and complexity.

**Verdict: ADOPT with awareness** — Use `StreamState.ANY` + UUIDv7 for idempotency. Event ordering within a stream is approximately chronological (append time). For V7's use case (agents take turns, not truly concurrent), this is sufficient. If true concurrent appends are needed, implement a single-writer queue.

---

## E9. Stream Deletion vs. Tombstoning — Run Cleanup

**Feature:** `delete_stream()` soft-deletes a stream (events are scavenged). `tombstone_stream()` hard-deletes a stream (stream name is permanently reserved, cannot be recreated).

**Current V7 approach:** No cleanup. Streams accumulate forever.

**What it does:** After a run completes and its artefacts are archived to B2, the `run-{run_id}` stream can be `delete_stream()`'d. Events are removed during the next scavenge. The stream name can be reused (unlike tombstoning).

**Cross-cutting value:**

| Current V7 pain point | Stream deletion solves | Integration cost |
|---|---|---|
| EventStoreDB disk grows forever | Delete completed run streams | Call `delete_stream()` on pipeline completion |
| No cleanup workflow | Explicit lifecycle: create → use → archive → delete | One line in Assembly Agent handler |

**Proposed integration:**
```python
# In Assembly Agent, after PipelineComplete:
client.delete_stream(
    stream_name=f"run-{run_id}",
    current_version=StreamState.ANY,
)
```

**Warning:** `delete_stream` is a soft delete. Events remain on disk until scavenge runs. Use `$maxCount` (E2) for automatic truncation, and `delete_stream` for explicit cleanup.

**Verdict: ADOPT** — One call at pipeline completion. Frees disk. Complements `$maxCount`.

---

## E10. Connection Resilience — `reconnect()` and Cluster Discovery

**Feature:** `esdbclient` supports `reconnect()` and automatic cluster node discovery. Handles node failures, leader election, and reconnects.

**Current V7 approach:** V7's EventStoreDB client wrapper likely has basic connection handling.

**What it does:** The client automatically discovers the cluster leader for writes. If the leader fails, it rediscovers and reconnects. `reconnect()` can be called manually after errors.

**Cross-cutting value:**

| Current V7 pain point | Connection resilience solves | Integration cost |
|---|---|---|
| EventStoreDB node failure crashes the pipeline | Auto-reconnect to new leader | Built into esdbclient |
| No handling of `LeaderNotFound` or `NodeIsNotLeader` | Client handles transparently | Zero |
| Single point of failure | Cluster mode with 3 nodes | Infrastructure change |

**Verdict: ADOPT** — Use `esdbclient`'s built-in cluster support. Configure 3-node cluster for production. No code changes needed; the client handles it.

---

## E11. Event Metadata — Correlation IDs and Custom Fields

**Feature:** `NewEvent(metadata=b'{"correlation_id": "...", "agent": "audio"}')`. EventStoreDB stores metadata alongside event data. Queryable, preserved in `RecordedEvent.metadata`.

**Current V7 approach:** V7 effects embed all fields in the payload (data). No separate metadata.

**What it does:** Move cross-cutting fields (`run_id`, `agent`, `timestamp`) to event metadata. Keep effect-specific fields in data. This separates "envelope" from "payload."

**Cross-cutting value:**

| Current V7 pain point | Event metadata solves | Integration cost |
|---|---|---|
| `run_id` and `agent` duplicated in every effect schema | Move to metadata; effect schema is pure domain | Refactor Effect base model |
| No fast filtering by agent | Server can't filter by data content, but metadata is inspectable | Minimal |
| Audit trail needs to know "who did what when" | Metadata has `recorded_at`, `agent`, `run_id` | Structured metadata |

**Proposed integration:**
```python
# Base Effect:
class Effect(BaseModel):
    effect_id: UUID = Field(default_factory=uuid7)
    kind: str = "effect"
    # run_id and agent removed from data; moved to metadata

# append_effect:
event = NewEvent(
    type=effect.kind,
    data=effect.model_dump_json(exclude={"run_id", "agent", "timestamp"}).encode(),
    metadata=json.dumps({
        "run_id": effect.run_id,
        "agent": effect.agent,
        "timestamp": effect.timestamp,
    }).encode(),
    id=effect.effect_id,
)
```

**Trade-off:** Reading events requires merging metadata + data to reconstruct the full effect. Adds one `json.loads()` per event.

**Verdict: CONSIDER** — Clean separation of concerns, but adds deserialization complexity. Current approach (all fields in data) is simpler. Metadata is more valuable if doing server-side routing or ACLs based on metadata fields.

---

## Summary Table — EventStoreDB Features

| # | Feature | Verdict | Complexity Delta | V7 Conflict? |
|---|---|---|---|---|
| E1 | Server-side filtering (`subscribe_to_all`) | **ADOPT** | Medium | None |
| E2 | Stream metadata `$maxCount` | **ADOPT** | Low | None |
| E3 | `include_caught_up` / `CaughtUp` signal | **ADOPT** | Zero | None |
| E4 | Persistent subscriptions | **CONSIDER** | Medium | Server-side `message_timeout` |
| E5 | System projections (`$et-`, `$ce-`) | **ADOPT** | Zero | None |
| E6 | Secondary indexes (`read_index`) | **DEFER** | Zero | Requires KurrentDB 25.1+ |
| E7 | Multi-append | **DEFER** | Low | Requires KurrentDB 25.1+ |
| E8 | Idempotent appends (`StreamState.ANY` + UUID) | **ADOPT** | Zero | Weakens optimistic concurrency |
| E9 | Stream deletion (`delete_stream`) | **ADOPT** | Low | None |
| E10 | Connection resilience / cluster | **ADOPT** | Zero | None |
| E11 | Event metadata separation | **CONSIDER** | Low | Adds deserialization step |

**Recommended immediate actions for EventStoreDB:**
1. **Adopt E1, E2, E3, E5, E8, E9, E10** — Configure at EventStoreDB startup or in `event_store.py`.
2. **Enable system projections:** `--run-projections=System` in Docker Compose.
3. **Set `$maxCount`** on stream creation.
4. **Use `StreamState.ANY` + UUIDv7** for idempotent appends.
5. **Delete streams** after pipeline completion.
6. **Consider E4** persistent subscriptions if HA/failover is needed.
7. **Defer E6, E7** until KurrentDB 25.1+ upgrade.

---

*EventStoreDB propositions — researched from KurrentDB docs (docs.kurrent.io), esdbclient 1.1.7 API, and EventStoreDB server documentation. Web search tools (Brave/Exa/Perplexity) unavailable; all research via direct documentation fetch and package introspection.*

---

## D1. ACP (Agent Client Protocol) — Dashboard & Operator Interface

**What it is.** ACP standardizes communication between clients (editors, dashboards, mobile apps) and AI agents. JSON-RPC over stdio (local) or WebSocket (remote). Features: streaming text, tool call visibility, model switching, permission prompts, session management, reasoning traces. Spec: agentclientprotocol.com. pydantic-deep docs describe `apps.acp.server` for Zed integration.

**Status in pydantic-deep 0.3.19:** `apps.acp` module is NOT present in the installed package. The documentation at vstorm-co.github.io describes it, but the actual code may be in a newer/unreleased version or a separate package. **This is a blocker for immediate adoption.**

**ACP UI Ecosystem (from research):**

| Project | Protocol | Type | Description |
|---|---|---|---|
| formulahendry/acp-ui | ACP | Cross-platform UI | Desktop + web + mobile client for any ACP agent (~300 stars, MIT) |
| formulahendry/vscode-acp | ACP | VS Code Extension | ACP client inside VS Code editor |
| OpenSource03/harnss | ACP | Desktop Client | Feature-rich ACP desktop UI with tools & terminal |
| RAIT-09/obsidian-agent-client | ACP | Obsidian Plugin | ACP chat inside Obsidian notes |
| Areo-Joe/chrome-acp | ACP | Chrome Extension | Browser-based ACP client / PWA |
| rebornix/Agmente | ACP | iOS App | iOS client for ACP coding agents |
| arafatamim/Ferngeist | ACP | Android App | Android ACP client + gateway |
| slopus/happy | ACP | Mobile/Web | iOS/Android/Web ACP client |
| olimorris/codecompanion.nvim | ACP | Neovim Plugin | Full ACP support in Neovim (6.6k stars) |
| formulahendry/wechat-acp | ACP | WeChat Bridge | Connect WeChat to any ACP agent |
| agentclientprotocol/agent-client-protocol | ACP | Core Protocol | Official ACP spec & reference |

**Cross-cutting value for V7:**

| Current V7 pain point | ACP solves | Integration cost |
|---|---|---|
| No dashboard for pipeline state | ACP UI (web/mobile/desktop) provides chat interface to query any agent | Near-zero if pydantic-deep ACP module is available |
| Operator must use raw HTTP/curl | Rich chat UI with session history, reasoning traces, tool call visualization | Enable ACP server |
| No visibility into agent reasoning | ACP streams reasoning in real-time | Built into protocol |
| No visibility into "tool calls" (GSA queries, subagent delegation) | ACP tool call events show every internal HTTP request and subagent invocation | Wrap internal calls in ACP tool events |
| No standardized client ecosystem | 10+ ACP clients across all platforms (web, desktop, mobile, editor plugins) | Standard protocol = any client works |

**How ACP maps to V7's activation cycle:**

ACP is conversational (chat/turn-based). V7 is single-shot (POST → response). But they can be unified:

```
ACP chat/turn → maps to → V7 POST /
  Operator: "What's the state of run X?"
  Agent: queries GSA → rebuilds projections → produces text answer
  ACP streams: ToolCallStart(get_state) → ToolCallResult → TextMessageContent → TextMessageEnd
  
ACP chat/turn → maps to → V7 agent activation with instruction
  Operator: "Skip scene 5"
  Agent: queries GSA → sees situation → produces text → parser extracts UpdateScript
  ACP streams: ToolCallStart(get_state) → ... → TextMessageContent → EffectExtracted(custom event)
```

**Key compatibility with V7 principles:**
- V7 Principle 8: "Agent memory does not persist." ✓ ACP turns are stateless — agent rebuilds from EventStoreDB on each turn.
- V7 HTTP surface: GET / and POST / remain primary. ACP is a secondary interface (WebSocket on same port, or separate process).
- EventStoreDB remains sole source of truth. ACP is observability + operator interaction, not state storage.

**Why ACP over AG-UI for dashboards:**
- ACP has a richer client ecosystem TODAY (acp-ui web/desktop/mobile, VS Code, Neovim, Obsidian, Chrome, iOS, Android)
- ACP is designed for agent interaction, not just rendering. The operator can actually SEND messages to the agent via ACP.
- AG-UI has better event types for observability, but fewer ready-made clients.
- ACP's `tool/invoke` maps naturally to subagent delegation and GSA queries.

**Blocker:**
`apps.acp` is NOT in pydantic-deep 0.3.19. Two paths forward:
1. **Wait for release:** ACP support may be in a newer pydantic-deep version or separate package.
2. **Build minimal ACP server:** Implement JSON-RPC over WebSocket manually (~200 lines) wrapping the existing agent logic.

**Verdict: ADOPT (pending package availability)** — ACP gives V7 a rich dashboard ecosystem for near-zero integration cost IF the pydantic-deep ACP module materializes. The operator gets a chat interface to every agent, with reasoning visibility and tool call traces. If `apps.acp` never ships, fall back to AG-UI (D2) or custom SSE (D4).

---

## D2. AG-UI (Agent-User Interaction Protocol) — Dashboard Integration

**What it is.** AG-UI is an open, lightweight, event-based protocol (13.9k stars) that standardizes how AI agents connect to user-facing applications. ~16 event types: lifecycle (`RunStarted`, `RunFinished`, `RunError`), text streaming (`TextMessageStart`, `Content`, `End`), tool calls (`ToolCallStart`, `Args`, `End`, `Result`), state management (`StateSnapshot`, `StateDelta`, `MessagesSnapshot`), reasoning, activity, custom events. Transport: SSE, WebSocket, webhooks. Python SDK: `ag_ui.core` + `ag_ui.encoder`. FastAPI server pattern: `POST /` returns `StreamingResponse` with SSE events.

**Cross-cutting value for V7:**

| Current V7 pain point | AG-UI solves | Integration cost |
|---|---|---|
| No dashboard for pipeline state | Any AG-UI client (CopilotKit, custom React) renders agent state | Add AG-UI event emission to agent handlers |
| Agent reasoning is invisible | `ReasoningStart`/`ReasoningMessageContent`/`ReasoningEnd` streams agent chain-of-thought | Emit reasoning events during LLM call |
| State changes only visible via GSA `GET /` | `StateSnapshot` + `StateDelta` (JSON Patch) push state to dashboard in real-time | Emit state events after projection update |
| No visibility into subagent delegation | `StepStarted`/`StepFinished` around subagent `task()` calls | Wrap subagent calls in step events |
| Effect extraction is invisible | Custom event: `EffectExtracted` with parsed effects | Emit after parser succeeds |
| No tool call visibility (V7 agents don't have tools) | V7's "tools" are HTTP calls to GSA/Provisioner. Emit `ToolCallStart` around `httpx.get/post` | Wrap HTTP calls in tool call events |
| No live graph of agent interactions | AG-UI clients (CopilotKit, custom) can render multi-agent graphs from event stream | Dashboard-side rendering |

**How AG-UI fits V7's architecture:**

AG-UI's server pattern is `POST /` returning SSE. **V7 already has `POST /`.** The two can be unified:

```python
# Content negotiation in agent POST / handler:
@app.post("/")
async def agent_post(request: Request, payload: AgentPayload):
    accept = request.headers.get("accept", "application/json")
    
    if accept == "text/event-stream":
        # AG-UI mode: stream events
        return StreamingResponse(
            _agui_event_generator(payload),
            media_type="text/event-stream"
        )
    else:
        # V7 mode: return JSON with extracted effects
        effects = await _handle_agent_turn(payload)
        return JSONResponse({"effects": effects})
```

This preserves V7's HTTP surface (only GET / and POST /) while adding dashboard capability via content negotiation.

**AG-UI event mapping for V7:**

| V7 concept | AG-UI event | When emitted |
|---|---|---|
| Agent receives POST / | `RunStarted` | Start of handler |
| Agent queries GSA | `ToolCallStart` (tool="get_state") | Before `httpx.get` |
| GSA responds | `ToolCallResult` | After GSA response |
| Agent runs LLM | `StepStarted` (step="llm_reasoning") | Before `agent.run()` |
| LLM produces text chunk | `TextMessageContent` | Each streaming chunk |
| LLM reasoning visible | `ReasoningMessageContent` | From `agent.iter()` nodes |
| Subagent invoked | `StepStarted` (step="subagent:{name}") | Before `task()` |
| Subagent returns | `StepFinished` | After subagent output |
| Parser extracts effects | Custom `EffectExtracted` | After `_parse_effects()` |
| Effects appended to EventStoreDB | `StateDelta` on projection state | After projection update |
| Agent returns | `RunFinished` | End of handler |
| Error occurs | `RunError` | On exception |

**Why this does NOT violate V7 principles:**
1. Same HTTP surface: `GET /` and `POST /` only
2. Content negotiation (`Accept` header) is HTTP-standard, not a new endpoint
3. Agent still produces text; parser still extracts effects
4. EventStoreDB is still sole source of truth
5. AG-UI events are observability-only; they don't change agent behavior

**Complexity analysis:**
- **Lines added:** ~30 lines per agent for AG-UI event emission
- **Lines removed:** Zero. V7 mode still works unchanged.
- **New dependency:** `ag_ui.core` + `ag_ui.encoder` Python packages
- **Dashboard:** Any AG-UI-compatible client (CopilotKit React components, custom web app)

**Verdict: ADOPT** — AG-UI fits V7's HTTP architecture perfectly. Content negotiation on `POST /` gives both V7 JSON responses and AG-UI SSE streams. Massive dashboard capability for minimal code.

---

## D3. BeeAI Platform / AgentStack — Multi-Agent Dashboard

**What it is.** IBM's open-source platform (Linux Foundation) for deploying agents as services. Built on A2A protocol. Features: instant web UI (`agentstack ui`), trajectory logging, multi-agent visualization, citation tracking. `agentstack` CLI for local deployment. Kubernetes Helm chart for production.

**Cross-cutting value for V7:**

| Current V7 pain point | BeeAI solves | Integration cost |
|---|---|---|
| No web UI for pipeline | `agentstack ui` generates full web UI automatically | Agent must be A2A-compatible |
| No multi-agent graph visualization | Trajectory logging shows agent interactions as graphs | A2A protocol wrapper |
| No citation/explanation tracking | Built-in citation extraction and formatting | Automatic |

**Critical conflict with V7 architecture:**
BeeAI / AgentStack uses the **A2A (Agent-to-Agent) protocol**, not HTTP GET/POST. A2A agents expose capability cards, task scheduling, and artifact exchange. This is a fundamentally different communication model from V7's HTTP request/response.

V7's agents communicate via HTTP POST with natural language text. A2A agents communicate via structured task requests with typed inputs/outputs. Converting V7 to A2A would require:
- Replacing `POST /` with A2A task endpoints
- Replacing natural language text with structured task parameters
- Adding capability discovery (what can this agent do?)
- Adding artifact exchange protocol

This would gut V7's core design: "agents produce text; parser extracts effects."

**Verdict: REJECT for V7** — BeeAI/A2A assumes structured task-based agents. V7 agents are text-producing HTTP services. The protocols are incompatible without redesigning the entire architecture.

---

## D4. Custom Dashboard via GSA + Server-Sent Events

**What it is.** Instead of adopting a full protocol like AG-UI or ACP, build a minimal custom dashboard that connects to the GSA's `GET /` and each agent's `POST /` (with `Accept: text/event-stream`).

**Cross-cutting value for V7:**

| Current V7 pain point | Custom dashboard solves | Integration cost |
|---|---|---|
| No visibility into pipeline | Web page polls GSA `GET /` every second | Simple HTML/JS page |
| No agent reasoning visibility | Agents emit `text/event-stream` with reasoning | Server-Sent Events |
| No event graph | JavaScript renders EventStoreDB events as a timeline | Client-side rendering |

**Implementation:**
```python
# In global_state_agent.py, add SSE endpoint (content negotiation on GET /):
@app.get("/")
async def gsa_get(run_id: str, request: Request):
    accept = request.headers.get("accept", "application/json")
    if accept == "text/event-stream":
        async def event_stream():
            while True:
                state = _get_projections(run_id)
                yield f"data: {state.model_dump_json()}\n\n"
                await asyncio.sleep(1)  # Poll interval
        return StreamingResponse(event_stream(), media_type="text/event-stream")
    else:
        return JSONResponse(_get_projections(run_id).model_dump())
```

**Why this is attractive:**
- Zero new dependencies (no AG-UI SDK, no ACP)
- Works with any browser
- Full control over event format
- GSA already has the data

**Why AG-UI is still better:**
- Standardized clients (CopilotKit) work out of the box
- Ecosystem of pre-built components (chat UI, tool call renderers, state viewers)
- Interoperability: any AG-UI dashboard works with any AG-UI agent
- Custom dashboard = custom code forever

**Verdict: CONSIDER as fallback** — If AG-UI adoption proves too complex, a custom SSE dashboard on GSA is a viable minimal alternative. But AG-UI gives more capability for similar effort.

---

## Dashboard Protocol Summary

| Protocol | Verdict | Fit for V7 | Reason |
|---|---|---|---|
| ACP | **REJECT** | Poor | Editor-centric (Zed/VS Code). V7 agents are pipeline services, not coding assistants. |
| AG-UI | **ADOPT** | Excellent | Frontend-centric, SSE over POST /, content negotiation preserves V7 endpoints, rich ecosystem (CopilotKit), maps naturally to V7 lifecycle. |
| BeeAI / A2A | **REJECT** | None | A2A protocol replaces V7's text-based HTTP model. Requires full architectural redesign. |
| Custom SSE | **CONSIDER** | Good | Zero dependencies, full control. But misses ecosystem benefits of AG-UI. |

**Recommended approach:**
1. **Adopt AG-UI** as the dashboard protocol for V7 agents.
2. Implement content negotiation on `POST /`: `Accept: application/json` → V7 mode; `Accept: text/event-stream` → AG-UI mode.
3. Emit AG-UI events during agent handler: `RunStarted`, `StepStarted` (subagent), `TextMessageContent` (LLM output), `ReasoningMessageContent` (chain-of-thought), `ToolCallStart/Result` (GSA/Provisioner HTTP calls), custom `EffectExtracted`, `StateSnapshot`/`StateDelta`, `RunFinished`/`RunError`.
4. Use CopilotKit React components or build a custom AG-UI client for the dashboard.
5. GSA also supports AG-UI via content negotiation on `GET /`.

---

*Dashboard protocol propositions — researched from ACP spec (agentclientprotocol.com), AG-UI docs (docs.ag-ui.com), pydantic-deep ACP docs, acp-ui GitHub, ag-ui-protocol GitHub, BeeAI/AgentStack GitHub. Web search tools (Brave/Exa/Perplexity) unavailable; all research via direct documentation fetch.*
