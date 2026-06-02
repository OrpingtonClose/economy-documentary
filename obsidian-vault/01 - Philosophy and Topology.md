---
{
  "title": "Core Philosophy & System Topology",
  "section": "1",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

[[00 - Index|◀ Back to Index]]

# 📖 Philosophy & Topology

This module details the core architectural commitments and structural topology of the Autonomous Documentary Production Pipeline. 

---

## 1. Core Philosophy

Thirteen foundational commitments and three long-term strategic pillars govern the pipeline. The hard principles in §1.11 enumerate every invariant and its enforcement mechanism.

### 1.1 Event Log as Sole Source of Truth

#### 1.1.1 All state derived from events; replay reconstructs everything
Every fact is an **Effect** — a typed Pydantic model — appended to an append-only SQLite event log. EventStoreDB is the future scalability path for distributed deployments. The OTIO timeline, job queue, VM inventory, and pipeline phase are **projections**: read models rebuilt by pure fold functions. Replay from sequence `0` reconstructs everything exactly.

#### 1.1.2 Event store is only persistent storage; all other state is ephemeral projection
The SQLite event store database (`events.db`) is the sole durable storage. EventStoreDB streams are the future scalability path. Agents hold no session state. VM workers are ephemeral. Projections are in-memory folds rebuilt from the event log on every Global State Agent (GSA) activation.

---

### 1.2 Effects as Only Legal Mutations

#### 1.2.1 Typed Pydantic models; parser extracts from agent text
A **category-conditioned parser** (§9.6) extracts Effects from agent text using `instructor` + `deepseek-v4-flash`. Every Effect carries `kind: Literal[...]`, `effect_id: UUID` (UUIDv7), `agent: str`, and `timestamp: float` (seconds since epoch). Invalid payloads are rejected before reaching the event store.

#### 1.2.2 No direct state mutation outside event store append
All state changes enter through SQLite event store append. Agents do not call projection methods. Projections are read-only consumers.

---

### 1.3 No State Machine — Prompt-Based Rules

**No state machine.** Pipeline "state" is emergent from projection state (e.g., "all audio blocks clean" emerges from Timeline, not from a state variable). Agents read projection-derived narratives and decide what to do. Rules live in the agent's system prompt, not in code.

Prioritization, filtering, and response selection happen **inside the agent via prompt instruction**. The agent's system prompt contains the rules for what to prioritize and how to respond.

> [!TIP]
> **Core Principle:** Whenever something can be done via prompt, do so — cut code complexity.

---

### 1.4 No Timeouts in Code

No pipeline code calls `setTimeout`, `threading.Timer`, `asyncio.timeout`, or any timer primitive. HTTP requests and subprocess calls on primary execution paths run to completion. This is architecture policy.

> [!WARNING]
> An explicit exception is granted for lightweight health and readiness checks (e.g. pinging a VM or agent status check) where setting a timeout is necessary to prevent the polling coordinator from blocking indefinitely on unreachable resources.

---

### 1.5 Real Engines Only

#### 1.5.1 Qwen3-TTS, LTX-2.3, DeepSeek API; no simulation layers
TTS uses **Qwen3-TTS** on GPU VMs. Video uses **LTX-2.3**. Agent LLM inference uses **DeepSeek API** (`deepseek-v4-flash`). No mocks, no stubs. Unavailable engines trigger `ClarificationRequest`.

---

### 1.6 Never Regex

#### 1.6.1 Category-conditioned extraction via instructor + deepseek-v4-flash
No regex extracts structured data from agent output. The parser uses the agent's current role to determine valid Effect subtypes and constrains the LLM to schema-compliant JSON via `instructor`. If extraction fails, the prompt is adjusted — the schema is not weakened.

---

### 1.7 Natural Language Only — Agents Never Emit Structured Output

#### 1.7.1 Agents write free-form prose; ALL extraction complexity lives in the parser
**This is an absolute, non-negotiable principle.** Agents produce natural language text and nothing else. They do not emit `EFFECT:` markers, JSON, XML, labeled sections, or any structured format. They do not know the parser exists. The parser is a post-processing step that extracts structured effects from genuinely free-form prose.

**Complexity belongs in the parser.** The parser is expected to be very complex — semantic understanding, context awareness, category-conditioned extraction, discriminated unions, field validators, reasking logic. This complexity is deliberate and welcome. What is forbidden is pushing any of this complexity onto the agent by requiring structured output.

**Enforcement:**
* Agent system prompts never mention effect types, `EFFECT:` markers, JSON schemas, or section labels.
* Parser system prompts contain all effect definitions, extraction rules, and validation logic.
* If extraction is hard, the parser system prompt is expanded — the agent prompt is never modified to make parsing easier.
* Phase-based parsing with fast deterministic paths is prohibited; all extraction is semantic via `instructor`.

**Rationale:** Structured output from agents leaks architecture details into agent behavior. It couples agent prompts to parser implementation. It prevents agents from being replaced or upgraded independently. Natural language is the only stable, future-proof interface.

---

### 1.8 Situation-Driven Agent Tasking

Agents query the **Global State Agent** via `GET /` frequently. They receive the complete projection bundle (OTIO, Job, VM, State, Budget) as a Pydantic model. They scan this state and decide what to do. Their system prompt contains situation-type guidance and prioritization rules. Agents do not read the event store directly.

---

### 1.9 pydantic-deep

Agents use **pydantic-deep** (built on `pydantic-ai`). Context compaction is implemented as a **pre-processing step** before `agent.run()`. The agent's `message_history` is compacted by querying the OTIO projection to determine the agent's current task/focus, then calling a compaction LLM that preserves task-relevant details. Token management is handled by the pydantic-deep `ContextManagerCapability`.

> [!NOTE]
> **Why pre-processing, not watcher-side compaction:** Token management is an agent-internal concern. pydantic-deep provides the hook infrastructure via `on_before_compress`; we provide the OTIO-aware compaction logic.

---

### 1.10 Prompt-Only HTTP Interface

No fields from JSON request payloads or custom request headers/query parameters (such as `notification_type` or `context`) may ever be consumed or processed by any agent or platform logic. The HTTP interface serves solely to trigger execution or transmit prompts. All context must be dynamically resolved from the environment/filesystem (e.g., loading the events database).

---

### 1.11 Principles at a Glance

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
| 11 | **Serialized turn execution** | Agent handlers use a global `LoopBoundLock` to serialize turn execution. | §5.6 |
| 12 | **Tick-driven** | Agents are HTTP services; they autonomously poll GSA. EventStoreDB provides native push subscriptions for distributed deployments. No central watcher loop. | **Watcher removed** |
| 13 | **Prompt-only HTTP interface** | No payload fields or query parameters are consumed. HTTP GET/POST serves only to transmit prompts. | **NEW** — ensures pure prompt-driven context |

### 1.12 Strategic Vision and Long-Term Pillars

While the current version (V7.1) focuses on a robust, error-free "happy path" using stable agent loops, the pipeline is designed to scale toward three long-term strategic pillars:

#### 1.12.1 Editorial Flexibility
The user/operator must have the ability to override, adjust, or tweak documentary generation outcomes at any step. Rather than a blind one-way pipeline, future revisions will allow interactive script adjustments, fine-grained visual/audio clip overrides, and custom transition configurations without forcing a full rebuild. The event-log replay model naturally supports this by allowing operator-injected override effects (e.g., `UserOverridePrompt`) to shape subsequent projections.

#### 1.12.2 Audio-Visual Quality at Scale
Visual and audio coherence must meet high aesthetic standards. This involves:
* Precise audio/video duration reconciliation (matching narrated script text to target block timings within strict limits).
* Professional audio production (loudness mixed strictly to -16.0 LUFS with true peak capped at -1.0 dBTP).
* Fluid visual continuity (applying cross-dissolve transitions at scene boundaries to eliminate abrupt black frames).
* Real-time video/audio drift correction to ensure timing errors never accumulate over long runtimes.

#### 1.12.3 Cost Optimization at Scale
Production efficiency is critical. The system must orchestrate fleet infrastructure dynamically, spawning GPU worker instances on-demand (e.g., via Vast.ai spot instance auctions) and immediately deallocating them upon job queue completion. Spot preemptions or boot timeouts are handled gracefully via event re-runs, ensuring robust fault tolerance while keeping total API and infrastructure costs under strict budgets.

---

## 2. System Topology

### 2.1 Architecture Diagram

```
                     Human Operator
                    (instruction, GET state)
                            │
            ┌───────────────┼───────────────┐
            │               │               │
            ▼               ▼               ▼
    ┌──────────────┐ ┌─────────────┐ ┌─────────────┐
    │ Scenario     │ │ Audio       │ │ SQLite      │
    │ Agent 8001   │ │ Agent 8002  │ │ Event Store │
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

**Data flow.** Agents are independent HTTP services. Each agent exposes `GET /` (health/status) and `POST /` (primary endpoint). The **Global State Agent** (port 8000) is the sole component that reads the SQLite event store directly; it maintains all five projections in memory and serves them to other agents via `GET /`. No other component reads the event store. 

Agents query the GSA frequently to receive the complete projection bundle. The agent LLM produces natural language text; the parser (`instructor` + `deepseek-v4-flash`) extracts typed effects from that text. The agent handler appends extracted effects to the SQLite event store — agents are barely aware of this process and never produce structured output or tool calls to write effects. 

The Global State Agent polls DB files and updates its projections. The **Provisioner** (port 8081) is an agent like all others — it reads state from the GSA, reasons about VM provisioning, executes Vast.ai commands via bash tool calls, and its natural language output is parsed for effects (`VMAllocated`, `VMDeallocated`, `JobCompleted`, etc.). VM Workers execute inference and report results back to the Provisioner via HTTP POST. The human operator interacts directly with agent endpoints; there is no intermediary HTTP service.

---

### 2.2 Component Inventory

Every agent exposes exactly `GET /` (health) and `POST /` (primary endpoint) on its own port.

| Component | Port | Type | Endpoints | Effects Produced | Effects Consumed |
|---|---|---|---|---|---|
| **Global State Agent** | 8000 | HTTP service | `GET /` only | — | all effects (from SQLite event store) |
| **Scenario Agent** | 8001 | HTTP agent | `GET /`, `POST /` | `UpdateScript`, `DeleteScene`, `ReorderScenes` | state from GSA |
| **Audio Agent** | 8002 | HTTP agent | `GET /`, `POST /` | `QueueJob`, `JobApproved`, `JobRequeued`, `DurationAdjusted`, `ReconciliationFailed`, `ReconciliationComplete` | state from GSA |
| **Video Agent** | 8003 | HTTP agent | `GET /`, `POST /` | `QueueJob`, `JobApproved`, `JobRequeued`, `MergeIntoOTIO` | state from GSA |
| **Assembly Agent** | 8005 | HTTP agent | `GET /`, `POST /` | `PipelineComplete`, `ProductionFailed` | state from GSA |
| **Provisioner** | 8081 | HTTP agent | `GET /`, `POST /` | `VMAllocated`, `VMDeallocated`, `VMProvisionFailed`, `VMObserved`, `JobCompleted`, `JobFailed`, `JobStarted` | state from GSA |
| **SQLite Event Store** | — | file (per run) | — | — | all effects |
| **Projections (5)** | in-memory | read models | — | — | all effects |
| **VM Workers** | 9000+ | HTTP service | `GET /`, `POST /` | `JobResult` (to Provisioner) | `JobRequest` |

---

### 2.3 HTTP Contract Specification

All HTTP responses in the pipeline use **JSON** (`Content-Type: application/json`). Interactive agent inputs are transmitted as plain text bodies to their `POST /` endpoint.

#### POST / — Agent Wake / Instruction
* **Endpoint:** `POST /` on every agent port (8001–8005, 8081)
* **Content-Type:** `text/plain`
* **Body:** Raw UTF-8 text containing the prompt or directive (e.g. "Wake up and check GSA" or a specific operator instruction)
* **Response:** `AgentResponse` Pydantic model

```python
class AgentResponse(BaseModel):
    """POST / response returned by every agent handler."""
    status: Literal["ok", "error", "halted"] = Field(
        ...,
        description="ok = effects extracted; error = exception caught; halted = ClarificationRequest extracted",
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

| HTTP Status | Condition | Response Body |
|---|---|---|
| `200 OK` | Agent ran successfully, effects extracted and appended | `AgentResponse(status="ok", ...)` |
| `202 Accepted` | Agent is already processing a prior request | `AgentResponse(status="ok", effects_extracted=[])` |
| `400 Bad Request` | Payload failed Pydantic validation | `AgentResponse(status="error", error_message="...")` |
| `500 Internal Server Error` | Unhandled exception in agent handler | `AgentResponse(status="error", error_message="...")` |

---

#### GET / — Health / State Read
* **Endpoint:** `GET /` on every agent port
* **Content-Type:** `application/json`
* **Response:** Varies by component

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
The GSA returns the `GlobalStateResponse` (§2.4.2 / §6.7.6). This is the only component whose `GET /` returns full state rather than just health.

---

### 2.4 Emergent Pipeline Phases

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
| **ABORTED** | `PipelineAborted` effect exists in store | None |

#### 2.4.1 Back-edges
* **`gap_unexpected`**: Narration scene count ≠ scene list. Triggers back-edge from `AUDIO_RECONCILE` or later back to `SCRIPT`.
* **`voice_mismatch`**: Final audio speaker ≠ scenario voice tag. Triggers back-edge from `VIDEO_PRODUCTION` back to `SCRIPT`.

---

### 2.5 Global State Agent (GSA)

The **Global State Agent** (GSA, port 8000) is the **sole read path** between the SQLite event store and all other agents. It polls DB files, maintains all five projections in memory, and serves them via `GET /`.

#### 2.5.1 GSA Invariants:
1. **GET / only:** The GSA exposes exactly one endpoint: `GET /`. It does not accept `POST /`.
2. **Read-only from agent perspective:** Agents treat the GSA as a state cache.
3. **The SQLite event store is the GSA's only input:** The GSA polls SQLite file changes and rebuilds projections.
4. **Ephemeral, no checkpointing:** The GSA holds no persistent state. It replays the event log from sequence 0 on every restart.
5. **No exceptions:** The Provisioner (port 8081) reads state from the GSA via `GET /` like all other agents.

---

## 3. Discarded Propositions and Rationale

This section records design alternatives that were considered and rejected during V7.1 development.

### 3.1 Discarded: Custom HTTP Services as "Deterministic Agents"
* **Proposition:** Build lightweight HTTP services for each pipeline step that execute authored Python scripts directly.
* **Why discarded:** Too rigid; loses agentic adaptability; violates "prompt-based rules" principle.
* **What we kept instead:** Real `pydantic-deep` agents with `bash_command` as their only tool.

### 3.2 Discarded: V6 SQLite Event Store with Writer Loop
* **Proposition:** Continue using the V6 SQLite event store with `BEGIN IMMEDIATE` locking and a background `_writer_loop` thread.
* **Why discarded:** Custom writer loops introduce operational complexity and single-point bottlenecks.
* **What we kept instead:** SQLite file store with direct synchronous `EventStore` interface designed for future ESDB swap.

### 3.3 Discarded: Regex + Marker-Based Parser
* **Proposition:** Use regex patterns (e.g. `EFFECT:QueueJob`) or XML/JSON markers in LLM output.
* **Why discarded:** Fragile; agent output quality degrades when forced to generate syntax.
* **What we kept instead:** Pure semantic `instructor` extraction from free-form text.

### 3.4 Discarded: Custom Causal Logging System
* **Proposition:** Build a custom causal logging framework tracking execution DAGs.
* **Why discarded:** Duplicates tested capability provided by `ProvenanceCapability` in `pydantic-ai-provenance`.
* **What we kept instead:** Out-of-the-box `ProvenanceCapability` integration.

### 3.5 Discarded: EventStoreDB on macOS
* **Proposition:** Run EventStoreDB via Docker on macOS for local development.
* **Why discarded:** No native macOS binary exists; Colima VM adds dev friction.
* **What we kept instead:** SQLite locally; ESDB protocol interface ready for distributed Linux deploy.

### 3.6 Discarded: Timeout-Based Guardrails
* **Proposition:** Add timeouts to agent turns, HTTP requests, and async operations.
* **Why discarded:** Timeouts cause silent failures and state inconsistencies. Handled instead by operator oversight and loop-bound lock serialization.
* **What we kept instead:** No timeouts anywhere on primary path.

### 3.7 Discarded: Mock-Based Testing
* **Proposition:** Use mocks for LLM and event store in unit tests.
* **Why discarded:** Creates fantasy behavior diverging from production reality.
* **What we kept instead:** Integration tests running real agents against real endpoints with cheap models.

### 3.8 Discarded: Environment Variable Configuration
* **Proposition:** Use `.env` files and `os.environ` for API keys and endpoint URLs.
* **Why discarded:** Environment variables are invisible state that changes between runs.
* **What we kept instead:** Explicit Pydantic `Config` objects passed down programmatically.