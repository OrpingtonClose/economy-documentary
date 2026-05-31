> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Abstract Architecture V4 — Documentary Pipeline

> Revised 2026-05-17. Orchestrator destroyed. State machine self-operates. SQLite event store. Async HTTP agents.
>
> Research: Perplexity (python-statemachine async/eventless, SQLite event sourcing, async HTTP agent patterns, Pydantic discriminated unions).

---

## 0. Philosophy Change From V3

| V3 | V4 |
|---|---|
| Orchestrator decides which agent runs | **No orchestrator.** State machine self-operates. Agents run when their state is active. |
| Agent memory persists across runs | **Agent memory does NOT persist.** Each run is self-contained. |
| Event store is JSONL file | **Event store is SQLite.** Handles unbounded growth. |
| Projections rebuild from JSONL | **Projections read from SQLite** with incremental checkpoints. |
| VM reports to Provisioner via POST / | **VM reports to Provisioner via POST /** — same mechanism, clarified. |
| State summary is prose | **State summary is JSON** — endpoints serve summarized JSON as text. |
| thread_id for context | **No thread_id.** Each agent turn is independent. Context built from state + events. |
| B2 for artifacts | **No B2 for now.** Artifacts stored locally. Event store is the only store. |

---

## 1. System Topology

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            HUMAN / OVERSEER                              │
│  Observes any agent via GET /. Corrects via POST /.                     │
│  No dashboard. No approval UI. Plain text only.                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         STATE MACHINE (self-operating)                   │
│  python-statemachine with eventless transitions. Watches global state.   │
│  No orchestrator. No central decider. The machine moves itself.          │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
           ┌────────────────────────┼────────────────────────┐
           │                        │                        │
           ▼                        ▼                        ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│  SCENARIO     │  │  AUDIO        │  │  VIDEO        │  │  PROVISIONER  │
│  AGENT        │  │  AGENT        │  │  AGENT        │  │  AGENT        │
│  port 8001    │  │  port 8002    │  │  port 8003    │  │  port 8004    │
└───────────────┘  └───────────────┘  └───────────────┘  └───────────────┘
       │                  │                  │                  │
       │                  │                  │                  │
       ▼                  ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      EVENT STORE (SQLite)                                │
│  Single table: events(id, seq, timestamp, agent, kind, payload_json)    │
│  Writers use BEGIN IMMEDIATE. One writer at a time, many readers.       │
│  Queue abstraction ensures ordering. Timestamps for debugging stuck.    │
└─────────────────────────────────────────────────────────────────────────┘
       ▲                  ▲                  ▲                  ▲
       │                  │                  │                  │
       ▼                  ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     PROJECTION SERVICES (read-only)                      │
│  Each service maintains incremental checkpoint. Serves GET /.            │
│  OTIO Service | Job Service | VM Service | State Service                 │
│  port 8101    | port 8102   | port 8103  | port 8104                    │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     VM WORKERS (ephemeral GPU instances)                 │
│  port 9000+  GET /  POST /                                              │
│  Runs inference (TTS/LTX). VM agent calls deepseek-v4-flash via API.    │
│  Receives jobs from Provisioner. Reports completions to Provisioner.    │
└─────────────────────────────────────────────────────────────────────────┘
```

**Endpoint Rule:** Every box exposes exactly `GET /` and `POST /` on its own port. No paths. No other endpoints.

---

## 2. Components

### 2.1 State Machine (self-operating)

**Framework:** `python-statemachine` with async support and eventless transitions.

**Key capability:** Eventless transitions fire automatically when their guard condition is true. The state machine embeds its own watcher loop — no external orchestrator needed.

```python
from statemachine import StateMachine, State

class PipelineMachine(StateMachine):
    # States
    init = State(initial=True)
    script = State()
    audio_video = State()
    assembly = State()
    done = State(final=True)

    # Eventless transitions: fire when guard is true
    init.to(script, event="auto")  # always fires from init
    script.to(audio_video, cond="script_complete")
    audio_video.to(assembly, cond="all_jobs_done")
    audio_video.to(script, cond="script_needs_rework")  # retry loop
    assembly.to(done, cond="assembly_complete")

    # Self-transition: re-evaluate while in state
    audio_video.to.itself(cond="jobs_still_pending")
    script.to.itself(cond="script_needs_refinement")
```

**Guard evaluation:** Guards are pure functions that read projection services:
- `script_complete` → OTIO Service shows script exists and has scenes
- `all_jobs_done` → Job Service shows 0 pending, 0 running, all completed
- `script_needs_rework` → Job Service shows failed jobs with "script error" reason
- `jobs_still_pending` → Job Service shows pending > 0

**Watcher loop:** The state machine runs an async task that:
1. Sleeps 1 second (throttle)
2. Polls projection services via GET /
3. Evaluates all guards for current state
4. If a guard is true, fires the transition
5. On transition, notifies the target agent that it's now active

**Why no orchestrator:** The state machine IS the control logic. It watches state. It transitions. It activates agents. No central decider needed.

**State machine prompt influence:** When the machine enters a state, it loads the state-specific instruction block (R slot in T/M/D/R/W) and injects it into the active agent's prompt.

---

### 2.2 Agents (4 Types)

| Agent | Port | Role | When Active |
|---|---|---|---|
| **Scenario** | 8001 | Writes narration script. | State = `script` |
| **Audio** | 8002 | Formulates TTS jobs. Judges audio artistry. | State = `audio_video` |
| **Video** | 8003 | Formulates LTX jobs. Judges video artistry. | State = `audio_video` |
| **Provisioner** | 8004 | Provisions/destroys VMs. Receives VM reports. | State = `audio_video` |

**OTIO Gate and Assembly are NOT separate agents.** They are behaviors within the agents that produce `MergeIntoOTIO` effects. The Scenario agent writes the initial OTIO. The Audio/Video agents emit `MergeIntoOTIO` when clips are ready. The last active agent in `assembly` state handles ffmpeg.

**Why 4 agents instead of 6:** The orchestrator is gone. There's no one to coordinate 6 agents. The state machine activates agents based on state. Fewer agents = fewer coordination points.

**Agent HTTP Surface:**
- `GET /` → Returns: current context, last effects produced, current state, idle time
- `POST /` → Accepts: text instruction. Appends to context. Returns: 202 Accepted + task_id

**Async turn pattern:**
1. State machine decides agent should run (guard fired, state entered)
2. State machine POSTs to agent with state summary JSON as text
3. Agent returns 202 Accepted with `task_id`
4. Agent runs LLM call (may take minutes)
5. Agent produces effects, sends them to Event Store
6. State machine polls agent GET / to check if turn complete
7. If agent stuck > threshold, overseer can POST / to correct

**Agent Properties:**
- **NOT stateful across runs.** Each run starts fresh. No memory.
- **One tool only: `bash`.**
- **Context is rebuilt each turn** from: persona + state instructions + state summary JSON + any prior effects from this run.
- **No internal tools.**

---

### 2.3 VM Agent (runs ON GPU instance)

**Architecture:** GPU instance runs:
1. **Inference worker** — Python script for TTS or LTX. Uses GPU VRAM.
2. **VM agent HTTP service** — FastAPI on port 9000+. Calls deepseek-v4-flash via API. Does NOT use GPU VRAM for LLM.

**VM Agent HTTP Surface:**
- `GET /` → Returns: current job, progress %, last output, health
- `POST /` → Accepts: job description text. Starts job. Returns: 202 Accepted

**VM Agent Behavior:**
1. Receives job via POST /
2. Runs inference via bash (`python inference_worker.py ...`)
3. Judges output quality via LLM call (metadata only — file size, duration, etc.)
4. If approved: reports `JobCompleted` to Provisioner via POST /
5. If rejected: reports `JobFailed` to Provisioner via POST /
6. Monitors pipeline health service via GET / every 60s
7. If no heartbeat for 15 min: self-destructs via `vastai destroy` (API key sent to VM)

**Credential flow (temporary, unsafe):**
- Vast.ai API key sent to VM via on-start script
- B2 credentials NOT sent (no B2 for now)
- VM uploads artifacts to local path or returns them via HTTP

**Why temporary:** The user explicitly said "send the API key to the VM agent" and "this is unsafe, but the stakes aren't high." This will be amended later.

---

### 2.4 Effect Parser

**Mechanism — Category-Conditioned Generation with Instructor:**

The agent knows abstract categories ("kinds") but NOT Pydantic field names. The parser maps `kind` to Pydantic models via `instructor`.

**Agent prompt snippet:**
```
When you accomplish something, report it with a kind marker:

Kind: script — you wrote or revised narration text
Kind: audio_job — you want TTS audio generated  
Kind: video_job — you want LTX video generated
Kind: vm_alloc — you provisioned a GPU worker
Kind: vm_free — you destroyed a GPU worker
Kind: vm_fail — provisioning failed
Kind: otio_merge — you added something to the timeline
Kind: job_done — a job completed successfully
Kind: job_fail — a job failed
Kind: requeue — you want to retry a failed job
Kind: transition — you think the pipeline should change phase
Kind: abort — the failure is unrecoverable
Kind: clarify — you need more information

Describe what happened naturally. The system will parse your description.
```

**Parser flow:**
1. Agent produces raw text containing a kind marker
2. Parser extracts `kind` via simple string search (not regex — just `text.find("Kind: ")`)
3. Parser calls `instructor` + `deepseek-v4-flash` with:
   - The raw text
   - The target Pydantic model (determined by `kind`)
   - No schema in the prompt — instructor uses tool-calling/JSON mode under the hood
4. Instructor extracts structured data into the Pydantic model
5. If validation fails: `max_retries` case-by-case (usually 2-3)
6. If exhausted: emit `ClarificationRequest` to same agent

**Why this works:** The agent knows 12 abstract words. The parser knows schemas. Instructor bridges the gap using the LLM's tool-calling capability — the schema is sent to the LLM via the API's tool/function mechanism, NOT via the prompt. The agent never sees field names.

**Critical rule:** NEVER use regex for extraction. NEVER optimize cost. Adjust prompts and instructor settings until it works.

---

### 2.5 Effect Schema

```python
from pydantic import BaseModel, Field
from typing import Literal, Union
from datetime import datetime

class EventMeta(BaseModel):
    seq: int
    timestamp: datetime
    agent: str

class Effect(EventMeta):
    kind: str

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
    artifact_path: str
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

class VMAllocated(Effect):
    kind: Literal["vm_alloc"]
    vm_id: str
    offer_id: str
    worker_url: str
    role: Literal["tts", "ltx"]

class VMDeallocated(Effect):
    kind: Literal["vm_free"]
    vm_id: str
    reason: str

class VMProvisionFailed(Effect):
    kind: Literal["vm_fail"]
    offer_id: str
    error: str

class VMObserved(Effect):
    kind: Literal["vm_observed"]
    vm_id: str
    status: str
    vastai_json: dict

class MergeIntoOTIO(Effect):
    kind: Literal["otio_merge"]
    track: Literal["video", "narration", "music"]
    scene: int
    slot: int
    artifact_path: str
    duration: float
    trim_start: float = 0.0
    trim_end: float = 0.0

class TransitionState(Effect):
    kind: Literal["transition"]
    from_state: str
    to_state: str
    reason: str

class PipelineComplete(Effect):
    kind: Literal["pipeline_complete"]
    output_path: str

class AbortPipeline(Effect):
    kind: Literal["abort"]
    reason: str

class ClarificationRequest(Effect):
    kind: Literal["clarify"]
    target_agent: str
    question: str

EffectUnion = Union[
    UpdateScript, QueueJob, JobCompleted, JobFailed, JobRequeued,
    VMAllocated, VMDeallocated, VMProvisionFailed, VMObserved,
    MergeIntoOTIO, TransitionState, PipelineComplete, AbortPipeline,
    ClarificationRequest
]
```

**Event Store assigns `seq` and `timestamp`.** Agents produce text with kind markers. Parser extracts. Event store wraps with metadata.

---

### 2.6 Event Store (SQLite)

**Schema:**
```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL UNIQUE,
    timestamp REAL NOT NULL,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload JSON NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX idx_events_kind ON events(kind);
CREATE INDEX idx_events_agent ON events(agent);
CREATE INDEX idx_events_timestamp ON events(timestamp);
```

**Interface:**
```python
class EventStore:
    def append(self, effect: Effect) -> EventRecord:
        """BEGIN IMMEDIATE. Insert. COMMIT. Returns record with assigned seq."""
    
    def read_since(self, seq: int) -> list[EventRecord]:
        """Read events with seq > given. Used by projections for incremental update."""
    
    def read_all(self) -> list[EventRecord]:
        """Full replay. Used on cold start."""
    
    def read_by_kind(self, kind: str) -> list[EventRecord]:
        """All events of a kind. Used by VM projection (Vast.ai is source of truth)."""
```

**Concurrency:**
- `BEGIN IMMEDIATE` acquires reserved lock at transaction start
- One writer at a time, many readers
- Writers are short: read max seq → insert → commit
- If disk full: SQLite raises error. Event kept in memory. Exception raised. Pipeline stops.

**Queue abstraction:**
```python
class EventQueue:
    """In-memory queue that feeds the SQLite writer."""
    async def put(self, effect: Effect) -> EventRecord:
        """Queue effect. Writer task picks it up, assigns seq, inserts to SQLite."""
```

**Why SQLite:** Handles unbounded growth. ACID. No file locking brittleness. Can query by kind, agent, timestamp.

---

### 2.7 Projection Services

**Incremental Update Pattern:**

Each projection service:
1. Maintains `last_processed_seq` in memory
2. On startup: loads from SQLite `SELECT * FROM events WHERE seq > ? ORDER BY seq`
3. Applies events to in-memory state
4. Updates `last_processed_seq`
5. Serves GET / with current state as JSON text

**Projection Services:**

| Service | Port | State | Events Consumed | GET / Returns |
|---|---|---|---|---|
| OTIO Service | 8101 | `opentimelineio.Timeline` | `MergeIntoOTIO`, `UpdateScript` | JSON: scenes, slots, durations, completeness |
| Job Service | 8102 | `dict[job_id, JobState]` | `QueueJob`, `JobCompleted`, `JobFailed`, `JobRequeued` | JSON: pending, running, completed, failed counts + list |
| VM Service | 8103 | `dict[vm_id, VMState]` | `VMAllocated`, `VMDeallocated`, `VMProvisionFailed`, `VMObserved` | JSON: active VMs, roles, health, idle time |
| State Service | 8104 | `current_state: str` | `TransitionState` | JSON: current state, state history, time in state |

**VM Service special behavior:** Vast.ai CLI is source of truth. The VM Service:
1. Reads all `vm_*` events from SQLite (there won't be many)
2. ALSO calls `vastai show instances` via bash
3. Compares: if Vast.ai shows a VM not in events → emits `VMObserved`
4. If events show a VM not in Vast.ai → marks as "orphaned"

**Why this matters:** Effects are elaborate logs for reasoning about failures. The projection brings ALL events from the VM category so agents can see the full history.

---

### 2.8 State Summary JSON

**Format:**
```json
{
  "pipeline": {
    "current_state": "audio_video",
    "state_since": 1747500000,
    "total_events": 47
  },
  "otio": {
    "scenes": 5,
    "complete_slots": 12,
    "total_slots": 20,
    "missing": ["scene3.video", "scene4.narration"]
  },
  "jobs": {
    "pending_audio": 3,
    "pending_video": 2,
    "running": 2,
    "completed": 10,
    "failed": 2,
    "failed_list": [
      {"job_id": "j7", "error": "OOM on GPU", "retryable": true}
    ]
  },
  "vms": {
    "active": 2,
    "idle": 1,
    "roles": {"tts": 1, "ltx": 1},
    "orphaned": 0
  },
  "instructions": "You are in AUDIO_VIDEO state. Pending jobs exist. Provision VMs if needed."
}
```

**How it's built:** The state machine queries each projection service GET /, combines the JSON responses, adds the current state and instructions.

**Verbosity:** Always full JSON. No levels. Simple.

---

## 3. Data Flow

### 3.1 Normal Cycle (no orchestrator)

```
1. State machine watcher wakes (every 1s)
2. State machine polls projection services GET /
3. State machine evaluates guards for current state
4. If guard true → fire eventless transition
5. On entering new state → notify target agent
6. Target agent receives state summary JSON via its own trigger
7. Agent runs LLM call (async, may take minutes)
8. Agent produces effects → sends to EventQueue
9. Writer task appends to SQLite
10. Projections incrementally update
11. State machine watcher sees new state on next cycle
12. Go to 1
```

**No orchestrator.** The state machine IS the loop.

---

### 3.2 Exception Flow

```
1. Exception during agent turn
2. Agent catches, writes to its own state
3. Next time state machine polls agent GET /, sees error
4. State machine keeps agent active (same state)
5. Agent receives exception in context on next turn
6. Agent responds with diagnosis + fix
7. Effects parsed, queued, stored
8. Cycle continues
```

**Same agent handles its own exceptions.** No routing needed.

**If agent loops infinitely:**
1. State machine detects same effect produced repeatedly (dedup)
2. State machine marks agent as "looping"
3. GET / shows "looping" status
4. Overseer (human) sees this, POSTs correction to agent
5. Agent receives correction, adjusts

---

### 3.3 Human Intervention Flow

```
1. Human GETs agent → sees current state, context, last effects
2. Human POSTs text to agent → text appended to agent context
3. Agent's next turn incorporates human instruction
4. Effects produced, queued, stored
5. Pipeline continues
```

**No real-time interruption.** The agent is async. Human text waits in context until the agent's next scheduled turn.

---

### 3.4 VM Lifecycle Flow

```
1. Audio/Video agents produce QueueJob effects
2. Job Service projection shows pending jobs
3. State machine in audio_video state, guard "jobs_pending" true
4. Provisioner agent activated
5. Provisioner sees pending jobs, decides how many VMs needed
6. Provisioner uses bash → vastai CLI → creates instance
7. Vast.ai API key sent in on-start script (temporary, unsafe)
8. VMAllocated effect queued and stored
9. VM boots, starts VM agent HTTP service
10. VM agent curls pipeline Health Service GET / every 60s
11. VM agent GETs Job Service for pending jobs
12. VM agent claims job (posts to Provisioner: "taking job X")
13. VM agent runs inference via bash
14. VM agent judges output via LLM call (metadata)
15. VM agent reports JobCompleted/JobFailed to Provisioner POST /
16. Provisioner produces effect, queued, stored
17. VM agent monitors Health Service; 15 min loss → self-destruct
18. Provisioner may deallocate VM when idle
```

**VM reports to Provisioner:** The Provisioner is the lackey of the artist agents. It receives VM reports, translates them into effects, and manages the VM lifecycle.

---

### 3.5 Media Agent → Provisioner → VM Flow

```
Audio Agent: "I need narration for scene 3"
  → QueueJob effect → stored

State machine sees pending job → activates Provisioner

Provisioner: "I see 3 audio jobs pending. I'll provision a TTS VM."
  → VMAllocated effect → stored
  → vastai create instance

VM boots, pulls job, generates audio

VM: "Job done. File at /tmp/scene3.wav. Quality acceptable."
  → POST to Provisioner

Provisioner: "Job completed."
  → JobCompleted effect → stored

Audio Agent (next turn): "I see job completed. I'll verify artistry..."
  → GETs Job Service
  → Judges output
  → If approved: MergeIntoOTIO effect
  → If rejected: JobRequeued effect with artistic notes
```

**Provisioner is the lackey.** Media agents are the artists. Media agents judge. Provisioner executes.

---

## 4. State Machine

### 4.1 States

```
[INIT] → [SCRIPT] → [AUDIO_VIDEO] → [ASSEMBLY] → [DONE]
            ↑__________|____________|
                 (retry loops)
```

| State | Active Agents | Entry Trigger | Exit Guard |
|---|---|---|---|
| **INIT** | Scenario | Always from start | script_has_scenes |
| **SCRIPT** | Scenario | From INIT or retry | script_complete_and_approved |
| **AUDIO_VIDEO** | Audio, Video, Provisioner | From SCRIPT | all_jobs_done |
| **ASSEMBLY** | Audio or Video (whoever is last) | From AUDIO_VIDEO | assembly_complete |
| **DONE** | None | From ASSEMBLY | (final) |

**Retry loop:** If jobs fail with "script error" reason, state machine transitions SCRIPT → AUDIO_VIDEO → SCRIPT. The script agent rewrites the problematic scene.

**Why this matches old mermaid:** The old diagrams had universal back-edges. We keep the SCRIPT ↔ AUDIO_VIDEO back-edge for script rewrites. Other back-edges removed (not implemented).

---

### 4.2 Guard Definitions

| Guard | Reads From | Condition |
|---|---|---|
| `script_has_scenes` | OTIO Service | scenes > 0 |
| `script_complete` | OTIO Service | all scenes have text for all speakers |
| `script_needs_rework` | Job Service | failed jobs with reason containing "script" |
| `jobs_pending` | Job Service | pending > 0 or running > 0 |
| `all_jobs_done` | Job Service | pending == 0 and running == 0 and completed > 0 |
| `assembly_complete` | OTIO Service | all slots have artifacts and final MP4 exists |

---

### 4.3 State Machine Prompt Injection

When state machine enters a state, it loads the instruction block:

```python
STATE_INSTRUCTIONS = {
    "init": "You are initializing the documentary. Write a script outline.",
    "script": "You are writing narration. Focus on pacing, speaker consistency, and timing.",
    "audio_video": "You are producing media. Formulate jobs, judge output quality, approve or reject.",
    "assembly": "You are assembling the final film. Use ffmpeg. Verify duration matches OTIO.",
    "done": "Pipeline complete. No further action needed."
}
```

Injected into the R slot of the active agent's prompt.

---

## 5. Communication Contracts

### 5.1 Agent ↔ Pipeline

| Agent | Protocol | Context Handling |
|---|---|---|
| Scenario, Audio, Video, Provisioner | HTTP (GET /, POST /) | Server-side in-memory context. Rebuilt from events + state summary each turn. |
| VM Agent (on GPU) | HTTP (GET /, POST /) | Server-side in-memory context. |

**Why HTTP for all:** Uniform interface. No special cases. The pipeline POSTs to agents. Agents POST effects back via EventQueue.

**Async pattern:**
- POST / returns 202 Accepted with `task_id`
- GET / returns current status, progress, last output
- State machine polls GET / until status == "complete"

---

### 5.2 Agent ↔ Agent

**No direct communication.** Agents communicate through the event store:
- Audio agent produces `QueueJob` → event store → Job Service
- Provisioner reads Job Service → provisions VM
- VM reports to Provisioner → `JobCompleted` → event store → Job Service
- Audio reads Job Service → judges output

---

### 5.3 Pipeline ↔ VM Workers

| Direction | Method | Content |
|---|---|---|
| Provisioner → VM | POST / | Job description text |
| VM → Provisioner | POST / | Job completion, failure, health |

**Result retrieval:** VM saves artifact to local path. Returns path in POST / report. Pipeline reads from local path.

---

## 6. Prompt Construction

### 6.1 System Prompt = Persona + State Instructions + Context

```
System Prompt = Base Persona + State Instructions + Context Window
```

| Component | Source |
|---|---|
| **Base Persona (T)** | Hardcoded string per agent ("You are the audio agent...") |
| **Memory (M)** | None. Each run is independent. |
| **Domain Knowledge (D)** | Hardcoded in Python files (skills) |
| **Rules/Constraints (R)** | State instruction block from state machine |
| **Weights/Priorities (W)** | Budget-aware emphasis ("prioritize completion" when over budget) |

**No persona files.** The user said "wtf is a persona, I know system prompts." Persona = system prompt. Hardcoded.

---

### 6.2 Context Window

Built fresh each turn:
1. Base Persona (system message)
2. State Instructions (system message)
3. State Summary JSON (user message)
4. Any human corrections from POST / (user messages)
5. Previous effects from this agent in this run (assistant messages)

**No conversation history across runs.** Each turn is independent. The event store provides continuity.

---

### 6.3 State Disclosure

**Implicit.** The agent receives state instructions without being told "the state changed."

If the agent produces wrong effects:
1. Next turn: explicit disclosure appended — "Current state: AUDIO_VIDEO"
2. Conversation history NOT cleared (but it's per-run anyway)

---

## 7. Hard Principles

### 7.1 Truth
- Event store (SQLite) is the only source of truth for state transitions.
- Artifacts stored locally. Event store tracks paths.
- State summary must be truthful. No caching.
- Projections rebuilt from events. Snapshots are optimization.

### 7.2 Agency
- **State machine self-operates.** No orchestrator.
- Agents decide what to do within their state.
- State machine influences ONLY the system prompt.
- No arbitrary timeout-based kills. VM idle budget is a hard ceiling.
- If agent loops: prompt failure → overseer corrects via POST /.

### 7.3 Communication
- Plain text only.
- Every service: GET / and POST / only.
- Text is accidental structure. Structure extracted by parser.

### 7.4 Failure
- Never silently fail.
- LLM API outage → pipeline failure.
- Vast.ai outage → pipeline failure.
- Disk full → exception raised, events in memory, pipeline stops.
- Report to overseer agent (pipeline launcher).

### 7.5 Isolation
- Each pipeline run is self-contained.
- Agent memory does NOT persist across runs.
- No global mutable state outside event store.

### 7.6 Configuration
- No environment variables.
- Credentials read from files.
- No CLI arguments.
- Hardcoded model: `deepseek-v4-flash`.

### 7.7 VMs
- Vast.ai CLI is source of truth.
- Start with one VM, confirm health, then escalate.
- VMs self-destruct on heartbeat loss (~15 min).
- API key sent to VM (temporary, will be amended later).
- No B2 for now. Artifacts local.

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
- NEVER optimize LLM cost.
- NEVER use regex for effect extraction.

---

## 8. Effect Types Reference

| Effect | Kind | Producer | Meaning |
|---|---|---|---|
| `UpdateScript` | script | Scenario | Narration text written/revised |
| `QueueJob` | audio_job / video_job | Audio / Video | Request media generation |
| `JobCompleted` | job_done | Provisioner | VM finished, artifact ready |
| `JobFailed` | job_fail | Provisioner | VM failed, error recorded |
| `JobRequeued` | requeue | Audio / Video | Retry with modifications |
| `VMAllocated` | vm_alloc | Provisioner | GPU instance created |
| `VMDeallocated` | vm_free | Provisioner | GPU instance destroyed |
| `VMProvisionFailed` | vm_fail | Provisioner | Could not create VM |
| `VMObserved` | vm_observed | VM Service | Vast.ai state observed |
| `MergeIntoOTIO` | otio_merge | Audio / Video | Clip added to timeline |
| `TransitionState` | transition | State Machine | Phase changed |
| `PipelineComplete` | pipeline_complete | Assembly | Final MP4 done |
| `AbortPipeline` | abort | Any | Unrecoverable, stop |
| `ClarificationRequest` | clarify | Effect Parser | Parse failed, need more info |

---

## 9. Glossary

| Term | Definition |
|---|---|
| **Effect** | Typed record of a state change proposed by an agent |
| **Event** | Effect that has been validated and stored in SQLite |
| **Projection** | Read model rebuilt incrementally from events |
| **Agent** | LLM with bash tool, HTTP service interface |
| **Worker** | GPU VM running inference + VM agent HTTP service |
| **State Machine** | Self-operating python-statemachine with eventless transitions |
| **OTIO Service** | Projection of timeline state |
| **Job Service** | Projection of job queue state |
| **VM Service** | Projection of VM fleet state |
| **State Service** | Projection of state machine state |
| **Effect Parser** | Category-conditioned extraction via instructor |
| **Event Queue** | In-memory queue feeding SQLite writer |
| **Overseer** | Human or agent that launched the pipeline and monitors it |
| **State Summary** | JSON describing current pipeline state |

---

*Abstract version: 2026-05-17 v4*
