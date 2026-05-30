---
{
  "title": "Agent Architecture \u2014 pydantic-deep",
  "section": "8",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[07 - Agent Environment & Tools|Agent Environment & Tools]] | [[00 - Index|Index]] | [[09 - Agents Per-Agent Implementations|Agents — Per-Agent Implementations]] ->

# Agent Architecture — pydantic-deep


All agents share a common infrastructure built on **pydantic-deep** (the real package, `pip install pydantic-deep`). Each agent is a **main agent** (not a subagent) created via `create_deep_agent()` with context compaction hooks. They differ only in role, permitted effects, and the focus function used for compaction.

In V7, each agent is wrapped in a lightweight **FastAPI ASGI application** that exposes `GET /` and `POST /`. The FastAPI handler:
1. Receives an instruction payload on POST /
2. Replays events from the SQLite event store to reconstruct projections
3. Builds the situation narrative and memory
4. Calls `await agent.run(user_prompt=..., deps=DeepAgentDeps(...))`
5. Parses effects from the agent output
6. Appends effects to the SQLite event store
7. Returns the extracted effects (or a 202 Accepted) to the caller
8. No wake notifications — agents are autonomous (see §8.7)

### 8.1 pydantic-deep Layer Stack (V7.1 Corrected)

pydantic-deep provides a layered context management system. We use all layers, configured for pipeline agents:

```
Message history (situation narratives + memory + prior turns)
│
▼
┌─────────────────────────────┐
│ ProvenanceCapability          │ Execution DAG, citation tracking, verification
│ (pydantic-ai-provenance)      │
├─────────────────────────────┤
│ EvictionCapability            │ Saves large bash outputs (>20K tokens) to files
│ (default: on)                 │
├─────────────────────────────┤
│ SlidingWindowProcessor        │ Hard fallback: trims oldest messages if compaction
│ (trigger: fraction 0.95)      │ capability fails
├─────────────────────────────┤
│ on_before_compress callback   │ OUR HOOK: queries OTIO, determines focus,
│ (direct param, not capability)│ LLM-compacts history preserving task context
├─────────────────────────────┤
│ ContextManagerCapability      │ Token tracking + auto-trigger at 90% threshold
│ (default: on)                 │
├─────────────────────────────┤
│ CostTracking                  │ Budget enforcement + token/cost tracking
│ (pydantic-ai-shields)         │
└─────────────────────────────┘
│
▼
Model request
```

| Layer | Source | Config | Purpose |
|---|---|---|---|
| `ProvenanceCapability` | `pydantic-ai-provenance` | `agent_name=role, source_tools=["bash_command"]` | Execution DAG + causal logging |
| `EvictionCapability` | pydantic-deep | `eviction_token_limit=20_000` | Bash outputs, ffprobe results, WhisperX JSON — saved to `/tmp/` |
| `SlidingWindowProcessor` | pydantic-deep | `trigger=("fraction", 0.95), keep=("fraction", 0.5)` | Last-resort hard trim; never splits causal pairs |
| `on_before_compress` | **Our callback** | Direct param to `create_deep_agent()` | Queries OTIO projection → determines focus → LLM compacts |
| `ContextManagerCapability` | `pydantic-ai-summarization` | `context_manager_max_tokens=128_000` | Tracks tokens, triggers auto-compression at 90% |
| `CostTracking` | `pydantic-ai-shields` | `budget_usd=10.0` | Token/cost tracking + budget enforcement |

**V7.1 correction:** `on_before_compress` is a **direct callback parameter** to `create_deep_agent()`, not part of `HooksCapability`. `ProvenanceCapability` comes from `pydantic-ai-provenance` (external package). There are no other tools; `bash_command` is the only tool exposed to agents.

### 8.2 Agent Construction (V7.1 Corrected)

```python
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai_provenance.capability import ProvenanceCapability
from pydantic_ai_summarization import ContextManagerCapability, create_sliding_window_processor
from pydantic_ai_shields import CostTracking
from pydantic_deep import create_deep_agent


# V7.1 fix: DeepAgentDeps is pydantic-deep's built-in deps protocol.
# PipelineDeps extends it with pipeline-specific fields.
# DeepAgentDeps is imported from pydantic_deep; no custom definition needed.
@dataclass
class PipelineDeps:
    """Dependencies for pipeline agents.

    Passed to agent.run(deps=...) on every turn. Carries GSA URL
    for self-sufficient compaction, agent role, and token limits.
    """
    gsa_url: str = "http://gsa:8000"
    agent_role: str = ""
    max_tokens: int = 128_000
    compaction_model: str = "deepseek-v4-flash"


class PeriodicReminderConfig(BaseModel):
    """V7.1: Config for pydantic-deep periodic reminder injection."""
    every_n_turns: int = 10
    first_after: int = 5


async def llm_complete(system: str, user: str, model: str) -> str:
    """V7.1: Minimal LLM completion helper used by compaction hook.

    Calls the specified model with system/user prompts. In production,
    this wraps the same API client used by pydantic-deep agents.
    """
    # Implementation wraps openrouter/deepseek API
    # (exact implementation depends on pydantic-ai model configuration)
    from pydantic_ai import Agent
    agent = Agent(model, system_prompt=system)
    result = await agent.run(user)
    return result.output


def _check_budget_and_append(cost_data: dict, store: EventStore, run_id: str) -> None:
    """V7.1: Check if budget exceeded and append BudgetExceeded effect.

    Called by handler after agent turn completes. Not called by CostTracking
    directly — the handler reads cost_data from the agent result and appends
    if needed.
    """
    budget = cost_data.get("budget_usd", 0)
    spent = cost_data.get("total_cost_usd", 0)
    if spent > budget:
        from effects import BudgetExceeded
        effect = BudgetExceeded(
            total_cost_usd=spent,
            budget_usd=budget,
            reason=f"Budget exceeded: ${spent:.2f} > ${budget:.2f}",
        )
        store.append(run_id, effect)


def _render_messages(messages: list) -> str:
    """Flatten message history to a single string for compaction LLM.

    V7.1: Defined here -- was referenced but never shown.
    """
    parts = []
    for m in messages:
        if hasattr(m, "content"):
            parts.append(str(m.content))
        else:
            parts.append(str(m))
    return "\n\n".join(parts)


async def otio_aware_compress(ctx, messages, **kwargs):
    """Hook called by ContextManagerCapability before compression.

    Self-sufficient: curls GSA for current state, determines focus,
    protects BASE KNOWLEDGE and SKILL CATALOG sections, then compacts
    the rest via LLM.
    """
    import httpx, json

    # 1. Curl GSA for current state
    try:
        resp = await httpx.get(ctx.deps.gsa_url)
        state = resp.json()
    except Exception:
        state = {}

    # 2. Determine focus from state
    focus = _determine_focus(ctx.deps.agent_role, state)

    # 3. Split protected sections from compressible content
    protected = []
    compressible = []
    for m in messages:
        content = str(m.content) if hasattr(m, "content") else str(m)
        if "=== BASE KNOWLEDGE (NEVER FORGET) ===" in content:
            protected.append(m)
        elif "=== SKILL CATALOG ===" in content:
            protected.append(m)
        else:
            compressible.append(m)

    if not compressible:
        return messages

    # 4. Compact compressible content only
    flat = _render_messages(compressible)
    system = (
        f"Compress this agent context. Preserve everything related to: {focus}. "
        f"Keep all IDs, numbers, durations, verdicts, failure reasons. "
        f"Remove redundant pleasantries, old success details, clean blocks. "
        f"Output ONLY compressed context."
    )
    compressed = await llm_complete(
        system=system, user=flat, model=ctx.deps.compaction_model
    )

    return protected + [SystemMessage(content="[Compacted]"), UserMessage(content=compressed)]


def create_pipeline_agent(role: str, config: Config):
    """Factory: create pydantic-deep agent with pipeline configuration.

    Disables pydantic-deep defaults that conflict with pipeline needs,
    then adds capabilities from the ecosystem.
    """

    provenance = ProvenanceCapability(
        agent_name=role,
        source_tools=["bash_command"],
    )

    agent = create_deep_agent(
        model=config.agent_models[role],
        instructions=ROLE_INSTRUCTIONS[role],
        on_before_compress=otio_aware_compress,
        history_processors=[
            create_sliding_window_processor(
                trigger=("messages", 100),
                keep=("messages", 50),
                max_input_tokens=config.max_tokens,
            ),
        ],
        eviction_token_limit=None,
        context_manager=True,
        context_manager_max_tokens=config.context_manager_max_tokens,
        include_todo=False,
        include_filesystem=False,
        include_plan=False,
        include_memory=False,
        include_checkpoints=False,
        web_search=False,
        thinking=True,
        cost_tracking=True,
        cost_budget_usd=config.max_run_budget_usd,
        # Budget checking is done by handler post-turn (§9)
        # on_cost_update removed — handler calls _check_budget_and_append directly
        stuck_loop_detection=True,
        periodic_reminder=PeriodicReminderConfig(every_n_turns=10, first_after=5),
        capabilities=[
            provenance,
            ContextManagerCapability(
                max_tokens=config.context_manager_max_tokens,
            ),
            CostTracking(budget_usd=config.max_run_budget_usd),
        ],
        deps_type=PipelineDeps,
    )
    return agent
```

**V7.1 corrections applied:**
- `ProvenanceCapability` imported from `pydantic-ai-provenance` (external package)
- `ContextManagerCapability` imported from `pydantic-ai-summarization` (installed)
- `CostTracking` imported from `pydantic-ai-shields` (installed)
- `PipelineDeps` defined as dataclass extending `DeepAgentDeps`
- `on_before_compress` noted as direct parameter, not capability
- No subagents, no todo tools, no tool guard — `bash_command` is the only tool

### 8.3 Prompt Construction

The handler builds the agent's **initial user prompt**. The system prompt
(`instructions` passed to `create_deep_agent`) contains role, base knowledge,
skill catalog, communication style, and workflow. The handler appends:

- Current context (run ID, GSA URL, available skill filenames)
- Recent history (last 5 effects by this agent from SQLite)

The agent is autonomous. It curls the GSA when it needs state. It reads skills
via `bash_command` when it needs knowledge. See §9.0 for the full handler code.

### 8.4 Context Compaction (Agent-Internal, via Callback)

**No caller-side compaction.** The POST / handler passes the full narrative. The agent's `on_before_compress` **callback parameter** (passed directly to `create_deep_agent()`, not part of any capability) handles compaction internally:

1. **ContextManagerCapability** counts tokens before each model call
2. If > 90% of budget, it calls `on_before_compress` callback
3. The callback queries OTIO via `ctx.deps.projections`
4. Determines focus (e.g., "block A1:3:2 reconciliation, attempt 2/3")
5. Calls compaction LLM with focus-prompt
6. Replaces message history with compressed version + focus marker
7. If compaction fails, `SlidingWindowProcessor` hard-trims oldest messages (never splitting causal pairs)

**V7.1 correction:** In V7 this was misattributed to `HooksCapability.on_before_compress`. `HooksCapability` provides decorator-style hooks for tool lifecycle events (`on_before_tool_call`, `on_after_tool_call`, etc.), NOT context compaction. The compaction callback is a **direct parameter** to `create_deep_agent()`.

**Causal pair preservation:** The `SlidingWindowProcessor` uses a "safe cutoff" algorithm that walks backward from the cutoff point to find the nearest point between complete effect pairs. A pair is:
- `QueueJob` → `JobCompleted`/`JobFailed`
- `MeasurementRequested` → `AudioMeasured`
- `ScriptGenerated` → any effect referencing that script

### 8.5 How the Hook Queries OTIO

```python
def _determine_focus(role: str, state: dict) -> str:
    """Read GSA state to determine what the agent is working on."""
    timeline = state.get("timeline", {})
    jobs = state.get("jobs", {})

    if role == "audio":
        slots = timeline.get("slots", {})
        dirty = [addr for addr, slot in slots.items() if slot.get("status") == "scripted"]
        if dirty:
            addr = dirty[0]
            slot = slots[addr]
            attempts = jobs.get("block_attempts", {}).get(addr, 0)
            return (
                f"audio reconciliation of block {addr}, "
                f"attempt {attempts}/5, "
                f"measured {slot.get('measured_sec')}s vs target {slot.get('scripted_sec')}s"
            )
        return "audio pipeline — all blocks clean, awaiting instructions"

    if role == "video":
        pending = [j for j in jobs.get("jobs", {}).values()
                   if j.get("status") in ("pending", "running") and j.get("job_type") == "ltx"]
        if pending:
            return f"video generation for {len(pending)} pending LTX jobs"
        return "video pipeline — awaiting approved audio"

    if role == "scenario":
        slots = timeline.get("slots", {})
        unfilled = [addr for addr, slot in slots.items() if slot.get("status") == "scripted"]
        if unfilled:
            return f"script writing: {len(unfilled)} unfilled slots"
        return "script refinement — all slots filled"

    if role == "assembly":
        return "final assembly — merging approved clips"

    if role == "provisioner":
        pending = [j for j in jobs.get("jobs", {}).values()
                   if j.get("status") == "pending"]
        if pending:
            return f"provisioning for {len(pending)} pending jobs"
        return "provisioner — no pending jobs"

    return f"{role} agent — no active task"
```

---

### 8.6 Handler Helpers (V7.1 fix — defined here, referenced in [[09 - Agents Per-Agent Implementations|§9]])

These helper functions are used by all agent handlers. They were referenced
but never defined in V7.

```python
def hash_otio(otio_projection) -> str:
    """Compute a deterministic hash of OTIO state for EventRecord.

    Used to detect concurrent modifications: if two handlers append
    with different otio_hash_before values, the second may be stale.
    """
    import hashlib, json
    # Serialize slot states in deterministic order
    slots = sorted(otio_projection.slots.items()) if otio_projection else []
    payload = json.dumps(slots, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# V7.1: rebuild_projections() REMOVED from agent handlers.
# Agents do NOT touch the event store. They query the GSA via GET /.
# The GSA is the sole component that reads the SQLite store.
# See query_gsa() above.


async def query_gsa(gsa_url: str = "http://gsa:8000") -> dict[str, Any]:
    """Fetch current projection bundle from the Global State Agent.

    V7.1: Agents NEVER read the event store directly. All state comes
    from the GSA via GET /. No query params — GSA is run-scoped.
    This is absolute — no exceptions.
    """
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{gsa_url}/")
        resp.raise_for_status()
        return resp.json()


# V7.1: notify_downstream() DELETED. There is no "wake" concept.
# Agents are autonomous loops. They probe their environment (the GSA)
# and act when they see work. No orchestration, no wake notifications.
# See §8.7 for the autonomous agent model.
```

---


### 8.7 Autonomous Agent Model (V7.1: No Wake Notifications)

**There is no "wake" concept.** Agents do not notify each other. Agents do not sleep and wait to be woken. Agents are autonomous processes that continuously probe their environment and act when they detect work.

#### The Problem with Wake Notifications

The previous design had agents POST "wake" notifications to each other after appending effects. This created three failure modes:

1. **Lost wakes:** Network blip, receiver down, or connection refused → wake is lost forever
2. **Stale GSA:** Receiver queries GSA before the sender's effect is visible → receiver sees no work, goes idle
3. **Cascade stalls:** One lost wake stalls the entire pipeline permanently with no recovery mechanism

All three are symptoms of the same architectural mistake: **treating agents as passive servants that must be told to work.**

#### The Autonomous Model

Agents are active. Each agent runs an internal loop:

```
while agent_is_running:
    state = bash_command("curl -s http://gsa:8000/")
    if work_detected(state, my_role):
        text = run_one_turn(agent, state)
        effects = parse_agent_text_multi(my_role, text)
        for effect in effects:
            append_to_store(run_id, effect)
    sleep(poll_interval)
```

**Key properties:**
- **No orchestration:** Agents do not coordinate. They read state, decide, act.
- **Self-correcting:** If an agent misses a state change (network blip, GSA lag), it will detect it on the next poll.
- **No central scheduler:** No watcher, no dispatcher, no cron. Each agent is responsible for its own timing.
- **POST / is for intervention only:** The POST endpoint is for external operators to send instructions (`HumanInstruction`), not for agent-to-agent communication.

#### Why This Works

The Scenario Agent writes `UpdateScript`. The Audio Agent doesn't need to be told — it polls the GSA, sees dirty audio blocks, and queues TTS jobs. The Video Agent polls, sees clean audio + unfilled video slots, and queues LTX jobs. No messages between agents. No dependency on delivery.

#### Poll Interval

| Agent | Default Interval | Rationale |
|---|---|---|
| Scenario | 30s | Script writing is human-paced; frequent polling wastes tokens |
| Audio | 10s | Audio reconciliation is I/O bound (VM worker latency dominates) |
| Video | 10s | Video generation is I/O bound |
| Assembly | 30s | Final assembly runs once per pipeline |
| Provisioner | 5s | VM provisioning needs responsive VM state tracking |

Agents can be triggered manually via POST / for operator intervention, but normal operation is autonomous polling.

#### ABSOLUTE RULE: Agents Never Touch Global State

Agents read state **only** from the GSA via `GET /`. Agents write effects **only** via their handler appending to the SQLite store. Agents never:
- Read the SQLite event store directly
- Maintain shared memory or caches
- Access projection objects
- Inspect each other's state

The GSA is the sole reader of the event store. The agent handlers are the sole writers. This boundary is absolute.

---

