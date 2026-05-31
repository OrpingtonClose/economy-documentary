> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Canonical Architecture — Documentary Pipeline

> What the user actually wants. Distilled from ~15 sessions, 4 skill files, and hundreds of architectural corrections.
> **Aligned with ARCHITECTURE_V7.**

---

## The One-Sentence Architecture

**Agents talk to agents in plain text. Their conversation is parsed into algebraic effects that append to an event-sourced log in EventStoreDB. A Global State Agent serves projections to all other agents via GET / only. VMs are ephemeral compute; B2 is ground truth. The human can intercept any agent at any time via HTTP.**

---

## 1. Agents First, Everything Else Second

The fundamental unit is the **agent** — a reasoning LLM with memory, not a function, not a tool, not a procedure.

- **Scenario agent** writes and refines narration in plain text. When satisfied, sends the entire text to the Assembly Agent.
- **Audio agent** formulates TTS jobs, puts them in a queue, QA's results, requeues with comments if bad.
- **Video agent** formulates LTX jobs, puts them in a queue, QA's results, requeues with comments if bad.
- **Assembly agent** gatekeeps OTIO fiercely — the ONLY being allowed to write the timeline. Muxes final MP4.
- **Provisioner** is an LLM agent with bash, research, and memory tools. It is the most intelligence-requiring part of the architecture. Provisions VMs using bash + `vastai` CLI. No wrapper tools.
- **Global State Agent** serves projections via GET / only. No agent can influence it. The middleman between EventStoreDB and all other agents.
- **Maintainer agent** gets called when exceptions happen. Enshrined pattern. Ephemeral — started by operator, shut down after repair.
- **VM agent** (on each GPU VM) boots, self-monitors, runs jobs, QA's output, retries failures, and self-destructs if the control plane disappears (~15 min deadman).

**"It is a LLM, how can't it be otherwise."** Every worker is an agent. There are no "dumb inference endpoints."

---

## 2. Text Is the Protocol

### Communication Rules
1. **Agents speak natural language to each other.**
2. **No JSON in prompts.** No structured output demands. No function calls for scenario generation.
3. **No files pass between agents.** Scenario agent refines text on disk, then sends the full text via message passing.
4. **HTTP surface: GET / and POST / only.**
   - `GET /` → probe, no trace left in agent memory
   - `POST /` → instruction, leaves trace in agent context
5. **Plain-text health checks.** `ok capability=audio`. No JSON parsing.
6. **Global State Agent is GET / only.** No POST /. No agent can instruct it.

**"You just send text to the agent — structure there is accidental."**

### Parsing Layer
- **instructor + Pydantic** parses agent text into typed algebraic effects.
- Category-conditioned: the parser knows the agent's role and only extracts permitted effect kinds.
- Discriminated unions with strict validators.
- If instructor exhausts retries, ask the agent for clarification. Don't guess.
- **"Implied types in text"** — LLM-reliant type inference. Use `deepseek-v4-flash` for instructor.

---

## 3. State: Event-Sourced OTIO

```
Text (agents talking)
    ↓
instructor parses → typed Effect
    ↓
append to EventStoreDB stream run-{run_id} (immutable, only source of truth)
    ↓
Global State Agent rebuilds projections from events
    ↓
agents GET / from GSA to receive OTIO + job queues + VM state
    ↓
OTIO is the materialized movie state
```

### Key Invariants
- **EventStoreDB stream is the ONLY source of truth.**
- **OTIO is a read model.** Rebuilt from events by the Global State Agent.
- **Job queues are read models.** Rebuilt from events by the Global State Agent.
- **OTIO is gatekept fiercely.** Only the Assembly Agent writes it.
- **In-memory OTIO with explicit save points.** No implicit disk writes.
- **Global State Agent is the sole read path.** No agent reads EventStoreDB directly except the Provisioner (which subscribes to QueueJob effects).

### Effect Types (V7 Names)
| Category | Examples |
|---|---|
| Script | `ScriptDrafted`, `UpdateScript`, `DeleteScene`, `ReorderScenes` |
| Work | `QueueJob`, `JobStarted`, `JobCompleted`, `JobFailed`, `JobApproved`, `JobRequeued` |
| VM | `VMAllocated`, `VMObserved`, `VMDeallocated`, `VMProvisionFailed` |
| QA | `AudioMeasured`, `VideoMeasured`, `DurationAdjusted` |
| OTIO | `MergeIntoOTIO` |
| System | `PipelineStarted`, `PipelineComplete`, `PipelineAborted`, `NoOp`, `ExecuteRawBash` |

---

## 4. Orchestration: Emergent Phases + Human Override

### No Central Orchestrator
There is no orchestrator agent. Pipeline phases are **emergent**, not enforced:

| Phase | Emergent Condition |
|---|---|
| **INIT** | No `PipelineStarted` effect |
| **SCRIPT** | `PipelineStarted` exists, OTIO has unfilled slots |
| **AUDIO_RECONCILE** | OTIO has dirty audio blocks |
| **VIDEO_PRODUCTION** | All audio clean, video slots unfilled |
| **ASSEMBLY** | All slots filled, final MP4 missing |
| **DONE** | Final MP4 exists and validates |
| **ABORTED** | `PipelineAborted` emitted |

No code enforces transitions. They emerge from what agents do.

### Human Override
**"This must enable infallibility of pipeline on virtue of external, TOTAL, intervention being possible."**
- The human can `GET /` any agent to see what it sees.
- The human can `GET /` the Global State Agent to see the complete pipeline state.
- The human can `POST /` any agent to give it new instructions.
- The human is **proactive**, not reactive. The agent doesn't ask — the human consults.
- The human can kill the pipeline, destroy VMs, redirect agents at any moment.

---

## 4.5 Agentic Timeouts

This architecture handles success and failure criteria under the **Agentic Timeouts** paradigm:

1. **Semantic Success Criteria**: Creative and quality success criteria (such as script coherence, pronunciation, and audio-visual artistry) are evaluated by an **LLM Eval Judge** (via DeepSeek v4-flash) rather than hardcoded logic.
2. **Absolute Ban on Timeouts (Forbidden by Default)**: Standard code timeouts, timer loops, and thread kills are strictly forbidden throughout the codebase (unless absolutely unavoidable for low-level socket-connect health probes to prevent TCP hangs).
3. **No Automatic Stale-State Detection**: The pipeline does not run automated timers or background watchers to reap jobs. If a process, VM, or agent hangs, it runs indefinitely until:
   - The **Provisioner Agent** (using its bash, research, and memory tools) queries VM/job projections, diagnoses a stall (e.g., OOM, driver crash), and decides to destroy the VM.
   - Or the **Human Operator** proactively checks state via the Global State Agent (`GET /`) and manually intervenes (escalating, restarting, or killing) to recover.

---

## 5. VM Model: Ephemeral Compute, Agentic Lifecycle

### Vast.ai CLI Is Source of Truth
```bash
vastai search offers --type on-demand --raw
vastai create instance <offer> --image <img> --disk <gb> --ssh --direct
vastai show instance <id> --raw
vastai destroy instance <id>
```
- Python wrappers are thin passthroughs only.
- Agents get bash tool + CLI commands in prompt.
- **Max 3 concurrent Vast.ai API calls.**

### Worker Specifications
| Role | VRAM | Disk | GPU | Budget |
|---|---|---|---|---|
| TTS (audio) | 24GB+ | 100GB | RTX 4090 / A6000 | ~$0.05–0.80/hr |
| LTX (video) | 48GB+ | 200GB+ | RTX A6000 | ~$1.20–4.00/hr |

- **Don't trust Vast.ai disk metadata** — always over-provision.
- **One worker per role.** TTS for audio, LTX for video. No sharing.

### VM Lifecycle
1. **Start with ONE VM.** Confirm it's healthy. Then escalate.
2. **VM agent** installs everything, runs jobs, QA's output, retries failures.
3. **Deadman switch:** If control plane gone for ~15 min, VM agent self-destructs.
4. **Ownership guard:** Only destroy VMs the Provisioner created for this run.
5. **Upload to B2 immediately** after creation. VM may vanish before next step.

### Provisioning Is Deterministic
- **Agent reasoning.** The Provisioner is an LLM agent that learns from failures across runs.
- **No procedural polling.** The Provisioner reacts to HTTP requests and EventStoreDB subscriptions.
- **No timeouts.** If a VM is stuck, the observing agent decides to kill it — via reasoning, not algorithm.
- **Agents optimize on CERTAINTY** — after gathering memory of successful deployments.

---

## 6. Configuration: Hardcoded, File-Based, Honest

### Rules
- **No environment variables.** Read from `~/api_keys/` files.
- **No CLI arguments.** "I will not give you easy options to destroy the pipeline."
- **Hardcoded models.** `deepseek-v4-flash` everywhere. No runtime model selection.
- **Config passed as parameters or file reads.**

### Failure Model
- **Honest failure.** If Vast.ai is not configured, pipeline fails. No silent degradation.
- **One path.** No "auto-provision vs BYO" fallback.
- **Never silently fail.** If you can't proceed, return to human immediately.
- **Kill everything on failure.** Destroy all VMs, stop all processes.

---

## 7. Anti-Patterns (The "Don't" List)

| Don't | Because |
|---|---|
| Add timeouts | "No timeouts ever." Agent decides when to intervene. |
| Use JSON between agents | "Structure is accidental." Plain text only. |
| Make scenario agent produce JSON | "The scenario agent DOES NOT PRODUCE JSON." |
| Write OTIO from scenario agent | "OTIO must be gatekept fiercely." Only Assembly Agent writes. |
| Create convenience wrapper tools | "Just give it bash." Remove wrappers that fail. |
| Add structured endpoints | Only GET / and POST / — nothing else. |
| Persist registry across runs | "Registry MUST NOT BE persistent." |
| Reuse VMs from previous runs | Each run starts with zero workers. |
| Return fake data on failure | "A placeholder is a movie that doesn't exist." |
| Use global mutable state | `_recovery_shell`, `_shared_otio_manager` = evil. |
| Optimize prematurely | "DO NOT TAKE EFFORT TO OPTIMIZE." Produce movies. |
| Assume knowledge | "Research first, act second." Brave/Exa/Perplexity mandatory. |
| Trust Vast.ai disk metadata | Always over-provision disk. |
| Workers as dumb endpoints | "It is a LLM, how can't it be otherwise." |
| Multiple VMs before confirming first | "Start with one, then escalate." |
| Silent failure | "You will never silently fail without returning to me." |
| `except Exception: pass` | Swallowing errors breaks recovery. |
| Build a state machine | Phases are emergent, not enforced. |
| Make the Global State Agent accept POST / | It is GET / only. No agent can influence it. |

---

## 8. Technology Stack

| Concern | Choice |
|---|---|
| LLM | DeepSeek `deepseek-v4-flash` (hardcoded) |
| Agent framework | `pydantic-deep` (pydantic-ai extension) |
| Event store | EventStoreDB (stream per run) |
| Effect parsing | `instructor` + Pydantic discriminated unions |
| Timeline | OpenTimelineIO 0.18.1 (in-memory, explicitly managed) |
| Artifacts | Backblaze B2 (`b2://bucket/runs/{run_id}/...`) |
| VMs | Vast.ai CLI (bash tool, no wrappers) |
| TTS | Qwen3-TTS (`qwen-tts` package) |
| Video | LTX-2.3 (~48GB FP16 / ~24GB mxfp8) |
| Package mgr | `uv` |
| Type check | `pyright` (zero errors policy) |
| Lint | `ruff` (mandatory gate) |
| Research | Brave, Exa, Perplexity, Firecrawl (mandatory) |

---

## 9. The User's Voice (Canonical Quotes)

> "Agents talking to each other to perform a task defined as a graph of effect types that are stages of a state machine."

> "OTIO is the state-machine-state."

> "You just send text to the agent — structure there is accidental."

> "The agent decides within agentic reasoning — NOT ALGORITHMICALLY."

> "You do not constraint the agent."

> "It is a LLM, how can't it be otherwise."

> "B2 Is Ground Truth; VMs Are Ephemeral Compute."

> "Agent Decides, Code Does Not Constrain."

> "The overseer is gone, I should die."

> "You will never silently fail without returning to me."

> "When you declare failure and can't do anything you KILL EVERYTHING IMMEDIATELY."

> "This must enable infallibility of pipeline on virtue of external, TOTAL, intervention being possible."

> "There MUST BE ONE PATH."

> "No timeouts ever."

> "They must ALWAYS start with one VM, then escalate."

> "A placeholder is a movie that doesn't exist."

> "DO NOT TAKE EFFORT TO OPTIMIZE."

> "Research first, act second."

> "I will not give you easy options to destroy the pipeline."

> "The Global State Agent is GET / only. No agent can influence it."

---

*Canonical version: 2026-05-27 (aligned with ARCHITECTURE_V7)*
