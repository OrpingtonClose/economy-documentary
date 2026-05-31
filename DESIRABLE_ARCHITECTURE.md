> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Desirable Architecture — Documentary Pipeline v4

> Distilled from multi-day architecture discussions across ~15 sessions and 4 skill files. This document captures the design principles the user found desirable, not necessarily the current implementation.

---

## 1. Core Philosophy

### Agents Talking to Each Other
The system is framed as **"agents talking to each other to perform a task defined as a graph of effect types that are stages of a state machine."** The state machine is mechanized by the text agents produce to each other, parsed via the `instructor` library into typed algebraic effects.

### OTIO Is the State
**"OTIO is the state-machine-state."** The OpenTimelineIO timeline is the single materialized state of the movie. It must be **gatekept fiercely** — only the OTIO gate agent writes to it. All changes are recorded as immutable events, making the workflow auditable and replayable.

### Event Log Is Source of Truth
The JSONL event log is the **only** source of truth. OTIO and job queues are read models rebuilt from events by projection handlers.

### Text Is Accidental Structure
**"You just send text to the agent — structure there is accidental."** Agents write naturally. The pipeline parses. No JSON schemas in prompts, no structured output demands, no forcing structure onto agent communication.

### Agent Decides, Code Does Not Constrain
**"The entire worker↔orchestrator interface is plain text. No JSON. No schemas. Intelligence lives in the agent."** The agent decides within agentic reasoning — not algorithmically. Don't hardcode polling intervals, timeout-based logic, or procedural constraints. Let the agent reason about what to do next.

### B2 Is Ground Truth; VMs Are Ephemeral Compute
**Every artifact is uploaded to Backblaze B2 the instant it is created.** VMs last ~1 hour. On restart, re-provision. Never persist worker URLs to OTIO. On pipeline restart, read scene state from B2 + OTIO, not from any VM.

---

## 2. Communication Layer

### Text-Only on the Wire
Agents communicate exclusively via **natural language text**. No JSON, no structured output demands in prompts, no function calls for scenario generation. The scenario generation process is **"just an ornate LLM call"** — a pure LLM call with no need for function calls.

### No Files Pass Between Agents
The scenario agent refines its scenario **on disk** in text form. When adequate, it sends **the entire text** of the scenario to the OTIO agent. No file paths, no shared directories — text flows through graph message passing.

### Minimal HTTP Surface — Only GET / and POST /
HTTP agents expose **only GET / and POST /**. No special endpoints based on what you want to do.

- **GET /** — probing while not leaving trace in agent memory (health/status)
- **POST /** — posting orders/configs; leaves trace as instruction in agent context

This minimal surface allows **external beings to instrument the pipeline** (like you). "This must enable infallibility of pipeline on virtue of external, TOTAL, intervention being possible."

### Plain-Text Health Checks
Worker health returns raw strings like `ok capability=audio`. Simple string parsing — no JSON conversion.

---

## 3. Effect System

### Algebraic Effect Types
Every state change is an **algebraic effect** parsed from agent text. Effect types are:

| Category | Effects |
|---|---|
| **Agent** | `UpdateScript`, `GenerateNarrationAudio`, `RenderVideoSegment`, `MergeIntoOTIO` |
| **Worker** | `JobQueued`, `JobStarted`, `JobCompleted`, `JobFailed`, `VMAllocated`, `VMDeallocated`, `VMProvisionFailed` |
| **QA** | `QAPassed`, `QAFailed`, `JobRequeued`, `JobQuestionReceived`, `JobQuestionAnswered` |
| **System** | `NoOp`, `ExecuteRawBash` |
| **Observation** | `VMObserved` (for tracking VM runtime status) |

### Instructor Parsing
Agent free text is parsed into typed effects using **instructor + Pydantic**. The parser uses discriminated unions with strict validators. If instructor exhausts retries, the pipeline asks the agent for clarification rather than guessing.

**"Implied types in text"** — types implied in text as a concept, LLM-reliant. Use `deepseek-v4-flash` for instructor.

### Progressive Disclosure via Skills
Base prompts never prescribe advanced techniques (camera jargon, quality words, tag soup) as desirable. Agents can `LOAD_SKILL: video-generation` to get advanced params (grid constraints, VRAM tables, motion keywords) when needed.

---

## 4. Orchestration

### Pydantic-Graph for State Machine
The dependency graph defines allowed transitions between effects and pipeline stages. **Pydantic-graph** provides:
- Granular state snapshots before every node
- Exact-moment resume via `iter_from_persistence()`
- Parallel execution via Fork/Join or `asyncio.gather()` within nodes

### Parallel Media Production
**"Media production must be parallel. One-VM-per-media is an exception."** Audio and video agents run concurrently. The graph handles this via broadcast edges or internal `asyncio.gather()`.

### Single Entry Point
There is **one entry path** to the pipeline. No CLI flags, no environment-variable fallbacks. Configuration is passed as parameters or read from files. If Vast.ai is not configured, the pipeline **must not work** — honest failure, not silent degradation.

### No Constraint on Agents
**"You do not constraint the agent."** The agent decides within **agentic reasoning**, not algorithmically. The agent polls intelligently, not procedurally. Don't hardcode polling intervals or timeout-based logic — let the agent reason about what to do next.

### Maintainer Agent Pattern
Upon exception, the code calls the **maintainer agent** and tells it what happened. This is an **enshrined pattern** — whenever a type of error is made in a unit, tell the agent responsible.

---

## 5. VM & Worker Model

### Vast.ai CLI Is Source of Truth
**"The `vastai` CLI is the source of truth. Python wrappers are thin passthroughs only."** Agents do not need Python abstractions — they need the right `vastai` command and the judgment to use it.

- `vastai search offers --type on-demand --raw`
- `vastai create instance <offer_id> --image <docker_image> --disk <gb> --ssh --direct`
- `vastai show instance <vm_id> --raw`
- `vastai show instances --raw`
- `vastai destroy instance <vm_id>`

### Agents Have Tools — Bash + Vast CLI
The provisioner agent is given **bash tools** so it can provision workers itself. No convenience wrappers — if a wrapper tool fails, remove it and use the raw CLI. Just give it a bash command tool and don't multiply tools.

### One Path for Workers
There is **one path** for worker deployment. No "auto-provision vs BYO" dual paths. The `VAST_API_KEY` is a hard preflight check. If it fails, the pipeline fails honestly.

### VM Lifecycle in Events
VM status is tracked through events:
- `VMAllocated` — VM provisioned
- `VMObserved` — runtime status observed (loading, running, etc.)
- `VMDeallocated` — VM destroyed
- `VMProvisionFailed` — provisioning failed

The state summary includes the latest observed status for each VM so the orchestrator can make informed decisions (e.g., wait while loading, not call provisioner repeatedly).

### VM Agent Self-Destructs
The VM agent (installed on each worker VM) monitors its overseer. If the overseer is gone, it self-destructs: **"The overseer is gone, I should die."** Make it ~15 minutes. This is a deadman switch to prevent orphaned VMs.

### Start With One VM, Then Escalate
**"They must ALWAYS start with one VM, then escalate."** If the VM is not healthy, troubleshoot until it is. If it is healthy and confirmed working, raise more. Never provision multiple VMs before confirming the first works.

### Workers Are LLM-Backed Agents
**"It is a LLM, how can't it be otherwise."** Media agents on VMs are not dumb inference endpoints. They bootstrap, self-monitor, escalate, run QA, retry failures, and self-destruct. They are full agents with reasoning capabilities.

### Personal Job Queues
Agents should have **personal job queues** that the provisioner sees and provisions VMs on the basis of. They look at what is returned from those jobs and perform QA. If they don't like it, it is put into the queue again with comments applied.

### Worker Specifications

| Worker | VRAM | GPU | Disk | Budget |
|---|---|---|---|---|
| **TTS (Audio)** | 8GB+ | Any CUDA (RTX 3070, GTX 1070) | 64GB | ~$0.05–0.30/hr |
| **LTX (Video)** | 80GB+ | A100 SXM4, H200, RTX PRO 6000 WS | 150GB+ | ~$1.50–4.00/hr |

- **Do NOT trust Vast.ai disk metadata** — always over-provision.
- **One worker per role** — TTS worker for audio, LTX worker for video. Do not share.
- **Concurrency limit** — max 3 concurrent Vast.ai API calls.

### Ownership Guard
Only destroy VMs registered in `_owned_instances.json` for this pipeline run. Never destroy VMs you did not create — they may belong to other users on the shared account.

---

## 6. Configuration & Constraints

### No Environment Variables
Configuration must be passed as **parameters or file reads**, not environment variables. API keys are read from `~/api_keys/` files, not `os.environ`. No CLI args either — "I will not give you easy options to destroy the pipeline."

### No Timeouts
**No timeouts anywhere.** Architecture guard enforces this. Agents run until they complete or fail. If a VM is stuck, an agent observing the process decides it must be killed — agentic reasoning, not algorithmic timeout.

**What to do instead of timeouts:**
- Let it hang.
- The operator will intervene manually and stop it.
- If you need periodic status checks, use `time.sleep()` for **polling** (checking "are you done yet?") but NEVER as a **deadline** ("stop after N seconds").

### Type Safety
- **Pyright clean** — zero type errors in pipeline code
- **No `Any` types** — use instructor for implied types in text
- Pydantic models with strict validators for all effects
- **Ruff static check** as mandatory gate

### Hardcoded Models
**"The pipeline MUST have hardcoded models."** Use `deepseek-v4-flash` specifically. Don't make models configurable at runtime — hardcode them.

---

## 7. Prompt Design

### Simple Present-Tense Prose
Video prompts use **simple present-tense prose with one clear motion per clip**. Advanced techniques (camera jargon, quality words, tag soup) are demoted as too failure-prone.

### Rich Concrete Prose, No Structured Output
Agent prompts use rich concrete prose. No structured output demands. No JSON schemas in prompts. Agents write naturally; the pipeline parses.

### Skill Fragments
`{skill_fragment}` placeholders in prompts are replaced with skill discovery text at runtime. Skills live in `skills/` directories and are loaded on demand.

### VM Recommendations Embedded in Context
VM provisioning recommendations must be **embedded into the agent's context** or as a skill, not looked up externally. The agent must intelligently understand what is needed — perhaps with MCP/API connections to Brave, Exa, Firecrawl for research.

---

## 8. Layers (Bottom to Top)

```
┌─────────────────────────────────────────┐
│  Text Layer — raw agent communication   │
├─────────────────────────────────────────┤
│  Parsing Layer — instructor + Pydantic  │
│  (turns text into typed effects)        │
├─────────────────────────────────────────┤
│  Graph + Engine Layer — pydantic-graph  │
│  (orchestration, satisfiability, resume)│
├─────────────────────────────────────────┤
│  Projection Layer — event → OTIO/queue  │
│  (read models rebuilt from events)      │
├─────────────────────────────────────────┤
│  OTIO Layer — materialized movie state  │
│  (single source of truth, gatekept)     │
├─────────────────────────────────────────┤
│  Event Store — JSONL append-only log    │
│  (immutable, auditable, replayable)     │
├─────────────────────────────────────────┤
│  Artifact Store — Backblaze B2          │
│  (ground truth for all media files)     │
└─────────────────────────────────────────┘
```

---

## 9. Failure Handling & Intervention

### Never Silently Fail
**"You will never silently fail without returning to me."** When you declare failure and can't do anything, **kill everything immediately**. Destroy all VMs, stop all processes.

### Deadman Switch
When the pipeline fails, VMs get destroyed. The VM agent's self-destruct mechanism is the first line of defense. A separate deadman switch process that must be pinged periodically or else it destroys all VMs is the second line.

### External Total Intervention
The architecture must enable **infallibility of the pipeline on virtue of external, total intervention being possible**. You personally can talk to the agents as the pipeline happens. You must be **proactive, not reactive**. The agent doesn't ask you — you consult it, seeing what is happening without being burdened with awful prompts.

### Agents Optimize on Certainty
Agents have the tendency to horribly troubleshoot. They must only optimize on grounds of **CERTAINTY** — after they gather memory of successful deployments.

### Each Run Is Self-Contained
**"Each run is completely self-contained — no scampering for VMs."**
- Each run starts with ZERO registered workers
- The agent provisions exactly the workers it needs for THIS run
- At the end of the run (or on fatal error), ALL VMs are destroyed
- The registry is in-memory only — never written to disk

### The Pipeline Must Produce Real Movies
A placeholder is a movie that doesn't exist. Every tool in the pipeline must:
1. Call a real service (LLM, GPU worker, ffmpeg, B2)
2. Return real data (scenes with real narration text, actual WAV bytes, real MP4 frames)
3. Fail loudly if the service is unavailable (raise, don't fake)

**Any code that returns fake data to "keep the pipeline moving" is a bug, not a feature.**

### No Recovery-Breaking Shortcuts
- No `except Exception: pass` that swallows errors
- No early exits that bypass retry
- No hardcoded fallbacks that bypass real services

---

## 10. State Management

### OTIO In Memory
**"What if OTIO wasn't saved to disk at all and was in-memory?"** OTIO should be explicitly state-managed — safeguarded fiercely. In-memory OTIO with explicit save points, not implicit disk writes.

### No Global Mutable State
Global mutable state is evil. No `_recovery_shell`, `_shared_otio_manager`, `_vm_agent`. Everything is passed through the graph context.

### Registry Must Not Be Persistent
**"The registry MUST NOT BE persistent."** The agent registry is discovered at runtime, not stored across sessions.

### Pipeline Is Self-Contained
**"The pipeline is SELF-CONTAINED."** No external dependencies that aren't explicitly declared and checked.

---

## 11. Tracing & Observability

### CPython sys.monitoring
Use **CPython `sys.monitoring`** for extensive auto-tracing. Don't write code for tracing — use the CPython tracing thing. SQLite for log storage. The datastore must be performant.

### Cheat Skill (/cheat)
Maintain a **"/cheat" skill** that checks for architecture violations:
- No timeouts added
- No mocks, stubs, or dummy data
- No env vars used for config
- Only GET / and POST / endpoints
- VMs aren't persistent across sessions
- Registry isn't persistent
- No recovery-breaking shortcuts

---

## 12. Anti-Patterns (Explicitly Rejected)

| Anti-Pattern | Why Rejected |
|---|---|
| Scenario agent produces JSON | "The scenario agent DOES NOT PRODUCE JSON" — pure text only |
| Function calls for scenario | "No need for function calls" — ornate LLM call suffices |
| OTIO written directly by scenario | "OTIO must be gatekept fiercely" — only gate agent writes |
| Two paths for workers (auto/BYO) | "There MUST BE ONE PATH" — honest failure |
| Lightweight VMs for tracing | Not on critical path — tracing is separate concern |
| CLI args / env vars for config | Passed as parameters or file reads |
| Timeouts | "No timeouts ever" — architecture guard |
| `Any` types in codebase | Use instructor for implied types |
| Camera jargon in base prompts | Too failure-prone; demoted to skill |
| Convenience wrapper tools | "Just give it bash" — remove wrappers |
| Structured endpoints (POST /configure) | Only GET / and POST / — no special endpoints |
| Health check tools in pipeline | Only vast CLI based checks |
| Procedural polling (every 15s) | Agent intelligently polls |
| Silent failure | "Never silently fail" — always report |
| Global mutable state | Evil — pass through graph context |
| Persistent registry | "Registry MUST NOT BE persistent" |
| Workers as dumb inference endpoints | "It is a LLM, how can't it be otherwise" |
| Multiple VMs before confirming first | "Start with one, then escalate" |
| Fake data / placeholders | "A placeholder is a movie that doesn't exist" |
| Premature optimization | "DO NOT TAKE EFFORT TO OPTIMIZE" |
| Assuming knowledge without search | "Research first, act second" |
| Writing worker URLs to OTIO | VMs are ephemeral; URLs die in ~1 hour |
| Destroying unowned VMs | May kill another user's run |
| Trusting Vast.ai disk metadata | Often wrong; always over-provision |
| Hardcoding offer IDs | Offers change minute-by-minute |

---

## 13. Technology Stack

| Layer | Technology |
|---|---|
| LLM API | DeepSeek (`deepseek-v4-flash`) via OpenAI-compatible client |
| Agent framework | `pydantic-deepagents` (pydantic-ai + pydantic-graph) |
| Graph orchestration | `pydantic-graph` v1 (BaseNode) for persistence + resume |
| Effect parsing | `instructor` + Pydantic discriminated unions |
| Timeline state | OpenTimelineIO (`opentimelineio==0.18.1`) — in-memory, explicitly managed |
| Artifact storage | Backblaze B2 (ground truth; upload immediately) |
| VM provisioning | Vast.ai CLI embedded as agent bash tool |
| Workers | GPU VMs on Vast.ai with LLM-backed agents |
| TTS | Qwen3-TTS (`qwen-tts` package, NOT Coqui) |
| Video | LTX-2.3 (~48GB FP16, ~24GB mxfp8_block32) |
| Package manager | `uv` |
| Type checking | `pyright` (zero errors policy) |
| Linting | `ruff` (mandatory gate) |
| Tracing | CPython `sys.monitoring` + SQLite |
| Research | Brave Search, Exa, Perplexity, Firecrawl (mandatory before guessing) |
| Codebase analysis | `agentralabs/codebase`, `wirelessr/codebase-analyzer-agent` |

---

## 14. Key Quotes (User's Voice)

> "Agents talking to each other to perform a task defined as a graph of effect types defined stages of a state machine."

> "OTIO is the state-machine-state."

> "The scenario agent DOES NOT PRODUCE JSON."

> "OTIO must be gatekept fiercely."

> "There MUST BE ONE PATH."

> "No timeouts ever."

> "You do not constraint the agent."

> "The agent decides within agentic reasoning — NOT ALGORITHMICALLY."

> "The overseer is gone, I should die."

> "You will never silently fail without returning to me."

> "When you declare failure and can't do anything you KILL EVERYTHING IMMEDIATELY."

> "This must enable infallibility of pipeline on virtue of external, TOTAL, intervention being possible."

> "You just send text to the agent — structure there is accidental."

> "It is a LLM, how can't it be otherwise."

> "They must ALWAYS start with one VM, then escalate."

> "The registry MUST NOT BE persistent."

> "The pipeline is SELF-CONTAINED."

> "I will not give you easy options to destroy the pipeline."

> "A placeholder is a movie that doesn't exist."

> "B2 Is Ground Truth; VMs Are Ephemeral Compute."

> "Agent Decides, Code Does Not Constrain."

> "DO NOT TAKE EFFORT TO OPTIMIZE."

> "Research first, act second."

---

*Last distilled: 2026-05-17*
