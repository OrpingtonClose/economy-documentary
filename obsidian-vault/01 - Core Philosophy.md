---
{
  "title": "Core Philosophy",
  "section": "1",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

[[00 - Index|Index]] | [[02 - System Topology|System Topology]] ->

# Core Philosophy


Six foundational commitments govern the pipeline. The hard principles in §1.10 enumerate every invariant and its enforcement mechanism.

### 1.1 Event Log as Sole Source of Truth

#### 1.1.1 All state derived from events; replay reconstructs everything

Every fact is an **Effect** — a typed Pydantic model — appended to an append-only SQLite event log. EventStoreDB is the future scalability path for distributed deployments. The OTIO timeline, job queue, VM inventory, and pipeline phase are **projections**: read models rebuilt by pure fold functions. Replay from sequence `0` reconstructs everything exactly.

#### 1.1.2 Event store is only persistent storage; all other state is ephemeral projection

SQLite event files (e.g., `events_{run_id}.db`) are the sole durable storage. EventStoreDB streams are the future scalability path. Agents hold no session state. VM workers are ephemeral. Projections are in-memory folds rebuilt from the event log on every GSA activation.

### 1.2 Effects as Only Legal Mutations

#### 1.2.1 Typed Pydantic models; parser extracts from agent text

A **category-conditioned parser** (§9.6) extracts Effects from agent text using `instructor` + `deepseek-v4-flash`. Every Effect carries `kind: Literal[...]`, `run_id: str`, `effect_id: UUID` (UUIDv7 — §3.1), `agent: str`, and `timestamp: datetime`. Invalid payloads are rejected before reaching the event store.

#### 1.2.2 No direct state mutation outside event store append

All state changes enter through SQLite event store append. Agents do not call projection methods. Projections are read-only consumers.

### 1.3 No State Machine — Prompt-Based Rules

**No state machine.** Pipeline "state" is emergent from projection state (e.g., "all audio blocks clean" emerges from Timeline, not from a state variable). Agents read projection-derived narratives and decide what to do. Rules live in the agent's system prompt, not in code.

Prioritization, filtering, and response selection happen **inside the agent via prompt instruction**. The agent's system prompt contains the rules for what to prioritize and how to respond.

This follows the principle: *whenever something can be done via prompt, do so — cut code complexity.*

### 1.4 No Timeouts in Code

#### 1.4.1 No setTimeout, threading.Timer, or asyncio.timeout anywhere in pipeline code

No pipeline code calls `setTimeout`, `threading.Timer`, `asyncio.timeout`, or any timer primitive. HTTP requests and subprocess calls run to completion. This is architecture policy.

### 1.5 Real Engines Only

#### 1.5.1 Qwen3-TTS, LTX-2.3, DeepSeek API; no simulation layers

TTS uses **Qwen3-TTS** on GPU VMs. Video uses **LTX-2.3**. Agent LLM inference uses **DeepSeek API** (`deepseek-v4-flash`). No mocks, no stubs. Unavailable engines trigger `ClarificationRequest`.

### 1.6 Never Regex

#### 1.6.1 Category-conditioned extraction via instructor + deepseek-v4-flash

No regex extracts structured data from agent output. The parser uses the agent's current role to determine valid Effect subtypes and constrains the LLM to schema-compliant JSON via `instructor`. If extraction fails, the prompt is adjusted — the schema is not weakened.

### 1.7 Natural Language Only — Agents Never Emit Structured Output

#### 1.7.1 Agents write free-form prose; ALL extraction complexity lives in the parser

**This is an absolute, non-negotiable principle.** Agents produce natural language text and nothing else. They do not emit `EFFECT:` markers, JSON, XML, labeled sections, or any structured format. They do not know the parser exists. The parser is a post-processing step that extracts structured effects from genuinely free-form prose.

**Complexity belongs in the parser.** The parser is expected to be very complex — semantic understanding, context awareness, category-conditioned extraction, discriminated unions, field validators, reasking logic. This complexity is deliberate and welcome. What is forbidden is pushing any of this complexity onto the agent by requiring structured output.

**Enforcement:**
- Agent system prompts never mention effect types, `EFFECT:` markers, JSON schemas, or section labels
- Parser system prompts contain all effect definitions, extraction rules, and validation logic
- If extraction is hard, the parser system prompt is expanded — the agent prompt is never modified to make parsing easier
- Phase-based parsing with fast deterministic paths is prohibited; all extraction is semantic via instructor

**Rationale:** Structured output from agents leaks architecture details into agent behavior. It couples agent prompts to parser implementation. It prevents agents from being replaced or upgraded independently. Natural language is the only stable, future-proof interface.

### 1.8 Situation-Driven Agent Tasking

Agents query the **Global State Agent** via `GET /` frequently. They receive the complete projection bundle (OTIO, Job, VM, State, Budget) as a Pydantic model. They scan this state and decide what to do. Their system prompt contains situation-type guidance and prioritization rules. Agents do not read the event store directly.

### 1.9 pydantic-deep

Agents use **pydantic-deep** (built on pydantic-ai). Context compaction is implemented as a **pre-processing step** before `agent.run()`. The agent's `message_history` is compacted by querying the OTIO projection to determine the agent's current task/focus, then calling a compaction LLM that preserves task-relevant details. Token management is handled by the pydantic-deep `ContextManagerCapability`.

**Why pre-processing, not watcher-side compaction:** Token management is an agent-internal concern. pydantic-deep provides the hook infrastructure via `on_before_compress`; we provide the OTIO-aware compaction logic.

### 1.10 Principles at a Glance

#### 1.10.1 Table of 12 hard principles with enforcement mechanism per principle

| # | Principle | Enforcement | V6→V7 Change |
|---|---|---|---|
| 1 | **Event log is sole source of truth** | All state derived from events. No hidden state. No projection writes independently. | SQLite replaces SQLite; ESDB for distributed |
| 2 | **Effects are only legal mutations** | Only Pydantic models enter event store. Parser validates against `EffectUnion`. | None |
| 3 | **No state machine — prompt-based rules** | Prioritization lives in agent system prompts. Agents scan projections and decide. | None |
| 4 | **No timeouts in code** | No `setTimeout`, `threading.Timer`, `asyncio.timeout`, or self-destruct logic in pipeline code. No timeout effects. Operator intervenes on hung jobs. | None |
| 5 | **Real engines only** | Qwen3-TTS, LTX-2.3, DeepSeek API. No mocks, no stubs, no simulation. | None |
| 6 | **Never regex** | Category-conditioned extraction via `instructor` + `deepseek-v4-flash`. | None |
| 7 | **Natural language only** | Agents write free-form prose. No structured output, no markers, no JSON, no section labels. ALL extraction complexity lives in the semantic parser. | **NEW** — eliminates Phase 1/2 fast paths |
| 8 | **Provisioner is an agent** | LLM agent with bash_command as its only tool. Provisions VMs, dispatches jobs, learns from failures. Most intelligence-requiring component. | Agent with tool use; reads GSA like all agents |
| 9 | **Agent memory does not persist in process** | Each turn rebuilt from projection summaries + bounded message history (last 5 turns). No session state in the agent process between POSTs. | None |
| 10 | **No automatic stale-state detection** | Operator monitors via `GET /` on agents and intervenes manually. No VM-side timers. | Removed TimeoutObserved; operator owns intervention |
| 11 | **Serialized per run, concurrent across runs** | Agent handlers use per-run_id locks. DB file access is serialized per run_id by asyncio.Lock. | §5.6 |
| 12 | **Tick-driven** | Agents are HTTP services; they autonomous polling of GSA. EventStoreDB provides native push subscriptions for distributed deployments. No central watcher loop. | **Watcher removed** |


---

