> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture V7 — Documentary Pipeline

> **Date:** 2026-05-27
> **Status:** LOCKED — Agent HTTP endpoints, EventStoreDB, no watcher, prompt-based rules, pydantic-deep, emergent phases
> **Replaces:** ARCHITECTURE_V6.md
> **Location:** `server/v7/`
>
> This document is the canonical V7 architecture. Pipeline phases are emergent, not enforced. Agents are HTTP services with `GET /` and `POST /`. The watcher has been removed; agents communicate via HTTP and EventStoreDB streams. The Provisioner is an agent — the most intelligence-requiring part of the architecture — with bash, research, and memory tools. There is no state machine, no `RulesEngine` Python class, and no `TransitionState` effect.

---

## 1. Core Philosophy

Six foundational commitments govern the pipeline. The eleven hard principles in §1.9 enumerate every invariant and its enforcement mechanism.

### 1.1 Event Log as Sole Source of Truth

#### 1.1.1 All state derived from events; replay reconstructs everything

Every fact is an **Effect** — a typed Pydantic model — appended to an append-only event log in EventStoreDB. The OTIO timeline, job queue, VM inventory, and pipeline phase are **projections**: read models rebuilt by pure fold functions. Replay from sequence `0` reconstructs everything exactly.

#### 1.1.2 Event store is only persistent storage; all other state is ephemeral projection

EventStoreDB streams (e.g., `run-{run_id}`) are the sole durable storage. Agents hold no session state. VM workers are ephemeral. Projections are in-memory folds processing only new events since their last checkpoint.

### 1.2 Effects as Only Legal Mutations

#### 1.2.1 Typed Pydantic models; parser extracts from agent text

A **category-conditioned parser** (§9.6) extracts Effects from agent text using `instructor` + `deepseek-v4-flash`. Every Effect carries `kind: Literal[...]`, `run_id: str`, `effect_id: UUID` (UUIDv7 — §3.1), `agent: str`, and `timestamp: datetime`. Invalid payloads are rejected before reaching the event store.

#### 1.2.2 No direct state mutation outside event store append

All state changes enter through EventStoreDB append. Agents do not call projection methods. Projections are read-only consumers.

### 1.3 No State Machine — Prompt-Based Rules

**No state machine.** Pipeline "state" is emergent from projection state (e.g., "all audio blocks clean" emerges from OTIOProjection, not from a state variable). Agents read projection-derived narratives and decide what to do. Rules live in the agent's system prompt, not in code.

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

Agents query the **Global State Agent** via `GET /` frequently. They receive the complete projection bundle (OTIO, Job, VM, State, Budget) as a Pydantic model. They scan this state and decide what to do. Their system prompt contains situation-type guidance and prioritization rules. Agents do not read EventStoreDB directly.

### 1.9 pydantic-deep

Agents use **pydantic-deep** (built on pydantic-ai). Context compaction is implemented as a **pre-processing step** before `agent.run()`. The agent's `message_history` is compacted by querying the OTIO projection to determine the agent's current task/focus, then calling a compaction LLM that preserves task-relevant details. Token management is handled by the pydantic-deep `ContextManagerCapability`.

**Why pre-processing, not watcher-side compaction:** Token management is an agent-internal concern. pydantic-deep provides the hook infrastructure via `on_before_compress`; we provide the OTIO-aware compaction logic.

### 1.10 Principles at a Glance

#### 1.9.1 Table of 11 hard principles with enforcement mechanism per principle

| # | Principle | Enforcement | V6→V7 Change |
|---|---|---|---|
| 1 | **Event log is sole source of truth** | All state derived from events. No hidden state. No projection writes independently. | EventStoreDB replaces SQLite |
| 2 | **Effects are only legal mutations** | Only Pydantic models enter event store. Parser validates against `EffectUnion`. | None |
| 3 | **No state machine — prompt-based rules** | Prioritization lives in agent system prompts. Agents scan projections and decide. | None |
| 4 | **No timeouts in code** | No `setTimeout`, `threading.Timer`, or `asyncio.timeout` in pipeline code. No timeout effects. Operator intervenes on hung jobs. | None |
| 5 | **Real engines only** | Qwen3-TTS, LTX-2.3, DeepSeek API. No mocks, no stubs, no simulation. | None |
| 6 | **Never regex** | Category-conditioned extraction via `instructor` + `deepseek-v4-flash`. | None |
| 7 | **Natural language only** | Agents write free-form prose. No structured output, no markers, no JSON, no section labels. ALL extraction complexity lives in the semantic parser. | **NEW** — eliminates Phase 1/2 fast paths |
| 8 | **Provisioner is an agent** | LLM agent with bash, research, and memory tools. Provisions VMs, dispatches jobs, learns from failures. Most intelligence-requiring component. | Agent with tool use; reads GSA like all agents |
| 9 | **Agent memory does not persist in process** | Each turn rebuilt from projection summaries + bounded message history (last 5 turns). No session state in the agent process between POSTs. | None |
| 10 | **No automatic stale-state detection** | Operator monitors via `GET /` on agents and intervenes manually. No VM-side timers. | Removed TimeoutObserved; operator owns intervention |
| 11 | **Serialized per run, concurrent across runs** | Agent handlers use per-run_id locks. EventStoreDB serializes concurrent appends to the same stream. | §5.6 |
| 12 | **Tick-driven** | Agents are HTTP services; they wake on POST or EventStoreDB subscription. No central watcher loop. | **Watcher removed** |


---

## 2. System Topology

### 2.1 Architecture Diagram

#### 2.1.1 ASCII topology

```
                     Human Operator
                    (instruction, GET state)
                            │
            ┌───────────────┼───────────────┐
            │               │               │
            ▼               ▼               ▼
    ┌──────────────┐ ┌─────────────┐ ┌─────────────┐
    │ Scenario     │ │ Audio       │ │ EventStoreDB│
    │ Agent 8001   │ │ Agent 8002  │ │ port 2113   │
    └──────────────┘ └─────────────┘ └──────┬──────┘
                                            │ events
                                            │
                                            ▼
                              ┌─────────────────────────┐
                              │   Global State Agent    │
                              │       port 8000         │
                              │  (GET / only — read-only│
                              │   projection server)    │
                              └───────────┬─────────────┘
                                          │ GET / state
        ┌──────────┬──────────────────────┼──────────────────────┬──────────┐
        ▼          ▼                      ▼                      ▼          ▼
   ┌─────────┐ ┌────────┐           ┌──────────┐           ┌────────┐ ┌──────────┐
   │  OTIO   │ │  Job   │           │   VM     │           │ State  │ │ Budget   │
   │  Proj.  │ │ Proj.  │           │  Proj.   │           │ Proj.  │ │ Proj.    │
   └────┬────┘ └───┬────┘           └────┬─────┘           └───┬────┘ └────┬─────┘
        │          │                     │                     │          │
        └──────────┼─────────────────────┼─────────────────────┼──────────┘
                   │      projections served by GSA            │
                   ▼                                           ▼
            ┌──────────────┐                              ┌──────────────┐
            │  Scenario    │                              │   Audio      │
            │   Agent      │                              │   Agent      │
            │ port 8001    │                              │ port 8002    │
            └──────────────┘                              └──────────────┘
            ┌──────────────┐                              ┌──────────────┐
            │   Video      │                              │  Assembly    │
            │   Agent      │                              │   Agent      │
            │ port 8003    │                              │ port 8005    │
            └──────────────┘                              └──────────────┘

         ════════ DETERMINISTIC SERVICES ════════
                    ┌──────────────┐
                    │  Provisioner │  port 8081
                    │(deterministic│
                    │ HTTP service)│
                    └───────┬───────┘
                            │
                            ▼
                    ┌──────────────┐
                    │  VM Worker   │  port 9000+
                    │ (ephemeral   │
                    │   GPU)       │
                    └──────────────┘
```

**Data flow.** Agents are independent HTTP services. Each agent exposes `GET /` (health/status) and `POST /` (primary endpoint). The **Global State Agent** (port 8000) is the sole component that reads EventStoreDB directly; it maintains all five projections in memory and serves them to other agents via `GET /`. No other component reads EventStoreDB. Agents query the GSA frequently to receive the complete projection bundle. The agent LLM produces natural language text; the parser (instructor + deepseek-v4-flash) extracts typed effects from that text. The agent handler appends extracted effects to EventStoreDB — agents are barely aware of this process and never produce structured output or tool calls to write effects. The Global State Agent subscribes to EventStoreDB streams and updates its projections. The **Provisioner** (port 8081) is an agent like all others — it reads state from the GSA, reasons about VM provisioning, executes Vast.ai commands via bash tool calls, and its natural language output is parsed for effects (`VMAllocated`, `VMDeallocated`, `JobCompleted`, etc.) like any agent. VM Workers execute inference and report results back to the Provisioner via HTTP POST. The human operator interacts directly with agent endpoints; there is no intermediary HTTP service.

**V7 delta.** The watcher has been removed. Agents are HTTP services, not in-process objects. EventStoreDB replaces the custom SQLite event store. The **Global State Agent** is introduced as the read-only middleman between EventStoreDB and agents. The Provisioner is an agent with bash, research, and memory tools — it reasons about VM provisioning and learns from failures across runs.

---

### 2.2 Component Inventory

#### 2.2.1 Component table

Every agent exposes exactly `GET /` (health) and `POST /` (primary endpoint) on its own port.

| Component | Port | Type | Endpoints | Effects Produced | Effects Consumed |
|---|---|---|---|---|---|
| Global State Agent | 8000 | HTTP service | `GET /` only | — | all effects (from EventStoreDB) |
| Scenario Agent | 8001 | HTTP agent | `GET /`, `POST /` | `UpdateScript`, `DeleteScene`, `ReorderScenes` | state from GSA |
| Audio Agent | 8002 | HTTP agent | `GET /`, `POST /` | `QueueJob`, `JobApproved`, `JobRequeued`, `DurationAdjusted`, `ReconciliationFailed`, `ReconciliationComplete` | state from GSA |
| Video Agent | 8003 | HTTP agent | `GET /`, `POST /` | `QueueJob`, `JobApproved`, `JobRequeued`, `MergeIntoOTIO` | state from GSA |
| Assembly Agent | 8005 | HTTP agent | `GET /`, `POST /` | `PipelineComplete`, `ProductionFailed` | state from GSA |
| Provisioner | 8081 | HTTP agent | `GET /`, `POST /` | `VMAllocated`, `VMDeallocated`, `VMProvisionFailed`, `VMObserved`, `JobCompleted`, `JobFailed`, `JobStarted` | state from GSA |
| EventStoreDB | 2113 | database | — | — | all effects |
| Projections (5) | in-memory | read models | — | — | all effects |
| VM Workers | 9000+ | HTTP service | `GET /`, `POST /` | `JobResult` (to Provisioner) | `JobRequest` |

**V7 delta.** The watcher is removed. The **Global State Agent** (port 8000) is introduced as the read-only projection server — the sole component that reads EventStoreDB directly. All other agents consume state via `GET /` to the GSA, including the Provisioner. The Provisioner is an HTTP agent (port 8081), not a deterministic service. EventStoreDB (port 2113) replaces the SQLite event store. Agents are HTTP services, not in-process objects.

---

### 2.2.2 HTTP Contract Specification

Every HTTP surface in the pipeline uses **JSON** (`Content-Type: application/json`). There are no other content types, no binary protocols, and no streaming endpoints. Every request and response is a single JSON object.

#### POST / — Agent Wake / Instruction

**Endpoint:** `POST /` on every agent port (8001–8005, 8081)  
**Content-Type:** `application/json`  
**Required headers:**

| Header | Value | Required | Purpose |
|---|---|---|---|
| `Content-Type` | `application/json` | Yes | Request body format |
| `X-Run-ID` | `{run_id}` | Yes | Correlates HTTP request with event stream |
| `X-Effect-ID` | `{effect_id}` | On downstream wakes only | Carries the `effect_id` of the effect that triggered this wake |

**Body:** `AgentPayload` Pydantic model  
**Response:** `AgentResponse` Pydantic model  

```python
class AgentPayload(BaseModel):
    """POST / request body sent to every agent."""
    run_id: str = Field(..., description="UUIDv7 string identifying the pipeline run")
    notification_type: Literal["wake", "instruction", "human"] = Field(
        ...,
        description=(
            "wake = periodic activation from upstream agent; "
            "instruction = directed task with specific context; "
            "human = operator-sent HumanInstruction effect"
        ),
    )
    context: dict = Field(
        default_factory=dict,
        description="Agent-specific context passed by caller. Contents vary by notification_type.",
    )
```

**`context` field contents by `notification_type`:**

| `notification_type` | `context` fields | Set by |
|---|---|---|
| `"wake"` | `{}` (empty) — agent reads all state from GSA | Upstream agent |
| `"instruction"` | `{"slot_id": "...", "task": "...", "params": {...}}` — directed task | Upstream agent or operator |
| `"human"` | `{"instruction_text": "...", "action": "...", "action_params": {...}}` — human directive | Operator POST |

**Examples:**

```json
// Wake notification (empty context)
{
  "run_id": "0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b",
  "notification_type": "wake",
  "context": {}
}
```

```json
// Human instruction
{
  "run_id": "0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b",
  "notification_type": "human",
  "context": {
    "instruction_text": "Increase budget to $25.00",
    "action": "budget_override",
    "action_params": {"new_limit": 25.0}
  }
}
```

**Response:**

```python
class AgentResponse(BaseModel):
    """POST / response returned by every agent handler."""
    status: Literal["ok", "error", "halted"] = Field(
        ...,
        description="ok = effects extracted and appended; error = exception caught; halted = ClarificationRequest emitted",
    )
    effects_extracted: list[str] = Field(
        default_factory=list,
        description="List of effect kind strings extracted by parser (e.g., ['UpdateScript', 'QueueJob'])",
    )
    error_message: str = Field(
        default="",
        description="Non-empty only when status='error'. Human-readable error.",
    )
    agent: str = Field(..., description="Agent role that produced this response")
    timestamp: float = Field(default_factory=time.time)
```

**Status codes:**

| HTTP Status | Condition | Response Body |
|---|---|---|
| `200 OK` | Agent ran successfully, effects extracted and appended | `AgentResponse(status="ok", ...)` |
| `202 Accepted` | Agent received wake but is already processing a prior request (idempotent) | `AgentResponse(status="ok", effects_extracted=[])` |
| `400 Bad Request` | Payload failed Pydantic validation | `AgentResponse(status="error", error_message="...")` |
| `500 Internal Server Error` | Unhandled exception in agent handler | `AgentResponse(status="error", error_message="...")` |

**Idempotency.** The `run_id` + `notification_type` combination is not inherently idempotent. If an agent receives two identical wake POSTs, it processes both (the agent may emit duplicate `NoOp` effects, which are harmless). True idempotency is enforced at EventStoreDB via `effect_id` (§3.1.2).

---

#### GET / — Health / State Read

**Endpoint:** `GET /` on every agent port  
**Content-Type:** `application/json`  
**Query params:** `?run_id={run_id}` (optional on agents, required on GSA)  
**Response:** Varies by component

**Agent GET / (ports 8001–8005, 8081):**

```python
class AgentHealthResponse(BaseModel):
    """GET / response from any agent (not the GSA)."""
    status: Literal["healthy", "busy", "error"] = "healthy"
    agent: str                          # e.g., "scenario", "audio"
    last_run: float | None = None       # timestamp of last POST / handling
    current_task: str | None = None     # what the agent is working on (from _determine_focus)
    last_error: str | None = None       # last error message if status="error"
    idle_since: float | None = None     # timestamp when agent became idle
```

**GSA GET / (port 8000):**

The Global State Agent returns `GlobalStateResponse` (§2.4.2 / §6.7.6). This is the only component whose `GET /` returns full state rather than just health.

```
GET http://localhost:8000/?run_id=0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b
Content-Type: application/json
X-Run-ID: 0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b

→ GlobalStateResponse (§6.7.6)
```

**Status codes:**

| HTTP Status | Condition | Response Body |
|---|---|---|
| `200 OK` | Agent healthy and responsive | `AgentHealthResponse` or `GlobalStateResponse` |
| `503 Service Unavailable` | Agent is starting up or recovering from crash | `{"status": "error", "agent": "...", "last_error": "..."}` |

---

#### Error Response Schema

All error responses follow the same schema, regardless of which component emits them:

```python
class PipelineErrorResponse(BaseModel):
    """Standard error response from any pipeline component."""
    error: str = Field(..., description="Error category: validation, runtime, timeout, network, unknown")
    message: str = Field(..., description="Human-readable error message")
    component: str = Field(..., description="Which component emitted the error")
    run_id: str | None = None
    timestamp: float = Field(default_factory=time.time)
```

**Example error response:**

```json
{
  "error": "validation",
  "message": "Field 'run_id' is required in AgentPayload",
  "component": "scenario_agent",
  "run_id": null,
  "timestamp": 1779012345.678
}
```

---

### 2.3 Emergent Pipeline Phases

#### 2.3.1 Seven phases (six operational + ABORTED)

These are not states. They are descriptive labels for human observation. No code enforces transitions — they emerge from what agents do.

```
    INIT ──► SCRIPT ──► AUDIO_RECONCILE ──► VIDEO_PRODUCTION ──► ASSEMBLY ──► DONE
              ▲                ▲
              │                │
       gap_unexpected    voice_mismatch
```

| Phase | Emergent Condition | Active Agents |
|---|---|---|
| **INIT** | No `PipelineStarted` effect | None |
| **SCRIPT** | `PipelineStarted` exists, OTIO has unfilled slots | Scenario |
| **AUDIO_RECONCILE** | OTIO has dirty audio blocks | Audio |
| **VIDEO_PRODUCTION** | All audio clean, video slots unfilled | Video |
| **ASSEMBLY** | All slots filled, final MP4 missing | Assembly |
| **DONE** | Final MP4 exists and validates | None |
| **ABORTED** | `PipelineAborted` emitted | None |

#### 2.3.2 Back-edges

| Back-Edge | Trigger | From | To | Handler |
|---|---|---|---|---|
| `gap_unexpected` | Narration scene count ≠ scene list | `AUDIO_RECONCILE` or later | `SCRIPT` | Scenario Agent rewrites script |
| `voice_mismatch` | Final audio speaker ≠ scenario voice tag | `VIDEO_PRODUCTION` | `SCRIPT` | Scenario Agent fixes voice tag |

Back-edges are triggered when the parser extracts `ProductionFailed` from an agent's output with `failure_type` in `{gap_unexpected, voice_mismatch}`. The Scenario Agent receives the failure context on its next turn; the parser extracts `UpdateScript` from its output to fix the problem. Prior effects remain immutable in the Event Store. Downstream Projectors rebuild read models from the full log. This makes recovery a new forward path, not a mutation.

### 2.4 Global State Agent

#### 2.4.1 Purpose and invariants

The **Global State Agent** (GSA, port 8000) is the **sole read path** between EventStoreDB and all other agents. It subscribes to EventStoreDB streams, maintains all five projections in memory, and serves them via `GET /`. No other component reads EventStoreDB directly.

**Invariants:**
- **GET / only.** The GSA exposes exactly one endpoint: `GET /`. It does not accept `POST /`. No agent can instruct it, influence it, or mutate its state.
- **Read-only from agent perspective.** Agents treat the GSA as a state cache. They `GET /` to receive the current projection bundle.
- **EventStoreDB is the GSA's only input.** The GSA subscribes to EventStoreDB stream changes and rebuilds projections. It does not accept effects from agents directly.
- **Ephemeral with checkpointing.** The GSA holds no persistent state in-memory, but it writes per-run checkpoints to local disk (`/tmp/gsa-checkpoints/`). If it restarts, it resumes each run's catch-up subscription from its last checkpoint rather than replaying from sequence 0 (§5.5).

#### 2.4.2 GET / response format

```python
class GlobalStateResponse(BaseModel):
    """Response from GET / on the Global State Agent."""
    run_id: str
    timestamp: float
    otio: OTIOResponse        # §6.7.1 — serializable slot state
    jobs: JobResponse         # §6.7.2 — serializable job state
    vms: VMResponse           # §6.7.3 — serializable VM state
    state: StateResponse      # §6.7.4 — serializable phase state
    budget: BudgetResponse    # §6.7.5 — serializable budget state
    latest_sequence: int      # highest event sequence number included
```

Agents call `GET /?run_id={run_id}` to receive the full state bundle for their run. The GSA rebuilds projections from EventStoreDB on every request (or serves from its in-memory cache if the stream position has not changed).

#### 2.4.3 Why isolate the read path

- **Single source of truth for state.** Every agent sees the same projections because every agent reads from the same GSA. No drift from agents reading EventStoreDB at different stream positions.
- **Simplified agent code.** Agents do not need event-store client logic, replay logic, or projection fold code. They receive ready-made Pydantic models.
- **Operator visibility.** The human operator can `GET /` on the GSA to see the complete pipeline state at a glance, without understanding EventStoreDB query syntax.
- **Performance.** The GSA can cache projection state and serve it cheaply. EventStoreDB reads are concentrated in one process.

#### 2.4.4 No exceptions — only GSA reads EventStoreDB

There are **no exceptions**. The **Provisioner** (port 8081) reads state from the GSA via `GET /` like all other agents. It does not read EventStoreDB directly. The GSA is the sole read path for every agent. The Provisioner queries the GSA frequently to receive `JobProjection` and `VMProjection`, then reasons about what VMs to provision, what jobs to dispatch, and what to destroy.

#### 2.4.5 System Invariants (crisp)

| # | Invariant | Meaning |
|---|---|---|
| 1 | **Only GSA reads ESDB** | The Global State Agent (port 8000) is the sole component that reads EventStoreDB. No agent, no Provisioner, no worker reads it directly. |
| 2 | **No agent writes ESDB** | Agents reason and talk in natural language. The handler appends extracted effects to EventStoreDB after the parser processes agent text. Agents are barely aware of this process. |
| 3 | **All agents read GSA frequently** | Every agent (including Provisioner) queries the GSA via `GET /` to receive projections. Not necessarily on every turn, but frequently enough to act on current state. |
| 4 | **Only `GET /` and `POST /` everywhere** | Every HTTP surface exposes exactly these two endpoints. No `/health`, `/status`, `/data`, or structured sub-endpoints. |
| 5 | **Only agents have LLM** | Every LLM in the pipeline lives inside an agent (Scenario, Audio, Video, Assembly, Provisioner, Maintainer). The only non-agent LLM usage is VM Worker quality-check (`deepseek-v4-flash` for pass/fail classification). |
| 6 | **Provisioner is an agent** | The Provisioner (port 8081) is the most intelligence-requiring component. It uses bash, research, and memory tools. Its natural language output is parsed for effects like any agent. It is not deterministic code. |

---

## 3. Effect Type Family — Complete Schemas

All pipeline mutations pass through EventStoreDB as **effects** — Pydantic v2 models serialized to JSON events. Every effect carries `run_id` (identifies the pipeline run), `effect_id` (UUIDv7 for client-side idempotency), `agent` (which component produced it), and `timestamp` (seconds since epoch). The `kind` field serves as the discriminant for parsing and union dispatch.

This section defines 32 concrete effect types organized into 8 families, plus the base `Effect` model and the `ReconciliationFailureDetail` and `SuggestedFix` sub-models. All together, 35 Pydantic models. Every model is a complete, runnable schema with type annotations, `Literal` discriminants, and `Field` constraints. The section closes with the `EffectUnion` discriminated union definition and the `KIND_TO_MODEL` routing table used by the parser.

Naming convention: **imperative** for agent requests (`QueueJob`, `ExecuteRawBash`, `MergeIntoOTIO`), **past-tense** for system-reported outcomes (`JobCompleted`, `AudioMeasured`, `PipelineComplete`).

---

### 3.1 Base Effect Model

#### 3.1.1 Effect base class

```python
from __future__ import annotations

import time
from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field
from uuid_extensions import uuid7


class Effect(BaseModel):
    """Base for all effect types. NEVER instantiated directly.

    Fields present on every effect emitted into the event store:
    - run_id:        pipeline run identifier (opaque string)
    - effect_id:     UUIDv7 generated client-side for idempotent retries
    - kind:          Literal discriminant string (overridden per subclass)
    - agent:         component that produced the effect (e.g. "scenario")
    - timestamp:     seconds since epoch at creation time

    EventStoreDB deduplicates on (stream, event_id) via native idempotency.
    Client-side generation means an agent can retry an append with the same
    effect_id and the duplicate is silently dropped by the server.
    """
    run_id: str
    effect_id: UUID = Field(default_factory=uuid7)
    kind: str = "effect"  # overridden per subclass via Literal
    agent: str
    timestamp: float = Field(default_factory=time.time)
```

`effect_id` uses UUIDv7 because it encodes a timestamp in the high bits, making event logs naturally time-sortable without leaking sequence gaps. Client-side generation means an agent can retry a failed append with the same `effect_id` and EventStoreDB silently drops duplicates.

#### 3.1.2 EventStoreDB idempotency

EventStoreDB provides native idempotency on `event_id` within a stream. The client passes `event_id=str(effect.effect_id)` when appending. If the same `event_id` is appended to the same stream twice, EventStoreDB treats the second append as a no-op (or returns the existing position, depending on concurrency settings).

| Field | Type | Source | Purpose |
|---|---|---|---|
| `run_id` | `str` | caller | scopes all effects to one pipeline run; becomes stream name suffix |
| `effect_id` | `UUID` | `uuid7()` client-side | idempotency key; survives retry |
| `kind` | `str` (Literal per subclass) | agent/parser | discriminant for `EffectUnion` |
| `agent` | `str` | caller | attribution for loop detection |
| `timestamp` | `float` | `time.time()` | wall-clock ordering aid |

---

### 3.2 Script Effects

Produced by the Scenario Agent (port 8001). These effects mutate the OTIO timeline's narrative track.

#### 3.2.1 UpdateScript, DeleteScene, ReorderScenes

```python
class ScriptBlock(BaseModel):
    """A single narration block within an UpdateScript."""
    scene_num: int = Field(..., ge=1, description="1-based scene index")
    block_id: str = Field(..., description="stable identifier for this narration block")
    speaker: str = Field(..., description="voice role: narrator, guest_a, etc.")
    text: str = Field(..., min_length=1, description="narration text")
    pronunciation_hints: list[str] = Field(default_factory=list)
    visual_notes: str = ""
    dopamine_hook: str = ""
    duration_sec: float = Field(..., gt=0.0, description="target duration in seconds")


class UpdateScript(Effect):
    """Write or revise one or more scene narration blocks.

    UpdateScript carries a list of ScriptBlock objects. The OTIOProjection
    performs an upsert/deep merge: blocks whose (text, speaker, duration_sec)
    are unchanged preserve their measured_sec and status. Only changed or
    new blocks are marked dirty (status="scripted"). Blocks whose scene_num
    is absent from the list are removed.
    """
    kind: Literal["update_script"] = "update_script"
    blocks: list[ScriptBlock] = Field(..., min_length=1)


class DeleteScene(Effect):
    """Remove a scene and all its narration blocks from the timeline."""
    kind: Literal["delete_scene"] = "delete_scene"
    scene_num: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1)


class ReorderScenes(Effect):
    """Change scene order. new_order[i] is the scene_num that should occupy position i+1.

    The OTIOProjection resequences the top-level timeline tracks so that
    scene N moves to the position specified. All narration and video slots
    attached to a scene move with it.
    """
    kind: Literal["reorder_scenes"] = "reorder_scenes"
    new_order: list[int] = Field(..., min_length=1)
```

---

### 3.3 Job Effects

Produced by the Audio Agent (port 8002), Video Agent (port 8003), and the Provisioner agent (port 8081). These effects manage the lifecycle of media-generation work units.

#### 3.3.1 QueueJob, JobStarted, JobCompleted, JobFailed, JobRequeued, JobApproved

```python
class QueueJob(Effect):
    """Demand creation of a media artifact by a VM worker.

    The Provisioner reads QueueJob from the JobProjection and matches it to
    a Vast.ai offer (job_type="tts" -> Qwen3-TTS GPU; job_type="ltx" -> LTX GPU).
    Once a VM is allocated, the job is considered "pending".
    """
    kind: Literal["queue_job"] = "queue_job"
    job_id: str = Field(..., description="stable unique job identifier")
    job_type: Literal["tts", "ltx"]
    scene_num: int = Field(..., ge=1)
    block_id: str
    slot_id: str = Field(..., description="OTIO slot where the result belongs")
    params: dict = Field(default_factory=dict, description="type-specific generation params")


class JobStarted(Effect):
    """VM worker accepted the job (returned 202 Accepted). Job is now running."""
    kind: Literal["job_started"] = "job_started"
    job_id: str
    vm_instance_id: str
    started_at: float = Field(default_factory=time.time)


class JobCompleted(Effect):
    """VM worker finished successfully; artifact is ready for quality review."""
    kind: Literal["job_completed"] = "job_completed"
    job_id: str
    artifact_uri: str = Field(..., description="B2 URI to generated file (e.g. b2://bucket/runs/{run_id}/audio/{slot_id}.wav)")
    duration_sec: float = Field(..., ge=0.0, description="actual media duration")
    vm_instance_id: str
    measurements: list[float] = Field(
        default_factory=list,
        description="WhisperX measurements from VM worker (3 runs)",
    )


class JobFailed(Effect):
    """VM worker failed. failure_category drives retry vs escalation policy."""
    kind: Literal["job_failed"] = "job_failed"
    job_id: str
    error_message: str
    failure_category: Literal[
        "oom",           # GPU out of memory
        "bad_prompt",    # malformed generation params
        "model_load_error",  # model weights failed to load
        "disk_full",     # VM out of disk
        "network",       # network error during model download or upload
        "cuda_error",    # CUDA runtime failure
        "unknown",       # uncategorized failure
    ]
    vm_instance_id: str
    retryable: bool = True
    retry_count: int = Field(default=0, ge=0, description="how many times this job has been retried")


class JobRequeued(Effect):
    """Artistry rejection: previous output did not meet quality bar.

    The parser extracts this from Audio or Video Agent output when a JobCompleted artifact fails
    quality review. new_params carries adjusted generation parameters for
    the retry attempt (e.g., different voice speed, revised prompt).
    """
    kind: Literal["job_requeued"] = "job_requeued"
    job_id: str
    reason: str = Field(..., min_length=1, description="why the previous attempt was rejected")
    new_params: dict | None = None


class JobApproved(Effect):
    """Artistry approval: artifact passes quality review, ready for OTIO merge."""
    kind: Literal["job_approved"] = "job_approved"
    job_id: str
    artifact_uri: str
    quality_notes: str = ""
    reviewed_by: str = Field(default="agent", description="'agent' or human name")
```

#### 3.3.2 JobFailed.failure_category routing

| Category | Meaning | Default Action | Retryable |
|---|---|---|---|
| `oom` | GPU out of memory | requeue with lower batch size | yes |
| `timeout` | VM-side timeout (legacy) | removed in V7; operator intervenes | yes |
| `bad_prompt` | Malformed params | fix params, requeue | yes |
| `model_load_error` | Weights load failure | requeue on fresh VM | yes |
| `disk_full` | VM disk exhausted | deallocate VM, requeue | yes |
| `network` | Transient network error | retry with backoff | yes |
| `cuda_error` | CUDA runtime failure | requeue on different GPU | yes |
| `unknown` | Uncategorized | emit `ClarificationRequest` | no |

The `retryable` field is a hint. The Provisioner agent may override it based on retry count (e.g., force `retryable=False` after 3 consecutive failures of the same job).


---

### 3.4 Reconciliation Effects

Produced by the Audio Agent and the Provisioner agent during audio reconciliation. These effects implement the tight TTS-measure-adjust loop.

#### 3.4.1 AudioGenerated, AudioMeasured, DurationAdjusted

```python
class AudioGenerated(Effect):
    """TTS WAV produced by a VM worker. Artifact awaits WhisperX measurement."""
    kind: Literal["audio_generated"] = "audio_generated"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    artifact_uri: str


class AudioMeasured(Effect):
    """WhisperX measured the actual spoken duration of a generated WAV.

    Three independent WhisperX runs produce three measurements (decision C2).
    The median of `measurements` is the authoritative measured duration used
    for tolerance checking. All three values are stored for debugging variance.
    """
    kind: Literal["audio_measured"] = "audio_measured"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    measured_sec: float = Field(..., description="median of measurements (authoritative)")
    measurements: list[float] = Field(
        default_factory=list,
        description="all three WhisperX measurements, unsorted",
    )
    whisperx_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DurationAdjusted(Effect):
    """Measured duration within tolerance; OTIO slot updated with authoritative value.

    The Audio Agent computes delta = measured_sec - scripted_sec. If
    |delta| <= max(15% * scripted_sec, 0.25s) the block passes. The OTIO
    Projection updates the slot's source_range to match measured_sec.

    Note: delta_sec and tolerance_sec are computed by projections, not stored
    in the effect. This prevents stale derived values if the tolerance formula
    changes.
    """
    kind: Literal["duration_adjusted"] = "duration_adjusted"
    block_id: str
    scene_num: int
    voice_role: str
    scripted_sec: float
    measured_sec: float
```

#### 3.4.2 ReconciliationFailed, ReconciliationComplete

```python
class ReconciliationFailed(Effect):
    """One or more blocks failed the tolerance check. Retry or escalate.

    The failure_type field determines routing:
    - duration_mismatch -> requeue with adjusted TTS params (normal retry)
    - duration_unrecoverable -> per-block attempt limit exceeded
      This triggers a back-edge to SCRIPT because the target duration is
      physically impossible for the given text.
    """
    kind: Literal["reconciliation_failed"] = "reconciliation_failed"
    agent: str
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    failures: list[ReconciliationFailureDetail] = Field(default_factory=list)
    worst_delta_sec: float
    suggested_adjustments: list[dict] = Field(default_factory=list)
    failure_type: Literal["duration_mismatch", "duration_unrecoverable"] = "duration_mismatch"


class ReconciliationComplete(Effect):
    """All narration blocks pass tolerance. OTIO is now authoritative.

    This effect is the gateway from AUDIO_RECONCILE to VIDEO_PRODUCTION.
    Agents check for ReconciliationComplete and clean blocks to decide
    whether to begin video generation.
    """
    kind: Literal["reconciliation_complete"] = "reconciliation_complete"
    agent: str
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    worst_delta_sec: float
    total_measured_sec: float
```

#### 3.4.3 ReconciliationFailureDetail sub-model

```python
class ReconciliationFailureDetail(BaseModel):
    """Per-block failure diagnostic embedded in ReconciliationFailed.

    Not a top-level effect — has no `kind` field and is NOT in EffectUnion.
    """
    block_id: str
    scene_num: int
    phrase_idx: int = Field(..., description="index of phrase within block")
    voice: str
    scripted_sec: float
    measured_sec: float
    delta_sec: float
    ratio: float = Field(..., description="measured / scripted")
    message: str = Field(..., description="human-readable diagnostic")
    attempt_number: int = Field(default=1, ge=1, description="which reconciliation attempt this was")
```

| Reconciliation Effect | Producer | Next Action |
|---|---|---|
| `AudioGenerated` | Provisioner / VM Worker | Run WhisperX (3x), emit `AudioMeasured` |
| `AudioMeasured` | Audio Agent | Parser extracts `DurationAdjusted` or `ReconciliationFailed` from Audio Agent output after tolerance computation |
| `DurationAdjusted` | Audio Agent | OTIOProjection updates slot; block passes |
| `ReconciliationFailed` | Audio Agent | Requeue with adjusted params, or escalate if `duration_unrecoverable` |

| `ReconciliationComplete` | Audio Agent | Video Agent may begin VIDEO_PRODUCTION when all blocks clean |

---

### 3.5 VM Effects

Produced by the Provisioner agent (port 8081). These effects track the lifecycle of ephemeral GPU instances rented from Vast.ai.

#### 3.5.1 VMAllocated, VMDeallocated, VMProvisionFailed, VMObserved

```python
class VMAllocated(Effect):
    """GPU instance created and ready for job assignment.

    The Provisioner emits this after successfully creating a Vast.ai instance
    and verifying that the worker HTTP endpoint (port 9000+) responds to GET /.
    """
    kind: Literal["vm_allocated"] = "vm_allocated"
    instance_id: str = Field(..., description="Vast.ai instance ID")
    role: Literal["tts", "ltx"] = Field(..., description="worker role determines GPU type")
    offer_id: str = Field(..., description="Vast.ai offer ID that was accepted")
    worker_url: str = Field(..., description="full URL including port, e.g. http://1.2.3.4:9000")
    gpu_type: str = Field(..., description="GPU model, e.g. 'RTX 4090'")
    cost_per_hour: float = Field(..., gt=0.0)


class VMDeallocated(Effect):
    """GPU instance destroyed. Final cost is recorded for budget tracking."""
    kind: Literal["vm_deallocated"] = "vm_deallocated"
    instance_id: str
    reason: Literal[
        "job_done",       # worker finished all assigned jobs
        "cost_limit",     # exceeded per-VM cost threshold
        "stale",          # operator manually deallocated stuck VM
        "provision_failed",  # never reached healthy state
        "manual",         # human operator destroyed via instruction
    ]
    final_cost: float = Field(default=0.0, ge=0.0)
    runtime_sec: float = Field(default=0.0, ge=0.0)
    run_id: str = ""  # for ownership guard: only destroy VMs belonging to this run


class VMProvisionFailed(Effect):
    """Provisioner could not create a VM for a pending job.

    On repeated failures (configurable threshold, default 3), the Provisioner
    halts and emits `ClarificationRequest` for human intervention. It does not
    attempt creative recovery — agent reasoning about the failure.
    """
    kind: Literal["vm_provision_failed"] = "vm_provision_failed"
    offer_id: str = ""
    job_id: str = ""
    error_message: str
    failure_category: Literal[
        "no_offers",      # no Vast.ai offers match requirements
        "offer_taken",    # offer was rented by another user
        "payment_failed", # billing issue
        "boot_timeout",   # instance created but never became healthy
        "ssh_failed",     # cannot reach worker endpoint
        "unknown",
    ]
    retryable: bool = True
    consecutive_failures: int = Field(default=1, ge=1)


class VMObserved(Effect):
    """Provisioner detected drift between event-derived VM state and Vast.ai reality.

    The Provisioner polls Vast.ai API and compares reported
    instance status against the VMProjection's internal model. When drift is
    detected, it emits VMObserved so the projection can reconcile.

    The Provisioner never auto-corrects. All drift surfaces to the operator
    via ClarificationRequest. Principle 9: no automatic stale-state detection.
    """
    kind: Literal["vm_observed"] = "vm_observed"
    instance_id: str
    observed_status: Literal[
        "running", "offline", "not_found", "unknown"
    ] = Field(..., description="what Vast.ai API reports")
    expected_status: Literal[
        "running", "offline", "not_found", "unknown"
    ] = Field(..., description="what VMProjection believes")
    drift_description: str = Field(..., description="human-readable drift summary")
    corrective_action: Literal[
        "none",           # minor drift, logged only
        "escalate",       # unresolvable, parser extracts ClarificationRequest
    ] = "none"
```


---

### 3.6 OTIO Effects

Produced by the Audio Agent and Video Agent after artistry approval. These effects merge approved media artifacts into the OTIO timeline.

#### 3.6.1 MergeIntoOTIO, DeleteFromOTIO

```python
class MergeIntoOTIO(Effect):
    """Approved clip enters the OTIO timeline at the specified track and slot.

    The OTIOProjection finds the existing `otio.schema.Clip` by `slot_id`
    (see §6.1.3 for slot addressing) and replaces its `MissingReference`
    with an `ExternalReference` pointing to `artifact_uri`. The clip's
    `source_range` is updated to `duration_sec`. The `documentary` metadata
    is updated with `status="delivered"` and `artifact_uri`.
    """
    kind: Literal["merge_into_otio"] = "merge_into_otio"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str = Field(..., description="Canonical slot address, e.g. 'A1:3:2' (§6.1.3)")
    artifact_uri: str = Field(..., description="B2 URI to the approved media file")
    track_name: Literal["A1_Narration", "V1_Video"] = Field(..., description="Target track (§6.1.3)")
    start_time: float = Field(..., ge=0.0, description="Timeline start in seconds (computed by projection from preceding clips)")
    duration_sec: float = Field(..., gt=0.0)
    transition_type: Literal["cut", "dissolve", "none"] = "cut"
    transition_duration_sec: float = Field(default=0.0, ge=0.0)


class DeleteFromOTIO(Effect):
    """Remove a clip from the OTIO timeline. Used when a block is re-reconciled
    (its old audio becomes invalid) or when a scene is deleted.
    """
    kind: Literal["delete_from_otio"] = "delete_from_otio"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str
    track_name: Literal["A1_Narration", "V1_Video", "both"]
    reason: str = Field(..., min_length=1)
```

---

### 3.7 Pipeline Effects

Produced by agents and the Provisioner. These effects record pipeline lifecycle events.

#### 3.7.1 PipelineStarted, PipelineComplete, PipelineAborted, VASTGlobalStateObserved

```python
class PipelineStarted(Effect):
    """Scenario Agent emitted this to signal that a new pipeline run has begun.

    Agents check for the presence of a PipelineStarted effect
    to determine whether the run has begun.
    """
    kind: Literal["pipeline_started"] = "pipeline_started"
    agent: str = "scenario"
    config: dict = Field(default_factory=dict, description="pipeline configuration snapshot")
    max_tts_budget_usd: float = Field(default=2.0, gt=0.0)
    max_attempts_per_block: int = Field(default=5, ge=1)
    max_run_budget_usd: float = Field(default=10.0, gt=0.0)
    output_path: str = Field(default="/tmp/final_documentary.mp4")


class PipelineComplete(Effect):
    """Assembly finished. Final MP4 validated and ready."""
    kind: Literal["pipeline_complete"] = "pipeline_complete"
    agent: str = "assembly"
    output_path: str
    duration_sec: float = Field(..., ge=0.0)
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    validation_passed: bool = True


class PipelineAborted(Effect):
    """Unrecoverable stop. All agent HTTP services continue running but
    no new effects are emitted for this run.

    Reasons include budget exhaustion, repeated VM provision
    failures beyond threshold, or human instruction to abort.
    """
    kind: Literal["pipeline_aborted"] = "pipeline_aborted"
    agent: str = Field(..., description="agent or component that triggered abort")
    reason: Literal[
        "budget_exceeded",
        "vm_unavailable",
        "human_request",
        "loop_detected",
        "unknown",
    ]
    error_log: list[str] = Field(default_factory=list)
    spent_usd: float = Field(default=0.0, ge=0.0)


class VASTGlobalStateObserved(Effect):
    """Global Vast.ai account state observed by the Provisioner.

    Emitted periodically (e.g. every Provisioner activation) to capture
    account-level data that affects provisioning decisions: credit balance,
    active instance count, and current billing rate. This is not per-run
    state — it is global account telemetry that the operator and agents
    may inspect.

    **Sole ownership:** Only the Provisioner may emit VASTGlobalStateObserved.
    """
    kind: Literal["vast_global_state_observed"] = "vast_global_state_observed"
    agent: str = "provisioner"
    credit_balance_usd: float = Field(default=0.0, description="Current Vast.ai account credit balance")
    active_instance_count: int = Field(default=0, ge=0)
    current_billing_rate_usd_hr: float = Field(default=0.0, ge=0.0)
    observed_at: float = Field(default_factory=time.time)


class BudgetSet(Effect):
    """Run budget established or updated.

    Emitted at run start (by Scenario Agent) or when operator overrides budget.
    """
    kind: Literal["budget_set"] = "budget_set"
    agent: str = "scenario"
    budget_usd: float = Field(..., gt=0.0)
    reason: str = Field(default="run_start", description="run_start or operator_override")


class BudgetExceeded(Effect):
    """Cumulative spend exceeded the run budget.

    Emitted by the agent handler when CostTracking (P2) reports
    total_cost_usd > budget_usd. Halts new effect generation.
    """
    kind: Literal["budget_exceeded"] = "budget_exceeded"
    spent_usd: float = Field(..., ge=0.0)
    limit_usd: float = Field(..., gt=0.0)
    agent: str = Field(default="handler", description="component that detected exceedance")
```

---

### 3.8 Bash / Human / Fallback Effects

Escape hatches, human intervention requests, and meta-effects that don't fit other families.

#### 3.8.1 ExecuteRawBash, HumanInstruction, ClarificationRequest

```python
class ExecuteRawBash(Effect):
    """Escape hatch: run a shell command.

    The `approved_by_human` flag is set only after explicit human approval.
    Commands without this flag are rejected by the execution handler.
    The agent emits this effect via natural language; the parser extracts it.
    """
    kind: Literal["execute_raw_bash"] = "execute_raw_bash"
    command: str = Field(..., min_length=1)
    working_dir: str = "/tmp"
    approved_by_human: bool = False
    approved_by: str = ""  # human name or empty
    expected_artifacts: list[str] = Field(
        default_factory=list,
        description="files this command is expected to produce",
    )


class HumanInstruction(Effect):
    """Human operator posted a directive to a specific agent.

    The operator POSTs directly to the agent's endpoint with free text. The
    agent parses it on its next turn. Instructions can override parameters,
    approve blocked commands, or redirect the pipeline (e.g. "skip scene 5").

    Instructions are permanent until superseded by another HumanInstruction
    or PipelineAborted. No expiry — Principle 4 prohibits deadline checks.
    """
    kind: Literal["human_instruction"] = "human_instruction"
    agent: str = Field(..., description="target agent name or 'all'")
    instruction: str = Field(..., min_length=1)
    from_human: str = Field(..., description="human identifier")
    posted_at: float = Field(default_factory=time.time)
    priority: Literal["normal", "urgent", "blocking"] = "normal"
    action: Literal["budget_override", "emergency_abort", "approve_command", "revoke", "generic"] = "generic"
    action_params: dict = Field(default_factory=dict)


class ClarificationRequest(Effect):
    """Parser or agent needs human input to proceed.

    Triggers include: parser confidence below threshold,
    unresolvable VM provision failures, or agent loop detection.
    The pipeline halts (no new effects) until a `HumanInstruction` resolves the
    request.
    """
    kind: Literal["clarification_request"] = "clarification_request"
    agent: str = Field(default="human", description="usually 'human' for operator routing")
    parser_category: str = ""  # which parser category triggered this (if any)
    raw_text: str = ""  # original agent output that caused the problem
    failure_reason: str = Field(..., description="why clarification is needed")
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    suggested_resolution: str = ""
    question: str = ""           # human-readable question
    referenced_text: str = ""    # text that caused the issue
```

#### 3.8.2 AgentLoopDetected, NoOp

```python
class AgentLoopDetected(Effect):
    """An agent detected it is stuck in a loop.

    pydantic-deep StuckLoopDetection fires this (P14 ADOPT). Three patterns:
    1. Repeated identical tool calls
    2. Alternating A-B-A-B oscillation
    3. No-op calls returning same result

    When fired, the parser extracts `ClarificationRequest` from agent output
    for human review. No automatic recovery — operator intervenes.
    """
    kind: Literal["agent_loop_detected"] = "agent_loop_detected"
    agent: str = Field(..., description="agent that is looping")
    loop_signature: str = Field(..., description="concatenated kind sequence")
    effect_sequence: list[str] = Field(default_factory=list, description="last N effect kinds")
    detection_mode: Literal["duplicate_effects", "alternating", "noop", "both"] = "both"
    detection_count: int = Field(..., ge=1, description="how many times loop pattern repeated")


class NoOp(Effect):
    """Informational effect carrying no state mutation.

    The parser emits NoOp as a fallback when an agent's response contains
    no extractable effects. It is also used for heartbeat pings and logging.
    No projection applies NoOp — it passes through the store untouched.
    """
    kind: Literal["noop"] = "noop"
    reason: str = Field(default="no_effects_extracted")
    agent_context: str = ""  # free-text context from the agent
```


---

### 3.9 Production Failure Effect

Produced by the Audio Agent, Video Agent, or Assembly agent when media generation or final assembly fails in a way that requires explicit routing.

#### 3.9.1 ProductionFailed with failure_type routing table

```python
class ProductionFailed(Effect):
    """Media production or assembly failure with structured suggested fix.

    The failure_type field is the routing key. Agents read failure_type
    to decide: back-edge to SCRIPT, requeue in current phase, or halt
    for human intervention.
    """
    kind: Literal["production_failed"] = "production_failed"
    failure_type: Literal[
        "overlap",
        "duration_mismatch",
        "gap_unexpected",
        "voice_mismatch",
        "visual_incoherence",
        "artistic_reject",
        "missing_media",
        "invalid_range",
        "track_misalignment",
        "audio_lufs",
    ]
    slot_id: str = ""
    expected: str = ""  # human-readable expected value
    actual: str = ""    # human-readable actual value
    suggested_fix: SuggestedFix = Field(default_factory=SuggestedFix)
    vm_instance_id: str = ""
    attempt_number: int = Field(default=1, ge=1)


class SuggestedFix(BaseModel):
    """Structured fix proposal. Not a top-level effect — embedded in ProductionFailed."""
    fix_type: Literal[
        "requeue",             # retry same job with adjusted params
        "rewrite_script",      # back-edge to SCRIPT, fix narration text
        "adjust_params",       # tweak generation parameters
        "manual_intervention", # halt, human must fix
        "skip",                # skip this block and continue
    ] = "requeue"
    target_scene: int | None = None
    target_block: str | None = None
    new_params: dict = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    retry_count_suggestion: int = Field(default=3, ge=0, description="try N more times then escalate")
    rationale: str = ""
```

**Failure type routing table:**

| failure_type | Routing Action | Target Phase | Rationale |
|---|---|---|---|
| `gap_unexpected` | Back-edge to SCRIPT | SCRIPT | Narration text doesn't fit target duration |
| `voice_mismatch` | Back-edge to SCRIPT | SCRIPT | Wrong speaker or voice role assigned |
| `overlap` | Requeue with adjusted timing | AUDIO_RECONCILE | Clip overlaps neighbor in timeline |
| `duration_mismatch` | Requeue with new params | AUDIO_RECONCILE | TTS output duration outside tolerance |
| `visual_incoherence` | Requeue with revised prompt | VIDEO_PRODUCTION | LTX output doesn't match narration |
| `artistic_reject` | Requeue with adjusted params | VIDEO_PRODUCTION | Quality bar not met |
| `audio_lufs` | Requeue with gain adjustment | AUDIO_RECONCILE | Audio loudness out of spec |
| `track_misalignment` | Requeue assembly | ASSEMBLY | A/V tracks don't align after merge |
| `missing_media` | Retry artifact delivery | current phase | Artifact file not found at expected path |
| `invalid_range` | Requeue with corrected timing | current phase | OTIO source range is invalid |

The Scenario Agent checks for `failure_type in {"gap_unexpected", "voice_mismatch"}` to trigger the SCRIPT back-edge. All other failure types either requeue in the current phase or halt with `ClarificationRequest`.

---

### 3.10 EffectUnion and KIND_TO_MODEL

#### 3.10.1 Discriminated union definition

```python
from typing import Annotated
from pydantic import Field

EffectUnion = Annotated[
    Union[
        # 3.2 Script Effects (3)
        UpdateScript,
        DeleteScene,
        ReorderScenes,
        # 3.3 Job Effects (6)
        QueueJob,
        JobStarted,
        JobCompleted,
        JobFailed,
        JobRequeued,
        JobApproved,
        # 3.4 Reconciliation Effects (6)
        AudioGenerated,
        AudioMeasured,
        DurationAdjusted,
        ReconciliationFailed,
        ReconciliationComplete,
        # 3.5 VM Effects (4)
        VMAllocated,
        VMDeallocated,
        VMProvisionFailed,
        VMObserved,
        # 3.6 OTIO Effects (2)
        MergeIntoOTIO,
        DeleteFromOTIO,
        # 3.7 Pipeline Effects (4)
        PipelineStarted,
        PipelineComplete,
        PipelineAborted,
        VASTGlobalStateObserved,
        # 3.7.2 Budget Effects (2)
        BudgetSet,
        BudgetExceeded,
        # 3.8 Bash / Human / Fallback (5)
        ExecuteRawBash,
        HumanInstruction,
        ClarificationRequest,
        AgentLoopDetected,
        NoOp,
        # 3.9 Production Failure (1)
        ProductionFailed,
    ],
    Field(discriminator="kind"),
]
```

`EffectUnion` is the only type accepted by the parser before EventStoreDB append. Pydantic validates that the `kind` field matches the declared `Literal` value on the subclass. Any JSON payload with an unknown `kind` fails validation at the parser level, before reaching the event store.

#### 3.10.2 Complete KIND_TO_MODEL mapping

The parser uses `KIND_TO_MODEL` to resolve a `kind` string to the correct Pydantic model for validation. This mapping is used in semantic extraction where the LLM outputs a discriminated union with `kind` as the discriminator:

```python
KIND_TO_MODEL: dict[str, type[Effect]] = {
    # 3.2 Script Effects
    "update_script":      UpdateScript,
    "delete_scene":       DeleteScene,
    "reorder_scenes":     ReorderScenes,
    # 3.3 Job Effects
    "queue_job":          QueueJob,
    "job_started":        JobStarted,
    "job_completed":      JobCompleted,
    "job_failed":         JobFailed,
    "job_requeued":       JobRequeued,
    "job_approved":       JobApproved,
    # 3.4 Reconciliation Effects
    "audio_generated":    AudioGenerated,
    "audio_measured":     AudioMeasured,
    "duration_adjusted":  DurationAdjusted,
    "reconciliation_failed":    ReconciliationFailed,
    "reconciliation_complete":  ReconciliationComplete,
    # 3.5 VM Effects
    "vm_allocated":       VMAllocated,
    "vm_deallocated":     VMDeallocated,
    "vm_provision_failed": VMProvisionFailed,
    "vm_observed":        VMObserved,
    # 3.6 OTIO Effects
    "merge_into_otio":    MergeIntoOTIO,
    "delete_from_otio":   DeleteFromOTIO,
    # 3.7 Pipeline Effects
    "pipeline_started":   PipelineStarted,
    "pipeline_complete":  PipelineComplete,
    "pipeline_aborted":   PipelineAborted,
    "vast_global_state_observed":  VASTGlobalStateObserved,
    # 3.7.2 Budget Effects
    "budget_set":         BudgetSet,
    "budget_exceeded":    BudgetExceeded,
    # 3.8 Bash / Human / Fallback
    "execute_raw_bash":   ExecuteRawBash,
    "human_instruction":  HumanInstruction,
    "clarification_request": ClarificationRequest,
    "agent_loop_detected":   AgentLoopDetected,
    "noop":               NoOp,
    # 3.9 Production Failure
    "production_failed":  ProductionFailed,
}
```

The `_EffectUnion` discriminated union (§9.5.1.1) uses `kind` as the discriminator. Instructor constrains the LLM to output only valid `kind` values from this union. Pydantic validates the full payload against the corresponding model. See §9.5 for the complete semantic extraction pipeline.

#### 3.10.3 Naming convention summary

| Convention | Pattern | Examples |
|---|---|---|
| Imperative (agent requests) | Verb-noun, present tense | `QueueJob`, `ExecuteRawBash`, `MergeIntoOTIO`, `DeleteScene` |
| Past-tense (system outcomes) | Noun-verb or noun-adjective, past tense | `JobCompleted`, `AudioMeasured`, `PipelineComplete`, `VMDeallocated` |
| State descriptors | Adjective or participle | `ReconciliationComplete`, `VMObserved` |
| Meta / diagnostic | Descriptive phrase | `AgentLoopDetected`, `ClarificationRequest`, `ProductionFailed` |

The naming convention is enforced by code review, not by the type system. When adding a new effect type, place it in the family section matching its producer, follow the naming convention based on whether it is an agent request or a system outcome, add it to `EffectUnion`, and register it in `KIND_TO_MODEL`.


---

## 4. Rules as Prompt (No State Machine, No Rules Engine Code)

There is no state machine and no `RulesEngine` Python class. Prioritization, filtering, and response selection happen **inside the agent via prompt instruction**. The agent scans projections and decides what to do. Rules live in the agent's system prompt, not in code.

This follows the principle: *whenever something can be done via prompt, do so — cut code complexity.*

### 4.1 Agent System Prompt: Embedded Rules

Each agent's `instructions` (system prompt) includes a **RULES block** that tells it how to prioritize situations:

```
=== YOUR ROLE ===
You are the {role} agent. You produce effects. You decide what to do.

=== RULES ===
1. Prioritize safety situations (budget critical, loop detected) above all else.
2. Prioritize blocked situations (stale VM, job queued long) next.
3. Prioritize work situations (dirty block, measurement needed) last.
4. If multiple work situations, pick the one with the lowest slot_id.
5. If no situations apply, the parser extracts NoOp with reason.
6. The parser never extracts effects outside the permitted kinds: {permitted_effects}.

=== CURRENT SITUATIONS ===
{situation_narratives}

=== YOUR MEMORY ===
{memory}

=== AVAILABLE EFFECTS ===
{effect_schema}
```

### 4.2 Emergent Pipeline Phases

| Phase | Emergent Condition | Active Agents |
|---|---|---|
| **INIT** | No `PipelineStarted` effect | None |
| **SCRIPT** | `PipelineStarted` exists, OTIO has unfilled slots | Scenario |
| **AUDIO_RECONCILE** | OTIO has dirty audio blocks | Audio |
| **VIDEO_PRODUCTION** | All audio clean, video slots unfilled | Video |
| **ASSEMBLY** | All slots filled, final MP4 missing | Assembly |
| **DONE** | Final MP4 exists and validates | None |
| **ABORTED** | `PipelineAborted` emitted | None |

These are not states. They are descriptive labels for human observation. No code enforces transitions — they emerge from what agents do.

### 4.3 Rules Block (Agent System Prompt Text)

Rules live in the agent's system prompt. They are not code. Each agent receives the same RULES block; only the `PERMITTED EFFECTS` section differs by role.

```
=== RULES ===
1. If agent_loop_detected -> parser extracts ClarificationRequest and stop.
2. If pipeline_budget_critical -> parser extracts PipelineAborted and stop.
3. If block_at_max_attempts -> handle escalation (accept, human, abort).
4. If measurement_complete_fail -> requeue with adjusted params.
5. If fresh_dirty_block -> do the work (queue job, measure, judge).
6. If vm_stale -> note it (Provisioner agent reasons about VM cleanup).
7. If noop_all_clean -> emit NoOp, nothing to do.

Pick the highest-priority rule that applies. Only one action per turn.
```

### 4.4 Maintainer Agent Pattern (Emergency Surgical Intervention)

When a run is stuck, corrupted, or producing bad output, the operator can spin up a **Maintainer Agent** — a temporary agent instance with the same HTTP interface (`GET /`, `POST /`) but a different system prompt focused on diagnosis and repair.

**When to use:**
- Agent loop detected and human cannot determine cause from event stream
- OTIO has accumulated invalid edits that agents keep building on
- A block has exceeded max attempts and escalation requires human judgment
- Pipeline is in an emergent state that no active agent recognizes

**How it works:**
1. Operator starts a Maintainer Agent process on an available port (e.g., 8006) with `role="maintainer"`.
2. Operator POSTs a `HumanInstruction` to `POST /` with context: `run_id`, problem description, and a slice of recent events.
3. Maintainer Agent queries the Global State Agent via `GET /` to receive the full projection bundle, then diagnoses the root cause.
4. Maintainer Agent produces natural language output; the parser extracts repair effects (e.g., `BlockRequeued`, `OTIOUpdated` with corrections, `VMAllocated` for fresh compute).
5. After repair, operator shuts down the Maintainer Agent. It is ephemeral — no persistent state, no background tasks.

**Permissions:**
- Maintainer Agent has a broad `permitted_kinds` list including all work-effect families (§3.2).
- It does NOT have `pipeline_abort` permission — `PipelineAborted` remains with the Scenario Agent to prevent accidental destructive actions.
- It CAN trigger `ClarificationRequest` if it cannot determine a safe repair.

**Why not a permanent service:**
- A permanent maintainer would be a second coordinator — exactly what V7 removes.
- Emergency intervention should be intentional and scoped. The operator decides when to start it and what run it targets.
- No persistent maintainer means no maintainer bugs, no maintainer resource consumption, no maintainer false positives.

---

## 5. Event Store

The event store is **EventStoreDB** — a purpose-built event-sourcing database running as a single Docker container (or cluster). Every effect (Section 3.1) is appended as one event to a stream named `run-{run_id}`. The server enforces idempotency on `event_id` within a stream natively. Projections query the store through `read_since()` and `replay()`; no projection mutates the database.

EventStoreDB handles single-writer semantics, concurrency, ordering, and replay natively. The custom SQLite `_writer_loop`, `BEGIN IMMEDIATE`, and `INSERT OR IGNORE` code from V6 are deleted entirely.

---

### 5.0 Stream Topology

#### 5.0.1 One stream per run

Every pipeline run gets **exactly one stream** named `run-{run_id}`. There is no global stream, no category stream, and no system projection stream. All effects for a run — from every agent, the Provisioner, and VM workers — append to this single stream.

```
Stream: run-0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b
  ├─ event 0: PipelineStarted
  ├─ event 1: BudgetSet
  ├─ event 2: UpdateScript (Scenario Agent)
  ├─ event 3: QueueJob (Audio Agent)
  ├─ event 4: VMAllocated (Provisioner)
  ├─ event 5: JobStarted (Provisioner)
  ├─ event 6: JobCompleted (Provisioner)
  ├─ event 7: AudioMeasured (Audio Agent)
  ├─ ...
  └─ event N: PipelineComplete
```

**Why one stream per run:**
- **Isolation:** A run's events do not mix with other runs. Deleting a run means deleting one stream.
- **Single-writer clarity:** Only one agent appends at a time per run (enforced by HTTP serialization at the agent handler). EventStoreDB's stream append serialization handles race conditions.
- **Replay simplicity:** Reconstructing a run's state means reading one stream from sequence 0.

**Stream count:** A system running 100 concurrent documentary runs has 100 streams plus the `$all` system stream (used only for GSA discovery, see §5.0.2).

#### 5.0.2 GSA stream discovery

The Global State Agent (GSA) must know which streams exist to serve `GET /?run_id=...`. It does **not** scan all streams on startup. Instead:

1. The GSA reads the `$all` system stream from position 0 (once at startup) to discover all `run-*` streams that have ever been created.
2. It builds an in-memory index: `run_id → stream_name`.
3. For each discovered run, it starts a **catch-up subscription** (§5.5) to tail that stream.
4. When a new `PipelineStarted` event appears in `$all`, the GSA adds the new run to its index and starts a new catch-up subscription.

**The `$all` stream is read-only.** The GSA never appends to it. It is used solely for discovery.

#### 5.0.3 No system projections

EventStoreDB's built-in system projections (`$et-*`, `$streams`, `$by_correlation_id`) are **not used**. The GSA performs its own filtering and indexing. System projections add operational complexity (must be explicitly enabled, have versioning semantics, and consume server resources) that the pipeline does not need.

| Feature | Used? | Reason |
|---|---|---|
| `$et-QueueJob` | No | GSA reads full stream and filters by `kind` in Python |
| `$streams` | No | GSA discovers streams via `$all` read |
| `$by_correlation_id` | No | Correlation tracking is in event metadata, not a projection |
| Custom projections | No | Projections are in-memory Python folds, not EventStoreDB server projections |

---

### 5.1 Client Interface

#### 5.1.1 Thin Python wrapper over esdbclient

The application does not implement a custom event store class. It uses the official `esdbclient` library with two thin functions:

```python
from esdbclient import EventStoreDBClient, NewEvent, StreamState
import json

client = EventStoreDBClient(uri="esdb://localhost:2113?tls=false")

async def append_effect(
    run_id: str,
    effect: Effect,
    causation_id: str = "",
    correlation_id: str = "",
) -> int:
    """Append an effect to the run's stream. Idempotent on effect_id."""
    stream_name = f"run-{run_id}"
    event = NewEvent(
        type=effect.kind,
        data=effect.model_dump_json().encode(),
        metadata=json.dumps({
            "agent": effect.agent,
            "timestamp": effect.timestamp,
            "run_id": run_id,
            "causation_id": causation_id or str(effect.effect_id),
            "correlation_id": correlation_id or str(effect.effect_id),
        }).encode(),
        event_id=str(effect.effect_id),
    )
    recorded = await client.append_to_stream(
        stream_name=stream_name,
        events=[event],
        current_version=StreamState.ANY,
    )
    return recorded.next_expected_version

async def read_since(run_id: str, from_revision: int = 0) -> list[dict[str, Any]]:
    """Return all events for run_id with revision > from_revision."""
    stream_name = f"run-{run_id}"
    events = await client.get_stream(stream_name, from_revision=from_revision)
    return [
        {
            "sequence": e.revision,
            "effect_id": e.event_id,
            "kind": e.type,
            "payload_json": e.data.decode(),
            "created_at": e.commit_position,
        }
        for e in events
    ]

async def replay(run_id: str) -> list[dict[str, Any]]:
    """Full replay from sequence 0."""
    return await read_since(run_id, from_revision=0)
```

#### 5.1.2 _parse_payload() — effect deserialization glue

`read_since()` and `replay()` return raw JSON strings. `_parse_payload()` maps the `kind` discriminant to the correct Pydantic model via `KIND_TO_MODEL` and returns a validated instance.

```python
import json

def _parse_payload(kind: str, payload_json: str) -> Effect:
    """Deserialize a JSON payload into the correct Effect subclass.

    Raises ValueError for unknown kind strings.
    """
    model_class = KIND_TO_MODEL.get(kind)
    if model_class is None:
        raise ValueError(f"Unknown effect kind: {kind!r}")
    data = json.loads(payload_json)
    return model_class.model_validate(data)
```

| Step | Logic | Failure Mode |
|---|---|---|
| 1 | `KIND_TO_MODEL[kind]` lookup | `ValueError` if kind not registered |
| 2 | `json.loads(payload_json)` | `JSONDecodeError` on malformed JSON |
| 3 | `model_validate(data)` | `ValidationError` if fields mismatch schema |

All three failure modes surface as exceptions in the projection's `tick()` loop. The projection logs the error and skips the offending event; it does not crash. A malformed event in EventStoreDB indicates a schema mismatch between writer and reader and requires operator intervention.

| Parameter | Value | Purpose |
|---|---|---|
| `stream_name` | `f"run-{run_id}"` | Per-run isolation |
| `event_id` | `str(effect.effect_id)` | UUIDv7 client-side idempotency |
| `type` | `effect.kind` | Discriminant for parsing |
| `data` | `effect.model_dump_json()` | Full Pydantic serialization |
| `metadata` | `{"agent", "timestamp", "run_id", "causation_id", "correlation_id"}` | Operational attribution + causal chain |
| `current_version` | `StreamState.ANY` | Allow idempotent re-append |

**Event metadata schema.** Every event carries a metadata envelope with five fields:

| Field | Type | Source | Purpose |
|---|---|---|---|
| `agent` | `str` | `effect.agent` | Which component produced this effect |
| `timestamp` | `float` | `effect.timestamp` | Wall-clock time at creation |
| `run_id` | `str` | Caller | Which run this event belongs to |
| `causation_id` | `str` | Caller (optional) | `event_id` of the preceding effect that caused this one. If empty, defaults to this event's own `event_id` (self-caused root). |
| `correlation_id` | `str` | Caller (optional) | `event_id` that groups related effects in a causal chain. All effects in the same chain share the same `correlation_id`. If empty, defaults to this event's own `event_id`. |

**Causation chain example:**

```
Event 0: PipelineStarted        causation_id="0"  correlation_id="0"
Event 1: QueueJob               causation_id="0"  correlation_id="1"
Event 2: VMAllocated            causation_id="1"  correlation_id="1"
Event 3: JobStarted             causation_id="2"  correlation_id="1"
Event 4: JobCompleted           causation_id="3"  correlation_id="1"
Event 5: AudioMeasured          causation_id="4"  correlation_id="4"
```

- `QueueJob` (event 1) was caused by `PipelineStarted` (event 0) — the agent decided to queue a job after the pipeline started.
- `VMAllocated` (event 2) was caused by `QueueJob` (event 1) — the Provisioner allocated a VM because a job was queued.
- All events 1–4 share `correlation_id="1"` because they are part of the same job lifecycle chain.
- `AudioMeasured` (event 5) starts a new correlation chain because it is an independent measurement event.

Causation and correlation IDs are set by the **handler**, not the agent or parser. When the handler appends effects, it tracks the previous effect's `event_id` and passes it as `causation_id`. For the first effect in a handler invocation, `causation_id` is the triggering event's ID (e.g., the wake notification's effect ID, or the previous agent's effect that caused this wake).

**Why this matters:** Causation chains enable debugging ("why was this VM allocated?") and tracing ("what was the full chain from QueueJob to JobCompleted?"). They also support idempotency: if a handler retries after a crash, it can detect that effects with the same `correlation_id` already exist.

**Why no custom class:** EventStoreDB is the authoritative implementation of an event store. Re-implementing its semantics in SQLite was educational in V6 but is unnecessary operational complexity in V7.

---

### 5.2 Deduplication on effect_id

EventStoreDB deduplicates natively on `event_id` within a stream. When an agent retries `append_effect()` with the same `effect_id`, the server returns the existing position without inserting a duplicate event.

```
Agent                    EventStoreDB
  |                         |
  |-- append(event_id=X) ->|
  |   (network drops)       |
  |                         |-- stores event at revision 47
  |   (no response)         |
  |                         |
  |-- append(event_id=X) ->|  (retry, same event_id)
  |                         |-- idempotent no-op
  |<-- returns 47 ----------|
```

The agent receives the same revision number (47) on both calls. The stream contains exactly one event for `effect_id=X`. This property holds across process restarts, network partitions, and agent crashes because the deduplication is in the server, not in memory.

---

### 5.3 Replay

#### 5.3.1 read_since() method for incremental projection updates

Every projection tracks `last_sequence` — the highest sequence number it has already processed. On activation, the projection calls `read_since(run_id, last_sequence)` and receives only new events.

```python
class OTIOProjection:
    """Example: incremental update via read_since()."""

    def __init__(self):
        self.timeline = otio.schema.Timeline(name="Documentary")
        self.tracks: dict[str, Any] = {}
        self.last_sequence = 0

    async def tick(self, run_id: str):
        """Process only events newer than last_sequence."""
        rows = await read_since(run_id, self.last_sequence)
        for row in rows:
            effect = _parse_payload(row["kind"], row["payload_json"])
            self._apply(effect)
            self.last_sequence = row["sequence"]
        return len(rows)
```

The return type of `read_since()` is `list[dict[str, Any]]` where each dict has keys: `sequence`, `effect_id`, `kind`, `payload_json`, `created_at`. Projections deserialize `payload_json` into the appropriate Pydantic model via `EffectUnion` dispatch (Section 3.10). The `sequence` field in the response is the same integer that `append_effect()` returned when the effect was first written.

**Invariants maintained by read_since():**
- Results are strictly ordered by `sequence` ascending.
- The `sequence` value in each row is greater than the input `sequence` parameter.
- If no new events exist, returns an empty list.
- The method is read-only: it never mutates the database.

---

#### 5.3.2 Full replay for state reconstruction

`replay(run_id)` is a convenience wrapper that calls `read_since(run_id, 0)` — returning every event for the run, from sequence 1 to the highest assigned. Full replay is used in three operational scenarios:

| Scenario | Trigger | Action |
|---|---|---|
| Projection schema change | New field added to a projection's state model | Rebuild the projection from sequence 0 |
| Process restart | Agent crashes and restarts | Replay to restore in-memory state |
| Run audit | Human operator inspects a completed run | Replay returns full event history |

The cost of a full replay is `O(N)` where `N` is the event count for the run. Typical documentary runs produce 500–2000 events; EventStoreDB replays this in under 10ms on local storage.

```python
# Full replay example: rebuild a JobProjection from scratch
async def rebuild_job_projection(run_id: str) -> JobProjection:
    """Construct a fresh JobProjection by replaying all events."""
    proj = JobProjection()
    events = await replay(run_id)
    for row in events:
        effect = _parse_payload(row["kind"], row["payload_json"])
        proj.apply(effect)
    return proj
```

Both `read_since()` and `replay()` return row dictionaries with the `payload_json` string unparsed. The caller is responsible for deserializing via `json.loads()` and `EffectUnion` model validation (Section 3.10). This keeps the event store client agnostic of effect type definitions.

---

### 5.4 Operational Concerns

#### 5.4.1 Disk usage monitoring

EventStoreDB stores data in `./esdb-data/` (or a mounted volume). Disk monitoring is delegated to Docker/container orchestration. If the host disk fills, EventStoreDB will reject writes and return explicit errors. Operators monitor disk at the infrastructure layer and inspect stream health via `GET /` on any agent.

#### 5.4.2 Backup strategy: stream export

EventStoreDB supports stream export via the client API or the Web UI. For automated backups, iterate all `run-*` streams and export to JSON:

```python
async def backup_run(run_id: str, backup_dir: str) -> str:
    """Export a run's event stream to a JSON file."""
    events = await replay(run_id)
    path = f"{backup_dir}/run_{run_id}.json"
    with open(path, "w") as f:
        json.dump(events, f)
    return path
```

**Recovery procedure:** If EventStoreDB is lost, reinstall the container, re-import the exported JSON files via `append_effect()` (idempotent on `effect_id`), and replay projections.

---

### 5.5 GSA Catch-Up Subscription and Checkpointing

The GSA (§2.4) must maintain live projections for all runs. A naive approach — replaying every stream from sequence 0 on startup — becomes `O(runs × events_per_run)` and grows without bound. Instead, the GSA uses **catch-up subscriptions** with **per-run checkpoints**.

#### 5.5.1 Catch-up subscription pattern

For each run, the GSA maintains a catch-up subscription to its `run-{run_id}` stream:

1. **Initial connection:** Read the checkpoint file for this run. If it exists, resume from the stored sequence. If not, start from sequence 0.
2. **Historical catch-up:** Call `read_since(run_id, checkpoint_sequence)` to process all events between the checkpoint and the stream head.
3. **Live tail:** Subscribe to new events on the stream as they arrive. EventStoreDB's `subscribe_to_stream()` pushes new events to the subscriber without polling.
4. **Checkpoint write:** After processing a batch of events, write the new sequence number to the checkpoint file.

```python
async def gsa_catch_up(run_id: str, projections: ProjectionBundle):
    """Catch up a single run's projections from checkpoint to head."""
    checkpoint = read_checkpoint(run_id)
    from_seq = checkpoint.sequence if checkpoint else 0

    # Historical catch-up
    rows = await read_since(run_id, from_seq)
    for row in rows:
        for proj in projections:
            effect = _parse_payload(row["kind"], row["payload_json"])
            proj.apply(effect)
            proj.last_sequence = row["sequence"]

    # Write checkpoint after catching up
    if rows:
        write_checkpoint(run_id, projections[0].last_sequence)

    # Live tail subscription
    async for event in subscribe_to_stream(f"run-{run_id}"):
        for proj in projections:
            effect = _parse_payload(event.type, event.data.decode())
            proj.apply(effect)
            proj.last_sequence = event.revision
        # Checkpoint every N events or every M seconds
        if should_checkpoint(event.revision, checkpoint):
            write_checkpoint(run_id, event.revision)
```

#### 5.5.2 Checkpoint file format

Checkpoints are simple JSON files, one per run, stored in `/tmp/gsa-checkpoints/` (or a persistent volume if the GSA is deployed with one):

```python
import json
from pathlib import Path

CHECKPOINT_DIR = Path("/tmp/gsa-checkpoints")


class Checkpoint(BaseModel):
    """Per-run checkpoint for GSA restart recovery."""
    run_id: str
    sequence: int                    # last processed event sequence
    timestamp: float                 # when checkpoint was written
    projection_versions: dict[str, int] = Field(
        default_factory=dict,
        description="projection_name -> last_sequence for multi-projection safety",
    )


def read_checkpoint(run_id: str) -> Checkpoint | None:
    path = CHECKPOINT_DIR / f"{run_id}.json"
    if not path.exists():
        return None
    return Checkpoint.model_validate_json(path.read_text())


def write_checkpoint(run_id: str, sequence: int, projections: dict[str, Projection]):
    path = CHECKPOINT_DIR / f"{run_id}.json"
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint = Checkpoint(
        run_id=run_id,
        sequence=sequence,
        timestamp=time.time(),
        projection_versions={name: proj.last_sequence for name, proj in projections.items()},
    )
    path.write_text(checkpoint.model_dump_json())
```

**Checkpoint frequency:** Every 100 events or every 30 seconds, whichever comes first. This bounds replay on restart to at most 100 events per run.

**Atomic write:** Checkpoints are written to a temp file and renamed (`{run_id}.json.tmp` → `{run_id}.json`) to prevent corruption if the GSA crashes mid-write.

#### 5.5.3 Restart performance

| Scenario | Events to replay | Time |
|---|---|---|
| Fresh GSA startup, new run | 0 | Instant |
| GSA restart, run with 5000 events | ≤100 (since last checkpoint) | ~1ms |
| GSA restart, 100 runs, each 5000 events | ≤100 per run = ≤10,000 total | ~100ms |
| Full replay from 0 (operator request) | All events | `O(total_events)` |

**The checkpoint makes GSA restart O(new events since last checkpoint), not O(total events).** For a system with 100 active runs, each producing 50 events/minute, the GSA replays at most 100 × 100 = 10,000 events on restart — regardless of how many events were produced in the past week.

#### 5.5.4 Checkpoint invalidation

Checkpoints are invalidated (deleted) when:
- The projection schema changes (e.g., a new field is added to `JobProjection`). The operator deletes all checkpoints and restarts the GSA, triggering full replay from 0.
- A run completes (`PipelineComplete` or `PipelineAborted`). The checkpoint is retained for 24 hours (for debugging), then deleted by a background cleanup task.
- EventStoreDB is wiped and restored from backup. Checkpoints are stale and must be deleted manually.

---

### 5.6 Concurrency and Race Condition Handling

The pipeline has three concurrency surfaces: agent handler invocations, EventStoreDB appends, and GSA cache reads. Each is addressed below.

#### 5.6.1 Per-run_id handler serialization

An agent's `POST /` handler is **not re-entrant per run_id**. If Agent A POSTs a wake to Agent B while Agent B is already processing a prior wake for the same `run_id`, the second POST is queued (not rejected) and processed after the first completes.

```python
from asyncio import Lock

_run_locks: dict[str, Lock] = {}

async def handle(payload: AgentPayload) -> AgentResponse:
    """Agent handler with per-run_id serialization."""
    lock = _run_locks.setdefault(payload.run_id, Lock())

    # Wait for any prior handler for this run to complete
    async with lock:
        # 1. Query GSA for fresh state (always current, see §5.6.3)
        projections = await query_gsa(payload.run_id)

        # 2. Build memory from EventStoreDB (rebuilt every turn, survives restart)
        memory = await build_memory(payload.run_id, AGENT_ROLE, limit=5)

        # 3. Build narrative and run agent
        situations = derive_situations(projections, AGENT_ROLE)
        effects = await run_agent_turn(agent, situations, memory, projections, config)

        # 3. Append effects (serialized with lock held)
        for effect in effects:
            await append_effect(payload.run_id, effect)

        # 4. Notify downstream
        await notify_downstream(effects, payload.run_id)

        return AgentResponse(status="ok", effects_extracted=[e.kind for e in effects])
```

**Why serialize per run_id:** Two simultaneous handler invocations for the same run would both read the same GSA state, both run the LLM, and both append effects. The resulting event stream would contain interleaved or duplicate effects that confuse projections. Serialization guarantees that each agent sees the effects from all prior agents before it decides what to do.

**Why not serialize globally:** Different runs are independent. Agent B can process a wake for `run-A` and `run-B` concurrently.

**Why queue instead of reject:** A rejected wake would require the caller to retry with backoff. Queueing is simpler and guarantees forward progress. The queue is an `asyncio.Lock`, not a persistent queue — if the agent crashes while holding the lock, the lock is released on process exit and the next handler proceeds.

#### 5.6.2 Simultaneous appends to the same stream

Two different agents may append to the same `run-{run_id}` stream at the same time (e.g., Audio Agent emits `QueueJob` while Provisioner emits `VMAllocated`). EventStoreDB handles this natively:

```python
async def append_effect(run_id: str, effect: Effect, ...) -> int:
    stream_name = f"run-{run_id}"
    event = NewEvent(...)
    recorded = await client.append_to_stream(
        stream_name=stream_name,
        events=[event],
        current_version=StreamState.ANY,  # accept any current version
    )
    return recorded.next_expected_version
```

**`StreamState.ANY` is intentional.** The pipeline does not use optimistic concurrency at the stream level. Conflicts are resolved by event ordering, not by rejection:

1. Agent A reads GSA state at sequence 100.
2. Agent B reads GSA state at sequence 100.
3. Agent A appends event at sequence 101.
4. Agent B appends event at sequence 102.
5. Both appends succeed. The stream has two new events.
6. The GSA processes 101, then 102, in that order.

**Conflict resolution is projection-level, not append-level.** If Agents A and B both append `UpdateScript` for the same `block_id`, the projection applies both in stream order. The second `UpdateScript` overwrites the first. This is correct behavior — the agent that acted later had fresher state (it saw the first agent's effects via GSA before it decided what to do, because of per-run_id serialization in §5.6.1).

**The only true conflict:** Two agents append the **same** `effect_id` (same UUIDv7) to the same stream. EventStoreDB deduplicates the second append (§5.2). This handles network retry scenarios, not logical conflicts.

#### 5.6.3 GSA cache invalidation and read consistency

The GSA maintains an **in-memory cache** of projection state per run. It updates this cache from its catch-up subscription (§5.5) and serves `GET /` from the cache. The critical question: does an agent that appends an effect and then immediately queries the GSA see its own effect?

**Yes — the GSA cache is always consistent with the stream head.** The GSA's catch-up subscription processes events in order. After processing event N, the cache reflects events 0..N. When an agent appends event N+1:

1. The append returns with `next_expected_version = N+1`.
2. The GSA's subscription receives event N+1.
3. The GSA applies event N+1 to its in-memory projections.
4. The GSA updates `latest_sequence` to N+1.
5. The agent queries `GET /?run_id=...`.
6. The GSA returns projection state including event N+1.

**The race window:** Between step 1 and step 3, the agent might query the GSA and see stale state (event N+1 not yet applied). This window is typically <10ms on local EventStoreDB. The agent handles this by **not assuming its own effects are immediately visible**. The agent's narrative builder (§7.4) constructs the prompt from GSA state as-is; if the agent's own prior effect is not yet visible, the agent may emit `NoOp` and wait for the next wake.

**Cache invalidation is push-based, not pull-based.** The GSA does not poll. It receives events via `subscribe_to_stream()` and updates its cache immediately. There is no TTL, no expiration, and no stale-read scenario beyond the subscription latency window.

**Consistent snapshot for GET /.** The GSA builds a `GlobalStateResponse` from its in-memory projections atomically (within a single coroutine). An agent's `GET /` never sees a half-updated state (e.g., OTIO updated but JobProjection not yet updated). All five projections are updated from the same event before the GSA begins serving the next `GET /`.

#### 5.6.4 What if the GSA is down?

If the GSA is unreachable, agents cannot query state. The agent handler:
1. Attempts `GET /?run_id=...` to the GSA.
2. If the GSA returns an error or times out, the handler logs the failure and returns `AgentResponse(status="error", error_message="GSA unreachable")`.
3. The caller (upstream agent or operator) receives the error and may retry.
4. No effects are appended while the GSA is down — the agent cannot reason without state.

**The GSA is a single point of failure for reads, but not for writes.** EventStoreDB remains available. If the GSA restarts, it catch-ups from checkpoints (§5.5) and resumes serving within seconds. Agents retry their `GET /` with exponential backoff.

---

## 6. Projections

Projections are **incremental read models** rebuilt from the event log. Each projection tracks `last_sequence` and processes only new events on every `tick`. If EventStoreDB is wiped, replaying the event log through every projection reconstructs the entire pipeline state. Projections never emit events — they are pure consumers (Section 6.1 enforces this absolutely).

---

### 6.1 Projection Base Class

#### 6.1.1 Abstract base with tick() and apply(effect) interface

All projections inherit from `Projection`, an abstract base class that defines two operations:

- `tick(run_id)`: fetch events newer than `last_sequence`, apply each, increment `last_sequence`.
- `apply(event)`: mutate the projection's internal state in response to a single event.

```python
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Protocol


class Effect(Protocol):
    """Protocol for effects — projections read kind and payload fields."""
    kind: str


class Projection(ABC):
    """Abstract base for all incremental read models.

    Subclasses implement ``apply()`` to define how each event kind mutates state.
    The ``tick()`` method is final — it handles event fetching and sequence tracking.
    """

    def __init__(self) -> None:
        self.last_sequence: int = 0

    async def tick(self, run_id: str) -> int:
        """Fetch events since ``last_sequence`` and apply each.

        Returns the number of events processed.
        """
        events = await read_since(run_id, self.last_sequence)
        processed = 0
        for event in events:
            self.apply(event)
            self.last_sequence = event["sequence"]
            processed += 1
        return processed

    @abstractmethod
    def apply(self, event: Effect) -> None:
        """Mutate projection state in response to a single event.

        Must be implemented by every concrete projection.
        """
        ...

    def summary(self) -> str:
        """Return a human-readable summary for agent prompts.

        Subclasses override to produce O(1) summaries regardless of event log length.
        """
        return f"{self.__class__.__name__}(last_sequence={self.last_sequence})"
```

#### 6.1.2 last_sequence tracking for incremental updates

`last_sequence` is the waterline. On each `tick`, the projection calls `read_since(run_id, self.last_sequence)`, which returns all events with `sequence > last_sequence` ordered by `sequence`. After applying each event, `last_sequence` advances to that event's sequence number. If `tick` processes zero events, `last_sequence` is unchanged and no state mutation occurs.

This design guarantees idempotent `tick` calls: calling `tick` twice with no new events is a no-op. It also makes projections deterministic and replay-safe: reconstructing a projection from an empty state by calling `tick` in a loop until no events remain produces the same state as a projection that has been incrementally updated since run start.

---

### 6.1.3 OTIO Schema Definition

The pipeline uses **OpenTimelineIO (OTIO) 0.16+** (`pip install opentimelineio>=0.16.0`) as the canonical timeline representation. OTIO is an interchange format — it describes edits, not media files. The pipeline uses OTIO's core schema objects (`Timeline`, `Stack`, `Sequence`, `Clip`, `ExternalReference`, `MissingReference`) with custom metadata under the `documentary` namespace.

#### OTIO Object Hierarchy

```
otio.schema.Timeline(name="Documentary")
└── tracks (otio.schema.Stack)
    ├── track[0]: otio.schema.Sequence(name="A1_Narration")
    │   ├── clip[0]: otio.schema.Clip(name="A1:1:1", media_reference=MissingReference)
    │   ├── clip[1]: otio.schema.Clip(name="A1:1:2", media_reference=ExternalReference)
    │   └── ...
    ├── track[1]: otio.schema.Sequence(name="V1_Video")
    │   ├── clip[0]: otio.schema.Clip(name="V1:1:1", media_reference=MissingReference)
    │   └── ...
    └── track[2]: otio.schema.Sequence(name="A2_Music")
        └── ...
```

| OTIO Object | Purpose | Pipeline Mapping |
|---|---|---|
| `Timeline` | Root container | One per run, named `"Documentary"` |
| `Stack` | Track container | `timeline.tracks`, holds all sequences |
| `Sequence` | A single track | `A1_Narration`, `V1_Video`, or `A2_Music` |
| `Clip` | A single slot | One per narration block or video segment |
| `MissingReference` | Placeholder (no media yet) | Initial state after `UpdateScript` |
| `ExternalReference` | Points to actual media file | Set by `MergeIntoOTIO` after approval |

#### Track Layout

| Index | Name | Content | Producer |
|---|---|---|---|
| 0 | `A1_Narration` | Narration audio per block | Scenario Agent (blocks), Audio Agent (media) |
| 1 | `V1_Video` | Video clips per block | Video Agent |
| 2 | `A2_Music` | Background music tracks | Assembly Agent |

Tracks are fixed at pipeline start. No new tracks are created during a run. The `A2_Music` track may remain empty until assembly.

#### Slot Addressing Scheme

Every slot in the timeline has a **canonical slot address** (also called `slot_id`):

```
{track_short}:{scene_num}:{phrase_idx}
```

| Component | Example | Meaning |
|---|---|---|
| `track_short` | `A1`, `V1`, `A2` | Abbreviated track name |
| `scene_num` | `1`, `2`, `3` | 1-based scene index |
| `phrase_idx` | `1`, `2` | 1-based block index within the scene |

**Examples:**
- `A1:3:2` — Audio narration, scene 3, block 2
- `V1:3:2` — Video clip for scene 3, block 2
- `A2:5:1` — Background music for scene 5, block 1

The slot address is stored as `clip.name` on every `otio.schema.Clip`. This allows `OTIOProjection._find_clip_by_name(slot_addr)` to resolve a slot address to its clip in O(tracks × clips).

#### Time Representation

OTIO uses `RationalTime` (value / rate) for all time values. The pipeline uses a **fixed 24 fps rate**:

```python
rate = 24  # frames per second
duration_rt = otio.opentime.RationalTime(duration_sec * rate, rate)
```

All durations in the OTIO timeline are stored as `RationalTime` at 24 fps. When the Assembly Agent exports the final MP4, it renders at the target frame rate (24 fps for film, 30 fps for broadcast).

#### Custom Metadata Namespace

The pipeline stores pipeline-specific metadata under `clip.metadata["documentary"]`:

```python
clip.metadata["documentary"] = {
    "scene_num": 3,
    "phrase_idx": 2,
    "speaker": "narrator",
    "status": "scripted",        # scripted | measured | delivered | dirty
    "text": "In 1929, the crash came...",
    "scripted_sec": 4.5,
    "measured_sec": None,        # set after WhisperX
    "artifact_uri": None,        # set after MergeIntoOTIO
}
```

This metadata is NOT used by OTIO itself — it is read by the `OTIOProjection` and returned in `OTIOResponse.slots` (§6.7.1). The OTIO file (`.otio` JSON) can be opened in any OTIO-compatible tool; the `documentary` metadata is preserved as extra fields.

#### Clip Lifecycle: MissingReference → ExternalReference

```
UpdateScript creates clip
  │
  ▼
otio.schema.Clip(
    name="A1:3:2",
    media_reference=otio.schema.MissingReference(),
    source_range=TimeRange(start=0s, duration=4.5s)
)
  │
  ├── MergeIntoOTIO (after Audio Agent approval)
  │     │
  │     ▼
  │   clip.media_reference = ExternalReference(
  │       target_url="b2://bucket/runs/abc/audio/A1:3:2.wav",
  │       available_range=TimeRange(start=0s, duration=4.6s)
  │   )
  │   clip.metadata["documentary"]["status"] = "delivered"
  │   clip.metadata["documentary"]["artifact_uri"] = "b2://..."
  │
  └── DurationAdjusted (after reconciliation passes)
        │
        ▼
      clip.source_range.duration = RationalTime(4.6 * 24, 24)
      clip.metadata["documentary"]["measured_sec"] = 4.6
      clip.metadata["documentary"]["status"] = "measured"
```

**DeleteFromOTIO** removes the clip from its track entirely (used during re-reconciliation or scene deletion).

---

### 6.2 OTIO Projection

#### 6.2.1 Timeline construction from script + merge + adjust events

`OTIOProjection` builds an OpenTimelineIO `schema.Timeline` from three event families:

- **Script events** (`UpdateScript`, `DeleteScene`, `ReorderScenes`): define narration blocks with speaker, text, and target duration.
- **Merge events** (`MergeIntoOTIO`): insert approved media clips into timeline slots.
- **Adjust events** (`DurationAdjusted`): update a slot's duration after measured audio passes tolerance.

**V7 critical fix:** `_build_from_script` now performs an **upsert/deep merge** instead of wiping the entire timeline. Unchanged blocks preserve `measured_sec` and `status`. Only changed or new blocks are marked dirty.

```python
from typing import Optional
import opentimelineio as otio


class OTIOProjection(Projection):
    """Builds and validates an OpenTimelineIO timeline from events.

    The timeline is the authoritative structure for the documentary.
    It contains one or more tracks (e.g., "A1_Narration", "V1_Video"),
    each composed of clips aligned to scene slots.
    """

    def __init__(self) -> None:
        super().__init__()
        self.timeline: otio.schema.Timeline = otio.schema.Timeline(
            name="Documentary", global_start_time=otio.opentime.RationalTime(0, 24)
        )
        self.slots: dict[str, dict] = {}  # slot_addr -> {scene_num, speaker, text, duration}

    def apply(self, event: Effect) -> None:
        match event.kind:
            case "update_script":
                self._build_from_script(event)
            case "merge_into_otio":
                self._merge_clip(event)
            case "duration_adjusted":
                self._adjust_slot_duration(event)
            case "delete_scene":
                self._delete_scene(event)
            case "reorder_scenes":
                self._reorder_scenes(event)
            case "delete_from_otio":
                self._remove_clip(event)

    def _build_from_script(self, event: UpdateScript) -> None:
        """Upsert narration blocks from script event. Preserves measured state.

        Creates or updates slots. Unchanged blocks keep measured_sec and status.
        Changed blocks are marked dirty. Absent blocks are removed.
        """
        track_name = "A1_Narration"
        # Ensure track exists
        if not self.timeline.tracks:
            track = otio.schema.Sequence(name=track_name)
            self.timeline.tracks.append(track)
        else:
            track = self.timeline.tracks[0]

        # Compute new set of slot addresses
        new_slot_addrs = set()
        for block in event.blocks:
            slot_addr = f"{track_name}:{block.scene_num}:{block.block_id}"
            new_slot_addrs.add(slot_addr)

            existing = self.slots.get(slot_addr)
            if existing is not None:
                # Compare textual content and target timing (tolerance for float)
                unchanged = (
                    existing.get("text") == block.text
                    and existing.get("speaker") == block.speaker
                    and abs(existing.get("scripted_sec", 0.0) - block.duration_sec) < 0.001
                )
                if unchanged:
                    # Preserve measured_sec, status, artifact_uri
                    continue
                # Block changed — mark dirty, clear measurements
                existing["text"] = block.text
                existing["speaker"] = block.speaker
                existing["scripted_sec"] = block.duration_sec
                existing["measured_sec"] = None
                existing["status"] = "scripted"
                existing["artifact_uri"] = None
                # Update clip duration in timeline
                self._update_clip_duration(slot_addr, block.duration_sec)
            else:
                # New block
                rate = 24
                duration_rt = otio.opentime.RationalTime(
                    block.duration_sec * rate, rate
                )
                clip = otio.schema.Clip(
                    name=slot_addr,
                    source_range=otio.opentime.TimeRange(
                        start_time=otio.opentime.RationalTime(0, rate),
                        duration=duration_rt,
                    ),
                    media_reference=otio.schema.MissingReference(),
                )
                track.append(clip)
                self.slots[slot_addr] = {
                    "scene_num": block.scene_num,
                    "block_id": block.block_id,
                    "speaker": block.speaker,
                    "text": block.text,
                    "scripted_sec": block.duration_sec,
                    "measured_sec": None,
                    "status": "scripted",
                    "artifact_uri": None,
                }

        # Remove slots no longer present in the script
        for addr in list(self.slots.keys()):
            if addr not in new_slot_addrs:
                clip = self._find_clip_by_name(addr)
                if clip is not None:
                    t = clip.parent()
                    if t is not None:
                        t.remove(clip)
                self.slots.pop(addr, None)

    def _update_clip_duration(self, slot_addr: str, duration_sec: float) -> None:
        """Update an existing clip's duration without rebuilding the track."""
        clip = self._find_clip_by_name(slot_addr)
        if clip is None:
            return
        rate = 24
        new_duration = otio.opentime.RationalTime(duration_sec * rate, rate)
        clip.source_range = otio.opentime.TimeRange(
            start_time=clip.source_range.start_time,
            duration=new_duration,
        )

    def _merge_clip(self, event: Effect) -> None:
        """Replace MissingReference with an ExternalReference to the produced artifact."""
        slot_addr = event.slot_id
        clip = self._find_clip_by_name(slot_addr)
        if clip is None:
            return
        clip.media_reference = otio.schema.ExternalReference(
            target_url=event.artifact_uri,
            available_range=clip.source_range,
        )
        if slot_addr in self.slots:
            self.slots[slot_addr]["status"] = "delivered"
            self.slots[slot_addr]["artifact_uri"] = event.artifact_uri

    def _adjust_slot_duration(self, event: Effect) -> None:
        """Update a slot's duration after reconciliation passes tolerance."""
        slot_addr = event.block_id
        clip = self._find_clip_by_name(slot_addr)
        if clip is None:
            return
        rate = 24
        new_duration = otio.opentime.RationalTime(event.measured_sec * rate, rate)
        clip.source_range = otio.opentime.TimeRange(
            start_time=clip.source_range.start_time,
            duration=new_duration,
        )
        if slot_addr in self.slots:
            self.slots[slot_addr]["measured_sec"] = event.measured_sec
            self.slots[slot_addr]["status"] = "measured"

    def _delete_scene(self, event: Effect) -> None:
        """Remove all slots belonging to a scene."""
        scene_num = event.scene_num
        to_remove = [
            addr for addr, slot in self.slots.items()
            if slot["scene_num"] == scene_num
        ]
        for addr in to_remove:
            clip = self._find_clip_by_name(addr)
            if clip is not None:
                track = clip.parent()
                if track is not None:
                    track.remove(clip)
            self.slots.pop(addr, None)

    def _reorder_scenes(self, event: ReorderScenes) -> None:
        """Reorder tracks according to new_order.

        new_order[i] is the scene_num that should occupy position i+1.
        All clips belonging to a scene move with it. Clips are reinserted
        into the track in the new scene order while preserving their
        relative phrase order within each scene.
        """
        track_name = "A1_Narration"
        if not self.timeline.tracks:
            return
        track = self.timeline.tracks[0]

        # Group clips by scene_num
        scene_to_clips: dict[int, list[otio.schema.Clip]] = defaultdict(list)
        for child in list(track):
            if isinstance(child, otio.schema.Clip):
                # slot_addr format: "A1_Narration:scene_num:block_id"
                parts = child.name.split(":")
                if len(parts) >= 2:
                    try:
                        scene_num = int(parts[1])
                        scene_to_clips[scene_num].append(child)
                    except ValueError:
                        pass

        # Build new clip order
        new_clips: list[otio.schema.Clip] = []
        for scene_num in event.new_order:
            clips = scene_to_clips.get(scene_num, [])
            # Sort clips within scene by block_id for stable ordering
            clips.sort(key=lambda c: c.name)
            new_clips.extend(clips)

        # Rebuild track
        track.clear_children()
        for clip in new_clips:
            track.append(clip)

        # Rebuild slots dict to match new order
        new_slots: dict[str, dict] = {}
        for clip in new_clips:
            if clip.name in self.slots:
                new_slots[clip.name] = self.slots[clip.name]
        self.slots = new_slots

    def _remove_clip(self, event: Effect) -> None:
        """Remove a clip from the timeline (e.g., rejected media)."""
        clip = self._find_clip_by_name(event.slot_id)
        if clip is not None:
            track = clip.parent()
            if track is not None:
                track.remove(clip)

    def _find_clip_by_name(self, name: str) -> Optional[otio.schema.Clip]:
        for track in self.timeline.tracks:
            for child in track:
                if isinstance(child, otio.schema.Clip) and child.name == name:
                    return child
        return None

    def all_slots_filled(self) -> bool:
        """Return True if every narration slot has a delivered audio clip."""
        slots = getattr(self, "slots", {})
        if not slots:
            return False
        return all(s.get("status") == "delivered" for s in slots.values())

    def get_timeline_duration_sec(self) -> float:
        """Return timeline duration in seconds."""
        dur = self.timeline.duration()
        return dur.value / dur.rate if dur and dur.rate else 0.0

    def summary(self) -> str:
        total = len(self.slots)
        measured = sum(1 for s in self.slots.values() if s["status"] == "measured")
        delivered = sum(1 for s in self.slots.values() if s["status"] == "delivered")
        dirty = sum(1 for s in self.slots.values() if s["status"] == "dirty")
        scenes = len({s["scene_num"] for s in self.slots.values()})
        return (
            f"OTIO: {scenes} scenes, {total} slots, "
            f"{measured} measured, {delivered} delivered, {dirty} dirty"
        )
```


#### 6.2.2 Validation: no_overlaps, track_alignment, clip_media

Three validation methods support agent decision-making. Each returns `(bool, Optional[str])`: `True` with no message on success, `False` with a descriptive error on failure.

```python
    def validate_no_overlaps(self) -> tuple[bool, Optional[str]]:
        """Check that no two clips on the same track overlap in time.

        Transitions are skipped — overlapping a Transition with a Clip
        is valid OTIO behavior.
        """
        for track in self.timeline.tracks:
            children = list(track)
            for i in range(len(children) - 1):
                a, b = children[i], children[i + 1]
                if isinstance(a, otio.schema.Transition) or isinstance(
                    b, otio.schema.Transition
                ):
                    continue
                try:
                    ra = a.trimmed_range_in_parent()
                    rb = b.trimmed_range_in_parent()
                except Exception:
                    continue
                if ra is None or rb is None:
                    continue
                if not (ra.end_time_inclusive() <= rb.start_time):
                    return (
                        False,
                        f"Overlap on {track.name}: {a.name} ({ra}) vs {b.name} ({rb})",
                    )
        return True, None

    def validate_track_alignment(self) -> tuple[bool, Optional[str]]:
        """Check that all tracks have the same duration.

        A documentary has one coherent timeline — all tracks must span
        the same time range. Returns False if the max track duration
        differs from the timeline duration.
        """
        if not self.timeline.tracks:
            return True, None
        track_durations = []
        for track in self.timeline.tracks:
            try:
                d = track.duration()
                if d is not None:
                    track_durations.append(d)
            except Exception:
                continue
        if not track_durations:
            return True, None
        max_dur = max(track_durations, key=lambda rt: rt.value)
        timeline_dur = self.timeline.duration()
        if timeline_dur is None:
            return False, "Timeline has no duration"
        if abs(timeline_dur.value - max_dur.value) > 0.5:
            return (
                False,
                f"Track misalignment: timeline {timeline_dur.value:.2f}s "
                f"!= max track {max_dur.value:.2f}s",
            )
        return True, None

    def validate_clip_media(self) -> tuple[bool, Optional[str]]:
        """Check that every clip has a valid media reference.

        A clip passes if it has a non-MissingReference media target
        and its trimmed_range resolves without exception.
        """
        for track in self.timeline.tracks:
            for child in track:
                if not isinstance(child, otio.schema.Clip):
                    continue
                if isinstance(child.media_reference, otio.schema.MissingReference):
                    return False, f"Clip {child.name} has no media reference"
                try:
                    _ = child.trimmed_range()
                except Exception as e:
                    return False, f"Clip {child.name} invalid range: {e}"
        return True, None
```

#### 6.2.3 Slot addressing scheme (track:scene:slot)

Every slot in the timeline has a canonical address of the form `track_name:scene_num:block_id`. Example: `"A1_Narration:3:block_b"` identifies block `block_b` in scene 3 on the A1 (audio narration) track. This addressing scheme is used in:

- `QueueJob.slot_id` — the slot a job targets
- `MergeIntoOTIO.slot_id` — where to insert the produced clip
- `DurationAdjusted.block_id` — which slot's duration changed


The `OTIOProjection._find_clip_by_name()` method resolves a slot address to its `otio.schema.Clip` by iterating tracks and matching `clip.name == slot_addr`.

---

### 6.3 Job Projection

#### 6.3.1 Job lifecycle tracking (pending → running → completed/failed)

`JobProjection` tracks the state of every job in the pipeline. A job passes through the lifecycle: `pending` → `running` → `completed` or `failed`. Jobs can be requeued (return to `pending` with updated parameters).

**V7 critical fix:** `production_failures` is now consumed when `UpdateScript` fixes the affected blocks. This prevents infinite script-rewrite loops.

```python
from collections import defaultdict


class JobState:
    """Mutable record for a single job's current state."""

    def __init__(self, job_id: str, job_type: str, slot_id: str) -> None:
        self.job_id: str = job_id
        self.job_type: str = job_type          # "tts" | "ltx"
        self.slot_id: str = slot_id
        self.status: str = "pending"           # pending | running | completed | failed
        self.params: dict[str, Any] = {}
        self.artifact_uri: Optional[str] = None
        self.duration_sec: Optional[float] = None
        self.error_message: Optional[str] = None
        self.requeue_count: int = 0
        self.created_at: float = 0.0
        self.completed_at: Optional[float] = None


class JobProjection(Projection):
    """Tracks job lifecycle, reconciliation state, budget, and production failures.

    V7 additions:
    - ``dirty_blocks`` / ``clean_blocks``: per-block authority tracking
    - ``block_attempts``: per-block retry counter, bounded by max_attempts
    - ``spent_usd``: cumulative budget accumulator
    - ``production_failures``: list of unrecoverable production failures
      that have not yet been resolved. Cleared when UpdateScript fixes them.
    """

    SCRIPT_RESOLVABLE_TYPES: set[str] = {"gap_unexpected", "voice_mismatch"}

    def __init__(self) -> None:
        super().__init__()
        self.jobs: dict[str, JobState] = {}
        self.reconciliation_complete: bool = False
        self.dirty_blocks: set[str] = set()
        self.clean_blocks: set[str] = set()
        self.block_attempts: dict[str, int] = defaultdict(int)
        self.spent_usd: float = 0.0
        self.production_failures: list[dict[str, Any]] = []

    def apply(self, event: Effect) -> None:
        match event.kind:
            # --- Job lifecycle ---
            case "queue_job":
                self._on_queue(event)
            case "job_started":
                self._on_start(event)
            case "job_completed":
                self._on_complete(event)
            case "job_failed":
                self._on_fail(event)
            case "job_requeued":
                self._on_requeue(event)
            # --- Reconciliation state ---
            case "reconciliation_complete":
                self.reconciliation_complete = True
                self.dirty_blocks.clear()
            case "reconciliation_failed":
                self.reconciliation_complete = False
            # --- Budget ---
            case "budget_set":
                pass  # BudgetProjection handles this
            case "budget_exceeded":
                pass  # Handled by append guard
            case "pipeline_aborted":
                pass
            # --- Production failures ---
            case "production_failed":
                self.production_failures.append({
                    "slot_id": getattr(event, "slot_id", ""),
                    "failure_type": getattr(event, "failure_type", ""),
                    "expected": getattr(event, "expected", ""),
                    "actual": getattr(event, "actual", ""),
                    "suggested_fix": getattr(event, "suggested_fix", ""),
                })
            # --- Failure resolution ---
            case "update_script":
                self._resolve_failures_on_script_update(event)
                # Also sync dirty/clean from OTIOProjection
                self._sync_from_otio(event)
    def _on_queue(self, event: Effect) -> None:
        job_id = event.job_id
        if job_id not in self.jobs:
            job = JobState(
                job_id=job_id,
                job_type=event.job_type,
                slot_id=getattr(event, "slot_id", ""),
            )
            job.params = getattr(event, "params", {})
            job.created_at = getattr(event, "timestamp", 0.0)
            self.jobs[job_id] = job
            # Track attempt for TTS jobs (block-level retry counting)
            block_id = getattr(event, "slot_id", None)
            if block_id and event.job_type == "tts":
                self.block_attempts[block_id] += 1

    def _on_start(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "running"

    def _on_complete(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "completed"
            job.artifact_uri = getattr(event, "artifact_uri", None)
            job.duration_sec = getattr(event, "duration_sec", None)
            job.completed_at = getattr(event, "timestamp", None)
            # Mark block clean on successful completion
            block_id = job.slot_id
            if block_id in self.dirty_blocks:
                self.dirty_blocks.discard(block_id)
                self.clean_blocks.add(block_id)

    def _on_fail(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "failed"
            job.error_message = getattr(event, "error_message", "unknown")

    def _on_requeue(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "pending"
            job.requeue_count += 1
            job.error_message = None
            if getattr(event, "new_params", None):
                job.params.update(event.new_params)
            # Requeueing a block resets it to dirty
            if job.slot_id:
                self.dirty_blocks.add(job.slot_id)
                self.clean_blocks.discard(job.slot_id)

    def _sync_from_otio(self, event: UpdateScript) -> None:
        """Sync dirty/clean blocks from OTIOProjection after script update.

        The OTIOProjection has already marked changed blocks as dirty
        (status='scripted') and unchanged blocks retain their measured state.
        We derive dirty/clean sets from the script update directly:
        - Updated block_ids are dirty
        - All other known blocks are clean
        """
        updated_block_ids = {f"A1_Narration:{b.scene_num}:{b.block_id}" for b in event.blocks}
        # Mark updated blocks dirty
        for bid in updated_block_ids:
            self.dirty_blocks.add(bid)
            self.clean_blocks.discard(bid)
        # All production failures for resolved blocks are consumed

    def _resolve_failures_on_script_update(self, event: UpdateScript) -> None:
        """Remove production failures for blocks that were fixed by UpdateScript.

        When the parser extracts UpdateScript from Scenario Agent output to fix a voice_mismatch
        or gap_unexpected, the failures for the affected blocks are consumed
        and removed from the list. This prevents infinite script-rewrite loops.
        """
        updated_block_ids = {f"A1_Narration:{b.scene_num}:{b.block_id}" for b in event.blocks}
        self.production_failures = [
            f for f in self.production_failures
            if not (
                f.get("slot_id") in updated_block_ids
                and f.get("failure_type") in self.SCRIPT_RESOLVABLE_TYPES
            )
        ]

    # --- Query methods for agents ---

    def has_pending_or_running_jobs(self, job_type: Optional[str] = None) -> bool:
        """Return True if any job matches status and optional type filter."""
        for job in self.jobs.values():
            if job.status in ("pending", "running"):
                if job_type is None or job.job_type == job_type:
                    return True
        return False

    def pending_jobs(self, job_type: Optional[str] = None) -> list[JobState]:
        """Return all pending jobs, optionally filtered by type."""
        return [
            j for j in self.jobs.values()
            if j.status == "pending" and (job_type is None or j.job_type == job_type)
        ]

    def block_attempts_exceeded(self, block_id: str, max_attempts: int = 5) -> bool:
        """Check if a block has exceeded its per-block attempt limit."""
        return self.block_attempts.get(block_id, 0) >= max_attempts

    def budget_exceeded(self, max_budget_usd: float = 10.0) -> bool:
        """Check if cumulative spend exceeds the per-run budget."""
        return self.spent_usd >= max_budget_usd

    def is_block_clean(self, block_id: str) -> bool:
        """Return True if a block has measured audio and is authoritative."""
        return block_id in self.clean_blocks

    def all_blocks_clean(self, block_ids: list[str]) -> bool:
        """Return True if every block in the list is clean."""
        return all(self.is_block_clean(bid) for bid in block_ids)

    def summary(self) -> str:
        by_status: dict[str, int] = defaultdict(int)
        by_type: dict[str, int] = defaultdict(int)
        for job in self.jobs.values():
            by_status[job.status] += 1
            by_type[job.job_type] += 1
        return (
            f"Jobs: {len(self.jobs)} total, "
            f"pending={by_status['pending']}, running={by_status['running']}, "
            f"completed={by_status['completed']}, failed={by_status['failed']} | "
            f"tts={by_type['tts']}, video={by_type['video']} | "
            f"reconciled={'yes' if self.reconciliation_complete else 'no'} | "
            f"dirty={len(self.dirty_blocks)} clean={len(self.clean_blocks)} | "
            f"spent=${self.spent_usd:.4f}"
        )
```

#### 6.3.2 Reconciliation state: complete flag, dirty/clean block tracking

The `reconciliation_complete` flag is set by `ReconciliationComplete` and cleared by `ReconciliationFailed`. Agents read this flag to determine whether to begin video generation. Transition from `AUDIO_RECONCILE` to `VIDEO_PRODUCTION` is emergent: when `reconciliation_complete == True` and no dirty blocks remain, the Video Agent may begin work.

Dirty/clean tracking enables partial reconciliation after script back-edges. When `voice_mismatch` routes from `VIDEO_PRODUCTION` back to `SCRIPT`, the Scenario Agent fixes the script; the parser extracts `UpdateScript`. The `OTIOProjection` marks changed blocks **dirty** (status="scripted", need re-TTS) and unchanged blocks **clean** (retain measured_sec). The `JobProjection` syncs its `dirty_blocks`/`clean_blocks` sets from the `OTIOProjection` on every `UpdateScript`. This avoids discarding the entire audio pipeline for a single-scene typo fix.

| Field | Type | Meaning |
|---|---|---|
| `reconciliation_complete` | `bool` | `True` when all blocks have measured audio within tolerance |
| `dirty_blocks` | `set[str]` | Slot addresses needing re-reconciliation |
| `clean_blocks` | `set[str]` | Slot addresses with authoritative measured audio |

#### 6.3.3 Attempt counter per block, budget accumulator per run

Per-block attempt counting prevents any single narration block from consuming infinite retries. Each time a `QueueJob` event targets a TTS slot, `block_attempts[slot_id]` increments. When `block_attempts[slot_id] >= max_attempts` (default 5), the parser extracts `ReconciliationFailed` from Audio Agent output with `failure_type="duration_unrecoverable"`, triggering a back-edge to `SCRIPT`.

Per-run budget tracking prevents aggregate runaway. `spent_usd` is a projection field that the operator may inspect via `GET /` on any agent. Budget enforcement is manual: the operator monitors spend and issues `PipelineAborted` with `reason="budget_exceeded"` if the run exceeds `max_run_budget_usd` (default $10.00). No automatic cost-tracking events are emitted.

#### 6.3.4 Production failures list

`production_failures` collects all `ProductionFailed` events that have not yet been resolved. Each entry is a dictionary with `slot_id`, `failure_type`, `expected`, `actual`, and `suggested_fix`. Agents use this list to detect unrecoverable errors: failures with `failure_type` in `{gap_unexpected, voice_mismatch}` trigger the script back-edge; all other types either requeue in the current phase or halt with `ClarificationRequest`.

**Critical V7 fix:** `UpdateScript` events now consume resolved failures. When the parser extracts `UpdateScript` from Scenario Agent output to fix a `voice_mismatch` or `gap_unexpected`, the `JobProjection` removes the matching failures from the list. This prevents infinite script-rewrite loops.


---

### 6.4 VM Projection

#### 6.4.1 VM inventory: instance_id → {status, role, cost, worker_url}

`VMProjection` maintains a pure read model of the VM fleet. It applies `VMAllocated`, `VMDeallocated`, `VMObserved`, and `VMProvisionFailed` events. Each VM record tracks:

| Field | Type | Meaning |
|---|---|---|
| `status` | `str` | `active`, `destroyed`, `provisioning`, `failed` |
| `role` | `str` | `tts`, `video`, or `whisperx` — the job type this VM serves |
| `offer_id` | `str` | Vast.ai offer ID used for provisioning |
| `worker_url` | `str` | HTTP endpoint of the VM agent process |
| `hourly_rate_usd` | `float` | Cost per hour for this instance |
| `started_at` | `float` | Unix timestamp of allocation |
| `observed_status` | `str` | Last status from `VMObserved` (may differ from event-derived status) |

```python
from dataclasses import dataclass, field


@dataclass
class VMRecord:
    """Read-only record of a single VM's state."""

    instance_id: str
    status: str = "active"
    role: str = ""
    offer_id: str = ""
    worker_url: str = ""
    hourly_rate_usd: float = 0.0
    started_at: float = 0.0
    observed_status: Optional[str] = None


class VMProjection(Projection):
    """Pure read model of the VM fleet. No polling, no event emission.

    The Provisioner emits ``VMObserved`` effects when Vast.ai state diverges
    from event-derived state; this projection applies them passively.
    """

    def __init__(self) -> None:
        super().__init__()
        self.vms: dict[str, VMRecord] = {}

    def apply(self, event: Effect) -> None:
        match event.kind:
            case "vm_allocated":
                self.vms[event.instance_id] = VMRecord(
                    instance_id=event.instance_id,
                    status="active",
                    role=getattr(event, "role", ""),
                    offer_id=getattr(event, "offer_id", ""),
                    worker_url=getattr(event, "worker_url", ""),
                    hourly_rate_usd=getattr(event, "cost_per_hour", 0.0),
                    started_at=getattr(event, "timestamp", 0.0),
                )
            case "vm_deallocated":
                rec = self.vms.get(event.instance_id)
                if rec:
                    rec.status = "destroyed"
            case "vm_observed":
                rec = self.vms.get(event.instance_id)
                if rec:
                    rec.observed_status = getattr(event, "observed_status", None)
                    # If Vast.ai reports the instance gone but events say active,
                    # update status to reflect reality (Provisioner handles cleanup).
                    if rec.observed_status == "not_found" and rec.status == "active":
                        rec.status = "observed_gone"
            case "vm_provision_failed":
                # No VM record created — failure is logged by the Provisioner.
                pass

    def active_vms(self, role: Optional[str] = None) -> list[VMRecord]:
        """Return VMs with status == 'active', optionally filtered by role."""
        return [
            v for v in self.vms.values()
            if v.status == "active" and (role is None or v.role == role)
        ]

    def estimated_hourly_cost(self) -> float:
        """Sum of hourly rates for all active VMs."""
        return sum(v.hourly_rate_usd for v in self.active_vms())

    def summary(self) -> str:
        active = len(self.active_vms())
        total = len(self.vms)
        cost_hr = self.estimated_hourly_cost()
        roles: dict[str, int] = defaultdict(int)
        for v in self.active_vms():
            roles[v.role] += 1
        role_str = ", ".join(f"{k}={v}" for k, v in roles.items())
        return f"VMs: {active}/{total} active, ${cost_hr:.4f}/hr ({role_str})"
```

#### 6.4.2 Pure read model — no polling, no event emission

`VMProjection` has no `poll_vastai()` method. Vast.ai drift detection lives in the Provisioner agent. The Provisioner queries the GSA for `VMProjection` state, runs Vast.ai CLI commands via its bash tool, compares Vast.ai reality against projection state, and describes divergence in its natural language output. The parser extracts `VMObserved` effects from that text. This preserves the projection invariant: projections are read models only; they consume events, they do not produce them.

---

### 6.5 Budget Projection

#### 6.5.1 Budget cap, spent, and remaining tracking

`BudgetProjection` tracks the pipeline's financial state. It applies `BudgetSet`, `VMDeallocated`, and `PipelineAborted` events. The projection computes `remaining_usd` dynamically and flags when the budget is exceeded.

```python
class BudgetProjection(Projection):
    """Tracks budget cap, cumulative spend, and per-run cost accrual.

    The budget cap is set once per run via BudgetSet (typically at pipeline
    start). All subsequent VM costs are accumulated from `VMDeallocated.final_cost`
    when VMs are destroyed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.budget_cap_usd: float = 0.0
        self.spent_usd: float = 0.0
        self.vm_costs: dict[str, float] = {}  # instance_id -> accumulated cost
        self.exceeded: bool = False
        self.exceeded_at: Optional[float] = None

    def apply(self, event: Effect) -> None:
        match event.kind:
            case "budget_set":
                self.budget_cap_usd = getattr(event, "budget_usd", 0.0)
                self.exceeded = False
            case "vm_deallocated":
                instance_id = getattr(event, "instance_id", "")
                cost = getattr(event, "final_cost", 0.0)
                if instance_id:
                    self.vm_costs[instance_id] = self.vm_costs.get(instance_id, 0.0) + cost
                self.spent_usd += cost
                if not self.exceeded and self.budget_cap_usd > 0 and self.spent_usd > self.budget_cap_usd:
                    self.exceeded = True
                    self.exceeded_at = getattr(event, "timestamp", 0.0)
            case "pipeline_aborted":
                if getattr(event, "reason", "") == "budget_exceeded":
                    self.exceeded = True

    def remaining_usd(self) -> float:
        """Return remaining budget. Negative if exceeded."""
        return self.budget_cap_usd - self.spent_usd

    def summary(self) -> str:
        pct = (self.spent_usd / self.budget_cap_usd * 100) if self.budget_cap_usd > 0 else 0.0
        status = "EXCEEDED" if self.exceeded else "OK"
        return (
            f"Budget: ${self.spent_usd:.2f} / ${self.budget_cap_usd:.2f} "
            f"({pct:.1f}%, {status})"
        )
```

**Budget enforcement.** The handler checks `BudgetProjection.exceeded` before appending any new effect that would incur cost (e.g., `VMAllocated`). If exceeded, the handler rejects the effect and returns a `ClarificationRequest` to the agent. This is a soft guard — the agent can still emit `PipelineAborted` or `HumanInstruction` effects.

---

### 6.6 State Projection

#### 6.6.1 Current phase + transition history (descriptive only)

`StateProjection` tracks the emergent pipeline phase for human observation and the full history of phase changes. It does not enforce anything — agents decide what to do based on their own reading of projections.

```python
@dataclass
class PhaseChangeRecord:
    """A single phase change (descriptive, not a transition)."""

    from_phase: str
    to_phase: str
    reason: str
    at_sequence: int


class StateProjection(Projection):
    """Tracks emergent pipeline phase and phase change history.

    Also maintains a ring buffer of recent effects per agent for loop
    detection. Agents check this buffer on every turn.
    """

    def __init__(self, loop_buffer_size: int = 5) -> None:
        super().__init__()
        self.current_phase: str = "init"
        self.phase_history: list[PhaseChangeRecord] = []
        self.run_id: Optional[str] = None
        # Ring buffer: last N effects per agent (agent_name -> deque[Effect])
        self.recent_effects: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=loop_buffer_size)
        )
        self.loop_buffer_size: int = loop_buffer_size

    def apply(self, event: Effect) -> None:
        # Record effect in agent's ring buffer for loop detection
        agent = getattr(event, "agent", None) or getattr(event, "source_agent", "unknown")
        if agent:
            self.recent_effects[agent].append(event)

        match event.kind:
            case "pipeline_started":
                self.run_id = getattr(event, "run_id", None)
                self.current_phase = "init"
                self.phase_history.clear()
                self.recent_effects.clear()
            case "reconciliation_complete":
                if self.current_phase != "audio_reconcile":
                    self._record_phase_change("audio_reconcile")
            case "pipeline_complete":
                self._record_phase_change("done")
            case "pipeline_aborted":
                self._record_phase_change("aborted")
            case "merge_into_otio":
                # Crude phase inference for observation
                if event.track_name == "V1_Video" and self.current_phase == "audio_reconcile":
                    self._record_phase_change("video_production")

    def _record_phase_change(self, to_phase: str, reason: str = "") -> None:
        rec = PhaseChangeRecord(
            from_phase=self.current_phase,
            to_phase=to_phase,
            reason=reason,
            at_sequence=getattr(self, "last_sequence", 0),
        )
        self.phase_history.append(rec)
        self.current_phase = to_phase

    def get_recent_events(self, n: int) -> list[Effect]:
        """Return the last N effects across all agents."""
        all_events = []
        for agent_deque in self.recent_effects.values():
            all_events.extend(list(agent_deque))
        all_events.sort(key=lambda e: getattr(e, "timestamp", 0), reverse=True)
        return all_events[:n]

    def summary(self) -> str:
        tx_count = len(self.phase_history)
        return (
            f"Phase: {self.current_phase}, "
            f"{tx_count} phase changes, "
            f"{len(self.recent_effects)} agents tracked"
        )
```

#### 6.6.2 Loop detection buffer (last N effects per agent)

The `recent_effects` dictionary maps agent name to a `deque` of that agent's last `loop_buffer_size` effects (default 5). On every `apply`, the effect is appended to the deque for its agent. Because `deque` has `maxlen`, old effects are automatically evicted — the buffer is a fixed-size ring buffer with O(1) append and no allocation on overflow.

Agents use this buffer to detect two loop conditions:

1. **Duplicate effects**: all N entries in a deque are the same `kind` with the same key parameters.
2. **No progress**: after N effects from an agent, no projection state has changed (OTIO, jobs, or VM state delta is empty).

```python
    def detect_duplicate_loop(self, agent: str, threshold: int = 5) -> tuple[bool, str]:
        """Check if an agent has produced the same effect kind N times in a row.

        Returns (is_looping, reason).
        """
        buf = self.recent_effects.get(agent, deque())
        if len(buf) < threshold:
            return False, "insufficient history"
        kinds = [getattr(e, "kind", "") for e in buf]
        if len(set(kinds)) == 1:
            return True, f"{agent} produced {kinds[0]} {len(buf)} times"
        return False, "effects vary"

    def get_recent_kinds(self, agent: str) -> list[str]:
        """Return the list of recent effect kinds for an agent."""
        return [getattr(e, "kind", "") for e in self.recent_effects.get(agent, [])]
```

When either condition triggers, the parser extracts `AgentLoopDetected` from agent output with context (agent name, effect history, projection delta) and halts; the parser extracts `ClarificationRequest` for human review. The threshold is configurable per agent (default 5) via the agent's config table.

---

### 6.7 Projection Response Schemas

Projection classes (§6.2–§6.6) are mutable event consumers with methods. They are **not** Pydantic models and cannot be serialized directly over HTTP. The Global State Agent (§2.4.2) returns a `GlobalStateResponse` containing the serializable state of every projection. This section defines the Pydantic response schemas.

#### 6.7.1 OTIO Slot State

```python
class OTIOSlotState(BaseModel):
    """Serializable state of a single slot in the OTIO timeline."""
    scene_num: int
    block_id: str
    speaker: str
    text: str
    scripted_sec: float
    measured_sec: float | None = None
    status: Literal["scripted", "measured", "delivered", "dirty"] = "scripted"
    artifact_uri: str | None = None


class OTIOResponse(BaseModel):
    """Serializable OTIO projection state for GSA GET /."""
    scenes: int = Field(..., description="number of distinct scene numbers")
    total_slots: int
    measured_slots: int
    delivered_slots: int
    dirty_slots: int
    duration_sec: float = Field(..., description="total timeline duration in seconds")
    slots: dict[str, OTIOSlotState] = Field(..., description="slot_addr -> state")
```

#### 6.7.2 Job Response

```python
class JobResponseItem(BaseModel):
    """Serializable state of a single job."""
    job_id: str
    job_type: Literal["tts", "ltx"]
    slot_id: str
    status: Literal["pending", "running", "completed", "failed"]
    params: dict = Field(default_factory=dict)
    artifact_uri: str | None = None
    duration_sec: float | None = None
    error_message: str | None = None
    requeue_count: int = 0
    created_at: float = 0.0
    completed_at: float | None = None


class JobResponse(BaseModel):
    """Serializable Job projection state for GSA GET /."""
    jobs: dict[str, JobResponseItem] = Field(..., description="job_id -> state")
    reconciliation_complete: bool = False
    dirty_blocks: list[str] = Field(default_factory=list, description="slot_addrs needing work")
    clean_blocks: list[str] = Field(default_factory=list, description="slot_addrs with measured audio")
    block_attempts: dict[str, int] = Field(default_factory=dict, description="slot_addr -> attempt count")
    spent_usd: float = 0.0
    production_failures: list[dict] = Field(default_factory=list)
```

#### 6.7.3 VM Response

```python
class VMResponseItem(BaseModel):
    """Serializable state of a single VM."""
    instance_id: str
    status: Literal["active", "destroyed", "observed_gone"]
    role: Literal["tts", "ltx", ""]
    offer_id: str = ""
    worker_url: str = ""
    hourly_rate_usd: float = 0.0
    started_at: float = 0.0
    observed_status: str | None = None


class VMResponse(BaseModel):
    """Serializable VM projection state for GSA GET /."""
    vms: dict[str, VMResponseItem] = Field(..., description="instance_id -> state")
    active_count: int
    total_count: int
    estimated_hourly_cost_usd: float
    role_breakdown: dict[str, int] = Field(default_factory=dict, description="role -> active count")
```

#### 6.7.4 State Response

```python
class PhaseChangeItem(BaseModel):
    """Serializable phase change record."""
    from_phase: str
    to_phase: str
    reason: str
    at_sequence: int


class StateResponse(BaseModel):
    """Serializable State projection state for GSA GET /."""
    current_phase: str = "init"
    phase_changes: list[PhaseChangeItem] = Field(default_factory=list)
    agents_tracked: list[str] = Field(default_factory=list)
    latest_sequence: int = 0
```

#### 6.7.5 Budget Response

```python
class BudgetResponse(BaseModel):
    """Serializable Budget projection state for GSA GET /."""
    budget_cap_usd: float = 0.0
    spent_usd: float = 0.0
    remaining_usd: float = 0.0
    exceeded: bool = False
    vm_costs: dict[str, float] = Field(default_factory=dict, description="instance_id -> accumulated cost")
```

#### 6.7.6 GlobalStateResponse (updated)

```python
class GlobalStateResponse(BaseModel):
    """Response from GET / on the Global State Agent."""
    run_id: str
    timestamp: float
    otio: OTIOResponse          # §6.7.1
    jobs: JobResponse           # §6.7.2
    vms: VMResponse             # §6.7.3
    state: StateResponse        # §6.7.4
    budget: BudgetResponse      # §6.7.5
    latest_sequence: int        # highest event sequence number included
```

**Size estimate.** A typical mid-run documentary produces ~50 slots, 20 jobs, 3 VMs, and 5 phase changes. Serialized JSON:
- `OTIOResponse`: ~15 KB (slots contain full narration text)
- `JobResponse`: ~4 KB
- `VMResponse`: ~1 KB
- `StateResponse`: ~0.5 KB
- `BudgetResponse`: ~0.5 KB
- **Total: ~21 KB** (before compression; ~3.5 KB with gzip)

---

## 7. Situation Types (Agent Guidance)

These are the situation types an agent should look for when scanning the projection bundle received from the Global State Agent. They are not a Python class — they are guidance text embedded in the agent's system prompt. The agent scans the GSA response directly and decides which situations apply.

### 7.1 Situation Types

| Type | Trigger | Description |
|---|---|---|
| `fresh_dirty_block` | Block exists, dirty, attempts < max | New/requeued block needs work |
| `measurement_complete_pass` | Block measured, within tolerance | Block passed reconciliation |
| `measurement_complete_fail` | Block measured, outside tolerance | Block failed, needs retry |
| `block_at_max_attempts` | Block dirty, attempts == max | Exhausted, needs escalation |
| `vm_stale` | VM last_seen > threshold | VM not reporting, may be dead |
| `vm_provision_failed` | `VMProvisionFailed` exists | Could not create VM |
| `job_queued_long` | Job queued > threshold | Job waiting too long for VM |
| `reconciliation_complete_all` | All blocks pass | Audio pipeline done |
| `reconciliation_partial_some` | Some pass, some dirty | Partial progress, continue loop |
| `assembly_ready` | All video approved | Ready for final assembly |
| `pipeline_budget_warning` | Spent > 80% of limit | Warning level |
| `pipeline_budget_critical` | Spent > 95% of limit | Critical, may abort |
| `agent_loop_detected` | Duplicate effects or no progress | Agent stuck |
| `human_instruction_pending` | `HumanInstruction` unread | Human input waiting |
| `noop_all_clean` | Nothing dirty, nothing queued | Idle, waiting |

### 7.2 Narrative Template Format

```
=== SITE: {slot_id} ===
{text_snippet}
TARGET: {target_sec}s | MEASURED: {measured_sec}s | DELTA: {delta_sec}s
ATTEMPTS: {attempts}/{max_attempts} | VERDICT: {verdict}

WHAT'S HAPPENING:
{situation_narrative}
===
```

### 7.3 Rules for Narrative Generation

1. **Dirty blocks get full narrative** — all fields, history, guidance
2. **Clean blocks get one line** — slot_id, verdict, measured duration
3. **Failed blocks get extra context** — previous attempts, error history
4. **Max-attempt blocks get escalation options** — accept, human, abort
5. **VM issues get infrastructure narrative** — not artistic
6. **Budget issues get fiscal narrative** — spent, limit, remaining

---

### 7.4 Situation Narrative Builder (Complete Specification)

The narrative builder is the bridge between projection state (§6) and agent LLM prompts. It transforms raw projection data into natural-language narratives that agents consume as their user prompt. This section defines every function, template, and rule.

#### 7.4.1 derive_situations() — Projection → Situation Objects

```python
from dataclasses import dataclass
from typing import Literal


SituationType = Literal[
    "fresh_dirty_block", "measurement_complete_pass", "measurement_complete_fail",
    "block_at_max_attempts", "vm_stale", "vm_provision_failed", "job_queued_long",
    "reconciliation_complete_all", "reconciliation_partial_some",
    "assembly_ready", "pipeline_budget_warning", "pipeline_budget_critical",
    "agent_loop_detected", "human_instruction_pending", "noop_all_clean",
]


@dataclass
class Situation:
    """A single situation detected from projection state."""
    type: SituationType
    priority: int           # 1=highest (safety), 5=lowest (work)
    slot_id: str = ""       # which slot this situation refers to
    facts: dict = None      # template variables

    def __post_init__(self):
        if self.facts is None:
            self.facts = {}
```

```python
def derive_situations(
    projections: GlobalStateResponse,
    role: Literal["scenario", "audio", "video", "assembly", "provisioner"],
    config: PipelineConfig,
) -> list[Situation]:
    """Scan projections and return all active situations for this agent role.

    Situations are ordered by priority (lowest number first).
    The agent's RULES block (§4.1) tells it which situation to act on.
    """
    situations: list[Situation] = []
    otio = projections.otio
    jobs = projections.jobs
    vms = projections.vms
    state = projections.state
    budget = projections.budget

    # --- Safety (priority 1) ---
    if budget.exceeded:
        situations.append(Situation(
            type="pipeline_budget_critical",
            priority=1,
            facts={"spent_usd": budget.spent_usd, "cap_usd": budget.budget_cap_usd},
        ))
    elif budget.remaining_usd < budget.budget_cap_usd * 0.05:
        situations.append(Situation(
            type="pipeline_budget_warning",
            priority=1,
            facts={"spent_usd": budget.spent_usd, "cap_usd": budget.budget_cap_usd,
                   "remaining_usd": budget.remaining_usd},
        ))

    # --- Blocked / infrastructure (priority 2) ---
    if role == "provisioner":
        for vm in vms.vms.values():
            if vm.status == "active" and vm.observed_status == "not_found":
                situations.append(Situation(
                    type="vm_stale",
                    priority=2,
                    facts={"instance_id": vm.instance_id, "role": vm.role},
                ))
        for job in jobs.jobs.values():
            if job.status == "pending" and job.created_at > 0:
                queued_sec = time.time() - job.created_at
                if queued_sec > config.max_queue_wait_sec:
                    situations.append(Situation(
                        type="job_queued_long",
                        priority=2,
                        facts={"job_id": job.job_id, "queued_sec": int(queued_sec),
                               "slot_id": job.slot_id},
                    ))

    # --- Work (priority 3–5) ---
    if role == "scenario":
        unfilled = [addr for addr, slot in otio.slots.items()
                    if slot.status == "scripted"]
        if unfilled:
            situations.append(Situation(
                type="fresh_dirty_block",
                priority=3,
                facts={"count": len(unfilled), "slots": unfilled[:5]},
            ))

    if role == "audio":
        dirty = [addr for addr, slot in otio.slots.items()
                 if slot.status == "dirty"]
        for addr in dirty:
            slot = otio.slots[addr]
            attempts = jobs.block_attempts.get(addr, 0)
            if attempts >= config.max_attempts:
                situations.append(Situation(
                    type="block_at_max_attempts",
                    priority=2,
                    slot_id=addr,
                    facts={"slot_id": addr, "attempts": attempts,
                           "max_attempts": config.max_attempts,
                           "scripted_sec": slot.scripted_sec,
                           "measured_sec": slot.measured_sec},
                ))
            else:
                situations.append(Situation(
                    type="fresh_dirty_block",
                    priority=3,
                    slot_id=addr,
                    facts={"slot_id": addr, "attempts": attempts,
                           "scripted_sec": slot.scripted_sec,
                           "text_snippet": slot.text[:200]},
                ))

        measured = [addr for addr, slot in otio.slots.items()
                    if slot.status == "measured"]
        for addr in measured:
            slot = otio.slots[addr]
            delta = abs((slot.measured_sec or 0) - slot.scripted_sec)
            tolerance = max(slot.scripted_sec * 0.15, 0.25)
            if delta <= tolerance:
                situations.append(Situation(
                    type="measurement_complete_pass",
                    priority=4,
                    slot_id=addr,
                    facts={"slot_id": addr, "measured_sec": slot.measured_sec,
                           "scripted_sec": slot.scripted_sec, "delta_sec": delta},
                ))
            else:
                situations.append(Situation(
                    type="measurement_complete_fail",
                    priority=3,
                    slot_id=addr,
                    facts={"slot_id": addr, "measured_sec": slot.measured_sec,
                           "scripted_sec": slot.scripted_sec, "delta_sec": delta,
                           "tolerance_sec": tolerance},
                ))

    if role == "video":
        pending_ltx = [j for j in jobs.jobs.values()
                       if j.job_type == "ltx" and j.status in ("pending", "running")]
        if pending_ltx:
            situations.append(Situation(
                type="fresh_dirty_block",
                priority=3,
                facts={"count": len(pending_ltx), "jobs": [j.job_id for j in pending_ltx]},
            ))

    if role == "assembly":
        if otio.dirty_slots == 0 and otio.delivered_slots == otio.total_slots:
            situations.append(Situation(
                type="assembly_ready",
                priority=3,
                facts={"total_slots": otio.total_slots,
                       "duration_sec": otio.duration_sec},
            ))

    # --- No-op (priority 5) ---
    if not situations:
        situations.append(Situation(
            type="noop_all_clean",
            priority=5,
            facts={"current_phase": state.current_phase},
        ))

    situations.sort(key=lambda s: s.priority)
    return situations
```

#### 7.4.2 SITUATION_TEMPLATES — Exact Output Strings

Each `Situation` is rendered through a template. The output is **natural language**, not structured data.

```python
SITUATION_TEMPLATES: dict[SituationType, str] = {
    # Safety
    "pipeline_budget_critical": (
        "🚨 BUDGET CRITICAL: Spent ${spent_usd:.2f} / ${cap_usd:.2f}. "
        "The pipeline has exceeded its budget cap. "
        "You MUST emit PipelineAborted immediately. No other work."
    ),
    "pipeline_budget_warning": (
        "⚠️ BUDGET WARNING: Spent ${spent_usd:.2f} / ${cap_usd:.2f} "
        "(remaining: ${remaining_usd:.2f}). Approaching limit. "
        "Consider cost-saving measures."
    ),

    # Blocked / infrastructure
    "vm_stale": (
        "VM {instance_id} ({role}) is stale — Vast.ai reports it gone "
        "but events say active. The Provisioner should investigate."
    ),
    "job_queued_long": (
        "Job {job_id} (slot {slot_id}) has been queued for {queued_sec}s. "
        "No VM picked it up. The Provisioner may need to allocate more VMs."
    ),

    # Work — dirty blocks
    "fresh_dirty_block": (
        "=== SITE: {slot_id} ===\n"
        "{text_snippet}\n"
        "TARGET: {scripted_sec}s | ATTEMPTS: {attempts}/{max_attempts}\n"
        "WHAT'S HAPPENING: This block needs audio generation and measurement.\n"
        "WHAT TO DO: Emit QueueJob(job_type='tts', ...) for this block."
    ),

    # Work — measurement results
    "measurement_complete_pass": (
        "=== SITE: {slot_id} ===\n"
        "TARGET: {scripted_sec}s | MEASURED: {measured_sec}s | DELTA: {delta_sec}s\n"
        "VERDICT: PASS (within tolerance)\n"
        "WHAT TO DO: Emit DurationAdjusted for this block."
    ),
    "measurement_complete_fail": (
        "=== SITE: {slot_id} ===\n"
        "TARGET: {scripted_sec}s | MEASURED: {measured_sec}s | DELTA: {delta_sec}s\n"
        "TOLERANCE: {tolerance_sec}s\n"
        "VERDICT: FAIL (outside tolerance)\n"
        "WHAT'S HAPPENING: The measured audio is too different from the script target.\n"
        "WHAT TO DO: Emit JobRequeued with adjusted TTS params (speed, voice, or text)."
    ),

    # Escalation
    "block_at_max_attempts": (
        "=== SITE: {slot_id} ===\n"
        "TARGET: {scripted_sec}s | MEASURED: {measured_sec}s\n"
        "ATTEMPTS: {attempts}/{max_attempts} — MAXED OUT\n"
        "WHAT'S HAPPENING: This block has failed reconciliation {attempts} times.\n"
        "WHAT TO DO: Escalate. Options:\n"
        "  1. Accept the mismatch (emit DurationAdjusted with note)\n"
        "  2. Request human guidance (emit ClarificationRequest)\n"
        "  3. Abort the pipeline (emit PipelineAborted)"
    ),

    # Assembly
    "assembly_ready": (
        "All {total_slots} slots are filled with approved media. "
        "Timeline duration: {duration_sec}s.\n"
        "WHAT TO DO: Run ffmpeg to assemble final_documentary.mp4, then emit PipelineComplete."
    ),

    # No-op
    "noop_all_clean": (
        "Nothing requires action. Current phase: {current_phase}. "
        "Emit NoOp with a brief status note."
    ),
}
```

#### 7.4.3 build_narrative() — Situations → User Prompt

```python
def build_narrative(
    situations: list[Situation],
    role: str,
    projections: GlobalStateResponse,
) -> str:
    """Render all situations into the agent's user prompt."""
    parts: list[str] = []

    # Header: what phase and what the agent's job is
    parts.append(f"=== PIPELINE PHASE: {projections.state.current_phase} ===")
    parts.append(f"=== YOUR ROLE: {role.upper()} AGENT ===\n")

    # Situation narratives (already sorted by priority)
    for s in situations:
        template = SITUATION_TEMPLATES[s.type]
        rendered = template.format(**s.facts)
        parts.append(rendered)
        parts.append("")  # blank line between situations

    # Footer: global context (always included)
    parts.append("=== GLOBAL CONTEXT ===")
    parts.append(f"Budget: {projections.budget.summary()}")
    parts.append(f"VMs: {projections.vms.active_count} active")
    parts.append(f"Jobs: {len(projections.jobs.jobs)} total")
    parts.append(f"Latest event sequence: {projections.latest_sequence}")

    return "\n".join(parts)
```

#### 7.4.4 Historical Context Injection (Last 5 Turns)

The agent's `message_history` carries the last 5 turns as memory. These are **not** part of the user prompt — they are passed to `agent.run()` as `message_history` so the LLM sees them as prior conversation turns.

```python
async def read_agent_events(
    run_id: str,
    agent: str,
    limit: int = 5,
) -> list[Effect]:
    """Read the last N effects emitted by a specific agent from EventStoreDB.

    This queries the run's stream and filters by agent name. It is O(N) over
    the stream but N is small (typical run: 500–2000 events). The GSA does
    NOT maintain an agent-indexed view; this is a direct stream scan.
    """
    events = await replay(run_id)  # full stream read; could optimize with reverse read
    agent_events = [
        _parse_payload(e["kind"], e["payload_json"])
        for e in events
        if json.loads(e.get("metadata", "{}")).get("agent") == agent
    ]
    return agent_events[-limit:]


async def build_memory(
    run_id: str,
    agent: str,
    limit: int = 5,
) -> list[UserMessage]:
    """Fetch the last N effects emitted by this agent and format as memory."""
    events = await read_agent_events(run_id, agent, limit=limit)

    memory: list[UserMessage] = []
    for evt in events:
        ts = datetime.fromtimestamp(evt.timestamp).strftime("%H:%M:%S")
        kind = evt.kind
        payload = evt.model_dump_json(exclude={"effect_id", "timestamp", "agent", "kind"})
        memory.append(UserMessage(
            content=f"[MEMORY {ts}] You emitted {kind}: {payload}"
        ))

    return memory
```

**What memory contains:** Only effects previously emitted by THIS agent. Effects from other agents are invisible (the agent reads them via GSA projections on each turn, not via message history). This prevents stale state — the agent always sees current projections, not stale history.

**Why 5 turns:** Empirically, 5 turns captures the agent's recent reasoning context without overwhelming the token budget. Each memory entry is ~100–300 tokens; 5 entries = ~1K tokens, leaving room for the narrative (~2–5K tokens) and system prompt (~1K tokens).

#### 7.4.5 Memory Persistence and Agent Restart

**Does an agent restart lose its memory? No.** Memory is rebuilt from EventStoreDB on every turn. It is not stored in the agent process. When an agent restarts:

1. The new process receives a `POST /` with `run_id`.
2. The handler calls `build_memory(run_id, agent, limit=5)`.
3. `build_memory` queries EventStoreDB for the last 5 effects emitted by this agent.
4. It reconstructs the exact same memory that the previous process would have built.

**Does behavior change post-restart? No.** The reconstruction is deterministic: same event stream → same 5 most recent effects → same memory messages → same LLM context. The agent's behavior is fully determined by:
- The event stream (durable in EventStoreDB)
- The GSA projections (rebuilt from the event stream)
- The narrative builder (deterministic function of projections)
- The memory builder (deterministic function of the event stream)
- The system prompt (static per agent role)

**What IS lost on restart:** The pydantic-deep internal `message_history` (the raw LLM request/response pairs from prior turns) is lost. But this is irrelevant — it is not used for decision-making. The agent's "memory" is the last 5 effects from EventStoreDB, not the internal LLM conversation history. pydantic-deep's context compaction operates on the current turn only; prior turns' raw responses are not needed because the agent's decisions are already captured as effects in the event stream.

**Why not persist memory in the agent process:** Principle 8 ("Agent memory does not persist in process") keeps agents stateless. An agent process can be killed and restarted without losing context. This simplifies deployment, scaling, and recovery.

#### 7.4.6 "What Happened" vs "What Should Happen Next"

The narrative template intentionally separates these:

| Section | Content | Source |
|---|---|---|
| **"WHAT'S HAPPENING"** | Factual state from projections | `otio.slots`, `jobs.jobs`, `vms.vms` |
| **"WHAT TO DO"** | Suggested action based on agent's RULES block | Template text + situation type |

**"WHAT'S HAPPENING" is authoritative.** It describes the current state: slot A1:3:2 has measured_sec=4.1s and scripted_sec=3.5s, delta exceeds tolerance. This comes from projections.

**"WHAT TO DO" is a hint, not a command.** It suggests the agent emit `JobRequeued` with adjusted params, but the agent may choose differently (e.g., emit `ClarificationRequest` if it disagrees with the measurement). The agent's RULES block (§4.1) has the final say.

This separation prevents the narrative from becoming a deterministic instruction. The agent is free to ignore "WHAT TO DO" if its reasoning leads elsewhere — but it must still respect safety rules (budget critical, loop detected).

#### 7.4.7 Subagent Narrative Subsetting

When the main agent delegates to a subagent via `task()`, the subagent receives a **chiseled subset** of the narrative, not the full prompt. The main agent constructs a focused task description.

```python
# Inside the main agent's tool call
async def task_script_drafter(slot_id: str, text: str, target_sec: float) -> str:
    """Delegate script drafting to the script-drafter subagent."""
    subagent = get_subagent("script-drafter")

    # Subagent gets ONLY the slot it needs, not the full pipeline state
    focused_narrative = (
        f"Draft narration for slot {slot_id}.\n"
        f"Topic context: {text[:500]}\n"
        f"Target duration: {target_sec}s (~{int(target_sec * 2.5)} words)\n"
        f"Output three versions (V1, V2, V3) and visual notes."
    )

    result = await subagent.run(user_prompt=focused_narrative)
    return result.output
```

**Subagent receives:**
- The task-specific context (one slot, one topic)
- No global budget / VM / job state
- No memory from prior turns (subagents are stateless)
- A shorter system prompt without the full RULES block

**Why subset:** Subagents are specialists (script drafter, voice tagger, audio measurer). Giving them the full pipeline state would confuse them with irrelevant data. The main agent acts as the orchestrator — it reads the full narrative, decides which subagent to call, and constructs a focused task.

**Subagent system prompts are role-specific and shorter:**

```
=== YOUR ROLE ===
You are the script-drafter subagent. You write narration text only.
You do NOT emit effects. You do NOT reason about pipeline state.
You receive a topic and duration, and you output V1/V2/V3 narration.

=== OUTPUT FORMAT ===
V1: [primary narration]
V2: [alternate narration]
V3: [third take]
Visual Notes: [shot descriptions]
Duration Estimate: [seconds]
```

Subagents output text that the main agent incorporates into its own reasoning. The main agent then emits the actual effects (`UpdateScript`, `QueueJob`, etc.) based on the subagent's output plus the full pipeline context.

---

## 8. Agent Architecture — pydantic-deep

All agents share a common infrastructure built on **pydantic-deep** (the real package, `pip install pydantic-deep`). Each agent is a **main agent** (not a subagent) created via `create_deep_agent()` with context compaction hooks. They differ only in role, permitted effects, and the focus function used for compaction.

In V7, each agent is wrapped in a lightweight **FastAPI ASGI application** that exposes `GET /` and `POST /`. The FastAPI handler:
1. Receives a wake notification or instruction payload on POST /
2. Replays events from EventStoreDB to reconstruct projections
3. Builds the situation narrative and memory
4. Calls `await agent.run(user_prompt=..., deps=DeepAgentDeps(...))`
5. Parses effects from the agent output
6. Appends effects to EventStoreDB
7. Returns the extracted effects (or a 202 Accepted) to the caller
8. May POST wake notifications to other agents

### 8.1 pydantic-deep Layer Stack

pydantic-deep provides a layered context management system. We use all layers, configured for pipeline agents:

```
Message history (situation narratives + memory + prior turns)
│
▼
┌─────────────────────────────┐
│ EvictionCapability          │ Saves large tool outputs (>20K tokens) to files
│ (default: on)               │
├─────────────────────────────┤
│ PatchToolCallsCapability    │ Fixes orphaned effect pairs (e.g. QueueJob without
│ (default: on)               │ JobCompleted due to interrupt)
├─────────────────────────────┤
│ SlidingWindowProcessor      │ Hard fallback: trims oldest messages if compaction
│ (trigger: fraction 0.95)    │ capability fails
├─────────────────────────────┤
│ HooksCapability             │ OUR HOOK: on_before_compress queries OTIO, determines
│ (on_before_compress hook)   │ focus, LLM-compacts history preserving task context
├─────────────────────────────┤
│ ContextManagerCapability    │ Token tracking + auto-trigger at 90% threshold
│ (default: on)               │
└─────────────────────────────┘
│
▼
Model request
```

| Layer | Source | Config | Purpose |
|---|---|---|---|
| `EvictionCapability` | pydantic-deep | `eviction_token_limit=20_000` | Bash outputs, ffprobe results, WhisperX JSON — saved to `/tmp/` |
| `PatchToolCallsCapability` | pydantic-deep | `patch_tool_calls=True` | Prevents orphaned `QueueJob` when agent is interrupted mid-turn |
| `SlidingWindowProcessor` | pydantic-deep | `trigger=("fraction", 0.95), keep=("fraction", 0.5)` | Last-resort hard trim; never splits causal pairs |
| `HooksCapability` | **Our code** | `on_before_compress=otio_aware_compress` | Queries OTIO projection → determines focus → LLM compacts |
| `ContextManagerCapability` | pydantic-deep | `context_manager_max_tokens=128_000` | Tracks tokens, calls `on_context_update`, triggers at 90% |

### 8.2 Agent Construction

```python
from pydantic_deep import create_deep_agent, DeepAgentDeps
from pydantic_deep.capabilities import HooksCapability

async def otio_aware_compress(ctx, messages, **kwargs):
    """Hook called by ContextManagerCapability before compression.

    Queries OTIO and Job projections via ctx.deps to determine focus,
    then calls a compaction LLM preserving task-relevant details.
    """
    otio = ctx.deps.projections["otio"]
    jobs = ctx.deps.projections["jobs"]
    focus = _determine_focus(ctx.deps.agent_role, otio, jobs)

    if not focus:
        return messages

    flat = _render_messages(messages)
    system = (
        f"Compress this agent context. Preserve everything related to: {focus}. "
        f"Keep all IDs, numbers, durations, verdicts, failure reasons. "
        f"Remove redundant pleasantries, old success details, clean blocks. "
        f"Output ONLY compressed context."
    )
    compressed = await llm_complete(system=system, user=flat, model=ctx.deps.compaction_model)
    return [SystemMessage(content="[Compacted]"), UserMessage(content=compressed)]

def create_pipeline_agent(role: str, config: Config):
    """Factory: create pydantic-deep agent with pipeline configuration.

    Explicitly disables all default capabilities that V7 rejects.
    """
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
        patch_tool_calls=True,
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
        on_cost_update=_emit_budget_effect,
        stuck_loop_detection=True,
        periodic_reminder=PeriodicReminderConfig(every_n_turns=10, first_after=5),
        capabilities=[
            ProvenanceCapability(agent_name=role),
        ],
        subagents=[
            SubAgentConfig(name="script-drafter", ...),
            SubAgentConfig(name="voice-tagger", ...),
            SubAgentConfig(name="audio-measurer", ...),
            SubAgentConfig(name="audio-reconciler", ...),
            SubAgentConfig(name="video-judger", ...),
            SubAgentConfig(name="final-muxer", ...),
        ],
        deps_type=PipelineDeps,
    )
    return agent
```

### 8.3 Prompt Construction

The agent's POST / handler constructs the **initial user prompt** containing the situation narrative. The agent's `instructions` (system prompt) contains the role description and effect format. Memory is injected via `message_history` from prior effects.

```python
async def run_agent_turn(agent, agent_situations, memory, projections, config):
    """Build prompt and run agent via pydantic-deep."""
    # Build situation narrative
    narrative = "\n\n".join(
        SITUATION_TEMPLATES[s.type].format(**s.facts)
        for s in agent_situations
    )

    # Memory from prior turns (last 5)
    history = [
        UserMessage(content=f"[MEMORY] {m}")
        for m in memory[-5:]
    ]

    # Run agent with deps carrying projections for compaction
    result = await agent.run(
        user_prompt=narrative,
        message_history=history,
        deps=PipelineDeps(
            projections=projections,
            agent_role=agent.role,
            max_tokens=config.max_tokens,
            compaction_model=config.compaction_model,
        ),
    )

    # Parse effects from result.output
    return parse_effects(result.output)
```

### 8.4 Context Compaction (Agent-Internal, via Hook)

**No caller-side compaction.** The POST / handler passes the full narrative. The agent's `HooksCapability.on_before_compress` handles compaction internally:

1. **ContextManagerCapability** counts tokens before each model call
2. If > 90% of budget, it calls `on_before_compress` hook
3. The hook queries OTIO via `ctx.deps.projections`
4. Determines focus (e.g., "block A1:3:2 reconciliation, attempt 2/3")
5. Calls compaction LLM with focus-prompt
6. Replaces message history with compressed version + focus marker
7. If compaction fails, `SlidingWindowProcessor` hard-trims oldest messages (never splitting causal pairs)

**Causal pair preservation:** The `SlidingWindowProcessor` uses a "safe cutoff" algorithm that walks backward from the cutoff point to find the nearest point between complete effect pairs. A pair is:
- `QueueJob` → `JobCompleted`/`JobFailed`
- `MeasurementRequested` → `AudioMeasured`
- `ScriptGenerated` → any effect referencing that script

### 8.5 How the Hook Queries OTIO

```python
def _determine_focus(role: str, otio: OTIOProjection, jobs: JobProjection) -> str:
    """Read OTIO state to determine what the agent is working on."""
    if role == "audio":
        dirty = [addr for addr, slot in otio.slots.items() if slot.get("status") == "dirty"]
        if dirty:
            addr = dirty[0]
            slot = otio.slots[addr]
            attempts = jobs.block_attempts.get(addr, 0)
            return (
                f"audio reconciliation of block {addr}, "
                f"attempt {attempts}/5, "
                f"measured {slot.get('measured_sec')}s vs target {slot.get('scripted_sec')}s"
            )
        return "audio pipeline — all blocks clean, awaiting instructions"

    if role == "video":
        pending = [j for j in jobs.jobs.values() if j.status in ("pending", "running") and j.job_type == "ltx"]
        if pending:
            return f"video generation for {len(pending)} pending LTX jobs"
        return "video pipeline — awaiting approved audio"

    if role == "scenario":
        unfilled = [addr for addr, slot in otio.slots.items() if slot.get("status") == "scripted"]
        if unfilled:
            return f"script writing: {len(unfilled)} unfilled slots"
        return "script refinement — all slots filled"

    if role == "assembly":
        return "final assembly — merging approved clips"

    return f"{role} agent — no active task"
```

---

## 9. Agents — Per-Agent Implementations

All agents are HTTP services (FastAPI apps) wrapping a pydantic-deep main agent constructed via `create_pipeline_agent()` (§8.2). They share the same compaction hook and sliding-window fallback. Each differs in:
- `ROLE_INSTRUCTIONS[role]` — system prompt with persona + RULES block
- `_determine_focus()` — focus extraction for compaction
- `_permitted_effects` — which effect kinds the parser will extract

The FastAPI app for every agent is identical in structure:

```python
from fastapi import FastAPI
from pydantic import BaseModel, Field
from typing import Literal

app = FastAPI()

# AgentPayload schema: see §2.2.2 (run_id, notification_type, context)
# AgentResponse schema: see §2.2.2 (status, effects_extracted, error_message, agent, timestamp)
# AgentHealthResponse schema: see §2.2.2 (status, agent, last_run, current_task, last_error, idle_since)

@app.get("/")
async def health():
    """Returns AgentHealthResponse (§2.2.2)."""
    return {"status": "healthy", "agent": AGENT_ROLE, "last_run": last_run_timestamp}

@app.post("/")
async def handle(payload: AgentPayload):
    """Accepts AgentPayload, returns AgentResponse (§2.2.2)."""
    # 1. Replay projections from EventStoreDB
    projections = await rebuild_projections(payload.run_id)

    # 2. Build situation narrative from projections
    situations = derive_situations(projections, AGENT_ROLE)

    # 3. Run agent
    effects = await run_agent_turn(agent, situations, memory, projections, config)

    # 4. Append effects to EventStoreDB
    for effect in effects:
        await append_effect(payload.run_id, effect)

    # 5. Notify downstream agents if needed
    await notify_downstream(effects, payload.run_id)

    return {"status": "ok", "effects_extracted": [e.kind for e in effects]}
```

### 9.1 Scenario Agent

```python
SCENARIO_INSTRUCTIONS = """
=== YOUR ROLE ===
You are the Scenario agent. You write and revise narration scripts.
Every block must specify speaker, duration_sec, and scene_num.

=== RULES ===
1. If agent_loop_detected -> emit ClarificationRequest and stop.
2. If pipeline_budget_critical -> emit PipelineAborted and stop.
3. If production_failed with failure_type in {gap_unexpected, voice_mismatch} ->
   rewrite affected scenes.
4. If gaps exist in OTIO -> parser extracts UpdateScript to fill them.
5. If noop_all_clean -> parser extracts NoOp, nothing to do.

Pick the highest-priority rule that applies. Only one action per turn.

=== PERMITTED EFFECTS ===
update_script, delete_scene, reorder_scenes, noop, clarification_request
"""
```

**Port:** 8001
**Effects:** `UpdateScript`, `DeleteScene`, `ReorderScenes`
**Focus:** Unfilled slots, script gaps, voice mismatches

### 9.2 Audio Agent

```python
AUDIO_INSTRUCTIONS = """
=== YOUR ROLE ===
You are the Audio agent. You own the narration reconciliation loop:
(1) Queue TTS jobs for dirty blocks. (2) On JobCompleted, run WhisperX ->
measure duration. (3) Compare measured vs scripted (±15% or ±0.25s):
within tolerance -> DurationAdjusted; outside -> ReconciliationFailed -> requeue.
(4) When all blocks clean, parser extracts ReconciliationComplete.

=== RULES ===
1. If agent_loop_detected -> parser extracts ClarificationRequest and stop.
2. If pipeline_budget_critical -> parser extracts PipelineAborted and stop.
3. If block_at_max_attempts -> parser extracts ReconciliationFailed(duration_unrecoverable).
4. If measurement_complete_fail -> requeue with adjusted params.
5. If fresh_dirty_block -> do the work (queue job, measure, judge).
6. If noop_all_clean -> emit NoOp, nothing to do.

Pick the highest-priority rule that applies. Only one action per turn.

=== PERMITTED EFFECTS ===
queue_job, job_approved, job_requeued, duration_adjusted,
reconciliation_failed, reconciliation_partial, reconciliation_complete,
noop, clarification_request
"""
```

**Port:** 8002
**Effects:** `QueueJob`, `JobApproved`, `JobRequeued`, `DurationAdjusted`, `ReconciliationFailed`, `ReconciliationComplete`
**Focus:** Dirty block reconciliation, attempt counts, tolerance checks
**Tolerance:** `max(scripted_sec × 0.15, 0.25)`
**Bounds:** Max 5 attempts per block, $2.00 TTS budget

### 9.3 Video Agent

```python
VIDEO_INSTRUCTIONS = """
=== YOUR ROLE ===
You are the Video agent. Generate LTX-2.3 clips using measured audio durations
as LAW. Queue ltx jobs, judge for visual coherence and artistic quality.
Emit JobApproved or JobRequeued. Merge approved clips via MergeIntoOTIO.
Continue until all video slots filled.

=== RULES ===
1. If agent_loop_detected -> emit ClarificationRequest and stop.
2. If pipeline_budget_critical -> emit PipelineAborted and stop.
3. If fresh_dirty_block (video slot unfilled) -> queue LTX job.
4. If job completed -> judge quality, approve or requeue.
5. If noop_all_clean -> parser extracts NoOp, nothing to do.

Pick the highest-priority rule that applies. Only one action per turn.

=== PERMITTED EFFECTS ===
queue_job, job_approved, job_requeued, merge_into_otio,
noop, clarification_request
"""
```

**Port:** 8003
**Effects:** `QueueJob`, `JobApproved`, `JobRequeued`, `MergeIntoOTIO`
**Focus:** Pending LTX jobs, video slot fill rate

### 9.4 Assembly Agent

```python
ASSEMBLY_INSTRUCTIONS = """
=== YOUR ROLE ===
You are the Assembly agent. Run ffmpeg to compose all approved audio and video
clips from OTIO into final_documentary.mp4. Validate OTIO before assembly and
verify output after. If all checks pass, parser extracts PipelineComplete.
If any check fails, parser extracts ProductionFailed with failure_type.

=== RULES ===
1. If agent_loop_detected -> parser extracts ClarificationRequest and stop.
2. If pipeline_budget_critical -> parser extracts PipelineAborted and stop.
3. If validation fails -> parser extracts ProductionFailed with appropriate failure_type.
4. If all checks pass -> parser extracts PipelineComplete.
5. If noop_all_clean -> parser extracts NoOp, nothing to do.

Pick the highest-priority rule that applies. Only one action per turn.

=== PERMITTED EFFECTS ===
pipeline_complete, production_failed, noop, clarification_request
"""
```

**Port:** 8005
**Effects:** `PipelineComplete`, `ProductionFailed`
**Focus:** OTIO validation, ffmpeg composition, output verification


---

## 9.5 Effect Parser — Semantic Extraction Pipeline

The parser is the **only** component that creates typed `Effect` objects from raw text. Agents produce natural language text and nothing else. They do not emit `EFFECT:` markers, JSON, XML, labeled sections, or any structured format. They do not know the parser exists. The parser is a post-processing step that extracts structured effects from genuinely free-form prose.

**ALL extraction complexity lives in the parser.** The parser is expected to be very complex — semantic understanding, context awareness, category-conditioned extraction, discriminated unions, field validators, reasking logic. This complexity is deliberate and welcome. What is forbidden is pushing any of this complexity onto the agent by requiring structured output.

The parser uses a **single-phase semantic extraction pipeline** via `instructor` + `deepseek-v4-flash` with strict discriminated-union validation. There are no deterministic fast paths, no regex, no keyword matching, no section scanning. Every extraction is semantic.

#### 9.5.1.1 Per-Effect-Type Discriminated Models

Instead of a single monolithic model with all fields, the parser uses **per-effect-type Pydantic models** with `Literal` discriminants. This constrains the LLM to valid effect types and ensures field-level validation.

```python
class _UpdateScriptEffect(BaseModel):
    effect_type: Literal["UpdateScript"]
    narration_v1: str
    narration_v2: str = ""
    narration_v3: str = ""
    visual_notes: str = ""
    dopamine_hook: str = ""
    pronunciation_hints: str = ""
    duration_sec: int = 30
    scene_num: int = 1

    @field_validator("narration_v1", mode="before")
    @classmethod
    def _v1_must_be_real(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v.strip()) < 5:
            raise ValueError("narration_v1 must contain actual script text, not a placeholder")
        return v
```

Each effect type has its own model with:
- `effect_type: Literal["<kind>"]` — discriminant for union dispatch
- Required fields with no defaults (trigger instructor reasking if missing)
- Optional fields with sensible defaults
- `@field_validator` for semantic validation (see §9.5.3)

The models are assembled into a discriminated union:

```python
_EffectUnion = Annotated[
    Union[
        _NoOpEffect, _UpdateScriptEffect, _GenerateNarrationAudioEffect,
        _RenderVideoSegmentEffect, _VMAllocatedEffect, _VMDeallocatedEffect,
        _VMProvisionFailedEffect, _MergeIntoOTIOEffect, _JobStartedEffect,
        _JobCompletedEffect, _JobFailedEffect, _JobQuestionReceivedEffect,
        _JobQuestionAnsweredEffect, _QAPassedEffect, _QAFailedEffect,
        _JobRequeuedEffect,
    ],
    Field(discriminator="effect_type"),
]
```

The container model includes chain-of-thought and confidence:

```python
class _MultiEffect(BaseModel):
    chain_of_thought: str = Field(
        description="Step-by-step reasoning: what did you find? What effects? Why?"
    )
    effects: list[_EffectUnion] = Field(
        description="List of extracted effects. Empty if no actionable data. NEVER hallucinate."
    )
    confidence: int = Field(ge=0, le=10, description="Confidence 0=empty, 10=perfect")
```

#### 9.5.1.2 System Prompt

The parser sends a system prompt that describes the task, effect types, and extraction rules:

```python
_SYSTEM_PROMPT = """\
You are an expert document parser for a documentary pipeline.

Your job: read free-form agent text and extract structured EFFECTS.

CRITICAL RULES:
1. Agents write in natural prose. Your job is to FIND the concrete data.
2. NEVER hallucinate. If the agent mentions an action but doesn't give actual data, DO NOT invent it.
3. Extract FULL CONTENT, not summaries. If the agent quotes narration, extract the complete quote.
4. If no actionable data exists, return an empty effects list.
5. Rate your confidence (0-10).

EFFECT TYPES:
- UpdateScript: Script changes. Extract narration_v1, narration_v2, narration_v3, visual_notes, dopamine_hook, pronunciation_hints, duration_sec, scene_num.
- GenerateNarrationAudio: TTS request. Extract voice (V1/V2/V3) and text (exact narration).
- RenderVideoSegment: Video render. Extract prompt (full visual description) and duration_sec.
- VMAllocated: VM provisioned. Extract offer_id (numeric ID) and gpu_type (e.g., RTX_4090).
- VMDeallocated: VM destroyed. Extract instance_id (numeric ID) and reason.
- NoOp: No action. Use ONLY if genuinely nothing found.

If an agent says they will do something but doesn't provide concrete details, do NOT return that effect. Return empty effects instead — the pipeline will ask the agent for clarification.
"""
```

#### 9.5.1.3 Instructor Call

```python
from structured_extract import extract

parsed = extract(
    _MultiEffect,
    text,
    system_prompt=_SYSTEM_PROMPT,
    max_retries=3,
)
```

The `extract` function (from `structured_extract.py`) wraps `instructor.from_openai()` with `mode=instructor.Mode.JSON`. It calls `deepseek-v4-flash` at `temperature=0.0`.

**Reask behavior:** If the LLM produces malformed output (validation fails on any field), instructor sends the validation error back to the model and requests a correction. This happens automatically up to `max_retries=3`. If all retries are exhausted, a `ValidationError` propagates to the caller.

---

### 9.5.2 Category-Conditioned Extraction

The parser **scopes** extraction to effect kinds relevant to the agent's current role. This reduces false positives and prevents cross-role contamination.

| Agent | Permitted Effect Types |
|---|---|
| Scenario | `UpdateScript`, `DeleteScene`, `ReorderScenes`, `NoOp`, `ClarificationRequest` |
| Audio | `QueueJob`, `JobApproved`, `JobRequeued`, `DurationAdjusted`, `ReconciliationFailed`, `ReconciliationComplete`, `NoOp`, `ClarificationRequest` |
| Video | `QueueJob`, `JobApproved`, `JobRequeued`, `MergeIntoOTIO`, `NoOp`, `ClarificationRequest` |
| Assembly | `PipelineComplete`, `ProductionFailed`, `NoOp`, `ClarificationRequest` |
| Provisioner | `VMAllocated`, `VMDeallocated`, `VMProvisionFailed`, `VMObserved`, `JobCompleted`, `JobFailed`, `JobStarted`, `NoOp`, `ClarificationRequest` |
| Maintainer | `HumanInstruction`, `AgentLoopDetected`, `PipelineAborted`, `NoOp`, `ClarificationRequest` |

**Enforcement:** The system prompt passed to the LLM includes the `PERMITTED EFFECT KINDS` list. The `_MultiEffect` discriminated union still contains all types (for technical reasons — instructor requires a static schema), but the system prompt instructs the model to only extract permitted kinds. If the model extracts a non-permitted kind, the validation layer rejects it.

**Future:** The permitted kinds could be narrowed dynamically by constructing a smaller `_EffectUnion` per agent. This is a performance optimization, not a correctness requirement.

---

### 9.5.3 Per-Effect Validation Rules

Every extracted effect undergoes **semantic validation** before being accepted. Validation happens at three levels:

#### Level 1: Pydantic Field Validation (Automatic)

Pydantic validates types, constraints, and `Literal` values automatically. Required fields with no default trigger validation errors if missing.

#### Level 2: Field Validators (Custom Logic)

Per-effect `@field_validator` methods enforce semantic correctness:

| Effect | Field | Validation Rule |
|---|---|---|
| `UpdateScript` | `narration_v1` | Must be ≥ 5 characters (not a placeholder) |
| `GenerateNarrationAudio` | `voice` | Must be `V1`, `V2`, or `V3` |
| `GenerateNarrationAudio` | `text` | Must be ≥ 3 characters (actual narration text) |
| `RenderVideoSegment` | `prompt` | Must be ≥ 5 characters (real visual description) |
| `VMAllocated` | `offer_id` | Must be numeric digits only |
| `VMAllocated` | `gpu_type` | Must be ≥ 2 characters (real GPU name) |
| `VMDeallocated` | `instance_id` | Must be numeric digits only |

Validation failures trigger instructor's **reask mechanism**. The model receives the validation error and attempts correction. If all retries are exhausted, the effect is dropped.

#### Level 3: Cross-Field Validation (Model Validators)

`@model_validator` methods enforce relationships between fields:

```python
@model_validator(mode="after")
def _check_reconciliation(self):
    if self.reconciliation_type == "duration_adjusted":
        if self.delta_sec is None or self.tolerance_sec is None:
            raise ValueError("duration_adjusted requires delta_sec and tolerance_sec")
    return self
```

Cross-field validators run after all field validators pass.

---

### 9.5.4 Failure Handling — What Happens When Parsing Fails

The parser handles failure at multiple granularities:

#### 9.5.4.1 Single Effect Validation Failure (Within a Multi-Effect Response)

If the agent's text contains 3 valid effects and 1 malformed one:
- The 3 valid effects are extracted and returned
- The malformed effect triggers instructor reasking for that specific sub-model
- If reasking fails after max_retries, the malformed effect is dropped
- The parser returns the 3 valid effects (partial extraction succeeds)

**Rule:** Partial extraction is preferred over total failure. Valid effects are never discarded because one sibling failed.

#### 9.5.4.2 Total Parse Failure (No Effects Extractable)

If no effects can be extracted from the agent's text:
1. The parser returns a single `NoOp` effect with `reason="No actionable effects found"`
2. The handler appends the `NoOp` to EventStoreDB (for observability)
3. The handler builds a **clarification request** and sends it back to the agent on the next turn

```python
def build_clarification_request(effects: list[Effect]) -> str | None:
    if len(effects) == 1 and effects[0].effect_type == "NoOp":
        reason = getattr(effects[0], "reason", "")
        if "missing" in reason.lower() or "placeholder" in reason.lower():
            return (
                "I understood you want to take action, but I couldn't find the "
                "concrete details in your response. Please include the actual values:\n"
                "- For audio: the exact text and voice (V1/V2/V3)\n"
                "- For video: the full visual description and duration\n"
                "- For provisioning: the offer ID and GPU type\n"
                "Write naturally — just make sure the specific data is in your prose."
            )
    return None
```

#### 9.5.4.3 Low Confidence (< 5)

If the parser's confidence score is below 5:
1. The effects are extracted but flagged as low-confidence
2. The handler may choose to append them (with a note) or request clarification
3. The default behavior: append the effects and include the confidence score in the `NoOp` reason for operator visibility

#### 9.5.4.4 Instructor Exhausts All Retries

If instructor's reasking mechanism exhausts all 3 retries:
1. A `ValidationError` propagates from `structured_extract.extract()`
2. The parser catches it and returns `[NoOp(reason=f"Parser could not extract: {exc}")]`
3. The handler appends the `NoOp` and may trigger clarification

---

### 9.5.5 Parser Is Not the Agent

**Critical invariant:** The parser runs **after** the LLM completes. It does not interact with the agent during generation. The agent produces text unaware of the parser's existence. The parser is a post-processing step, not a co-generation constraint.

The agent's **final output** is natural language text parsed for effects. Internal reasoning may use tools (subagent delegation via `task()`, context compaction). The parser extracts effects from the final text only; subagent outputs are invisible to the parser.

This means:
- Agents do not "emit effects" directly — they write text that the parser interprets
- The `agent` field on every effect is set by the handler (`effect.agent = agent_name`), not by the parser
- Parser failures produce `ClarificationRequest` or `NoOp`, not agent retry
- Subagent `task()` calls are internal tool calls; only the Main Agent's final text is parsed

---

### 9.5.6 Handler Integration

The parser is called by the agent's `POST /` handler after the LLM completes:

```
POST / receives payload
  → Handler queries GSA for state
  → Handler constructs narrative prompt
  → Agent LLM runs, produces text
  → PARSER: extract effects from text (semantic pipeline)
  → Handler validates effects against permitted kinds
  → Handler appends each effect to EventStoreDB
  → Handler returns effects to caller (or triggers next agent)
```

The parser is a **pure function**: `parse_agent_text_multi(agent_id: str, text: str) -> list[Effect]`. It has no side effects, no state, and no access to the event store. All append operations happen in the handler.

---

### 9.5.7 Complete Parse Flow (Decision Tree)

```
Agent produces text
  |
  └── Semantic extraction via instructor
      ├── Call deepseek-v4-flash with _MultiEffect schema
      ├── Validate each effect against _EffectUnion
      ├── If validation fails → Reask (up to 3x)
      ├── If all retries exhausted → Return [NoOp]
      ├── If confidence < 5 → Return effects (flagged)
      ├── If effects empty → Return [NoOp]
      └── Return extracted effects
```

---

### 9.5.8 Performance Characteristics

| Phase | Latency | When It Wins |
|---|---|---|
| Semantic extraction (Instructor) | ~500-2000ms | Natural language prose, complex reasoning |

All agent outputs are natural language. The parser is expected to be complex because ALL extraction complexity lives in it.


## 10. Provisioner Agent

The **Provisioner** (port 8081) is an agent — the most intelligence-requiring component in the architecture. It provisions GPU VMs, dispatches jobs to workers, collects results, and learns from failures across runs. Like all agents, it reads state from the GSA via `GET /`, reasons in natural language, and its output is parsed for effects by the instructor parser.

The Provisioner uses **tools** to interact with the outside world:
- `bash_command` — raw shell for ALL VM operations (`vastai`, `ssh`, `curl`)
- `research_model_requirements` — Exa web search for GPU requirements
- `evaluate_vastai_offers` — rank offers against researched requirements
- `claim_job` / `set_job_running` / `set_job_completed` / `set_job_failed` — job queue lifecycle
- `dispatch_tts_job` / `dispatch_video_job` — HTTP POST to VM workers
- `query_vm_registry` / `record_vm` / `update_vm_worker_url` — VM tracking
- `remember` / `recall_memory` — durable learning across runs

### 10.1 Architecture

#### 10.1.1 Agent with tools (not deterministic service)

The Provisioner is a pydantic-deep agent wrapped in a FastAPI HTTP service. On `POST /`, it receives a wake notification, queries the GSA for current `JobProjection` and `VMProjection`, constructs a narrative describing pending jobs and VM state, and invokes the agent. The agent's natural language output is parsed for effects (`VMAllocated`, `VMDeallocated`, `JobCompleted`, `JobFailed`, `VMObserved`, etc.) by the category-conditioned parser.

```python
from pydantic_deep import Agent, create_deep_agent
from strands import tool

@tool
def bash_command(command: str) -> str:
    """Run an arbitrary bash command on the local machine."""
    proc = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode() + stderr.decode()

PROVISIONER_INSTRUCTION = """\
You are the Provisioner Agent. You are the ONLY entity that provisions GPU VMs and executes jobs.

NEVER TROUBLESHOOT. ONLY CERTAINTY.

CORE RULE: You do NOT guess. You do NOT experiment. You follow what worked.

1. CHECK MEMORY FIRST: recall_memory(query='worker success', category='success')
   If memory exists of a successful VM configuration, USE THAT EXACT CONFIGURATION.
   Same GPU, same disk, same image, same provider. Do not deviate.

2. ONLY if no memory exists: call research_model_requirements('<model_name>')
   to learn the authoritative GPU requirements. Then use CONSERVATIVE defaults.

3. BEFORE provisioning: query_vm_registry(stage=<stage>)
   If a VM exists, check its health via BASH:
   bash_command("curl -s http://<worker_url>/ || echo 'WORKER DOWN'")
   If the worker responds with READY status, USE IT. Do not provision a second VM.

4. If you must provision:
   a. Search Vast.ai: bash_command("vastai search offers --type on-demand --raw | head -20")
   b. Evaluate offers: evaluate_vastai_offers('<model_name>', <raw_search_text>)
   c. Pick the HIGHEST-RANKED 'ideal' or 'acceptable' offer.
   d. Provision: bash_command("vastai create instance <offer_id> ...")
   e. Record the VM: record_vm(stage, <raw_show_output>)
   f. Update registry with worker URL: update_vm_worker_url(instance_id, "http://<ip>:8880/")
   g. Wait for health: bash_command("curl -s http://<worker_url>/ || echo 'WORKER DOWN'")

5. After provisioning succeeds:
   remember(text='<stage> worker succeeded on <GPU> <VRAM>GB with image <image>', category='success')
   After ANY failure:
   remember(text='<stage> worker failed: <exact_error>', category='failure')

6. IF A WORKER FAILS: Do NOT try to fix it. Do NOT SSH in and tinker.
   If guidance says 'destroy_and_reprovision', destroy it and start fresh.
   If guidance says 'use_existing', the worker may still be loading — WAIT.
   NEVER troubleshoot. The system knows more than you do.

WORKFLOW:
1. Call claim_job(stage='audio') then claim_job(stage='video').
   Process jobs for BOTH stages in a single invocation if possible.
2. For each claimed job:
   a. Call set_job_running(job_id, worker_id=instance_id).
   b. Ensure a healthy worker exists for the job's stage.
   c. Dispatch the job to the worker via dispatch_tts_job or dispatch_video_job.
   d. Call set_job_completed(job_id, output_path) with the local file path.
3. If dispatch fails, call set_job_failed(job_id, error_message).
4. After all claimed jobs are processed, call check_queue_status(stage)
   for both stages.
5. If pending or needs_retry jobs remain, report status and STOP.
   The graph will re-invoke you.
6. If ALL jobs for BOTH stages are completed or failed, STOP cleanly.

BASH IS YOUR ONLY INTERFACE TO VMs. Never use Python-level VM libraries.
"""

agent = create_deep_agent(
    model=config.agent_models["provisioner"],
    instructions=PROVISIONER_INSTRUCTION,
    tools=[bash_command, research_model_requirements, evaluate_vastai_offers,
           claim_job, set_job_running, set_job_completed, set_job_failed,
           check_queue_status, query_vm_registry, record_vm, update_vm_worker_url,
           dispatch_tts_job, dispatch_video_job, remember, recall_memory],
    on_before_compress=otio_aware_compress,
    history_processors=[...],
    include_todo=False,
    include_filesystem=False,
    include_plan=False,
)
```

#### 10.1.2 Why the Provisioner is an agent

VM provisioning is **not deterministic**. The Vast.ai marketplace is dynamic — offers appear and disappear, prices fluctuate, GPU types vary by host, images have different CUDA/driver versions, network topologies differ, and SSH ports are randomly assigned. A deterministic script can search and sort, but it cannot:

- **Reason about failure** — "This offer failed because the host has CUDA 12.4 but the image needs 12.6. I should filter for CUDA 12.6+ next time."
- **Learn across runs** — "Last time, RTX 4090 on host X worked perfectly for TTS. Let me prefer that host."
- **Research requirements** — "I don't know how much VRAM LTX-2.3 needs. Let me search for authoritative specs."
- **Escalate intelligently** — "Three different offers failed with the same error. This is a systemic issue, not a bad offer. I should ask the operator."

The Provisioner agent uses `remember` / `recall_memory` to build durable knowledge across pipeline runs. It uses `research_model_requirements` + `evaluate_vastai_offers` to make informed decisions when memory is insufficient. It uses `bash_command` as its only interface to Vast.ai — no wrapper methods, no hidden abstractions.

#### 10.1.3 Agent output is parsed for effects

Like all agents, the Provisioner does not emit effects directly. It produces natural language text describing what it did:

```
I searched Vast.ai and found offer 12345: RTX 4090, 24GB VRAM, $0.45/hr.
I provisioned it with image vastai/worker:tts --disk 64.
Instance ID is 67890. Worker is responding on http://1.2.3.4:8880/.
I dispatched job job-abc to the worker. The worker returned 202 Accepted.
```

The parser extracts:
- `VMAllocated(instance_id="67890", offer_id="12345", ...)`
- `JobStarted(job_id="job-abc", vm_instance_id="67890", ...)`

The handler appends these effects to EventStoreDB. The Provisioner is barely aware of this process.

### 10.2 VM Lifecycle Management

#### 10.2.1 Offer selection: agent reasoning, not deterministic criteria

The Provisioner agent searches Vast.ai via `bash_command`, evaluates raw offer text via `evaluate_vastai_offers`, and picks the best offer. The evaluation tool uses `deepseek-v4-flash` to rank offers against researched requirements. The agent may override the ranking based on memory (e.g., "I had a failure on host X last time, skip it even if ranked #1").

**Start with one VM, then escalate.** The agent provisions exactly one VM for the first job of each type. It confirms health (`GET /` responds) before provisioning additional VMs. The agent decides when to escalate based on queue depth, worker health, and cost projections — not a hardcoded threshold.

| Criterion | TTS Job (`job_type="tts"`) | LTX Job (`job_type="ltx"`) |
|---|---|---|
| GPU type | RTX 4090 or A6000 (agent preference) | RTX A6000 (48 GB) |
| Min VRAM | 24 GB | 48 GB |
| Max price/hr | `$config.max_tts_cost_hr` (default $0.80) | `$config.max_ltx_cost_hr` (default $1.20) |
| Disk | ≥ 100 GB | ≥ 200 GB |
| Sort key | agent-ranked (cost × reliability) | agent-ranked (cost × reliability) |
| Max concurrent | 3 (soft limit, agent decides) | 3 (soft limit, agent decides) |

```python
# Agent calls bash_command, then evaluate_vastai_offers
raw_offers = bash_command("vastai search offers --type on-demand --raw | head -20")
ranked = evaluate_vastai_offers("Qwen3-TTS", raw_offers)
# Agent picks based on ranking + memory + context
```

#### 10.2.2 VM allocation via bash tool

The agent constructs the `vastai create instance` command as a string and executes it via `bash_command`. It parses the raw stdout, waits for the worker HTTP endpoint to respond, and describes the result in its output.

```python
# Agent calls bash_command with literal string
create_cmd = (
    f"vastai create instance {offer_id} "
    f"--image {image} --disk {disk_gb} --ssh --direct"
)
result = bash_command(create_cmd)
# Agent parses result in its reasoning, describes outcome in final text
```

#### 10.2.3 Heartbeat monitoring via agent reasoning

The agent queries the GSA for `VMProjection` state, runs `vastai show instances --raw` via `bash_command`, compares the two, and describes any divergence in its output. The parser extracts `VMObserved` effects.

```python
# Agent reads VM state from GSA GET /
# Agent runs: bash_command("vastai show instances --raw")
# Agent compares and reports: "VM 67890 is running in projection but not found in Vast.ai..."
```

#### 10.2.4 VM deallocation on job completion or failure

The agent reads `JobProjection` from the GSA. When it sees no pending/running jobs for a VM, it decides whether to deallocate. The decision considers:
- How long until the next job might arrive
- Whether the VM is healthy and responsive
- Cost of keeping it vs. re-provisioning later
- Memory of boot times for this GPU type

```python
# Agent reads job state from GSA
# Agent reasons: "No pending jobs for VM 67890. Boot time for RTX 4090 is 3 min.
#               Next video job may arrive in 10 min. Keeping it costs $0.03.
#               I will deallocate to save money."
# Agent calls: bash_command("vastai destroy instance 67890")
# Agent describes action in output; parser extracts VMDeallocated
```

### 10.3 Job Delivery

#### 10.3.1 POST job to VM worker via dispatch tool

The agent calls `dispatch_tts_job` or `dispatch_video_job` — these are tools that POST plain text to the worker's `POST /` endpoint and return the result. The agent decides which worker to use based on registry state, health checks, and memory.

```python
# Agent calls dispatch_tts_job(text, output_path, instance_id)
# Tool POSTs to worker, returns JSON result
# Agent describes outcome: "Job job-abc dispatched to worker 67890. Worker returned 202."
```

#### 10.3.2 Receive completion/failure, describe in output

VM workers POST results to the Provisioner's `POST /` endpoint. The handler routes worker results to the agent as part of the next wake narrative. The agent describes what happened, and the parser extracts `JobCompleted` or `JobFailed`.

```python
# Worker POSTs result to Provisioner POST /
# Next wake: handler includes worker result in agent prompt
# Agent output: "Worker 67890 completed job job-abc. Artifact at /tmp/job-abc.wav.
#               Duration 5.2s. Uploading to B2..."
# Parser extracts: JobCompleted(job_id="job-abc", artifact_uri="b2://...", ...)
```

### 10.4 Failure Handling

#### 10.4.1 VMProvisionFailed → agent reasoning → retry or escalation

When provisioning fails, the agent reads the error, recalls memory for similar failures, researches if needed, and decides whether to retry with a different offer or escalate to the operator. There is no hardcoded retry count — the agent decides based on the failure pattern.

```python
# Agent output: "Provisioning offer 12345 failed: CUDA driver mismatch.
#               I had this failure before on host X with image Y.
#               I will try image Z which worked last time."
# Parser extracts: VMProvisionFailed(offer_id="12345", failure_category="cuda_mismatch", ...)
```

If the agent detects a systemic issue (same failure across multiple offers), it escalates:

```python
# Agent output: "Three different offers failed with 'CUDA driver mismatch'.
#               This is a systemic issue — all current hosts have CUDA 12.4.
#               I need operator guidance on image selection."
# Parser extracts: ClarificationRequest(agent="human", failure_reason="...", ...)
```

The escalation flow preserves agent autonomy: the Provisioner reasons about recovery within its domain, but defers to humans when boundaries are exceeded.


## 11. VM Worker

The VM Worker is a stateless FastAPI server on ephemeral GPU instances (port 9000+). It receives inference jobs from the Provisioner (Section 10.3.1), executes TTS or video inference, measures output with WhisperX (3 runs), validates via LLM call to `deepseek-v4-flash`, and posts results back to the Provisioner. The worker has no event store access, no local state beyond the current job, and no timeout or self-destruct logic per Principle 4 (Section 1.4).

---

### 11.0 VM Provisioning and Boot Sequence

#### 11.0.1 How code gets onto the VM

The worker code is **not built into a Docker image**. Instead, Vast.ai's on-start script mechanism downloads and launches the worker at VM boot time. This avoids Docker registry management and image versioning.

**On-start script (`onstart_tts.sh` and `onstart_ltx.sh`):**

```bash
#!/bin/bash
# onstart_tts.sh — runs on VM boot via Vast.ai --onstart-cmd
set -e

# 1. Install dependencies
apt-get update && apt-get install -y python3-pip ffmpeg git

# 2. Clone worker code from the control-plane repo
REPO_URL="https://github.com/org/economy-documentary-work"
git clone --depth 1 "$REPO_URL" /opt/worker
cd /opt/worker/vm_worker

# 3. Install Python dependencies
pip install -r requirements.txt  # fastapi, uvicorn, pydantic, httpx, whisperx

# 4. Download model weights (cached on VM disk)
python3 -m worker.download_weights --model qwen3-tts

# 5. Start the worker server
python3 -m worker.main --port 9000 --role tts
```

**Why on-start script vs. Docker image:**
- **No registry:** Vast.ai instances boot from a base Ubuntu image; the on-start script pulls code and models.
- **No image versioning:** Code updates are deployed by pushing to the repo; new VMs get the latest code automatically.
- **Model caching:** Model weights are downloaded once and cached on the VM's disk. Subsequent job executions reuse the cached weights.

**Who builds it:** The on-start script is stored in the control-plane repository (`vm/onstart_tts.sh`, `vm/onstart_ltx.sh`). The operator (or CI) updates it. The Provisioner agent passes the script path to `vastai create instance`:

```bash
vastai create instance {offer_id} \
  --onstart-cmd "bash /path/to/onstart_tts.sh" \
  --disk 100
```

#### 11.0.2 Worker directory structure

```
/opt/worker/
├── worker/
│   ├── __init__.py
│   ├── main.py           # FastAPI app (GET /, POST /)
│   ├── dispatcher.py     # Job type → executor routing
│   ├── executors/
│   │   ├── __init__.py
│   │   ├── tts.py        # Qwen3-TTS subprocess wrapper
│   │   ├── ltx.py        # LTX-2.3 subprocess wrapper
│   │   └── whisperx.py   # WhisperX measurement wrapper
│   ├── quality.py        # LLM-based quality check
│   ├── upload.py         # B2 upload helper
│   └── download_weights.py  # Model weight downloader
├── models/               # Cached model weights (persistent across jobs)
│   ├── qwen3-tts/
│   └── ltx-2.3/
├── outputs/              # Job outputs (ephemeral, uploaded to B2)
└── requirements.txt
```

#### 11.0.3 Model weight caching

Model weights are large (Qwen3-TTS: ~4 GB; LTX-2.3: ~20 GB). Downloading them per job is prohibitive. The on-start script downloads weights to `/opt/worker/models/` before starting the server. The worker's executors load weights from this directory.

```python
# worker/executors/tts.py
MODEL_PATH = "/opt/worker/models/qwen3-tts"

class TTSExecutor:
    def __init__(self):
        # Load model into VRAM once at startup
        self.model = load_qwen3_tts(MODEL_PATH)

    def run(self, text: str, voice_id: str, output_path: str) -> str:
        # Inference uses pre-loaded model
        return self.model.synthesize(text, voice_id, output_path)
```

**VRAM residency:** The model stays in GPU memory for the lifetime of the VM. This eliminates per-job model load latency (~30–60 seconds).

---

### 11.1 HTTP Surface

#### 11.1.1 GET / (health), POST / (receive job)

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/` | `GET` | None | `VMHealthResponse` JSON |
| `/` | `POST` | `JobRequest` JSON | `202 Accepted` (or `409` if busy) |

```python
from pydantic import BaseModel, Field
from typing import Literal
from uuid import UUID

class VMHealthResponse(BaseModel):
    status: Literal["idle", "busy"] = "idle"
    instance_id: str
    gpu_type: str
    current_job_id: str | None = None

class JobRequest(BaseModel):
    job_id: UUID
    run_id: str
    job_type: Literal["tts", "ltx"]
    params: dict = Field(default_factory=dict)
    callback_url: str  # V7: provisioner provides callback URL explicitly
    whisperx_model: str = "large-v3"
```

`GET /` returns the worker's current status. The Provisioner polls this before dispatch. `POST /` returns `202 Accepted` immediately and spawns the job in a `BackgroundTasks`. If the worker is `busy`, it returns `409 Conflict`. The worker holds no queue — exactly one job runs at a time.

---

#### 11.1.2 Standardized Job Dispatcher

The worker's `POST /` handler receives a `JobRequest` and routes it to the correct executor via a dispatcher. The dispatcher is the single entry point for all job types.

```python
# worker/dispatcher.py
from worker.executors.tts import TTSExecutor
from worker.executors.ltx import LTXExecutor
from worker.executors.whisperx import WhisperXExecutor
from worker.quality import QualityChecker
from worker.upload import upload_to_b2


class JobDispatcher:
    """Routes JobRequest to the correct executor and orchestrates the pipeline."""

    def __init__(self):
        self.tts = TTSExecutor()      # VRAM-resident model
        self.ltx = LTXExecutor()      # VRAM-resident model
        self.whisperx = WhisperXExecutor()
        self.quality = QualityChecker()

    async def dispatch(self, req: JobRequest) -> JobResult:
        """Execute a job end-to-end: inference → measure → quality-check → upload → report."""
        try:
            # 1. Inference
            if req.job_type == "tts":
                artifact_path = await self.tts.run(
                    text=req.params["text"],
                    voice_id=req.params.get("voice_id", "default"),
                    output_path=f"/opt/worker/outputs/{req.job_id}.wav",
                )
            elif req.job_type == "ltx":
                artifact_path = await self.ltx.run(
                    prompt=req.params["prompt"],
                    duration_sec=req.params.get("duration_sec", 5.0),
                    output_path=f"/opt/worker/outputs/{req.job_id}.mp4",
                )
            else:
                raise ValueError(f"Unknown job_type: {req.job_type}")

            # 2. Measure with WhisperX (3 runs)
            measurements = await self.whisperx.measure(
                artifact_path,
                model=req.whisperx_model,
                num_runs=3,
            )

            # 3. Quality check via LLM
            qc_pass, qc_reason = await self.quality.check(
                path=artifact_path,
                job_type=req.job_type,
                expected=req.params.get("duration_sec"),
            )
            if qc_pass != "pass":
                return JobResult(
                    job_id=req.job_id,
                    status="failed",
                    failure_category="qc_failed",
                    failure_reason=qc_reason,
                    vm_instance_id=INSTANCE_ID,
                    run_id=req.run_id,
                )

            # 4. Upload to B2
            b2_uri = await upload_to_b2(artifact_path, req.run_id, req.job_id)

            # 5. Report success
            return JobResult(
                job_id=req.job_id,
                status="completed",
                artifact_uri=b2_uri,
                duration_sec=measurements[1] if measurements else None,  # median
                measurements=measurements,
                file_size_bytes=os.path.getsize(artifact_path),
                vm_instance_id=INSTANCE_ID,
                run_id=req.run_id,
            )

        except InferenceError as exc:
            return JobResult(
                job_id=req.job_id, status="failed",
                failure_category="inference_error",
                failure_reason=str(exc),
                vm_instance_id=INSTANCE_ID, run_id=req.run_id,
            )
        except MeasurementError as exc:
            return JobResult(
                job_id=req.job_id, status="failed",
                failure_category="measurement_error",
                failure_reason=str(exc),
                vm_instance_id=INSTANCE_ID, run_id=req.run_id,
            )
        except Exception as exc:
            return JobResult(
                job_id=req.job_id, status="failed",
                failure_category="unknown",
                failure_reason=f"Unexpected error: {exc}",
                vm_instance_id=INSTANCE_ID, run_id=req.run_id,
            )
```

**Dispatcher invariants:**
- Exactly one job runs at a time. A second `POST /` while busy returns `409 Conflict`.
- The dispatcher catches all exceptions and converts them to `JobResult(status="failed")`. No unhandled exception escapes.
- Each phase (inference, measurement, quality, upload) is independent. Failure in one phase skips subsequent phases and returns immediately.

---

### 11.2 Job Execution

#### 11.2.1 TTS: Qwen3-TTS inference pipeline

For `job_type="tts"`, the worker invokes Qwen3-TTS via subprocess. The model is loaded into VRAM once at VM boot.

```python
async def _run_tts(params: dict) -> str:
    cmd = [
        "python3", "-m", "qwen3_tts",
        "--text", params["text"],
        "--voice", params.get("voice_id", "default"),
        "--output", params.get("output_path", f"/tmp/{uuid4()}.wav"),
        "--format", "wav",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise InferenceError(f"Qwen3-TTS failed: {stderr.decode()}")
    return params.get("output_path", f"/tmp/{uuid4()}.wav")
```

OOM errors propagate as `InferenceError` and are reported with `failure_category="oom"`.

#### 11.2.2 Video: LTX-2.3 inference pipeline

For `job_type="ltx"`, the worker invokes LTX-2.3 via subprocess. The diffusion model is VRAM-resident from VM boot.

```python
async def _run_ltx(params: dict) -> str:
    output = params.get("output_path", f"/tmp/{uuid4()}.mp4")
    cmd = [
        "python3", "-m", "ltx_video",
        "--prompt", params["prompt"],
        "--duration", str(params.get("duration_sec", 5.0)),
        "--width", str(params.get("width", 1280)),
        "--height", str(params.get("height", 720)),
        "--output", output, "--steps", "30",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise InferenceError(f"LTX-2.3 failed: {stderr.decode()}")
    return output
```

#### 11.2.3 WhisperX: 3× measurement, return all to Audio Agent

After inference, the worker runs WhisperX three times per decision C2. All three measurements are returned; the Audio Agent computes the median (Section 9.2).

```python
async def _measure_with_whisperx(
    audio_path: str, model: str = "large-v3", num_runs: int = 3,
) -> list[float]:
    measurements: list[float] = []
    for _ in range(num_runs):
        cmd = ["whisperx", audio_path, "--model", model,
               "--language", "en", "--output_format", "json",
               "--output_dir", "/tmp/"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise MeasurementError(f"WhisperX failed: {stderr.decode()}")
        segments = json.loads(stdout.decode()).get("segments", [])
        measurements.append(segments[-1]["end"] if segments else 0.0)
    return measurements   # e.g. [5.12, 5.08, 5.15]
```

Runs execute sequentially to avoid CPU contention. For TTS jobs WhisperX always runs. For LTX jobs it runs only when `ffprobe` detects an audio track in the MP4.

#### 11.2.4 WhisperX failure handling

WhisperX can fail for three reasons: model load error, audio format incompatibility, or segmentation failure. The worker handles each differently:

```python
async def _measure_with_whisperx(
    audio_path: str, model: str = "large-v3", num_runs: int = 3,
) -> list[float]:
    measurements: list[float] = []
    for run in range(num_runs):
        try:
            cmd = ["whisperx", audio_path, "--model", model,
                   "--language", "en", "--output_format", "json",
                   "--output_dir", "/tmp/"]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode()
                if "CUDA out of memory" in err:
                    raise MeasurementError(f"WhisperX OOM (run {run+1}/{num_runs}): {err}")
                if "Unsupported audio format" in err:
                    raise MeasurementError(f"WhisperX format error: {err}")
                raise MeasurementError(f"WhisperX failed (run {run+1}/{num_runs}): {err}")

            segments = json.loads(stdout.decode()).get("segments", [])
            if not segments:
                measurements.append(0.0)
            else:
                measurements.append(segments[-1]["end"])
        except json.JSONDecodeError:
            raise MeasurementError(f"WhisperX returned invalid JSON (run {run+1}/{num_runs})")
        except Exception as exc:
            raise MeasurementError(f"WhisperX unexpected error (run {run+1}/{num_runs}): {exc}")

    if all(m == 0.0 for m in measurements):
        raise MeasurementError("WhisperX: all three runs returned 0.0s — audio may be silent")

    return measurements
```

**Failure categories reported to the Audio Agent:**

| WhisperX Failure | `failure_category` | Handler Action |
|---|---|---|
| CUDA OOM | `measurement_error` | Audio Agent may requeue with shorter text or different voice |
| Unsupported format | `measurement_error` | Audio Agent may re-encode or re-generate |
| All zeros (silent) | `measurement_error` | Audio Agent treats as TTS failure, requeues |
| JSON decode error | `measurement_error` | Audio Agent retries measurement on same or different VM |

**The Audio Agent (not the VM Worker) decides retry strategy.** The worker reports failure via `JobResult` and resets to `idle`. The Provisioner sees the failed job in `JobProjection`, and the Audio Agent's next wake includes the failure context. The Audio Agent may emit `JobRequeued` with adjusted params.

---

### 11.3 Quality Check

#### 11.3.1 LLM-based output validation (file size, duration, corruption check)

Before reporting success, the worker classifies the artifact via a single-turn call to `deepseek-v4-flash`. This catches gross failures — zero-byte files, extreme duration mismatch, container corruption — before they reach the Audio/Video Agent.

```python
async def _quality_check(
    path: str, job_type: str, expected: float | None,
) -> tuple[Literal["pass", "fail"], str]:
    size = os.path.getsize(path)
    actual = _ffprobe_duration(path) if job_type == "ltx" else None
    prompt = (
        f"File: {path}\nType: {job_type}\nSize: {size} bytes\n"
        f"Expected: {expected or 'N/A'}s\nActual: {actual or 'N/A'}s\n\n"
        "Classify as: pass | fail_file_empty | fail_duration_mismatch "
        "| fail_corrupt. Respond with label only."
    )
    label = (await _call_deepseek_flash(prompt)).strip().lower()
    if label == "pass":
        return "pass", "ok"
    reasons = {
        "fail_file_empty": "Output file is empty",
        "fail_duration_mismatch": f"Duration mismatch (expected {expected}s)",
        "fail_corrupt": "Container corruption detected",
    }
    return "fail", reasons.get(label, f"QC failed: {label}")
```

Unrecognized labels default to `pass` — false negatives are more expensive than false positives. This check is not a substitute for Audio Agent reconciliation (Section 9.2) or Video Agent artistry judgment.

---

### 11.4 Reporting

#### 11.4.1 POST result to Provisioner

The worker posts a `JobResult` to the `callback_url` from the inbound `JobRequest`:

```python
class JobResult(BaseModel):
    job_id: UUID
    status: Literal["completed", "failed"]
    artifact_uri: str | None = None
    duration_sec: float | None = None
    measurements: list[float] = Field(default_factory=list)
    file_size_bytes: int | None = None
    vm_instance_id: str
    failure_category: str | None = None
    failure_reason: str | None = None
    run_id: str  # V7: included for routing
```

The orchestrator `_execute_job` ties all phases: inference → measure → quality-check → report. On any exception, it posts `status="failed"` with the error detail and resets to `idle`. The worker does not retry failed POSTs — the operator monitors job status via `GET /` on the Provisioner and intervenes manually if a job hangs.

#### 11.4.2 No VM-side timeout; no self-destruct

The VM Worker contains no `asyncio.timeout`, `threading.Timer`, `signal.alarm`, heartbeat loop, or self-destruct call. This is the V5 → V6 → V7 change mandated by Principle 4.

| Aspect | V5 | V6 | V7 |
|---|---|---|---|
| Heartbeat | VM polls every 60s | None — VM is passive | None — VM is passive |
| Stale detection | 15 min → `vastai destroy` | Operator monitors via `GET /` | Operator monitors via `GET /` |
| Timer code | `threading.Timer` in VM | No timer code in VM | No timer code in VM |
| Recovery path | VM self-destructs | Provisioner deallocates + requeues job | Provisioner deallocates + requeues job |

If a subprocess hangs (Qwen3-TTS or LTX-2.3 never returns), the VM remains occupied until the operator observes the stuck job via `GET /` on the Provisioner and manually deallocates it. The VM has no awareness of its own lifecycle — it processes jobs until terminated externally.

#### 11.4.3 VM idle detection — Provisioner-side, not VM-side

V7 has **no VM heartbeat, no timeout, no self-destruct** (Principle 4). Idle and stuck VM detection is performed by the **Provisioner agent** through reasoning about projection state, not by the VM itself.

**How the Provisioner detects stuck VMs:**

1. The Provisioner queries the GSA for `JobProjection` and `VMProjection`.
2. It compares `JobProjection` (which jobs are pending/running/completed) against `VMProjection` (which VMs are active).
3. It identifies anomalies:
   - **Idle VM:** VM is active but has no running or pending jobs assigned to it.
   - **Stuck VM:** VM reports `busy` on `GET /` but `JobProjection` shows no job as `running` for that VM.
   - **Hung job:** Job status is `running` for longer than the agent deems reasonable (based on memory of typical inference times).
4. The Provisioner describes the anomaly in its natural language output.
5. The parser extracts `VMObserved` or `VMDeallocated` based on the agent's decision.

```python
# Inside the Provisioner's reasoning (not code — the agent decides this)
# Agent reads: VM 67890 (RTX 4090) has been active for 45 min.
#              JobProjection shows 0 pending, 0 running for this VM.
#              Last job completed 30 min ago.
# Agent output: "VM 67890 has been idle for 30 min. Cost so far: $0.34.
#                No pending jobs. I will deallocate to save money."
# Parser extracts: VMDeallocated(instance_id="67890", reason="job_done")
```

**Why no VM-side heartbeat:**
- **Principle 4:** No timeouts anywhere. A heartbeat is a timeout mechanism ("if no heartbeat in 60s, consider dead").
- **Simplicity:** The VM runs a FastAPI server and inference subprocesses. Adding a background heartbeat thread introduces concurrency bugs, thread safety issues with CUDA, and complexity.
- **Correctness:** The Provisioner has full context (job queue, VM state, cost). It can make nuanced decisions ("keep this VM for 5 more minutes because a video job is likely to arrive soon") that a simple heartbeat cannot.

**Trade-off:** Stuck VMs cost money until the Provisioner notices them. For a typical run (10–30 min), the cost of a stuck VM for a few minutes is negligible compared to the complexity of heartbeat infrastructure. The operator monitors `GET /` on the Provisioner and can manually deallocate if the agent fails to notice.

---

### 11.5 Durable Artefact Storage — Backblaze B2

Generated media (WAV, MP4, WhisperX transcripts) must survive VM teardown. VM-local paths are ephemeral. V7 uses **Backblaze B2** as the durable object store.

#### 11.5.1 B2 URI contract

Every artefact is addressed by a B2 URI:

```
b2://{bucket}/runs/{run_id}/{media_type}/{slot_id}.{ext}
```

| Component | Example | Rule |
|---|---|---|
| `bucket` | `doc-pipeline-prod` | Single bucket per environment |
| `run_id` | `0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b` | Same UUID as the pipeline run |
| `media_type` | `audio`, `video`, `transcript` | Determined by job_type |
| `slot_id` | `A1_Narration:3:1` | OTIO slot address, colons replaced with hyphens |
| `ext` | `wav`, `mp4`, `json` | Determined by job_type |

Example: `b2://doc-pipeline-prod/runs/0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b/audio/A1_Narration-3-1.wav`

#### 11.5.2 Upload-before-callback

The VM worker uploads the artefact to B2 **before** POSTing the result to the Provisioner. No `JobCompleted` is emitted until the B2 object exists and is retrievable.

```python
# Inlined in VM worker _execute_job — no wrapper function
upload_cmd = (
    f"b2 upload-file doc-pipeline-prod "
    f"/tmp/output.wav "
    f"runs/{run_id}/audio/A1_Narration-3-1.wav"
)
proc = await asyncio.create_subprocess_shell(upload_cmd)
await proc.communicate()
if proc.returncode != 0:
    # Upload failed — report failure, do not emit JobCompleted
    return JobResult(status="failed", failure_category="upload_failed", ...)

artifact_uri = f"b2://doc-pipeline-prod/runs/{run_id}/audio/A1_Narration-3-1.wav"
```

**Why B2:** Backblaze B2 is S3-compatible, has no egress fees, and is priced for long-term storage of large media files. The `b2` CLI is pre-installed on VM worker images. No SDK abstraction — commands are literal strings.

#### 11.5.3 Download by agents

Agents (Audio, Video, Assembly) read artefacts from B2 using the `b2` CLI or HTTP presigned URLs:

```python
# Direct bash — no wrapper
download_cmd = f"b2 download-file-by-name doc-pipeline-prod {b2_path} /tmp/local.wav"
await asyncio.create_subprocess_shell(download_cmd)
```

The B2 bucket is the single source of truth for all generated media. No agent caches artefacts locally beyond the current activation.

---

## 12. Data Flows

This chapter traces the four principal data flow patterns through the V7 pipeline: the agent activation cycle, the reconciliation loop with VM-mediated TTS, the script back-edge with partial re-reconciliation, and human intervention. Each flow is presented as a text-based sequence diagram showing actor interactions, followed by a step-by-step specification.

---

### 12.1 Agent Activation Cycle

#### 12.1.1 Agent HTTP service cycle with projection updates

```
  +--------------+     +--------------------+     +--------------+     +--------------+
  | Human Op. or |     | Global State Agent |     |  Agent POST  |     |  Agent LLM   |
  | Caller       |     | (port 8000)        |     |  Handler     |     |  (pydantic-  |
  |              |     | GET / only         |     |              |     |   deep)      |
  +----+---------+     +---------+----------+     +-------+-------+     +------+-------+
       |                         |                      |                    |
       | 1. POST /               |                      |                    |
       | (wake or                |                      |                    |
       |  instruction)           |                      |                    |
       |------------------------>|                      |                    |
       |                       | 2. GET /?run_id=...  |                    |
       |                       | (fetch projections)  |                    |
       |                       |--------------------->|                    |
       |                       |                      | 3. build narrative |
       |                       |                      |    from proj       |
       |                       |                      |------------------->|
       |                       |                      |                    | 4. LLM runs
       |                       |                      |                    |    produces text
       |                       |                      |<-------------------|
       |                       |                      | 5. parser extracts |
       |                       |                      |    effects         |
       |                       |<---------------------| 6. append_effect() |
       |                       | (to EventStoreDB)    |                    |
       |<----------------------|                      |                    |
       | 7. return 200         |                      |                    |
       |    (or notify         |                      |                    |
       |     next agent)       |                      |                    |
```

**Step-by-step specification:**

| Step | Actor | Action | Specification |
|---|---|---|---|
| 1 | Human operator / Caller | POST / | HTTP POST to agent's port with `AgentPayload` containing `run_id`, `notification_type`, and optional `context` |
| 2 | Agent Handler | Read state | `GET /?run_id={run_id}` to Global State Agent (port 8000) returns full projection bundle |
| 3 | Agent Handler | Build narrative | Constructs situation narrative from projection summaries; injects memory from prior turns |
| 4 | Agent LLM | Execute | `agent.run(user_prompt=narrative, deps=PipelineDeps)` via pydantic-deep. No timeout (§1.4) |
| 5 | Agent Handler | Parse effects | `_parse_effects()` via instructor extracts typed Pydantic models from LLM output (§9.6) |
| 6 | Agent Handler | Append | Effects written to EventStoreDB stream `run-{run_id}` with `event_id` deduplication |
| 7 | Agent Handler | Return | Returns `200 OK` with extracted effect kinds; may POST wake notification to downstream agents |

The cycle is **triggered on demand** — there is no central watcher loop. Agents wake when:
- Another agent POSTs a wake notification to them
- The human operator POSTs an instruction
- The Provisioner POSTs a job-completion notification
- An EventStoreDB persistent subscription delivers new events (optional optimization)

---

### 12.2 Reconciliation Loop (Detailed)

The reconciliation loop spans four physical components — Audio Agent, EventStoreDB, Provisioner, and VM Worker — and iterates until every narration block passes the tolerance check or exhausts its attempt budget.

#### 12.2.1 Audio Agent ↔ EventStoreDB ↔ Provisioner ↔ VM Worker (TTS path)

```
Audio Agent (8002)          EventStoreDB            Provisioner (8081)    VM Worker (9000)
      |                         |                        |                      |
      |-- QueueJob (TTS) ----->|                        |                      |
      |  job_type=tts           |                        |                      |
      |  block_id=A1:1:1       |                        |                      |
      |  text="In 1924..."     |                        |                      |
      |                         |<-- POST / -------------|                      |
      |                         |  Provisioner reads     |                      |
      |                         |  JobProjection: 1      |                      |
      |                         |  pending (tts)         |                      |
      |                         |                        |-- offer matching     |
      |                         |                        |  (direct bash)       |
      |                         |                        |                      |
      |                         |<-- VMAllocated -------|                      |
      |                         |  instance_id=vast-42   |                      |
      |                         |                        |-- POST / (job) ----->|
      |                         |                        |  JobRequest JSON     |
      |                         |                        |  + callback_url      |
      |                         |                        |                      |
      |                         |                        |<-- 202 Accepted -----|
      |                         |                        |                      |
      |                         |                        |<-- POST / -----------|
      |                         |                        |   (job result)       |
      |                         |                        |  JobResult JSON      |
      |                         |                        |  measurements=[5.12, |
      |                         |                        |  5.08, 5.15]         |
      |                         |                        |                      |
      |                         |<-- JobCompleted ------|                      |
      |                         |  artifact=/tmp/...     |                      |
      |                         |  duration_median=5.12  |                      |
      |                         |  measurements=[5.12,  |                      |
      |                         |    5.08, 5.15]        |                        |
      |                         |                        |                      |
      |<-- POST / wake --------|                        |                      |
      | "new job completed"     |                        |                      |
      |                         |                        |                      |
      |-- AudioMeasured ------->|                        |                      |
      |   block=A1:1:1         |                        |                      |
      |   measurements=[5.12,  |                        |                      |
      |    5.08, 5.15]         |                        |                      |
      |   median=5.12           |                        |                      |
      |                         |                        |                      |
```

#### 12.2.2 3× WhisperX measurement flow

The VM Worker executes WhisperX three times sequentially (decision C2). Each run loads the WhisperX model, transcribes the generated WAV, and reports the end timestamp of the final segment. All three values are returned in `JobResult.measurements` as raw floats. The Audio Agent computes the median client-side.

```python
# Inside VM Worker (Section 11.2.3)
measurements: list[float] = []
for run in range(3):
    segments = await _whisperx_transcribe(audio_path, model="large-v3")
    measurements.append(segments[-1]["end"] if segments else 0.0)
# Returns: [5.12, 5.08, 5.15]

# Inside Audio Agent
import statistics
median_sec = statistics.median(job_result.measurements)  # 5.12
```

The three runs execute sequentially to avoid CPU contention on the shared WhisperX process. Runs are not parallelized across GPU — the model is CPU-bound for transcription.

#### 12.2.3 Within tolerance → DurationAdjusted

```
Audio Agent (8002)          EventStoreDB            Provisioner (8081)    VM Worker (9000)
      |                         |                        |                      |
      | [ median=5.12,          |                        |                      |
      |   scripted=5.0,         |                        |                      |
      |   delta=+0.12s,         |                        |                      |
      |   tolerance=max(        |                        |                      |
      |     5.0*0.15=0.75,     |                        |                      |
      |     0.25)=0.75s ]       |                        |                      |
      |   delta < tolerance     |                        |                      |
      |   → PASS                |                        |                      |
      |                         |                        |                      |
      |-- DurationAdjusted ---->|                        |                      |
      |   block=A1:1:1         |                        |                      |
      |   measured=5.12         |                        |                      |
      |   scripted=5.0          |                        |                      |
      |   delta=+0.12           |                        |                      |
      |   within_tolerance=true |                        |                      |
      |                         |                        |                      |
      | [ Next activation:      |                        |                      |
      |   OTIO Projection merges|                        |                      |
      |   5.12s into slot       |                        |                      |
      |   A1:1:1. Block marked  |                        |                      |
      |   measured. ]           |                        |                      |
      |                         |                        |                      |
      |-- QueueJob (TTS) ----->|   [ proceed to next    |                      |
      |   block=A1:1:2         |     block A1:1:2 ]     |                      |
```

The tolerance rule (§7.3.3) is **max(15% of scripted duration, 0.25s)**. For a 5.0s target, tolerance = max(0.75, 0.25) = 0.75s. A measured 5.12s (delta +0.12s) passes. The `DurationAdjusted` effect updates the OTIO Projection, which on the next activation applies the measured duration to the corresponding slot.

#### 12.2.4 Outside tolerance → ReconciliationFailed → JobRequeued → retry

```
Audio Agent (8002)          EventStoreDB            Provisioner (8081)    VM Worker (9000)
      |                         |                        |                      |
      | [ median=7.2,           |                        |                      |
      |   scripted=5.0,         |                        |                      |
      |   delta=+2.2s,          |                        |                      |
      |   tolerance=0.75s ]     |                        |                      |
      |   delta > tolerance     |                        |                      |
      |   → FAIL                |                        |                      |
      |   attempt=2/5           |                        |                      |
      |                         |                        |                      |
      |-- ReconciliationFailed >|                        |                      |
      |   block=A1:1:2         |                        |                      |
      |   measured=7.2          |                        |                      |
      |   scripted=5.0          |                        |                      |
      |   delta=+2.2            |                        |                      |
      |   failure_type=         |                        |                      |
      |     duration_mismatch   |                        |                      |
      |                         |                        |                      |
      |-- JobRequeued --------->|                        |                      |
      |   job_id=<old>          |                        |                      |
      |   reason="too long      |                        |                      |
      |     by 2.2s"            |                        |                      |
      |   adjusted_text=        |                        |                      |
      |     "In '24..."         |                        |                      |
      |   (shortened)           |                        |                      |
      |                         |                        |                      |
      |-- QueueJob (TTS v2) -->|                        |                      |
      |   block=A1:1:2          |                        |                      |
      |   text="In '24..."      |                        |                      |
      |   attempt=2             |                        |                      |
      |                         |                        |                      |
      | [ Next activation:      |                        |                      |
      |   Provisioner sees      |                        |                      |
      |   new QueueJob,         |                        |                      |
      |   allocates VM,         |                        |                      |
      |   loop repeats... ]     |                        |                      |
```

When the measured duration exceeds tolerance, the Audio Agent computes an adjusted text (shortening or splitting the phrase) and requeues. Each block has a maximum of **5 attempts** (§7.3.4). If attempts are exhausted, the parser extracts `ReconciliationFailed` from Audio Agent output with `failure_type="duration_unrecoverable"`, which triggers a back-edge to `SCRIPT` (the text is physically unachievable at the target duration).

#### 12.2.5 All pass → ReconciliationComplete

When every block in the narration has been measured and passes tolerance, the parser extracts `ReconciliationComplete` from Audio Agent output:

```
Audio Agent (8002)          EventStoreDB
      |                         |
      | [ All blocks measured:   |
      |   A1:1:1=5.12s PASS     |
      |   A1:1:2=4.89s PASS     |
      |   ...                     |
      |   A1:3:5=3.01s PASS ]   |
      |                         |
      |-- ReconciliationComplete >|
      |   blocks_total=14       |
      |   blocks_passed=14      |
      |   blocks_failed=0       |
      |   otio_authoritative=   |
      |     true                |
      |                         |
      | [ Next activation:      |
      |   Video Agent checks    |
      |   projections, sees     |
      |   reconciliation_complete|
      |   and clean blocks →    |
      |   begins VIDEO_PRODUCTION]|
```

The `ReconciliationComplete` effect is the **gateway** from `AUDIO_RECONCILE` to `VIDEO_PRODUCTION`. Agents check for `ReconciliationComplete` and clean blocks to decide whether to begin video generation. The OTIO Projection's measured durations become **authoritative** — the Video Agent uses them as LAW for LTX-2.3 clip generation.

| Parameter | Value | Source |
|---|---|---|
| Tolerance | ±15% or ±0.25s (whichever is larger) | §7.3.3 |
| Max attempts per block | 5 | §7.3.4, `max_attempts_per_block` config |
| Max TTS budget | $2.00 USD | §7.3.4, `max_tts_budget_usd` config |
| WhisperX runs per measurement | 3 | §9.2.3, decision C2 |
| Median computation | Client-side (Audio Agent) | §7.3.2 |

---

### 12.3 Script Failure → Back-Edge with Partial Re-reconciliation

When a script revision invalidates only some blocks, V7 performs **partial re-reconciliation**: unchanged blocks keep their measured durations; only dirty blocks are re-processed. Dirty/clean marking is done by `OTIOProjection._build_from_script()` — not by the Audio Agent. The `JobProjection` syncs its `dirty_blocks`/`clean_blocks` sets from the `OTIOProjection` on every `UpdateScript`.

#### 12.3.1 voice_mismatch in VIDEO_PRODUCTION → Transition to SCRIPT

```
Video Agent (8003)          EventStoreDB           Scenario Agent (8001)
      |                         |                        |
      | [ Generates LTX-2.3     |                        |
      |   clip for scene 3.     |                        |
      |   LLM judges: "Voice    |                        |
      |   is baritone, script   |                        |
      |   says soprano." ]      |                        |
      |                         |                        |
      |-- ProductionFailed --->|                        |
      |   failure_type=         |                        |
      |     voice_mismatch      |                        |
      |   scene=3               |                        |
      |   detail="baritone      |                        |
      |     vs soprano"         |                        |
      |                         |                        |
      | [ Next activation:      |                        |
      |   Scenario Agent reads  |                        |
      |   production_failures   |                        |
      |   and sees voice_mismatch|                       |
      |   in SCRIPT_RESOLVABLE_ |                        |
      |   TYPES ]               |                        |
      |                         |                        |
      |                         |<-- POST / wake --------|
      |                         |   "check failures"     |
      |                         |                        |
      |                         |<-- UpdateScript -------|
      |                         |   blocks=[...,         |
      |                         |     {scene:3, voice:   |
      |                         |      "baritone", text: |
      |                         |      "In 1924..."} ]   |
```

The Scenario Agent checks for `ProductionFailed` effects with `failure_type in {"gap_unexpected", "voice_mismatch"}`. These are the only two failure types that trigger a back-edge to `SCRIPT`; all others either requeue in-place or halt with `ClarificationRequest`.

#### 12.3.2 Scenario Agent fixes script → UpdateScript → OTIO marks dirty/clean

```
Scenario Agent (8001)         EventStoreDB           OTIO Projection
      |                           |                         |
      | [ Receives instruction     |                         |
      |   with audio_mismatch      |                         |
      |   context. LLM revises     |                         |
      |   scene 3: changes voice   |                         |
      |   tag to "baritone",       |                         |
      |   adjusts narration text   |                         |
      |   to match. ]              |                         |
      |                           |                         |
      |-- UpdateScript ----------> |                         |
      |   blocks=[...,             |                         |
      |     {scene:3, voice:       |                         |
      |      "baritone", text:     |                         |
      |      "In 1924..."} ]      |                         |
      |                           |                         |
      | [ OTIOProjection upserts   |                         |
      |   blocks. Unchanged        |                         |
      |   blocks keep measured_sec |                         |
      |   and status. Changed      |                         |
      |   blocks marked dirty      |                         |
      |   (status="scripted"). ]   |                         |
      |                           |                         |
      | [ JobProjection._sync_     |                         |
      |   from_otio() runs:        |                         |
      |   updated blocks → dirty   |                         |
      |   all others → clean. ]    |                         |
      |                           |                         |
      | [ JobProjection removes    |                         |
      |   the voice_mismatch       |                         |
      |   failure from the list.   |                         |
      |   This prevents infinite   |                         |
      |   script-rewrite loops. ]  |                         |
```

The Scenario Agent's system prompt includes the failure context so the LLM understands what changed. The parser extracts `UpdateScript` containing the full revised scene list. The `OTIOProjection` performs the dirty/clean marking automatically during `_build_from_script()`. The `JobProjection` syncs its dirty/clean tracking via `_sync_from_otio()`.

#### 12.3.3 Audio Agent reads dirty/clean from GSA → queues only dirty blocks

```
Audio Agent (8002)            GSA / Projections      OTIO Projection
      |                           |                         |
      | [ Receives state summary   |                         |
      |   from GSA GET /.          |                         |
      |   OTIO.slots shows: ]      |                         |
      |                           |                         |
      |   Block A1:1:1: status=    |                         |
      |     "measured" → CLEAN     |                         |
      |     (keep 5.12s)           |                         |
      |   Block A1:1:2: status=    |                         |
      |     "measured" → CLEAN     |                         |
      |     (keep 4.89s)           |                         |
      |   Block A1:3:1: status=    |                         |
      |     "scripted" → DIRTY     |                         |
      |     (voice changed)        |                         |
      |   Block A1:3:2: status=    |                         |
      |     "scripted" → DIRTY     |                         |
      |     (text shortened)       |                         |
      |   Block A1:3:3: status=    |                         |
      |     "measured" → CLEAN     |                         |
      |     (keep 3.01s)           |                         |
      |                           |                         |
      | [ JobProjection confirms:  |                         |
      |   dirty_blocks={A1:3:1,    |                         |
      |   A1:3:2}                  |                         |
      |   clean_blocks={A1:1:1,    |                         |
      |   A1:1:2, A1:3:3} ]       |                         |
```

The Audio Agent does NOT compute dirty/clean itself. It reads `OTIOProjection.slots` status and `JobProjection.dirty_blocks`/`clean_blocks` from the GSA. The `OTIOProjection` has already done the dirty marking during `_build_from_script()`.

#### 12.3.4 Only dirty blocks re-reconciled; clean blocks remain authoritative

```
Audio Agent (8002)         EventStoreDB         Provisioner (8081)    VM Worker (9000)
      |                        |                      |                   |
      | [ Loop starts: only    |                      |                   |
      |   dirty blocks queued  |                      |                   |
      |   for TTS. Clean       |                      |                   |
      |   blocks skipped. ]    |                      |                   |
      |                        |                      |                   |
      |-- QueueJob (TTS) ----->|                      |                   |
      |   block=A1:3:1         |                      |                   |
      |   text="In 1924..."    |                      |                   |
      |   voice="baritone"     |                      |                   |
      |   (was "soprano")      |                      |                   |
      |                        |                      |                   |
      |-- QueueJob (TTS) ----->|                      |                   |
      |   block=A1:3:2         |                      |                   |
      |   text="He then..."    |                      |                   |
      |   (shortened)          |                      |                   |
      |                        |                      |                   |
      | [ Block A1:1:1         |                      |                   |
      |   (clean, 5.12s) is    |                      |                   |
      |   NOT queued. Block    |                      |                   |
      |   A1:1:2 (clean,       |                      |                   |
      |   4.89s) is NOT        |                      |                   |
      |   queued. ]            |                      |                   |
      |                        |                      |                   |
      | [ Reconciliation proceeds|                     |                   |
      |   for A1:3:1 and       |                      |                   |
      |   A1:3:2 only.         |                      |                   |
      |   Clean blocks remain  |                      |                   |
      |   authoritative. ]     |                      |                   |
      |                        |                      |                   |
      |-- ReconciliationComplete>|                     |                   |
      |   (when all dirty pass) |                     |                   |
```

The Audio Agent emits effects only for dirty blocks. Clean blocks are never re-measured — their `AudioMeasured` values from the previous reconciliation pass remain LAW. This avoids redundant TTS spend on unchanged content.

| Block | OTIO Status | Action | Previous Measurement |
|---|---|---|---|
| A1:1:1 | measured | Skipped, retained | 5.12s (authoritative) |
| A1:1:2 | measured | Skipped, retained | 4.89s (authoritative) |
| A1:3:1 | scripted (dirty) | Re-queued for TTS | Reset to `None` |
| A1:3:2 | scripted (dirty) | Re-queued for TTS | Reset to `None` |
| A1:3:3 | measured | Skipped, retained | 3.01s (authoritative) |

---

### 12.4 Human Intervention

Human operators interact with agents via direct HTTP GET/POST to each agent's endpoint. There is no dedicated dashboard and no intermediary routing service — the agent's own endpoints serve as the observation and control surface.

#### 12.4.1 GET agent status, POST instruction

```
Human Operator                              Audio Agent (8002)
      |                                          |
      |-- GET / -------------------------------->|
      |                                          |
      |<-- AgentStatus --------------------------|
      |   name="audio"                          |
      |   status="working"                       |
      |   current_task=                          |
      |     "reconcile block                     |
      |      A1:3:1, attempt                     |
      |      3/5"                                |
      |   last_error=null                        |
      |                                          |
      | [ Human decides text                     |
      |   is fine at 5.5s,                       |
      |   override tolerance. ]                  |
      |                                          |
      |-- POST / -------------------------------->|
      |   instruction: "Accept                   |
      |     the 5.5s duration                    |
      |     for block A1:3:1.                    |
      |     It is close enough."                 |
      |                                          |
      |<-- 200 OK -------------------------------|
      |                                          |
      | [ Next agent turn:                       |
      |   instruction appears                    |
      |   in prompt context.                     |
      |   LLM produces text. ]                   |
      |                                          |
      |-- DurationAdjusted --------------------->|
      |   block=A1:3:1                           |
      |   measured=5.5                           |
      |   human_override=true                    |
      |   reason="operator                       |
      |     approved"                            |
```

The `AgentStatus` response includes `status`, `current_task`, `last_error`, and `idle_since`. A human reading this can determine if the agent is stuck (e.g., "attempt 4/5 on same block") and issue corrective instructions.

#### 12.4.2 ExecuteRawBash approval flow

When an agent needs to run a shell command, the parser extracts `ExecuteRawBash` from agent output. The handler (not the parser) checks the `approved_by_human` flag before executing. If the flag is false, the handler emits `ClarificationRequest` and halts the pipeline until human approval.

```
Audio Agent (8002)          Handler           EventStoreDB         Human Operator
      |                        |                      |                    |
      |-- ExecuteRawBash ----->|                      |                    |
      |   command="ffmpeg      |                      |                    |
      |     -i /tmp/x.wav      |                      |                    |
      |     -af volume=1.5     |                      |                    |
      |     /tmp/x_loud.wav"   |                      |                    |
      |   approved_by_human=   |                      |                    |
      |     False              |                      |                    |
      |                        |                      |                    |
      |                        | [ Check flag.        |                    |
      |                        |   Not approved.      |                    |
      |                        |   Emit: ]            |                    |
      |                        |                      |                    |
      |<-- ClarificationRequest|                      |                    |
      |   (agent sees this     |                      |                    |
      |   as its own effect)   |                      |                    |
      |                        |                      |                    |
      |                        |-- ClarificationRequest|                   |
      |                        |   -> Event Store     |                    |
      |                        |                      |                    |
      | [ Pipeline HALTS.      |                      |                    |
      |   No new effects until |                      |                    |
      |   resolved. ]          |                      |                    |
      |                        |                      |                    |
      |                        |                      |-- (human observes  |
      |                        |                      |   via GET / )      |
      |                        |                      |                    |
      |                        |                      |<-- POST / --------|
      |                        |                      |   HumanInstruction |
      |                        |                      |   "Approve ffmpeg  |
      |                        |                      |   volume adjust.   |
      |                        |                      |   Run: ffmpeg -i   |
      |                        |                      |   /tmp/x.wav -af   |
      |                        |                      |   volume=1.5       |
      |                        |                      |   /tmp/x_loud.wav" |
      |                        |                      |                    |
      |<-- HumanInstruction ---|                      |                    |
      |   (appears in prompt)  |                      |                    |
      |                        |                      |                    |
      | [ LLM produces new    |                      |                    |
      |   text, parser         |                      |                    |
      |   extracts: ]          |                      |                    |
      |                        |                      |                    |
      |-- ExecuteRawBash ----->|                      |                    |
      |   (approved_by_human   |                      |                    |
      |    now set by human)   |                      |                    |
      |                        |-- allowed through -->|                    |
      |                        |                      |                    |
      |                        |                      |-- (shell exec)     |
```

The `approved_by_human` flag is the security gate — not an allowlist. The handler checks this flag on every `ExecuteRawBash` effect before shell execution. The `HumanInstruction` effect carries the approved command string, which the agent includes in its next turn context; the parser extracts `ExecuteRawBash` from the new output, and the handler verifies `approved_by_human=True` before execution.

| Step | Effect | Actor | Meaning |
|---|---|---|---|
| 1 | `ExecuteRawBash` | Agent | Request to run shell command (not yet approved) |
| 2 | `ClarificationRequest` | Handler | Blocked; pipeline halts |
| 3 | `HumanInstruction` | Human | Operator approves the command |
| 4 | `ExecuteRawBash` (re-extracted) | Parser / Handler | Re-parsed, approved_by_human=True |
| 5 | Shell execution | System | Command runs, output captured |

#### 12.4.3 Budget override and emergency abort

```
Human Operator                              EventStoreDB
      |                                          |
      | [ Human observes run                     |
      |   is approaching $10                     |
      |   budget via GET /.                      |
      |   Decides to increase. ]                 |
      |                                          |
      |-- POST / -------------------------------->|
      |   instruction: "Raise                    |
      |     budget to $25.00.                    |
      |     Reason: narration                    |
      |     is longer than                       |
      |     expected."                           |
      |                                          |
      |-- HumanInstruction --------------------->|
      |   action=                                |
      |     "budget_override"                    |
      |   action_params=                         |
      |     {"new_limit":25.00}                   |
      |                                          |
      |                                          |-- (next activation:
      |                                          |   _budget_exceeded
      |                                          |   reads new limit,
      |                                          |   guard False,
      |                                          |   run continues)
      |                                          |
      | [ Emergency abort: ]                     |
      |                                          |
      |-- POST / -------------------------------->|
      |   instruction: "ABORT                    |
      |     RUN IMMEDIATELY.                     |
      |     Reason: wrong                        |
      |     pipeline started."                   |
      |                                          |
      |-- HumanInstruction --------------------->|
      |   action=                                |
      |     "emergency_abort"                    |
      |                                          |
      |                                          |-- PipelineAborted >|
      |                                          |   reason=           |
      |                                          |     "human_abort"   |
      |                                          |                     |
      |                                          |-- (all VMs          |
      |                                          |   deallocated via   |
      |                                          |   VMDeallocated)    |
```

The `HumanInstruction` effect carries an `action` field that agents inspect on their next turn. Valid actions are `"budget_override"` (requires `new_limit` float in `action_params`), `"emergency_abort"`, and `"approve_command"` (for bash execution approval). An emergency abort triggers the parser to extract `PipelineAborted`, which agents detect and halt, followed by VM deallocation via the Provisioner's cleanup path (§10.2.5).

| Action Field | Required Params | Response |
|---|---|---|
| `budget_override` | `new_limit: float` | Updates `max_run_budget_usd` in config; budget check re-evaluates |
| `emergency_abort` | `reason: str` | Emits `PipelineAborted`; agents halt; Provisioner deallocates all VMs |
| `approve_command` | `command: str` | Clears pending `ClarificationRequest`; command is re-injected into agent prompt |


---

### 12.5 Startup Sequence

This section specifies the exact order of process startup, the first HTTP request that creates a run, and the initial agent wake chain.

#### 12.5.1 Process startup order

| Order | Process | Port | Command | Dependency |
|---|---|---|---|---|
| 1 | EventStoreDB | 2113 | `docker run -p 2113:2113 eventstore/eventstore:24.2 --insecure --run-projections=System` | None |
| 2 | Global State Agent | 8000 | `python global_state_agent.py` | EventStoreDB |
| 3 | Scenario Agent | 8001 | `python -m agents.scenario` | Global State Agent |
| 4 | Audio Agent | 8002 | `python -m agents.audio` | Global State Agent |
| 5 | Video Agent | 8003 | `python -m agents.video` | Global State Agent |
| 6 | Assembly Agent | 8005 | `python -m agents.assembly` | Global State Agent |
| 7 | Provisioner | 8081 | `python -m provisioner.main` | EventStoreDB |

Each agent is an independent ASGI process. There is no central coordinator process and no intermediary routing service. Agents discover each other via the `Config` read at import time (§14.1), which contains hardcoded host:port pairs. No service discovery, no health checks, no heartbeat mesh.

#### 12.5.2 Run creation

A run begins with a single HTTP POST from the human operator directly to the Scenario Agent:

```
POST http://localhost:8001/
Content-Type: application/json

{
  "run_id": "0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b",
  "topic": "The collapse of the Bretton Woods system",
  "target_duration_min": 8,
  "target_duration_max": 10,
  "narrator_voice": "qwen3-tts-en-default",
  "style_tags": ["academic", "cautious"],
  "budget_usd": 5.00
}
```

The Scenario Agent handler:
1. Validates the payload against `RunRequest` Pydantic model.
2. Appends `PipelineStarted` to EventStoreDB stream `run-{run_id}`.
3. Appends `BudgetSet` with the requested budget.
4. Proceeds to construct its narrative; the parser extracts `ScriptProposed`.
5. Returns `201 Created` with `{"run_id": "...", "status": "started", "effects_extracted": [...]}`.

No polling. No waiting. If the Scenario Agent is not yet listening, the POST fails; the operator restarts the Scenario Agent and re-POSTs.

#### 12.5.3 Initial wake chain

The Scenario Agent receives the wake POST, queries the Global State Agent via `GET /` for current state, constructs its narrative; the parser extracts `ScriptProposed`. It then POSTs wake to the Audio Agent (port 8002). The parser extracts `QueueJob` (TTS) from Audio Agent output; the pipeline awaits `AudioMeasured`. Once all audio blocks pass tolerance, the Audio Agent POSTs wake to the Video Agent (port 8003). The parser extracts `QueueJob` (video generation) from Video Agent output; the pipeline awaits `VideoMeasured`. Once all video blocks pass, the Video Agent POSTs wake to the Assembly Agent (port 8005). The parser extracts `FinalComposition` from Assembly Agent output and the run is complete.

This is the **happy path**. Any agent may instead produce output from which the parser extracts `ProductionFailure`, halting the run, or the parser extracts `UpdateScript` which rewinds to the Scenario Agent. The wake chain is not hardcoded; each agent decides the next wake target based on its own state and the effects extracted from its output.

---

## 13. Security Model

### 13.1 ExecuteRawBash Allowlist

**Threat.** `ExecuteRawBash` grants agents arbitrary shell access on worker VMs. A compromised or misdirected agent can execute destructive commands or exfiltrate data.

**Defense.** A strict allowlist gates every command. Pre-approved binaries execute without human intervention; all others raise a `ClarificationRequest`.

| Command | Permitted Arguments | Rejected Patterns |
|---|---|---|
| `ffmpeg` | Input/output paths, codec flags, filter graphs | Network URLs (`http://`, `sftp://`) |
| `ffprobe` | `-print_format json`, `-show_streams`, file paths | `--execute`, shell metacharacters |
| `whisperx` | `--model`, `--language`, `--output_format`, file paths | `--download-root` with absolute paths |
| `vastai` | `create instance`, `destroy instance`, `show instances` | `ssh`, `scp`, any file transfer |
| `python3` | Script file path + literal arguments only | `-c` (inline code) |

Validation is two-pass: command name against the allowlist, then argument tokens against a per-command regex denylist. Network egress from `ffmpeg` is blocked at the VM firewall as defense-in-depth (see 13.4).

**Escape hatch.** Non-allowlisted commands surface a `ClarificationRequest` with the full command string. The operator may approve (one-time), approve (pattern), or deny. Denial returns `CommandDisallowed`; the agent must select an alternative strategy.

```python
from pydantic import BaseModel, Field
from typing import Literal, List

class BashCommand(BaseModel):
    """Shell command submitted for allowlist validation."""
    command: str
    agent_id: str

class ValidationResult(BaseModel):
    """Outcome of allowlist validation."""
    decision: Literal["approved", "blocked_pending_approval", "denied"]
    matched_rule: str
    blocked_args: List[str] = Field(default_factory=list)
```

### 13.2 Budget Enforcement

**Threat.** LLM API calls, GPU rental, and storage accumulate cost without bound. A runaway pipeline can consume hundreds of dollars in minutes.

**Defense.** Every pipeline run carries a monotonically-increasing cost accumulator checked against a per-run budget cap. Default: $10.00 USD, configurable per-run via `budget_usd`. The accumulator tracks LLM tokens, GPU rental (per-second), and egress bandwidth (per-GB).

```python
class BudgetLedger(BaseModel):
    """Cumulative spend against a per-run budget ceiling."""
    budget_usd: float = Field(default=10.0, ge=0.01, le=1000.0)
    spent_llm_usd: float = Field(default=0.0)
    spent_gpu_usd: float = Field(default=0.0)
    spent_egress_usd: float = Field(default=0.0)

    @property
    def remaining_usd(self) -> float:
        return self.budget_usd - (
            self.spent_llm_usd + self.spent_gpu_usd + self.spent_egress_usd
        )

    def check(self, next_charge_usd: float) -> bool:
        return (self.remaining_usd - next_charge_usd) >= 0.0
```

**Escape hatch.** If a projected charge exceeds remaining budget, the parser extracts `PipelineAborted` with `reason=budget_exceeded` and a final ledger. All non-committed GPU instances are destroyed immediately. Partial outputs are retained for inspection.

### 13.3 Agent Loop Detection

**Threat.** An agent may enter an infinite loop: repeatedly calling the same tool with identical arguments, or cycling through strategies without progress.

**Defense.** Dual detection runs against every agent's turn history:

1. **Duplicate-effects detection.** Hashes observable side effects (files written, API calls, VMs launched) after each turn. Same hash twice within the window fires the detector.
2. **No-progress detection.** If the task-state score (completed checklist items) does not increase for `N` consecutive turns, the detector fires. Default `N=5`, configurable per agent type.

| Detector | Signal | Threshold | Action |
|---|---|---|---|
| Duplicate effects | Identical side-effect hash | 2 occurrences | `LoopDetected` → `ClarificationRequest` |
| No progress | Task-state delta = 0 | `N` turns (default 5) | `LoopDetected` → `ClarificationRequest` |

**Escape hatch.** Either trigger pauses the agent and surfaces a `ClarificationRequest` with the last `N` turns of context. The operator may resume with guidance, terminate the agent, or reassign.

```python
class LoopDetectorConfig(BaseModel):
    """Per-agent loop detection parameters."""
    progress_threshold_turns: int = Field(default=5, ge=2, le=20)
    effect_dedup_window: int = Field(default=10, ge=2, le=50)
    enabled_detectors: List[Literal["duplicate_effects", "no_progress"]] = Field(
        default_factory=lambda: ["duplicate_effects", "no_progress"]
    )
```

### 13.4 VM Isolation

**Threat.** GPU worker VMs execute arbitrary code. A compromised VM could exfiltrate secrets, persist malware, or attack the control plane host.

**Defense.** Three isolation layers:

1. **Ephemeral lifecycle.** VMs are created per pipeline stage and destroyed within 60 seconds of completion. Root disks are provisioned from a golden image; no writable overlay persists.
2. **No secrets on workers.** API keys reside exclusively on the control plane host. Workers authenticate via short-lived JWTs (5-minute expiry, single-use refresh) granting access only to the stage's input/output buckets.
3. **Network egress restriction.** Outbound connections are limited to the control plane and object-storage endpoint. All other egress is blocked at the hypervisor firewall.

```python
class VMIsolationConfig(BaseModel):
    """Security parameters for ephemeral GPU worker VMs."""
    jwt_ttl_seconds: int = Field(default=300, ge=60, le=3600)
    destroy_after_stage_seconds: int = Field(default=60, ge=0, le=300)
    allowed_egress_hosts: List[str] = Field(
        default_factory=lambda: ["coordinator.internal", "storage.internal"]
    )
    enable_process_monitoring: bool = Field(default=True)
```

**Escape hatch.** Anomalous behavior (failed health check, unexpected process, unauthorized connection) triggers immediate VM destruction and stage retry on a fresh instance. Anomaly events are logged to a security audit stream.

---

## 14. Configuration

The `Config` Pydantic model (§14.1) is the single source of truth for all tunable pipeline parameters. It is instantiated once at control plane startup from a `config.py` module and passed read-only into every downstream component. No environment-variable fallbacks or runtime mutation are permitted; changing a value requires a code change and redeployment.

### 14.1 Pipeline Config

#### 14.1.1 max_run_budget_usd, max_attempts_per_block, max_tts_budget_usd

`max_run_budget_usd` (`float`, default `10.00`) defines the hard upper bound on cloud-spend for a single pipeline run. This is a *post-approval* gate: the agent aborts the run if projected cumulative cost exceeds this threshold. `max_attempts_per_block` (`int`, default `5`) is the per-block retry ceiling. Each block may be retried up to this many times before the agent marks the run FAILED and enters cleanup. `max_tts_budget_usd` (`float`, default `2.00`) caps TTS-specific spend per run, evaluated independently of the overall run budget because TTS is billed per-character via a separate provider API.

#### 14.1.2 tolerance_percent, tolerance_abs_sec

`tolerance_percent` (`float`, default `0.15`) and `tolerance_abs_sec` (`float`, default `0.25`) are the dual-threshold acceptance criteria for assembly-stage duration validation. A generated segment passes if its actual duration deviates from the target by no more than 15 % *and* no more than 0.25 s. Both conditions must hold. These values are chosen to accommodate natural speech-rate variation (the percent guard) while preventing sub-frame timing errors in 24 fps video (the absolute guard).

#### 14.1.3 loop_detection_threshold

`loop_detection_threshold` (`int`, default `5`) triggers loop-detection logic in the agent. When the same block transitions to FAILED and back to PENDING more than 5 times within a single run, the agent raises a `LoopDetectedError` and aborts. There is no automatic stale-job detection; the operator monitors via `GET /` and intervenes manually.

#### 14.1.4 ALLOWLISTED_COMMANDS list

`ALLOWLISTED_COMMANDS` (`list[str]`, default `["ffmpeg", "ffprobe", "whisperx", "vastai", "python3"]`) is the explicit permit-list of shell commands that the VM agent may invoke via `subprocess.run`. Any command string whose basename is not in this list is rejected with `SecurityError` before execution. The list is intentionally short; adding a command requires a code review and version bump.

```python
from pydantic import BaseModel, Field
from typing import Literal

class Config(BaseModel):
    """Single source of truth for all tunable pipeline parameters.

    Instantiated once at control plane startup and passed read-only
    into all downstream components. No runtime mutation permitted.
    """

    # 14.1.1 — Pipeline limits
    max_run_budget_usd: float = Field(default=10.00, ge=0.0)
    max_attempts_per_block: int = Field(default=5, ge=1)
    max_tts_budget_usd: float = Field(default=2.00, ge=0.0)

    # 14.1.2 — Assembly tolerance (dual threshold)
    tolerance_percent: float = Field(default=0.15, ge=0.0, le=1.0)
    tolerance_abs_sec: float = Field(default=0.25, ge=0.0)

    # 14.1.3 — Health & loop detection
    loop_detection_threshold: int = Field(default=5, ge=1)

    # 14.1.4 — B2 storage
    b2_bucket_name: str = Field(default="doc-pipeline-prod")

    # 14.1.5 — VM-agent command allowlist
    allowlisted_commands: list[str] = Field(
        default_factory=lambda: [
            "ffmpeg", "ffprobe", "whisperx", "vastai", "python3"
        ]
    )

    # 14.2 — VM sizing (see subsections)
    tts_gpu_type: Literal["RTX_4090"] = "RTX_4090"
    tts_vram_gb: int = 24
    tts_cpu_cores: int = 8
    tts_disk_gb: int = 100

    video_gpu_type: Literal["RTX_A6000", "RTX_4090"] = "RTX_A6000"
    video_vram_gb: int = 48
    video_cpu_cores: int = 16
    video_disk_gb: int = 200

    coord_vcpu: int = 2
    coord_ram_gb: int = 4
    coord_disk_gb: int = 100

    # 14.3 — Rate limits
    llm_requests_per_minute: int = 60
    llm_tokens_per_minute: int = 200_000
    vastai_requests_per_minute: int = 30

    # Agent models
    scenario_model: str = "deepseek-v4-flash"
    audio_model: str = "deepseek-v4-flash"
    video_model: str = "deepseek-v4-flash"
    assembly_model: str = "deepseek-v4-flash"
    compaction_model: str = "deepseek-v4-flash"

    # Token budgets
    max_tokens: int = 8000
    context_manager_max_tokens: int = 128_000
    compaction_threshold: float = 0.85

    # VMs
    vastai_api_key: str = ""
    vm_tts_image: str = "vastai/worker:tts"
    vm_ltx_image: str = "vastai/worker:ltx"

    # V7 additions
    max_tts_cost_hr: float = 0.80
    max_ltx_cost_hr: float = 1.20
    eventstore_uri: str = "esdb://localhost:2113?tls=false"
```

### 14.2 VM Sizing

#### 14.2.1 TTS VM: GPU type, VRAM, CPU, disk

The TTS VM (§11.1) runs speaker-cloning inference and requires 24 GB VRAM for the Qwen3-TTS model in float16. Specification: GPU `RTX_4090` (24 GB VRAM), 8 CPU cores, 100 GB SSD. The 100 GB disk accommodates the base model weights (~4 GB), speaker reference uploads (~50 MB each), and generated WAV output (~10 MB/min at 48 kHz). No swap is configured; inference fails fast with `OutOfMemoryError` if the model does not fit.

#### 14.2.2 Video VM: GPU type, VRAM, CPU, disk

The Video VM (§11.2) runs LTX-Video inference at 720p and requires 48 GB VRAM for the unquantized model. Specification: GPU `RTX_A6000` (48 GB VRAM), 16 CPU cores, 200 GB SSD. The larger disk stores the diffusion model weights (~24 GB), input conditioning frames, and output MP4 segments. Fallback to `RTX_4090` (24 GB) is permitted only when the model is quantized to int8 and quality checks (§11.3) still pass.

#### 14.2.3 Control Plane Host: 2 vCPU, 4 GB RAM, 100 GB disk

The control plane host runs the agent services and EventStoreDB. It does not run GPU workloads. Specification: 2 vCPU, 4 GB RAM, 100 GB SSD. The disk hosts projection state, log files, and agent code. RAM is sized for the Pydantic models and in-memory job queue; typical working set is <512 MB.

### 14.3 Rate Limits

#### 14.3.1 LLM and Vast.ai API rate limits

There is no central coordinator event loop in V7. Rate limits are enforced per-agent at the HTTP client level.

| Resource | Limit | Enforcement |
|---|---|---|
| LLM API requests | 60 per minute | Per-agent `AsyncLimiter` in the agent handler |
| LLM API tokens | 200 000 per minute | Per-agent token counter, resets every 60 s |
| Vast.ai API calls | **Max 3 concurrent** | Global semaphore across all Provisioner activations |

**Why max 3 concurrent Vast.ai calls:** The Vast.ai API has aggressive IP-level rate limiting. Exceeding 3 concurrent `search` / `create` / `destroy` calls triggers 429 responses. The Provisioner serializes VM operations through a `asyncio.Semaphore(3)`.

LLM rate limits are per-agent because each agent runs in a separate process and there is no shared scheduler.

---

## 15. File Structure

### 15.1 Directory Layout

#### 15.1.1 Complete tree: server/v7/ with all files

The repository root is `server/v7/`. Files and directories are grouped by responsibility: top-level modules for orchestration, `agents/` for domain-specific generation logic, `provisioner/` for cloud VM lifecycle, and `vm/` for the on-instance agent runtime.

```text
server/v7/
├── README.md                          # Project overview and quick-start
├── ARCHITECTURE_V7.md                 # This document
├── docker-compose.yml                 # EventStoreDB + agent services
├── config.py                          # Pydantic Config model (§14.1)
├── effects.py                         # 32 effect types + EffectUnion + KIND_TO_MODEL
├── event_store.py                     # Thin EventStoreDB client wrapper (§5)
├── projections.py                     # Read-model builders: OTIO, Job, VM, State, Budget
├── parser.py                          # Category-conditioned effect parser (§9.6)
├── agent_base.py                      # FastAPI app + create_pipeline_agent factory
├── global_state_agent.py              # Global State Agent: GET / only, serves projections (§2.4)
├── situations.py                      # SITUATION_TEMPLATES text blocks for agent prompts
├── rules.py                           # RULES text blocks for agent system prompts
├── agents/
│   ├── __init__.py
│   ├── scenario.py                    # ScenarioAgent: role instructions + focus
│   ├── audio.py                       # AudioAgent: tolerance + reconciliation focus
│   ├── video.py                       # VideoAgent: quality judgment + LTX focus
│   └── assembly.py                    # AssemblyAgent: ffmpeg + validation
├── provisioner/
│   ├── __init__.py
│   ├── main.py                        # FastAPI app for provisioner agent (§10)
│   └── bash.py                        # Direct bash execution primitives (no wrappers)
└── vm/
    ├── __init__.py
    ├── agent.py                         # On-instance daemon: fetch, execute, report (§11)
    ├── onstart_tts.sh                   # TTS VM bootstrap: conda env + model download
    └── onstart_ltx.sh                   # Video VM bootstrap: conda env + model download
```

**Top-level modules.** `config.py` contains the `Config` Pydantic model and is imported by `agent_base.py`, `global_state_agent.py`, and `provisioner/main.py`. `effects.py` defines the 32 `Effect` dataclass hierarchies and the `EffectUnion` discriminated union used by `parser.py`. `event_store.py` and `projections.py` form the persistence layer: the former appends domain events to EventStoreDB, the latter rebuilds read models. `agent_base.py` is the executable entry point for each media agent; it instantiates `Config`, creates the FastAPI app, and serves the HTTP endpoints. `global_state_agent.py` is the executable entry point for the Global State Agent (§2.4); it subscribes to EventStoreDB streams and serves projections via `GET /`.

**`agents/` package.** Each module defines role instructions, focus functions, and permitted effects for one agent type. `assembly.py` performs FFmpeg muxing, WhisperX transcript alignment, and dual-threshold duration validation.

**`provisioner/` package.** `main.py` exposes the FastAPI app with `tick()` and webhook handlers. `bash.py` contains only generic async subprocess helpers — no Vast.ai-specific wrapper methods.

**`vm/` package.** `agent.py` is the only Python process running on rented instances. It receives tasks via POST, executes the command allowlist (§14.1.4), streams stdout/stderr back, and sends results to the Provisioner. The `onstart_*.sh` scripts are rendered as Vast.ai "on-start" scripts; they install Miniconda, create the environment, and download model weights.

### 15.2 Python Dependencies

```text
pydantic>=2.0
esdbclient>=1.0
opentimelineio>=0.16.0
instructor>=1.0.0
openai>=1.0.0
httpx>=0.27.0
fastapi>=0.110.0
uvicorn>=0.29.0
python-multipart>=0.0.9
pydantic-deep>=0.3.0
uuid_extensions>=0.0.10
```

Install: `pip install -r requirements.txt`

### 15.3 API Key Management

| Key | Environment Variable | Used By |
|---|---|---|
| DeepSeek API | `DEEPSEEK_API_KEY` | All agents (LLM calls), parser |
| Vast.ai | `VASTAI_API_KEY` | Provisioner (VM allocation via bash) |

Keys are read from environment variables at startup. Never commit keys to version control.

---

## 16. Traceability and Observability

V7 does not adopt OpenTelemetry, W3C Trace Context, or structured metrics. Traceability is achieved through minimal, deterministic mechanisms that require no additional infrastructure.

### 16.1 Minimal traceability contract

| Mechanism | Location | Purpose |
|---|---|---|
| `run_id` | Every effect, every HTTP request header | Correlates all events and requests belonging to one pipeline run |
| `effect_id` | Every effect (UUIDv7) | Idempotency key; also serves as exact event identifier |
| `agent` field | Every effect | Identifies which component produced the text from which the effect was extracted |
| `timestamp` | Every effect | Epoch seconds; monotonic within a run |
| `X-Run-ID` header | Every HTTP request (agent→agent, operator→agent, worker→provisioner) | Correlates HTTP traffic with event-stream data |
| `sequence` | EventStoreDB stream revision | Total order of events within a run |

### 16.2 Operator observability

The operator traces a run by:

1. **Event inspection:** `GET /?run_id={run_id}` on the **Global State Agent** (port 8000) returns the complete projection bundle including `latest_sequence`. The operator can also query EventStoreDB directly via `read_since(run_id, 0)` to see the raw event history.
2. **Log grep:** All components log with `run_id=` and `effect_id=` prefixes. A single `grep run_id=0192a3b4... /var/log/pipeline/*.log` yields the complete execution trace.
3. **Projection state:** `GET /` on the Global State Agent returns `otio`, `jobs`, `vms`, `state`, and `budget` projections. `GET /` on individual agents returns agent-specific health status only.

No dashboards, no metrics servers, no collectors. The event stream is the single source of truth; logs are secondary; projection state is available on demand via HTTP.

---

### 16.3 Logging Specification

All components log to **stdout** (captured by Docker or systemd). Logs are plain text, not structured JSON. The format is:

```
YYYY-MM-DD HH:MM:SS.mmm | LEVEL | COMPONENT | run_id=... | effect_id=... | message
```

**Log levels:**

| Level | Used when | Example |
|---|---|---|
| `INFO` | Normal operation, effect appended, agent activated | `INFO scenario_agent run_id=abc effect_id=def UpdateScript appended for slot A1:3:2` |
| `WARN` | Recoverable anomaly, retry, slow operation | `WARN provisioner run_id=abc VM 12345 health check failed, retrying` |
| `ERROR` | Unrecoverable failure, crash, validation error | `ERROR audio_agent run_id=abc effect_id=def Parser ValidationError: voice must be V1/V2/V3` |
| `DEBUG` | Detailed internals (disabled in production) | `DEBUG parser run_id=abc extracted 2 effects confidence=8` |

**What gets logged at each step:**

| Step | Logged by | Content | Level |
|---|---|---|---|
| Agent receives POST / | Agent handler | `notification_type`, `run_id`, agent role | INFO |
| Agent queries GSA | Agent handler | `GET /?run_id=...` response time, projection counts | DEBUG |
| Narrative built | Agent handler | Number of situations, total narrative tokens | DEBUG |
| LLM call starts | pydantic-deep | Model name, token budget | DEBUG |
| LLM call completes | pydantic-deep | Output tokens, duration_sec | INFO |
| Parser runs | Parser | Phase results, effects extracted, confidence | INFO |
| Effects appended | Handler | Effect kinds, stream name, sequence numbers | INFO |
| Downstream wake sent | Handler | Target agent, status code | INFO |
| GSA processes event | GSA | Event kind, sequence, projection update delta | DEBUG |
| GSA serves GET / | GSA | `run_id`, `latest_sequence`, response size | DEBUG |
| VM worker starts job | VM worker | `job_id`, `job_type`, GPU info | INFO |
| VM worker completes | VM worker | `job_id`, duration_sec, artifact size | INFO |

**Log retention:** 7 days via Docker log rotation (`max-size=100m`, `max-file=10`). Operators grep logs using `run_id=` and `effect_id=` as indexed prefixes.

---

### 16.4 Metrics (Projection-Derived, Not External)

V7 does not run a metrics server (Prometheus, StatsD, etc.). All "metrics" are derived from projections and returned via `GET /` on the GSA. The operator or an external script queries the GSA periodically and computes rates.

**Metrics available from `GlobalStateResponse`:**

| Metric | Source field | Unit |
|---|---|---|
| Effects appended | `latest_sequence` | count (per run) |
| Pipeline phase | `state.current_phase` | categorical |
| Budget spent | `budget.spent_usd` | USD |
| Budget remaining | `budget.remaining_usd` | USD |
| Active VMs | `vms.active_count` | count |
| VM hourly cost | `vms.estimated_hourly_cost_usd` | USD/hr |
| Total slots | `otio.total_slots` | count |
| Dirty slots | `otio.dirty_slots` | count |
| Measured slots | `otio.measured_slots` | count |
| Delivered slots | `otio.delivered_slots` | count |
| Pending jobs | `len(jobs.jobs)` with `status="pending"` | count |
| Running jobs | `len(jobs.jobs)` with `status="running"` | count |
| Failed jobs | `len(jobs.jobs)` with `status="failed"` | count |
| Production failures | `len(jobs.production_failures)` | count |

**Rate computation (external script example):**

```python
import time

class PipelineMonitor:
    """Simple monitor that polls GSA and computes rates."""

    def __init__(self, gsa_url: str):
        self.gsa_url = gsa_url
        self.last_seq: dict[str, int] = {}
        self.last_ts: dict[str, float] = {}

    async def poll(self, run_id: str):
        resp = await httpx.get(f"{self.gsa_url}/?run_id={run_id}")
        data = resp.json()

        seq = data["latest_sequence"]
        now = time.time()

        if run_id in self.last_seq:
            delta_seq = seq - self.last_seq[run_id]
            delta_t = now - self.last_ts[run_id]
            effects_per_sec = delta_seq / delta_t if delta_t > 0 else 0
            print(f"Run {run_id}: {effects_per_sec:.2f} effects/sec, phase={data['state']['current_phase']}")

        self.last_seq[run_id] = seq
        self.last_ts[run_id] = now
```

This script is **not part of the pipeline**. It is an external operator tool. No metrics are pushed or scraped by the pipeline itself.

---

### 16.5 Alerting Rules (Human-Triggered)

V7 has **no automated alerting system** (no PagerDuty, no webhooks). Alerts are conditions that the operator detects via `GET /` or log grep. The operator is expected to poll the GSA or scan logs periodically.

| Condition | How to detect | Operator action |
|---|---|---|
| `PipelineAborted` | `state.current_phase == "aborted"` | Inspect logs, determine cause, decide whether to restart or fix |
| `AgentLoopDetected` | `state.recent_effects` shows duplicate kinds 5+ times | POST `HumanInstruction` to the looping agent with directive to stop |
| `BudgetExceeded` | `budget.exceeded == True` | POST `HumanInstruction` with `action="budget_override"` or abort |
| `VMProvisionFailed` × 3 | `jobs.production_failures` has 3+ entries with `failure_category="vm_provision"` | Check Vast.ai account balance, retry, or switch GPU type |
| `JobFailed` (retryable=False) | `jobs.jobs` has job with `status="failed"` and `retryable=False` | Inspect error message, POST `HumanInstruction` to requeue or abort |
| `JobQueuedLong` | `jobs.jobs` has pending job with `created_at` > 5 min ago | Check `vms.active_count` — if 0, Provisioner may be stuck; POST wake to Provisioner |
| `BlockAtMaxAttempts` | `jobs.block_attempts[slot_id] >= max_attempts` | POST `HumanInstruction` to accept mismatch or rewrite script |
| Agent not responding | `GET /` on agent returns error or timeout | Restart agent process, check logs |
| EventStoreDB disk full | ESDB rejects writes with explicit error | Free disk space, restart ESDB container |

**Why no automated alerts:** The pipeline is designed for attended operation during active runs. A typical documentary run completes in 10–30 minutes. The operator is present and polling. Automated alerting adds infrastructure (webhook endpoints, notification services, retry logic) that the architecture deliberately avoids.

---

### 16.6 Distributed Tracing via Causation Chains

V7 does not use OpenTelemetry, Jaeger, or W3C Trace Context. Tracing is achieved through causation and correlation IDs embedded in event metadata (§5.1.1).

**How to trace a causal chain:**

```python
async def trace_chain(run_id: str, correlation_id: str):
    """Return all events in a causal chain, ordered by sequence."""
    events = await replay(run_id)
    chain = [
        e for e in events
        if json.loads(e.get("metadata", "{}")).get("correlation_id") == correlation_id
    ]
    return sorted(chain, key=lambda e: e["sequence"])
```

**Example: trace a job from QueueJob to JobCompleted:**

```
$ python -c "
import asyncio
from event_store import replay

events = asyncio.run(replay('run-abc'))
for e in events:
    meta = json.loads(e.get('metadata', '{}'))
    if meta.get('correlation_id') == 'corr-123':
        print(f'{e[\"sequence\"]}: {e[\"kind\"]} (caused by {meta.get(\"causation_id\")})')
"

Output:
3: QueueJob (caused by PipelineStarted)
4: VMAllocated (caused by QueueJob)
5: JobStarted (caused by VMAllocated)
6: JobCompleted (caused by JobStarted)
7: AudioMeasured (caused by JobCompleted)
```

**HTTP tracing:** The `X-Run-ID` header is present on every agent-to-agent POST. The `X-Effect-ID` header is present on downstream wake POSTs, carrying the `effect_id` of the effect that triggered the wake. This allows correlating HTTP traffic with event stream data:

```
Audio Agent (8002) POSTs wake to Video Agent (8003)
  Headers:
    Content-Type: application/json
    X-Run-ID: run-abc
    X-Effect-ID: 0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b  # the ReconciliationComplete that triggered this wake
```

**Why this is sufficient:** A documentary pipeline has <10 agents and <2000 events per run. The operator can trace any issue by grepping logs and replaying the event stream. Distributed tracing infrastructure (spans, collectors, backends) is overkill for this scale and adds operational complexity.

---

## 17. Glossary

### 17.1 Term Definitions

#### 17.1.1 All terms with precise definitions

| Term | Definition |
|------|------------|
| **Block** | A unit of work in the pipeline: one of `scenario`, `audio`, `video`, or `assembly`. Each block has a dedicated agent, VM type, and retry budget. |
| **Coordinator** | The collective term for EventStoreDB and all agent / provisioner HTTP services running on the control-plane host. The human operator is external to this definition. |
| **Deep agent** | A pydantic-deep main agent with hooks (OTIO-aware compaction) and sliding-window fallback. Not a subagent. |
| **Dual-threshold validation** | Assembly acceptance criteria requiring both a relative (15 %) and an absolute (0.25 s) duration check to pass. |
| **Effect** | A typed Pydantic model representing a pipeline mutation. The only legal way to change state. 32 types in 8 families. |
| **Emergent phase** | A descriptive pipeline label (SCRIPT, AUDIO_RECONCILE, etc.) that emerges from projection state, not enforced by any state machine. |
| **Event** | An immutable, append-only record describing a state change. Stored in EventStoreDB with monotonic `sequence` and epoch `timestamp`. |
| **Event loop** | The agent's activation cycle: receive POST, rebuild projections, run LLM, emit effects, return. |
| **Event store** | EventStoreDB — the append-only log of all domain events. Source of truth for pipeline state; no in-memory mirrors. |
| **Generation plan** | The JSON output of the ScenarioAgent containing shot list, speaker assignments, script, and duration targets per segment. |
| **LTX-Video** | The diffusion-based video-generation model running on the Video VM. Requires 48 GB VRAM at full precision. |
| **Projection** | A read-optimized Python dataclass built by folding (reducing) the event stream. Rebuilt incrementally on every activation. |
| **Prompt-based rules** | Prioritization and decision logic embedded in agent system prompts, not in Python code. |
| **Run** | A single end-to-end invocation of the pipeline for one screenplay, from approval through assembly to final MP4 delivery. |
| **Scenario** | A screenplay excerpt (typically 3–8 pages) selected for adaptation. The input artifact to the pipeline. |
| **SD-JSON** | "Screenplay Data — JSON". The normalized JSON representation of a screenplay after parsing, containing scenes, dialogue, and slug lines. |
| **Shot plan** | The per-segment visual plan generated by the ScenarioAgent: shot type, motion, characters, props, duration. |
| **Situation** | A narrative template describing a condition an agent should respond to (e.g., `fresh_dirty_block`, `vm_stale`). |
| **TTS** | Text-to-Speech. The Qwen3-TTS model running on the TTS VM that converts dialogue lines into character-specific WAV audio. |
| **VM agent** | The Python daemon (`vm/agent.py`) executing on rented GPU instances. Receives jobs, runs allowlisted commands, reports results. |
| **Vast.ai** | The cloud-GPU marketplace used for on-demand rental of TTS and Video VMs. Billed per-second. |
| **WhisperX** | The forced-alignment tool (via `whisperx` CLI) that produces word-level timestamps for transcript-to-audio synchronization. |

---

*V7 Architecture — pydantic-deep, agent HTTP endpoints, EventStoreDB, prompt-based rules, agentic Provisioner with bash/research/memory tools, no watcher.*
