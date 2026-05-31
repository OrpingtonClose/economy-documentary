> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Abstract Architecture V3 — Documentary Pipeline

> Revised 2026-05-17. Tightened. Every BLOCKER from V2 now has a defined resolution.
> All endpoints are GET / or POST / only. No paths. Each service runs on its own port.
>
> Research sources: Perplexity (instructor internals, event-sourcing patterns, VM credential provisioning, StateFlow FSM prompt construction, bash-only agent architectures).

---

## 0. What Changed From V2

| V2 Problem | V3 Resolution |
|---|---|
| Effect schema undefined | §2.5 — Full schemas with 12 abstract kinds |
| Effect parser paradox (no schema in prompt + no regex + reliable extraction) | §2.4 — Category-conditioned generation: agent knows abstract `kind`, parser maps to Pydantic |
| State summary format undefined | §2.9 — Explicit template with 7 sections, 3 verbosity levels |
| State machine contradictory descriptions | §4 — 6 states, transition effects in event log, PROVISIONING is an activity not a state |
| Orchestrator "read-only" but triggers transitions | §2.1 — Orchestrator produces `TransitionState` effects; transitions are events |
| VM ingestion endpoint undefined | §2.3 — VM POSTs to Provisioner agent's POST /; Provisioner parses and produces effects |
| VM agent "is an LLM" unspecified how | §2.3 — VM agent calls deepseek-v4-flash via API; GPU runs inference only |
| VM self-destruct credentials missing | §7.7 — Short-lived JWT (30 min, upload-only) passed via on-start |
| Event store atomicity under parallel | §2.6 — Single writer actor with queue |
| O(n²) projection rebuild | §2.7 — Incremental checkpoints with snapshots |
| Agent HTTP vs direct API ambiguity | §5 — Per-agent specification table |
| No timeout but VM costs unbounded | §7.2 — Budget ceiling changes priorities, not capability |
| Exception may route to wrong agent | §3.3 — Exception in state summary; orchestrator defaults to same agent |
| Kill switch undefined | §3.4 — `AbortPipeline` effect; pipeline traps and destroys VMs |
| Human notification channel undefined | §3.5 — stderr + agent GET / returns stuck state |
| Agent persona undefined | §6.1 — Full persona template with T/M/D/R/W |
| Skills loading undefined | §6.3 — Files in `skills/` directory |
| B2 credential flow undefined | §7.7 — JWT with B2 claims; VM exchanges for pre-signed URL |
| max_retries undefined | §2.4 — `max_retries=2` for parser; infinite for agent turns |
| thread_id undefined | §5.1 — UUID per pipeline run |
| overseer undefined | §2.3 — Pipeline health service on fixed port; VM curls every 60s |
| ScriptApproved undefined | §2.5 — Renamed to `TransitionState` |

---

## 1. System Topology

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              HUMAN OPERATOR                              │
│  GET / on any agent to observe. POST / to any agent to correct.         │
│  No dashboard. No approval UI. No fleet viewer. Plain text only.        │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
           ┌────────────────────────┼────────────────────────┐
           │                        │                        │
           ▼                        ▼                        ▼
┌───────────────┐        ┌───────────────┐        ┌───────────────────────┐
│  ORCHESTRATOR  │───────▶│   AGENTS      │◀──────│   EVENT STORE (JSONL)  │
│   (decides)    │        │  (6 types)    │       │   Single Writer Actor  │
└───────────────┘        └───────────────┘       └───────────────────────┘
      │                          │                          ▲
      │ produces effects         │ produces effects         │
      ▼                          ▼                          │
┌─────────────────────────────────────────────────────────┐
│                    EFFECT PARSER                         │
│   Category-conditioned extraction via instructor         │
│   Agent knows abstract kind; parser maps to Pydantic     │
└─────────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────────┐
│              PROJECTION HANDLERS                         │
│   Incremental: each tracks last_processed_seq            │
│   Snapshots every 100 events or graceful stop            │
└─────────────────────────────────────────────────────────┘
      │
      ├──────────────┬──────────────┬──────────────┐
      ▼              ▼              ▼              ▼
┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐
│  OTIO   │  │  JOBS   │  │   VMs   │  │  STATE  │
│ SERVICE │  │ SERVICE │  │ SERVICE │  │ SERVICE │
│ GET /   │  │ GET /   │  │ GET /   │  │ GET /   │
└─────────┘  └─────────┘  └─────────┘  └─────────┘
      │
      ▼
┌─────────────────────────────────────────────────────────┐
│                 VM WORKERS (ephemeral)                   │
│   GPU runs TTS/LTX. VM agent calls LLM API.             │
│   JWT for B2. Self-destruct on heartbeat loss.          │
└─────────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────────┐
│              ARTIFACT STORE (Backblaze B2)               │
│   Pre-signed upload URLs. Ground truth for media.       │
└─────────────────────────────────────────────────────────┘
```

**Endpoint Rule:** Every box in the topology exposes exactly `GET /` and `POST /` on its own port. No other paths.

---

## 2. Components

### 2.1 Orchestrator

- **Input:** State summary (§2.9) + current state machine state (from State Service)
- **Output:** Decision — which agent runs next + reason
- **Side effects:** Produces `TransitionState` effects (§2.5). These are events like any other.
- **Hard rule:** The orchestrator never runs bash. It only decides and emits effects.
- **Hard rule:** The orchestrator is the ONLY component that emits `TransitionState` effects.

**Why this resolves the V2 contradiction:** In V2, the orchestrator was "read-only" but triggered transitions. In V3, transitions are effects appended to the event log. The orchestrator proposes a transition; the event store records it; projections rebuild; the orchestrator's next decision is based on the rebuilt state.

---

### 2.2 Agents (6 Types)

| Agent | Protocol | Responsibility | Effects Produced |
|---|---|---|---|
| **Scenario** | Direct API | Writes narration text. | `UpdateScript` |
| **Audio** | Direct API | Formulates TTS jobs. QA's completed audio. | `QueueJob`, `JobCompleted`, `JobFailed` |
| **Video** | Direct API | Formulates LTX jobs. QA's completed video. | `QueueJob`, `JobCompleted`, `JobFailed` |
| **Provisioner** | Direct API | Monitors job queues. Provisions/destroys VMs. | `VMAllocated`, `VMDeallocated`, `VMProvisionFailed` |
| **OTIO Gate** | Direct API | **ONLY** component that writes the timeline. | `MergeIntoOTIO` |
| **Assembly** | Direct API | Combines clips into final MP4. | `MergeIntoOTIO`, `PipelineComplete` |

**VM Worker Agents** (run ON GPU instances) are HTTP services. They communicate with the pipeline by POSTing to the Provisioner agent's POST /.

**Agent Properties:**
- **Stateful.** All agents accumulate wisdom across turns. Conversation history persists.
- **One tool only: `bash`.** The agent's only tool is executing bash commands.
- **No internal tools.** No `write_file`, no `queue_job`, no `check_b2`.
- **The state machine influences ONLY the system prompt.** It gives direction. It does not restrict tools.

---

### 2.3 VM Agent (runs ON GPU instance)

**Architecture:** The GPU instance runs TWO processes:
1. **Inference worker:** Python script that runs TTS or LTX. Not an LLM. Standard inference.
2. **VM agent:** HTTP service that receives instructions, reasons via LLM API call, executes bash.

**The GPU VRAM is NOT shared with an LLM.** The VM agent calls `deepseek-v4-flash` via HTTP API. The GPU runs only the inference model.

**VM Agent Properties:**
- **Is an LLM.** Reasons about survival, output quality, retry strategy — by calling the DeepSeek API.
- **Boots, installs deps, pulls jobs from queue** via bash (`curl` to Jobs Service GET /).
- **Runs TTS or LTX inference** via bash (`python inference_worker.py ...`).
- **QA's own output** using reasoning (LLM call with file metadata, not raw media).
- **Monitors overseer heartbeat.** Pipeline health service on fixed IP:port. VM curls it every 60s. Self-destructs on 15 min loss.
- **Uploads artifacts to B2** via bash (`curl` with pre-signed URL obtained via JWT).
- **Reports `JobCompleted` or `JobFailed`** back to pipeline by POST / to the Provisioner agent.
- **Throttled.** If API call cost > $0.10/hour, reduces reasoning frequency to once per scene.

**VM Agent HTTP Surface:**
- `GET /` — returns current status, last job, health
- `POST /` — receives instruction text, appends to context, returns response

**Why VM reports to Provisioner:** The Provisioner agent is the authority on VM lifecycle. VM sends text like "Job 123 completed. Uploaded to b2://..." via POST / to the Provisioner. The Provisioner treats this as context and produces `JobCompleted` effects.

---

### 2.4 Effect Parser

**Input:** Raw natural-language text from any agent.
**Output:** Zero or more typed `Effect` objects.

**Mechanism — Category-Conditioned Generation:**

The agent is NOT told Pydantic field names. It IS told abstract categories ("kinds"):

```
When you want to change something, describe it naturally.
Your description will be parsed into one of these kinds:
- script      — when you wrote or revised narration text
- audio_job   — when you want TTS audio generated
- video_job   — when you want LTX video generated
- vm_alloc    — when you provisioned a GPU worker
- vm_free     — when you destroyed a GPU worker
- vm_fail     — when provisioning failed
- otio_merge  — when you want to add something to the timeline
- job_done    — when a job completed successfully
- job_fail    — when a job failed
- requeue     — when you want to retry a failed job
- transition  — when you think the pipeline should change phase
- abort       — when the failure is unrecoverable
- clarify     — when you need more information
```

The agent produces text like:
```
I revised scene 3 narration. Cassandra's line is now shorter.
Kind: script
Scene: 3
Speaker: Cassandra
Text: "The economy collapsed, not because of policy, but because of trust."
```

The parser uses `instructor` + `deepseek-v4-flash` to extract:
1. `kind` — one of the 12 abstract categories
2. `payload` — free-form text containing the details

Then a downstream router maps `kind` to the correct Pydantic model:
```python
kind_router = {
    "script":     UpdateScript,
    "audio_job":  QueueJob,
    "video_job":  QueueJob,
    "vm_alloc":   VMAllocated,
    ...
}
```

**Why this works:** The agent knows categories (12 items) but not schemas. The parser knows schemas but lets the LLM handle semantic extraction. This is the practical middle ground.

**Validation:** Pydantic discriminated unions enforce schema. `max_retries=2` for the parser. If exhausted, emit `ClarificationRequest` to the same agent.

**Hard rule:** Never silently drop unparsable text. Always report back.

---

### 2.5 Effect Schema

```python
class Effect(BaseModel):
    seq: int          # assigned by event store
    kind: str         # abstract category
    agent: str        # which agent produced this
    timestamp: float  # Unix epoch

# --- Content Effects ---

class UpdateScript(Effect):
    kind: Literal["script"]
    scene: int
    speaker: str
    text: str
    duration_hint: float | None = None

class QueueJob(Effect):
    kind: Literal["audio_job", "video_job"]
    job_id: str
    scene: int
    slot: int
    params: dict
    priority: int = 0

class JobCompleted(Effect):
    kind: Literal["job_done"]
    job_id: str
    b2_path: str
    duration_actual: float
    vm_id: str

class JobFailed(Effect):
    kind: Literal["job_fail"]
    job_id: str
    error: str
    vm_id: str
    retryable: bool = True

class JobRequeued(Effect):
    kind: Literal["requeue"]
    job_id: str
    reason: str
    modified_params: dict | None = None

# --- VM Effects ---

class VMAllocated(Effect):
    kind: Literal["vm_alloc"]
    vm_id: str
    offer_id: str
    worker_url: str
    role: Literal["tts", "ltx"]
    jwt: str

class VMDeallocated(Effect):
    kind: Literal["vm_free"]
    vm_id: str
    reason: str

class VMProvisionFailed(Effect):
    kind: Literal["vm_fail"]
    offer_id: str
    error: str

# --- OTIO Effects ---

class MergeIntoOTIO(Effect):
    kind: Literal["otio_merge"]
    track: Literal["video", "narration", "music"]
    scene: int
    slot: int
    b2_path: str
    duration: float
    trim_start: float = 0.0
    trim_end: float = 0.0

# --- Pipeline Effects ---

class TransitionState(Effect):
    kind: Literal["transition"]
    from_state: str
    to_state: str
    reason: str

class PipelineComplete(Effect):
    kind: Literal["pipeline_complete"]
    output_b2_path: str

class AbortPipeline(Effect):
    kind: Literal["abort"]
    reason: str

class ClarificationRequest(Effect):
    kind: Literal["clarify"]
    target_agent: str
    question: str
```

**Event Store Assignment:** The event store assigns `seq` and `timestamp`. Agents do NOT set these.

---

### 2.6 Event Store

**Interface:**
```python
class EventStore:
    def append(self, effect: Effect) -> EventRecord: ...
    def read_since(self, seq: int) -> list[EventRecord]: ...
    def read_all(self) -> list[EventRecord]: ...
    def snapshot(self, seq: int, projections: dict) -> None: ...
```

**Storage:** JSONL file (`events.jsonl`) + snapshot files (`snapshot.{seq}.json`).

**Single Writer Actor:** All agents send effects to a central writer task via `asyncio.Queue`. The writer task:
1. Receives `Effect` from queue
2. Assigns `seq = last_seq + 1`
3. Appends one JSON line to `events.jsonl`
4. Calls `fsync`
5. Notifies projections that new events are available

**Why this works:** File locking is brittle. A single writer with a queue is correct by construction. Throughput is sufficient for 5-10 agents.

---

### 2.7 Projection Handlers

**Incremental Update Pattern:**

Each projection maintains `last_processed_seq`. On startup:
1. Load latest snapshot if available.
2. Call `event_store.read_since(last_processed_seq)`.
3. Apply new events.
4. Update `last_processed_seq`.

**Projections:**

| Projection | State | Events Consumed |
|---|---|---|
| OTIOProjection | `opentimelineio.Timeline` | `MergeIntoOTIO` |
| JobQueueProjection | `dict[job_id, JobState]` | `QueueJob`, `JobCompleted`, `JobFailed`, `JobRequeued` |
| VMRegistryProjection | `dict[vm_id, VMState]` | `VMAllocated`, `VMDeallocated`, `VMProvisionFailed` |
| StateMachineProjection | `current_state: str` | `TransitionState` |

**Snapshotting:** Every 100 events or on graceful stop, each projection writes to `snapshot.{seq}.{name}.json`.

**This resolves the V2 O(n²) problem.**

---

### 2.8 HTTP Surfaces

**Every service exposes exactly GET / and POST / on its own port.** No other paths.

| Service | Port | GET / returns | POST / accepts |
|---|---|---|---|
| Orchestrator | 8001 | Current decision log | Human text (appended to orchestrator context) |
| Scenario Agent | 8002 | Current context + last effects | Human text or pipeline instruction |
| Audio Agent | 8003 | Current context + last effects | Human text or pipeline instruction |
| Video Agent | 8004 | Current context + last effects | Human text or pipeline instruction |
| Provisioner Agent | 8005 | Current context + last effects | Human text, pipeline instruction, or VM report |
| OTIO Gate Agent | 8006 | Current context + last effects | Human text or pipeline instruction |
| Assembly Agent | 8007 | Current context + last effects | Human text or pipeline instruction |
| OTIO State Service | 8101 | Plain text: scene list, slot status, durations | (no-op or error) |
| Job Queue Service | 8102 | Plain text: pending, running, completed, failed | (no-op or error) |
| VM Registry Service | 8103 | Plain text: active VMs, roles, health | (no-op or error) |
| State Machine Service | 8104 | Plain text: current state, history | (no-op or error) |
| Health Service | 8200 | `"alive"` | (no-op) |
| VM Agent (on GPU) | 9000+ | Current status, last job | Instruction text |

**Agent endpoints:** Agents that run as direct API calls (Scenario, Audio, Video, Provisioner, OTIO Gate, Assembly) still expose GET / and POST / for human observation and intervention. When the pipeline calls them, it uses the direct API. When a human calls them, it uses HTTP.

**VM reports to Provisioner:** VM sends text via POST / to the Provisioner agent. The Provisioner parses this as context and produces effects.

**State services:** These are NOT agents. They are read-only projections exposed as HTTP. POST / is a no-op or returns an error message.

---

### 2.9 State Summary Builder

**Template (7 sections, verbosity controlled by state):**

```
=== PIPELINE STATE ===
Current phase: {current_state}
Phase since: {state_entry_time} ago
Total events: {event_count}

=== OTIO ===
Scenes: {scene_count}
Complete slots: {complete_count}/{total_slots}
Missing: {missing_list}

=== JOBS ===
Pending audio: {pending_audio}
Pending video: {pending_video}
Running: {running_count}
Completed: {completed_count}
Failed (awaiting retry): {failed_count}

=== VMs ===
Active: {active_vm_count}
Idle: {idle_vm_count}
Roles: {role_breakdown}

=== LAST EVENTS ===
{last_5_events_bulleted}

=== INSTRUCTIONS ===
{state_specific_instruction}

=== YOUR TURN ===
You are the {agent_name} agent.
Decide what to do. Use bash. Report outcomes with kind markers.
```

**Verbosity levels:**
- `verbose`: All sections (agent first enters state)
- `normal`: Omit "Last Events" if no new events since last turn
- `exception`: Full + exception traceback

---

## 3. Data Flow

### 3.1 Normal Cycle

```
1. Load projections from latest snapshots + replay tail events
2. Build state summary from projections
3. Read current state from StateMachineProjection
4. Orchestrator reads state summary + current state
5. Orchestrator decides → "run [agent]" + reason
6. Construct agent prompt: persona + state instructions + context
7. Call agent (direct API or HTTP POST)
8. Receive raw text response
9. Parse into effects (§2.4)
10. Validate effects against schemas
11. Send valid effects to EventStore writer queue
12. Writer appends to JSONL, assigns seq
13. Projections incrementally update from new events
14. Go to 1
```

---

### 3.2 Exception Flow

```
1. Exception occurs during agent turn
2. Exception text + traceback captured
3. Exception added to state summary (exception verbosity)
4. Orchestrator reads state summary, sees exception
5. Orchestrator decides: "run [same_agent]" (default)
6. Agent receives: exception, state summary, previous context
7. Agent responds with diagnosis + proposed fix
8. Effects parsed, validated, sent to writer queue
9. Cycle resumes
10. If agent produces AbortPipeline → kill all VMs, exit
```

**No separate Maintainer agent.** The orchestrator routes exceptions. Default: same agent.

---

### 3.3 State Transition Flow

```
1. Orchestrator decides state should change
2. Orchestrator produces TransitionState effect
3. Effect sent to writer queue, appended to event log
4. StateMachineProjection updates current_state
5. Next cycle: orchestrator reads new state, new instructions in prompt
```

---

### 3.4 Human Intervention Flow

```
1. Human GETs an agent: returns agent context + state
2. Human POSTs text to agent: text appended to agent context
3. Next time orchestrator runs that agent, new text is in context
4. Agent response incorporates human instruction
5. Effects parsed, validated, appended
6. Pipeline continues
```

---

### 3.5 VM Lifecycle Flow

```
1. Provisioner sees pending jobs (curl GET / on Job Queue Service)
2. Provisioner uses bash → vastai CLI → creates instance
3. Provisioner generates JWT (30-min expiry, upload-only, path-scoped)
4. JWT embedded in VM on-start script
5. VMAllocated effect sent to writer queue
6. VM boots, starts VM agent HTTP service
7. VM agent curls Health Service GET / every 60s
8. VM agent pulls job: GET / on Job Queue Service
9. VM agent runs inference via bash
10. VM agent QA's output via LLM call (metadata only)
11. VM agent requests pre-signed B2 URL by POST / to Provisioner with JWT
12. Provisioner validates JWT, returns pre-signed URL
13. VM agent uploads artifact via curl with pre-signed URL
14. VM agent reports JobCompleted by POST / to Provisioner
15. VM agent monitors Health Service; if 15 min without response → self-destruct
16. Pipeline may explicitly deallocate VM (Provisioner emits VMDeallocated)
```

**JWT Content:**
```json
{
  "sub": "vm-{vm_id}",
  "iss": "pipeline",
  "aud": "b2-upload",
  "exp": 1747500000,
  "scope": "upload-only",
  "bucket": "documentary-clips",
  "path_prefix": "runs/{run_id}/"
}
```

Signed with pipeline's private key. VM presents JWT to Provisioner POST /. Provisioner validates and returns pre-signed B2 URL.

---

## 4. State Machine

### 4.1 States

```
[INIT] → [SCRIPT] → [AUDIO_VIDEO] → [ASSEMBLY] → [DONE]
            ↑__________|____________|
                 (retry loops)
```

| State | Meaning | Orchestrator Default Action |
|---|---|---|
| **INIT** | No script exists. | Run Scenario agent. |
| **SCRIPT** | Script exists, not approved. | Run Scenario agent for refinement, or emit TransitionState to AUDIO_VIDEO. |
| **AUDIO_VIDEO** | Jobs in queues or being produced. | Run Audio agent if pending audio. Run Video agent if pending video. Run Provisioner if pending jobs > active VMs. |
| **ASSEMBLY** | All clips ready. | Run Assembly agent. Emit PipelineComplete when done. |
| **DONE** | Output exists. | Stop. |

**PROVISIONING is NOT a state.** It is an activity within AUDIO_VIDEO.

**Hard rule:** The orchestrator CAN transition between any states. Default is linear. Exceptional transitions require a reason.

---

### 4.2 State Machine Prompt Injection (StateFlow Pattern)

Per the StateFlow research, each state maps to an instruction block:

```
[State: INIT]
"You are the scenario agent. Write a narration script."

[State: SCRIPT]
"You are the scenario agent. Refine the existing script."

[State: AUDIO_VIDEO]
"You are the audio/video agent. Formulate generation jobs."

[State: ASSEMBLY]
"You are the assembly agent. Combine clips into final MP4."
```

**Tools are never restricted by state.**

---

### 4.3 Retry Mechanics

- Failed jobs requeued with `JobRequeued` effect.
- Agents see requeued jobs in state summary.
- **No automatic retry count limits.**
- **Budget guard:** If total cost exceeds `DEFAULT_BUDGET`, orchestrator injects "Budget threshold reached. Prioritize completion."

---

## 5. Communication Contracts

### 5.1 Agent ↔ Pipeline

| Agent | Protocol | thread_id |
|---|---|---|
| Scenario, Audio, Video, Provisioner, OTIO Gate, Assembly | Direct LLM API | Pipeline run UUID |
| VM Agent (on GPU) | HTTP (GET /, POST /) | N/A (server-side context) |

**Direct API calls:** Pipeline calls DeepSeek directly with `model="deepseek-v4-flash"`.

**HTTP agents:** Pipeline sends POST / with text. Agent service maintains context server-side.

---

### 5.2 Agent ↔ Agent

**No direct communication.** Pipeline passes context via state summary.

---

### 5.3 Pipeline ↔ VM Workers

| Direction | Method | Target |
|---|---|---|
| Pipeline → VM | POST / | VM Agent POST / (job description) |
| VM → Pipeline | POST / | Provisioner Agent POST / (VM reports) |
| VM → State | GET / | Job Queue Service, VM Registry Service |

**Result retrieval:** VM uploads to B2. Pipeline reads from B2.
**Hard rule:** Pipeline never pulls artifacts from VM directly.

---

## 6. Dynamic Prompt Construction

### 6.1 System Prompt = Persona + State Instructions + Context

```
System Prompt = Base Persona + State Instructions + Context Window
```

| Component | Source |
|---|---|
| **Base Persona (T)** | `personas/{agent_name}.txt` |
| **Memory (M)** | LLM provider conversation history |
| **Domain Knowledge (D)** | Skills from `skills/{skill_name}.md` |
| **Rules/Constraints (R)** | State instruction block (§4.2) |
| **Weights/Priorities (W)** | State-specific emphasis |

---

### 6.2 State Disclosure Strategy

**Default: Implicit.** Agent receives new instructions without being told "state changed."

**Escalation:** If agent produces wrong effects:
1. Reject: "Your instructions have changed."
2. Next turn APPENDS: "Current state: AUDIO_VIDEO."
3. Does NOT clear conversation history.

---

### 6.3 Skills Loading

**Location:** `skills/` directory, one `.md` file per skill.
**Loading:** By filename. Read `skills/vastai-provisioning.md`, append to Domain Knowledge slot.

---

## 7. Hard Principles

### 7.1 Truth
- Event log is the only source of truth for state transitions.
- B2 is ground truth for artifacts.
- State summary must be truthful.
- Projections are rebuilt from events. Snapshots are optimization.

### 7.2 Agency
- Orchestrator decides which agent runs; agents decide what to do.
- No procedural logic overriding agent decisions.
- State machine influences ONLY the system prompt.
- No arbitrary timeout-based kills.
- Budget ceiling changes priorities (W), not capability.

### 7.3 Communication
- Plain text only between agents and pipeline.
- Every service: GET / and POST / only. No other paths.
- Text is accidental structure. Structure extracted by parser.

### 7.4 Failure
- Never silently fail. Report to human immediately.
- Kill everything on unrecoverable failure.
- Honest failure: Missing dependency → pipeline fails.

### 7.5 Isolation
- Each pipeline run is self-contained.
- Agent memory persists across runs.
- No global mutable state. Everything flows through projections.

### 7.6 Configuration
- No environment variables.
- External credentials read from files.
- No CLI arguments.
- Hardcoded model: `deepseek-v4-flash`.

### 7.7 VMs
- Vast.ai CLI is source of truth.
- Start with one VM, confirm health, then escalate.
- VMs self-destruct on heartbeat loss (~15 min).
- Upload to B2 immediately.
- No long-lived credentials on VM. JWT only. 30-min expiry.

### 7.8 Tools
- Agent's ONLY tool is `bash`.
- No internal tools.
- Everything goes through bash.
- State machine does not restrict tools.

### 7.9 Quality
- Pyright zero errors.
- Ruff clean.
- No mocks, stubs, placeholders.
- No premature optimization.

---

## 8. Glossary

| Term | Definition |
|---|---|
| **Effect** | Typed, immutable record of a state change proposed by an agent |
| **Event** | Effect that has been validated and appended to the event log |
| **Projection** | Read model rebuilt from events, with incremental checkpoints |
| **Agent** | LLM with persistent memory, reasoning, ability to produce effects |
| **Worker** | GPU VM running inference + VM agent HTTP service |
| **Orchestrator** | Agent that decides which agent runs and emits TransitionState |
| **OTIO Gate** | Sole agent authorized to mutate the timeline |
| **State Summary** | Human-readable string describing pipeline state (§2.9) |
| **Skill** | Loadable `.md` from `skills/`, appended to Domain Knowledge |
| **B2** | Backblaze B2 — ground truth artifact storage |
| **State Machine** | FSM that selects instruction blocks for prompts |
| **Effect Parser** | Category-conditioned extraction via instructor |
| **Category-conditioned** | Agent knows abstract kinds, not Pydantic fields |
| **JWT** | Short-lived, scoped credential for VM B2 upload |
| **Single Writer Actor** | Central task receiving effects via queue, appending atomically |
| **Snapshot** | Periodic projection state write for fast restart |
| **ClarificationRequest** | Effect when parser exhausts retries |

---

*Abstract version: 2026-05-17 v3*
