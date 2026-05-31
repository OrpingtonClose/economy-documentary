> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Abstract Architecture V2 — Documentary Pipeline

> Revised 2026-05-17. Incorporates runtime corrections.

---

## 1. System Topology

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              HUMAN OPERATOR                              │
│  Observes via GET / to any agent. Corrects via POST / to any agent.     │
│  No dashboard. No approval UI. No fleet viewer. Plain text only.        │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
           ┌────────────────────────┼────────────────────────┐
           │                        │                        │
           ▼                        ▼                        ▼
┌───────────────┐        ┌───────────────┐        ┌───────────────────────┐
│  ORCHESTRATOR  │───────▶│   AGENTS      │◀──────│   EVENT STORE (JSONL)  │
│   (decides)    │        │  (6 types)    │       │   (immutable, append)  │
└───────────────┘        └───────────────┘       └───────────────────────┘
                               │                          ▲
                               │ produce effects          │
                               ▼                          │
┌─────────────────────────────────────────────────────────┐
│                    EFFECT PARSER                         │
│         (text → typed effects via instructor)            │
│              using deepseek-v4-flash only                │
└─────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────┐
│              PROJECTION HANDLERS                         │
│    (rebuild read-models from event stream)               │
└─────────────────────────────────────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│   OTIO STATE  │    │  JOB QUEUES   │    │  VM REGISTRY  │
│  (materialized│    │  (audio/video)│    │  (active VMs) │
│   timeline)   │    │               │    │               │
└───────────────┘    └───────────────┘    └───────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────┐
│                 VM WORKERS (ephemeral)                   │
│            GPU instances on Vast.ai — each runs          │
│          an LLM agent that self-monitors and self-destructs│
└─────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────┐
│              ARTIFACT STORE (Backblaze B2)               │
│              Ground truth for all media files             │
└─────────────────────────────────────────────────────────┘
```

---

## 2. Components

### 2.1 Orchestrator
- **Single input:** State summary (projections of OTIO, queues, VMs, jobs)
- **Single output:** Decision — which agent runs next + reason
- **No side effects.** Read-only observer. Delegates all action to agents.
- **Hard rule:** The orchestrator never runs tools. It only decides.
- **Hard rule:** The orchestrator is the ONLY component that triggers state machine transitions.

### 2.2 Agents (6 Types)

| Agent | Responsibility | Effects Produced |
|---|---|---|
| **Scenario** | Writes narration text. | `UpdateScript` |
| **Audio** | Formulates TTS jobs. QA's completed audio. | `GenerateNarrationAudio`, `JobCompleted`, `JobFailed` |
| **Video** | Formulates LTX jobs. QA's completed video. | `RenderVideoSegment`, `JobCompleted`, `JobFailed` |
| **Provisioner** | Monitors job queues. Provisions/destroys VMs via bash. | `VMAllocated`, `VMDeallocated`, `VMProvisionFailed` |
| **OTIO Gate** | **ONLY** component that writes the timeline. | `MergeIntoOTIO` |
| **Assembly** | Combines clips into final MP4. | `MergeIntoOTIO`, pipeline completion |

**Agent Properties:**
- **Stateful.** All agents accumulate wisdom across turns. Conversation history persists. The pipeline does not clear context.
- **One tool only: `bash`.** The agent's only tool is executing bash commands. All work (writing files, calling APIs, running ffmpeg) goes through bash.
- **No internal tools.** There is no `write_file` tool, no `queue_job` tool, no `check_b2` tool. Bash is the universal interface.
- **The state machine influences ONLY the system prompt.** It gives direction, priorities, and current-phase context. It does not restrict tools.

### 2.3 VM Agent (runs ON GPU instance)
- **Is an LLM.** Reasons about survival, output quality, and retry strategy.
- **Boots, installs deps, pulls jobs from queue** via bash.
- **Runs TTS or LTX inference** via bash (calling Python scripts).
- **QA's own output** using reasoning. Retries on failure with adjusted parameters.
- **Monitors overseer heartbeat.** Self-destructs on ~15 min heartbeat loss.
- **Uploads artifacts to B2 immediately** via bash.
- **Reports `JobCompleted` or `JobFailed`** back to pipeline via bash (curl POST).
- **Throttled.** If inference cost exceeds threshold, the VM agent reduces its reasoning frequency (e.g., only reasons between scenes, not between clips).

### 2.4 Effect Parser
- **Input:** Raw natural-language text from any agent
- **Output:** Zero or more typed `Effect` objects
- **Mechanism:** `instructor` + `deepseek-v4-flash` extracts typed effects from raw text. No regex pre-extraction. Types are *implied in text* — semantically present in the agent's natural language.
- **Validation:** Pydantic discriminated unions enforce schema. instructor re-asks on validation failure (up to `max_retries`).
- **Failure mode:** If instructor exhausts retries, emit a `ClarificationRequest` effect to the same agent.
- **Hard rule:** Never silently drop unparsable text. Always report back.

### 2.5 Event Store
- **Interface:** `append(effect)` — atomic, append-only
- **Storage:** JSONL file (`events.jsonl`)
- **Properties:** Immutable, ordered, replayable
- **Hard rule:** The event log is the ONLY source of truth for pipeline state transitions.

### 2.6 Projection Handlers
- **Input:** Stream of effects from event store
- **Output:** Materialized read-models (OTIO, queues, VM registry)
- **Properties:** Deterministic, idempotent, pure functions
- **Hard rule:** Read models are rebuilt from events on every cycle.

### 2.7 State Summary Builder
- **Input:** Projected read-models
- **Output:** Human-readable state summary string fed to orchestrator
- **Hard rule:** State summary must be truthful. No caching, no optimistic updates.

### 2.8 HTTP Agent Surface
- **Interface per agent:** `GET /` (probe, returns fresh agent state), `POST /` (instruction, appends to agent context)
- **Properties:** Plain text in, plain text out. No JSON envelope. No status codes with semantics.
- **Hard rule:** Only GET / and POST /. No other endpoints. No special paths for special actions.

### 2.9 State Stores (Read-Only Endpoints)

Three named endpoints provide read-only access to projected state. They do NOT follow the agent GET/POST pattern — they are query endpoints for the pipeline and agents.

| Endpoint | Content | Purpose |
|---|---|---|
| `GET /otio` | Current timeline state | Agents read timeline without direct file access |
| `GET /vms` | Active VM registry | Agents see VM status without calling vastai CLI |
| `GET /jobs` | Job queue state | Agents see pending/completed/failed jobs |

**Properties:**
- Read-only. No POST.
- Plain text output (human-readable lists, not JSON).
- Rebuilt from event log projections on every request.
- Agents may read these endpoints via bash (`curl`).

---

## 3. Data Flow

### 3.1 Normal Cycle
```
1. Rebuild projections from event log
2. Build state summary from projections
3. Reconstruct state machine from transition effects
4. Orchestrator reads state summary + state machine state
5. Orchestrator decides → "run [agent]"
6. Construct agent prompt: persona + state instructions + context
7. Call agent (HTTP GET/POST or direct LLM API)
8. Receive raw text response
9. Parse into effects (Effect Parser)
10. Validate effects
11. Append valid effects to event log
12. Effects trigger projections → update OTIO, queues, VM registry
13. Go to 1
```

### 3.2 Exception Flow
```
1. Exception occurs during an agent turn
2. Exception is returned to the SAME agent as context
3. Agent receives: exception type, traceback, current state summary
4. Agent responds with diagnosis + proposed fix (as effects)
5. Effects parsed, validated, appended to event log
6. Cycle resumes from step 1
7. If the agent declares the failure unrecoverable → KILL EVERYTHING
```

**No separate Maintainer agent.** The agent that caused the error handles it.

### 3.3 State Transition Flow
```
1. Effect appended to event log (e.g., ScriptApproved)
2. Projection handlers rebuild read-models
3. Orchestrator reads updated state summary
4. Orchestrator decides: "Transition from SCRIPT to PROVISIONING"
5. Orchestrator calls state machine transition
6. State machine updates current state
7. Next agent invocation gets new state instructions in prompt
```

**Transitions are triggered by the orchestrator reading projections, NOT by effects directly.** The effect goes into the log, projections rebuild, the orchestrator sees the new state, and decides to transition.

### 3.4 Human Intervention Flow
```
1. Human GETs an agent to observe its context
2. Human POSTs text to the agent's URL
3. Text is appended to agent context
4. Agent's next response incorporates human instruction
5. Effects parsed, validated, appended
6. Pipeline continues
```

### 3.5 VM Lifecycle Flow
```
1. Provisioner agent sees pending jobs in queue
2. Provisioner uses bash → vastai CLI → creates instance
3. VMAllocated effect appended to event log
4. VM boots, VM agent starts, reports health
5. VM agent pulls job, runs inference, QA's output
6. VM agent uploads artifact to B2 via bash
7. VM agent reports JobCompleted or JobFailed via bash (curl)
8. VM agent monitors overseer; if gone → self-destruct (~15 min)
9. Pipeline may explicitly deallocate VM when done
```

---

## 4. State Machine

### 4.1 States

```
[INIT] → [SCRIPT] → [AUDIO+VIDEO parallel] → [ASSEMBLY] → [DONE]
           ↑______________|___________|
                    (retry loops)
```

- **INIT:** No script exists. Orchestrator runs scenario agent.
- **SCRIPT:** Script exists. Orchestrator runs audio + video agents (parallel or sequential).
- **AUDIO+VIDEO:** Jobs in queues. Provisioner provisions VMs. Workers produce clips.
- **ASSEMBLY:** All clips ready. Assembly agent merges into final MP4.
- **DONE:** Output exists. Pipeline stops.

**Hard rule:** The orchestrator is free to transition between any states based on the actual state summary. No hardcoded stage sequence.

### 4.2 State Mutation Rules
- State changes ONLY via orchestrator decision
- Orchestrator decides based on projected read-models
- Agents produce effects → projections rebuild → orchestrator reads → orchestrator transitions

### 4.3 Retry Mechanics
- Failed jobs are requeued with `JobRequeued` effect containing QA comments
- Agents see requeued jobs with comments in their state summary
- Agents decide whether to retry, modify, or escalate
- **Hard rule:** No automatic retry count limits. Agent decides when to stop.

---

## 5. Communication Contracts

### 5.1 Agent ↔ Pipeline
- **Protocol:** Plain text over HTTP (or direct LLM call)
- **Request:** Text prompt (natural language)
- **Response:** Text (natural language, may contain effect markers)
- **Stateful:** Agents accumulate context across turns. The pipeline passes a thread_id to the LLM provider. Prior context persists.

### 5.2 Agent ↔ Agent
- **No direct communication.** Agents only talk to the pipeline.
- The pipeline passes context from one agent to another via state summary.
- **Hard rule:** Agents never message each other directly.

### 5.3 Pipeline ↔ VM Workers
- **Protocol:** Plain text over HTTP (GET / health, POST / jobs)
- **Job submission:** Text describing the work
- **Result retrieval:** VM uploads to B2. Pipeline reads from B2.
- **Hard rule:** Pipeline never pulls artifacts from VM directly.

---

## 6. Dynamic Prompt Construction

### 6.1 System Prompt = Persona + State Instructions + Context

```
System Prompt = Base Persona + State Instructions + Context Window
```

| Component | Source |
|---|---|
| **Base Persona** | Hardcoded identity ("You are the audio agent...") |
| **State Instructions** | Injected by state machine based on current state |
| **Context Window** | State summary + feedback + exception context |

**The state machine affects agents ONLY by changing the system prompt.** It does not restrict tools, block actions, or override decisions.

### 6.2 State Disclosure Strategy

**Default: Implicit.** The agent receives new instructions without being told "the state changed."

**Escalation:** If the agent produces wrong effects:
1. Reject with feedback: "Your instructions have changed."
2. Next turn APPENDS explicit disclosure: "Current state: AUDIO_PRODUCTION."
3. Does NOT clear conversation history.

### 6.3 Prompt Parameterization

Drawing from Policy-Parameterized Prompts research, each prompt is:

| Component | Content |
|---|---|
| **T — Task/Persona** | Base identity |
| **M — Memory** | Dialogue history |
| **D — Domain Knowledge** | Skills, reference material |
| **R — Rules/Constraints** | State-specific rules (in prompt only) |
| **W — Weights/Priorities** | Emphasis on responsiveness, evidence, non-repetition |

The state machine selects **R** and adjusts **W**. **T**, **M**, **D** are stable.

---

## 7. Hard Principles

### 7.1 Truth
- **Event log is the only source of truth for state transitions.**
- **B2 is ground truth for artifacts.** VMs are ephemeral.
- **State summary must be truthful.** No caching, no optimistic updates.

### 7.2 Agency
- **Orchestrator decides which agent runs; agents decide what to do.**
- **No procedural logic overriding agent decisions.** The code does not second-guess the agent.
- **The state machine influences ONLY the system prompt.** It gives direction. It does not constrain actions.
- **No timeout-based kills in the pipeline.** Agent turns run until the agent decides to stop.

### 7.3 Communication
- **Plain text only between agents and pipeline.** No JSON, no schemas in prompts.
- **GET / and POST / only.** No special endpoints.
- **Text is accidental structure.** Structure is extracted by parser, not demanded from agent.

### 7.4 Failure
- **Never silently fail.** If stuck, report to human immediately.
- **Kill everything on unrecoverable failure.** Destroy all VMs, stop all processes.
- **Honest failure:** Missing dependency → pipeline fails. No graceful degradation.

### 7.5 Isolation
- **Each pipeline run is self-contained.**
- **Agent memory persists across runs.** Accumulated wisdom is valuable.
- **No global mutable state.** Everything flows through projections.

### 7.6 Configuration
- **No environment variables.** All configuration is hardcoded.
- **External credentials are hardcoded or read from files.** Vast.ai key, B2 credentials, LLM API key — read from known file paths, not env vars.
- **No CLI arguments.** The entry point is `python run_pipeline.py`. Brief is read from stdin or a hardcoded file.
- **Hardcoded model:** `deepseek-v4-flash` everywhere.

### 7.7 VMs
- **Vast.ai CLI is source of truth.** No wrapper abstractions.
- **Start with one VM, confirm health, then escalate.**
- **VMs self-destruct when overseer gone.** ~15 min deadman. This is a safety mechanism, not an agent decision.
- **Upload to B2 immediately.** VM may vanish any moment.

### 7.8 Tools
- **The agent's ONLY tool is `bash`.**
- **No internal tools.** No `write_file`, no `queue_job`, no `check_b2`.
- **Everything goes through bash:** file writes, API calls, ffmpeg, Vast CLI, B2 uploads.
- **The state machine does not restrict tools.** It guides the agent via prompts.

### 7.9 Quality
- **Pyright zero errors.** No `Any` types.
- **Ruff clean.** Mandatory static check gate.
- **No mocks, stubs, placeholders.** Every bash command calls real services.
- **No premature optimization.** Produce movies, not perfect architecture.
- **"Produce movies, not perfect architecture" means:** Ship a working pipeline with a flat state machine before designing a complex one. It does NOT mean skip QA or use fake video.

---

## 8. Research Context

| Paper / Framework | Key Insight | Relevance |
|---|---|---|
| **Policy-Parameterized Prompts** (arXiv:2603.09890) | Prompts are actions; 5 components (T/M/D/R/W) | Validates prompt-as-action |
| **StateAct** (arXiv:2410.02810) | Self-prompting + chain-of-states | Self-awareness mechanism |
| **ReflAct** (arXiv:2505.15182) | Reflect on state-goal distance | Grounds behavior |
| **StateFlow** (arXiv:2403.11322) | LLM workflows as state machines | Per-state prompts |
| **MetaAgent** (arXiv:2507.22606) | FSM-driven multi-agent | FSM orchestration |
| **Statewright** (GitHub) | State machine guardrails controlling tools | Tool-level enforcement (NOT used — we use prompt-only) |
| **LangGraph** (LangChain) | StateGraph with typed state | Production orchestration |
| **Pydantic AI** | `@agent.system_prompt` decorator | Runtime prompt construction |

---

## 9. Glossary

| Term | Definition |
|---|---|
| **Effect** | A typed, immutable record of a state change proposed by an agent |
| **Event** | An effect that has been validated and appended to the event log |
| **Projection** | A read model (OTIO, queue, VM registry) rebuilt deterministically from events |
| **Agent** | An LLM with persistent memory, reasoning, and the ability to produce effects |
| **Worker** | A GPU VM running an LLM agent that performs inference (TTS/LTX) |
| **Orchestrator** | The agent that decides which agent runs next |
| **OTIO Gate** | The sole agent authorized to mutate the timeline |
| **State Summary** | A human-readable string describing current pipeline state |
| **Skill** | A loadable prompt fragment giving advanced params for a domain |
| **B2** | Backblaze B2 — ground truth artifact storage |
| **State Machine** | Lightweight FSM per pipeline that constructs agent prompts from current state |
| **Effect Parser** | Component that extracts typed `Effect` objects from raw agent text via instructor |
| **instructor** | Python library for structured LLM extraction |
| **Re-ask** | instructor's feedback loop: validation error → ask model to correct |
| **ClarificationRequest** | Effect type emitted when parsing exhausts all retries |

---

*Abstract version: 2026-05-17 v2*
