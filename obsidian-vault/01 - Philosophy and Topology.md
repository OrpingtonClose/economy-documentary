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

#### Event Log is the sole source of truth of global state
⚡ All global state must be derived by folding over the SQLite events.db event log; no direct database or projection updates from any agent or process
⚡ Agents must hold no local session state, process-level memory, or in-memory caches between turns; each turn must rebuild context from the GSA projection bundle

All global pipeline state must be derived passively by folding over the SQLite `events.db` event log. Direct updates to databases or projections from agents are strictly banned. Replay from sequence `0` reconstructs the entire state exactly. The event store is the only persistent storage; all other state projections are ephemeral. EventStoreDB streams are the future scalability path. Agents hold no session state and VM workers are ephemeral.

---

### 1.2 Effects as Only Legal Mutations

#### Mutations of global state only via typed Effect models appended to EventStore
⚡ Every state mutation must enter through a typed Pydantic Effect model appended to the EventStore; direct mutation of projections is strictly prohibited

All mutations of the global pipeline state must enter through SQLite event store appends. Direct state mutation of projections is prohibited. Projections are read-only consumers. Every mutation must be represented by a typed Pydantic model (`Effect`) containing metadata for validation, tracking, and idempotency (UUIDv7), extracted from agent prose using a category-conditioned semantic parser. Invalid payloads are rejected before reaching the event store.

---

### 1.3 No State Machine — Prompt-Based Rules

#### Prompt-driven state execution (No state machines)
⚡ The pipeline must contain no hardcoded state machine, transition table, or phase-enforcing switch statement; all prioritization lives in agent system prompts

The pipeline has no hardcoded state machine. Prioritization, control rules, and action selection must be governed inside the agent's system prompt rather than hardcoded in the execution code. Prioritization, filtering, and response selection occur inside the agent via prompt instructions.

> [!TIP]
> **Core Principle:** Whenever something can be done via prompt, do so — cut code complexity.

---

### 1.4 No Timeouts in Code

#### Time-based timeouts are strictly forbidden across all execution and test code
⚡ Time-based timeouts are strictly forbidden in all execution code, test code, HTTP requests, and subprocess launches; operations must run to completion or wait indefinitely
⚡ Test runners and test harnesses are not exempt from the rule of NO-TIMEOUT; they must also avoid hardcoded timeouts and wait loops
⚡ Shell subprocesses such as ffmpeg or vastai must never be launched with timeout arguments; they must execute asynchronously and be observed externally

All processes, tests, loop checks, agent tasks, HTTP queries (including lightweight GET health/readiness check queries), wakepost triggers, LLM inference, and test suites must never utilize time-based timeouts. They must run to completion or wait indefinitely. Crucially, shell subprocesses (such as `ffmpeg` or `vastai` operations) must never be launched with timeout limits; they must be executed asynchronously and observed for completion. Hang detection, resource unreachability, and execution delays are observed and reacted to dynamically by the helper LLM agent operating from outside the pipeline, usually connected to a human operator directly. Test runners and test harnesses are not exempt from the rule of NO-TIMEOUT.


---

### 1.5 Real Engines Only

#### Production execution paths must not use mock implementations
⚡ Production execution paths must not use mock implementations, facades, stubs, or simulated worker endpoints; mocks are reserved for offline unit tests only
⚡ Production media generation must use real engines only: Qwen3-TTS for audio, LTX-2.3 for video, and DeepSeek API for agent inference

Mocks, facades, and simulated worker endpoints are strictly forbidden in production runs. All VM provisioning, audio generation, and video generation steps must perform genuine system calls or API queries. Mocks are reserved exclusively for the offline test suites. TTS uses **Qwen3-TTS** on GPU VMs. Video uses **LTX-2.3**. Agent LLM inference uses **DeepSeek API** (`deepseek-v4-flash`). Unavailable engines trigger `ClarificationRequest`.

---

### 1.6 Never Regex

#### Category-conditioned extraction via instructor (Never Regex)
⚡ No regular expressions may be used to extract structured data from agent LLM outputs; extraction must use instructor with category-conditioned semantic parsing

No regular expressions may be used to extract structured data from agent outputs. The parser must use the agent's current role to determine valid `Effect` subtypes and constrain the LLM to schema-compliant JSON via `instructor` + `deepseek-v4-flash`. If extraction fails, the prompt is adjusted — the schema is not weakened.

---

### 1.7 Natural Language Only — Agents Never Emit Structured Output

#### Production agents must communicate strictly in PlainTextResponse
⚡ Agents must communicate strictly in natural language plain text; no JSON, XML, EFFECT: markers, section labels, or structured output formats
⚡ Agent system prompts must never mention effect types, JSON schemas, parsing instructions, or the existence of the semantic parser
⚡ All complexity for structured data extraction must live in the semantic parser; agent prompts must never be modified to make parsing easier

Production agents and HTTP endpoints are strictly prohibited from exchanging or exposing structured JSON payloads, key-value metadata strings (such as `ltx=yes`, `tts=yes`), or accepting JSON content headers for core agent state checks. All communication between agents must flow as conversational, natural-language plain text responses. The only exception is the GSA endpoint which exposes projections for fold functions. Agents produce natural language text and nothing else. They do not emit `EFFECT:` markers, JSON, XML, labeled sections, or any structured format. They do not know the parser exists. The parser is a post-processing step that extracts structured effects from genuinely free-form prose.

**Complexity belongs in the parser.** The parser is expected to be very complex — semantic understanding, context awareness, category-conditioned extraction, discriminated unions, field validators, reasking logic. This complexity is deliberate and welcome. What is forbidden is pushing any of this complexity onto the agent by requiring structured output.

**Enforcement:**
* Agent system prompts never mention effect types, `EFFECT:` markers, JSON schemas, or section labels.
* Parser system prompts contain all effect definitions, extraction rules, and validation logic.
* If extraction is hard, the parser system prompt is expanded — the agent prompt is never modified to make parsing easier.
* Phase-based parsing with fast deterministic paths is prohibited; all extraction is semantic via `instructor`.

**Rationale:** Structured output from agents leaks architecture details into agent behavior. It couples agent prompts to parser implementation. It prevents agents from being replaced or upgraded independently. Natural language is the only stable, future-proof interface.

---

### 1.8 Situation-Driven Agent Tasking

#### Situation-driven agent tasking via GSA
⚡ The Global State Agent on port 8000 is the sole component permitted to read the SQLite event store; all other agents must read state exclusively via GET / from the GSA

Agents must never read the SQLite database directly. They query the read-only GSA status endpoint to receive the folded projection bundle, scanning this state to determine their next action based on instructions in their system prompts.

---

### 1.10 Prompt-Only Narrative HTTP Interface

The HTTP boundary uses natural-language narrative text for all inputs and outputs. No JSON is consumed or returned in production. 

The HTTP contract is split into three specific behaviors:
1. **GET (`GET /` or `GET /{prompt}`)**: Always available, read-only status query. Root `/` calls return a conversational description of the agent's busy/idle status and current task. Does not pollute the database.
2. **POST (`POST /` or `POST /{prompt}`)**: Non-interrupting standard/scheduled execution run. If the agent is already busy executing a turn, it immediately returns `409 Conflict` (agent busy) instead of interrupting it. Otherwise, it starts the turn in the background and returns a conversational confirmation message.
3. **PUT (`PUT /` or `PUT /{prompt}`)**: Interrupting external operator intervention (electric bolt to the system). If the agent is busy, it immediately cancels the running task, terminates active subprocesses (ssh/curl), and executes the new human prompt in the background, returning `204 No Content` with an empty response body.

---

### 1.11 Principles at a Glance

| # | Principle | Enforcement | V6→V7 Change |
|---|---|---|---|
| 1 | **Event log is sole source of truth** | All state derived from events. No hidden state. No projection writes independently. | SQLite replaces SQLite; ESDB for distributed |
| 2 | **Effects are only legal mutations** | Only Pydantic models enter event store. Parser validates against `EffectUnion`. | None |
| 3 | **No state machine — prompt-based rules** | Prioritization lives in agent system prompts. Agents scan projections and decide. | None |
| 4 | **No timeouts in code** | No `setTimeout`, `threading.Timer`, `asyncio.timeout`, or self-destruct logic in pipeline code. No timeout effects. Operator intervenes on hung jobs. Test runners and harnesses are not exempt. | None |

| 5 | **Real engines only** | Qwen3-TTS, LTX-2.3, DeepSeek API. No mocks, no stubs, no simulation. | None |
| 6 | **Never regex** | Category-conditioned extraction via `instructor` + `deepseek-v4-flash`. | None |
| 7 | **Natural language only** | Agents write free-form prose. No structured output, no markers, no JSON, no section labels. ALL extraction complexity lives in the semantic parser. | **NEW** — eliminates Phase 1/2 fast paths |
| 8 | **Provisioner is an agent** | LLM agent with bash_command as its only tool. Provisions VMs, dispatches jobs, learns from failures. Most intelligence-requiring component. | Agent with tool use; reads GSA like all agents |
| 9 | **Agent memory does not persist in process** | Each turn rebuilt from projection summaries + bounded message history (last 5 turns). No session state in the agent process between POSTs. | None |
| 10 | **No automatic stale-state detection** | Operator monitors via `GET /` on agents and intervenes manually. No VM-side timers. | Removed TimeoutObserved; operator owns intervention |
| 11 | **Serialized turn execution** | Agent handlers use a global `LoopBoundLock` to serialize turn execution. | §5.6 |
| 12 | **Tick-driven** | Agents are HTTP services; they autonomously poll GSA. EventStoreDB provides native push subscriptions for distributed deployments. No central watcher loop. | **Watcher removed** |
| 13 | **Prompt-only HTTP interface** | Plain-text narrative HTTP GET (status), POST (scheduled non-interrupting runs), and PUT (interrupting interventions). | **NEW** — ensures pure prompt-driven context |

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

### 2.2 Component Inventory

Every agent exposes exactly `GET /` (status), `POST /` (scheduled run), and `PUT /` (operator intervention) on its own port, supporting arbitrary prompt paths (e.g. `GET /hello there`).

| Component | Port | Type | Endpoints | Effects Produced | Effects Consumed |
|---|---|---|---|---|---|
| **Global State Agent** | 8000 | HTTP service | `GET /` | — | all effects (from SQLite event store) |
| **Scenario Agent** | 8001 | HTTP agent | `GET /`, `POST /`, `PUT /` | `UpdateScript`, `DeleteScene`, `ReorderScenes` | state from GSA |
| **Audio Agent** | 8002 | HTTP agent | `GET /`, `POST /`, `PUT /` | `QueueJob`, `JobApproved`, `JobRequeued`, `DurationAdjusted`, `ReconciliationFailed`, `ReconciliationComplete` | state from GSA |
| **Video Agent** | 8003 | HTTP agent | `GET /`, `POST /`, `PUT /` | `QueueJob`, `JobApproved`, `JobRequeued`, `MergeIntoOTIO` | state from GSA |
| **Assembly Agent** | 8005 | HTTP agent | `GET /`, `POST /`, `PUT /` | `PipelineComplete`, `ProductionFailed` | state from GSA |
| **Provisioner** | 8081 | HTTP agent | `GET /`, `POST /`, `PUT /` | `VMAllocated`, `VMDeallocated`, `VMProvisionFailed`, `VMObserved`, `JobCompleted`, `JobFailed`, `JobStarted` | state from GSA |
| **SQLite Event Store** | — | file (per run) | — | — | all effects |
| **Projections (5)** | in-memory | read models | — | — | all effects |
| **VM Workers** | 9000+ | HTTP service | `GET /`, `POST /` | `JobResult` (to Provisioner) | `JobRequest` |

---

### 2.3 HTTP Contract Specification

#### Pipeline state and agent actions must be controlled strictly via HTTP endpoints
⚡ All production HTTP endpoints must use Content-Type text/plain with natural language narrative text; JSON is forbidden at HTTP boundaries except for the GSA internal projection endpoint

Direct manipulation of files or databases, running independent shell scripts, or mutably bypassing control endpoints is strictly prohibited. All execution, monitoring, and human intervention must flow through the ASGI HTTP endpoints (GET, POST, PUT). Silent process restarts are banned. All HTTP inputs and responses in the production pipeline use **plain narrative text** (`Content-Type: text/plain`). JSON has been completely eliminated from the external communication boundaries of production components, and endpoints do not support structured output formats.

#### Non-blocking GET queries and health status
⚡ GET / must return conversational status text immediately without blocking on a running turn

The GET endpoint (`GET /` and `GET /{prompt:path}`) is non-blocking. It queries the GSA to describe the current focus task in a plain-text conversational format. If no prompt is passed, it returns a conversational description of the agent's busy/idle status.

#### POST queries for scheduled executions
⚡ POST / must return 409 Conflict immediately if the agent is busy; it must not interrupt, queue, or block until idle

The POST endpoint (`POST /` and `POST /{prompt:path}`) triggers a conversational turn. If the agent is already busy executing a turn, it must return a `409 Conflict` (or plain text indicating busy) immediately instead of blocking or interrupting. Otherwise, it executes the turn and returns the monologue. Custom instruction text is appended to the event store as a `HumanInstruction` event.

#### PUT queries for interrupting operator intervention
⚡ PUT / must cancel the active asyncio task and any spawned subprocesses immediately, then schedule the new turn in the background and return 204 No Content with no body

The PUT endpoint (`PUT /` and `PUT /{prompt:path}`) acts as an operator intervention tool. It cancels any active turn task and spawned subprocesses immediately, schedules a new turn in the background, and returns `204 No Content` immediately. Custom instruction text is appended to the event store as a `HumanInstruction` event.

---

#### VM Port Mapping and State Consistency Guardrails

To prevent routing collisions and maintain GSA state projection accuracy, the pipeline enforces three architectural rules:

#### Unique HTTPS endpoints per VM (Endpoint Overlap Guard)
⚡ VM workers must be provisioned with distinct HTTPS URLs and ports; concurrent VMs must never share a hostname, port, or endpoint

Active VM workers must be provisioned with distinct local tunnel ports. Multiple concurrent VMs (e.g., TTS and LTX) are strictly forbidden from sharing `localhost:8888`. Sharing endpoints causes job routing mixups where video requests are sent to audio workers and vice versa.

#### Re-queued blocks must transition to dirty in read models
⚡ Re-queuing a new job for a block that was previously marked clean must instantly move that block back to dirty_blocks and remove it from clean_blocks

If a block has a completed job (and was therefore marked clean), queueing a new job ID for that block must instantly transition the block back to the `dirty_blocks` set and discard it from `clean_blocks` in the GSA read models. This prevents GSA from reporting a block as "clean" when work is pending.

#### No unreachable ghost VMs allowed
⚡ Active VMs must have a confirmed healthy worker_url; any VM without a reachable endpoint during its bootstrap grace period is a ghost VM and must be destroyed

VMs in an `active` status must have a confirmed, healthy `worker_url`. If a VM remains `unknown` or fails to bind its port during its bootstrap grace period, it is treated as a ghost VM and must be destroyed/reallocated.

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

#### Unexpected gap back-edge trigger
If narration scene count does not match the scene list, a `gap_unexpected` condition must trigger a back-edge from `AUDIO_RECONCILE` or later phases back to `SCRIPT`.

#### Voice mismatch back-edge trigger
If the final audio speaker role does not match the scenario voice tag, a `voice_mismatch` condition must trigger a back-edge from `VIDEO_PRODUCTION` back to `SCRIPT`.

---

### 2.5 Global State Agent (GSA)

The **Global State Agent** (GSA, port 8000) is the **sole read path** between the SQLite event store and all other agents. It polls DB files, maintains all five projections in memory, and serves them via `GET /`.

#### GSA GET / only
⚡ The GSA must not persist folded projection states on disk; on restart it must replay the event log from sequence 0 to reconstruct all projections

The GSA exposes exactly one endpoint: `GET /`. It does not accept `POST /` or `PUT /` requests.

#### Read-only GSA status from agent perspective
Agents must treat the GSA as a read-only state cache. Direct writes to GSA state or projections are prohibited.

#### SQLite event store is the GSA's only input
The GSA must derive its state solely by polling the SQLite events database (`events.db`) and rebuilding projections in-memory.

#### Ephemeral GSA state with no checkpointing
The GSA must not persist folded projection states on disk. On restart, the GSA must replay the event log from sequence 0 to reconstruct projections.

#### Consistent GSA interface across all components
All components, including the deterministic Provisioner, must read system state from the GSA via `GET /` using the same HTTP interface.

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

---

## 4. Style

### 4.1 Transitions Banned
* **No transitions allowed**: The pipeline does not support visual or audio transition effects (e.g., cross-dissolves, fade-ins, fade-outs, or audio cross-fades) between clips. All scene transitions must be compiled as simple, clean cuts. Under-the-hood transitions introduce severe layout math and overlapping timespan complexities, and are strictly disabled.

### 4.2 Quality-Destroying Timing Tricks Banned
* **No stretching or shrinking**: Audio and video clips must never be stretched or shrunk using digital speed-adjustment filters (e.g. `atempo` or frame-dropping speed filters).
* **No looping or reusing media**: Background music, visuals, or narration clips must not be looped or duplicated to fill time mismatches.
* **No gap media padding**: Empty gap media placeholders must not be inserted between sequential narrative segments to pad duration.
* **Preserving native media quality**: All duration discrepancies must be resolved by dynamically shifting timeline offsets (cascading offsets via `CoordinateTimeline`) and generating new clips matching the actual measured durations. Quality-destroying tricks are strictly prohibited.