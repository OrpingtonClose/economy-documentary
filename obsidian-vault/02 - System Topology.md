---
{
  "title": "System Topology",
  "section": "2",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[01 - Core Philosophy|Core Philosophy]] | [[00 - Index|Index]] | [[03 - Effect Type Family Complete Schemas|Effect Type Family — Complete Schemas]] ->

# System Topology


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

**Data flow.** Agents are independent HTTP services. Each agent exposes `GET /` (health/status) and `POST /` (primary endpoint). The **Global State Agent** (port 8000) is the sole component that reads the SQLite event store directly; it maintains all five projections in memory and serves them to other agents via `GET /`. No other component reads the event store. Agents query the GSA frequently to receive the complete projection bundle. The agent LLM produces natural language text; the parser (instructor + deepseek-v4-flash) extracts typed effects from that text. The agent handler appends extracted effects to the SQLite event store — agents are barely aware of this process and never produce structured output or tool calls to write effects. The Global State Agent polls DB files and updates its projections. The **Provisioner** (port 8081) is an agent like all others — it reads state from the GSA, reasons about VM provisioning, executes Vast.ai commands via bash tool calls, and its natural language output is parsed for effects (`VMAllocated`, `VMDeallocated`, `JobCompleted`, etc.) like any agent. VM Workers execute inference and report results back to the Provisioner via HTTP POST. The human operator interacts directly with agent endpoints; there is no intermediary HTTP service.

**V7 delta.** The watcher has been removed. Agents are HTTP services, not in-process objects. SQLite event store replaces the custom SQLite event store. The **Global State Agent** is introduced as the read-only middleman between the event store and agents. EventStoreDB is the future scalability path for distributed deployments. The Provisioner is an agent with bash_command as its only tool — it reasons about VM provisioning and learns from failures across runs.

---

### 2.2 Component Inventory

#### 2.2.1 Component table

Every agent exposes exactly `GET /` (health) and `POST /` (primary endpoint) on its own port.

| Component | Port | Type | Endpoints | Effects Produced | Effects Consumed |
|---|---|---|---|---|---|
| Global State Agent | 8000 | HTTP service | `GET /` only | — | all effects (from SQLite event store) |
| Scenario Agent | 8001 | HTTP agent | `GET /`, `POST /` | `UpdateScript`, `DeleteScene`, `ReorderScenes` | state from GSA |
| Audio Agent | 8002 | HTTP agent | `GET /`, `POST /` | `QueueJob`, `JobApproved`, `JobRequeued`, `DurationAdjusted`, `ReconciliationFailed`, `ReconciliationComplete` | state from GSA |
| Video Agent | 8003 | HTTP agent | `GET /`, `POST /` | `QueueJob`, `JobApproved`, `JobRequeued`, `MergeIntoOTIO` | state from GSA |
| Assembly Agent | 8005 | HTTP agent | `GET /`, `POST /` | `PipelineComplete`, `ProductionFailed` | state from GSA |
| Provisioner | 8081 | HTTP agent | `GET /`, `POST /` | `VMAllocated`, `VMDeallocated`, `VMProvisionFailed`, `VMObserved`, `JobCompleted`, `JobFailed`, `JobStarted` | state from GSA |
| SQLite Event Store | — | file (per run) | — | — | all effects |
| Projections (5) | in-memory | read models | — | — | all effects |
| VM Workers | 9000+ | HTTP service | `GET /`, `POST /` | `JobResult` (to Provisioner) | `JobRequest` |

**V7 delta.** The watcher is removed. The **Global State Agent** (port 8000) is introduced as the read-only projection server — the sole component that reads the SQLite event store directly. All other agents consume state via `GET /` to the GSA, including the Provisioner. The Provisioner is an HTTP agent (port 8081), not a deterministic service. SQLite replaces the SQLite event store. EventStoreDB (port 2113) is the future scalability path for distributed deployments. Agents are HTTP services, not in-process objects.

---

### 2.2.2 HTTP Contract Specification

All HTTP responses in the pipeline use **JSON** (`Content-Type: application/json`). Interactive agent inputs are transmitted as plain text bodies to their `POST /` endpoint.

#### POST / — Agent Wake / Instruction

**Endpoint:** `POST /` on every agent port (8001–8005, 8081)  
**Content-Type:** `text/plain`  

**Body:** Raw UTF-8 text containing the prompt or directive (e.g. "Wake up and check GSA" or a specific operator instruction)  
**Response:** `AgentResponse` Pydantic model  

```python
class AgentResponse(BaseModel):
    """POST / response returned by every agent handler."""
    status: Literal["ok", "error", "halted"] = Field(
        ...,
        description="ok = effects extracted and appended; error = exception caught; halted = ClarificationRequest extracted by parser",
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
| `202 Accepted` | Agent received request but is already processing a prior request | `AgentResponse(status="ok", effects_extracted=[])` |
| `400 Bad Request` | Payload failed Pydantic validation | `AgentResponse(status="error", error_message="...")` |
| `500 Internal Server Error` | Unhandled exception in agent handler | `AgentResponse(status="error", error_message="...")` |

**Idempotency.** If an agent receives two identical instruction POSTs, it processes both (the agent may produce text from which the parser extracts duplicate `NoOp` effects, which are harmless). True idempotency is enforced at the event store via `effect_id` (§3.1.2).

---

#### GET / — Health / State Read

**Endpoint:** `GET /` on every agent port  
**Content-Type:** `application/json`  
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
GET http://localhost:8000/
Content-Type: application/json

→ GlobalStateResponse (§6.7.6)
```

**Status codes:**

| HTTP Status | Condition | Response Body |
|---|---|---|
| `200 OK` | Agent healthy and responsive | `AgentHealthResponse` or `GlobalStateResponse` |
| `503 Service Unavailable` | Agent is starting up or recovering from crash | `{"status": "error", "agent": "...", "last_error": "..."}` |

---

#### Error Response Schema

All error responses follow the same schema, regardless of which component produces them:

```python
class PipelineErrorResponse(BaseModel):
    """Standard error response from any pipeline component."""
    error: str = Field(..., description="Error category: validation, runtime, timeout, network, unknown")
    message: str = Field(..., description="Human-readable error message")
    component: str = Field(..., description="Which component produced the error")
    timestamp: float = Field(default_factory=time.time)
```

**Example error response:**

```json
{
  "error": "validation",
  "message": "Method Not Allowed: Only GET and POST permitted",
  "component": "scenario_agent",
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
| **ABORTED** | `PipelineAborted` effect exists in store | None |

#### 2.3.2 Back-edges

| Back-Edge | Trigger | From | To | Handler |
|---|---|---|---|---|
| `gap_unexpected` | Narration scene count ≠ scene list | `AUDIO_RECONCILE` or later | `SCRIPT` | Scenario Agent rewrites script |
| `voice_mismatch` | Final audio speaker ≠ scenario voice tag | `VIDEO_PRODUCTION` | `SCRIPT` | Scenario Agent fixes voice tag |

Back-edges are triggered when the parser extracts `ProductionFailed` from an agent's output with `failure_type` in `{gap_unexpected, voice_mismatch}`. The Scenario Agent receives the failure context on its next turn; the parser extracts `UpdateScript` from its output to fix the problem. Prior effects remain immutable in the Event Store. Downstream Projectors rebuild read models from the full log. This makes recovery a new forward path, not a mutation.

### 2.4 Global State Agent

#### 2.4.1 Purpose and invariants

The **Global State Agent** (GSA, port 8000) is the **sole read path** between the SQLite event store and all other agents. It polls DB files, maintains all five projections in memory, and serves them via `GET /`. No other component reads the event store directly.

**Invariants:**
- **GET / only.** The GSA exposes exactly one endpoint: `GET /`. It does not accept `POST /`. No agent can instruct it, influence it, or mutate its state.
- **Read-only from agent perspective.** Agents treat the GSA as a state cache. They `GET /` to receive the current projection bundle.
- **The SQLite event store is the GSA's only input.** The GSA polls SQLite file changes and rebuilds projections. It does not accept effects from agents directly.
- **Ephemeral, no checkpointing.** The GSA holds no persistent state. It replays the event log from sequence 0 on every restart. For documentary runs (500–2000 events), this takes milliseconds. No disk checkpoints, no stale-cache risk (§5.5).

#### 2.4.2 GET / response format

```python
class GlobalStateResponse(BaseModel):
    """Response from GET / on the Global State Agent."""
    timestamp: float
    otio: OTIOResponse        # §6.7.1 — serializable slot state
    jobs: JobResponse         # §6.7.2 — serializable job state
    vms: VMResponse           # §6.7.3 — serializable VM state
    state: StateResponse      # §6.7.4 — serializable phase state
    budget: BudgetResponse    # §6.7.5 — serializable budget state
    latest_sequence: int      # highest event sequence number included
    # V7.1 note: Also present on StateResponse (§6.7.4) for per-projection
    # sequence tracking. GlobalStateResponse carries the authoritative value.
```

Agents call `GET /` to receive the full state bundle for their run. The GSA rebuilds projections from the SQLite event store on every request (or serves from its in-memory cache if no new events have been appended).

#### 2.4.3 Why isolate the read path

- **Single source of truth for state.** Every agent sees the same projections because every agent reads from the same GSA. No drift from agents reading the event store at different positions.
- **Simplified agent code.** Agents do not need event-store client logic, replay logic, or projection fold code. They receive ready-made Pydantic models.
- **Operator visibility.** The human operator can `GET /` on the GSA to see the complete pipeline state at a glance, without understanding event store internals.
- **Performance.** The GSA can cache projection state and serve it cheaply. SQLite file reads are concentrated in one process.

#### 2.4.4 No exceptions — only GSA reads the event store

There are **no exceptions**. The **Provisioner** (port 8081) reads state from the GSA via `GET /` like all other agents. It does not read the SQLite event store directly. The GSA is the sole read path for every agent. The Provisioner queries the GSA frequently to receive `Jobs` and `VMs`, then reasons about what VMs to provision, what jobs to dispatch, and what to destroy.

#### 2.4.5 System Invariants (crisp)

| # | Invariant | Meaning |
|---|---|---|
| 1 | **Only GSA reads the store** | The Global State Agent (port 8000) is the sole component that reads the SQLite event store. No agent, no Provisioner, no worker reads it directly. |
| 2 | **No agent writes the store** | Agents reason and talk in natural language. The handler appends extracted effects to the SQLite event store after the parser processes agent text. Agents are barely aware of this process. |
| 3 | **All agents read GSA frequently** | Every agent (including Provisioner) queries the GSA via `GET /` to receive projections. Not necessarily on every turn, but frequently enough to act on current state. |
| 4 | **Only `GET /` and `POST /` everywhere** | Every HTTP surface exposes exactly these two endpoints. No `/health`, `/status`, `/data`, or structured sub-endpoints. |
| 5 | **Only agents have LLM** | Every LLM in the pipeline lives inside an agent (Scenario, Audio, Video, Assembly, Provisioner, Maintainer). No exceptions. VM Worker uses deterministic checks (ffprobe, file size) — no LLM. |
| 6 | **Provisioner is an agent** | The Provisioner (port 8081) is the most intelligence-requiring component. It uses bash_command as its only tool. Its natural language output is parsed for effects like any agent. It is not deterministic code. |

---

