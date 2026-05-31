> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture Tightening Proposal

## Overview

This document identifies every loose edge, contradiction, and ambiguity in `ABSTRACT_ARCHITECTURE.md` and proposes concrete replacements.

---

## T1: Exception Flow — "Maintainer Agent" is wrong

**Current (3.2):**
```
2. Catch and route to Maintainer Agent
```

**Problem:** The table lists Maintainer as a separate agent type. But in practice, the agent that threw the exception receives the exception back and decides what to do. There is no separate "Maintainer" dispatch.

**Proposed:**
```
1. Exception occurs during an agent turn
2. Exception is returned to the SAME agent as context (not routed elsewhere)
3. Agent receives: exception type, traceback, current state summary
4. Agent responds with diagnosis + proposed fix (as effects)
5. Effects parsed, validated, appended to event log
6. Cycle resumes from step 1
7. If the agent declares the failure unrecoverable → KILL EVERYTHING
```

**Rationale:** The agent that caused the error has full context. Routing to a different agent loses that context. The agent itself is the maintainer of its own errors.

---

## T2: "Code does not constrain" contradicts tool guardrails

**Current (7.2):**
```
- Orchestrator decides; agents act; code does not constrain.
- No procedural logic overriding agent decisions.
```

**Current (6.5):**
```
The state machine is laws, not suggestions. It constrains what agents CAN do.
```

**Problem:** Direct contradiction. Either code constrains or it doesn't.

**Proposed replace 7.2 with:**
```
### 7.2 Agency
- **Orchestrator decides which agent runs; the state machine decides which tools are available.**
- **No procedural logic overriding agent decisions WITHIN the available tool set.** If an agent decides to retry, retry. If it decides to escalate, escalate. The code does not second-guess the agent.
- **Tool allowlists ARE constraints.** The state machine removes tools the agent should not see. This is not "overriding" — it is shaping the action space.
- **No timeout-based kills.** The agent reasons about whether to continue or stop.
```

---

## T3: "Agent's only tool is bash" contradicts everything else

**Current (6.10):**
```
- Effects are NOT tools. The agent's only tool is `bash`.
```

**Problem:** This is directly contradicted by the STATE_TOOL_ALLOWLISTS (section 6.5/6.10) which lists `write_file`, `read_file`, `queue_job`, `check_b2`, `merge_clips`, etc.

**Proposed:**
```
- **Effects are not tools the agent invokes.** Effects are semantic outputs extracted from the agent's natural language by the effect parser.
- **Tools are capabilities the agent CAN use.** The state machine's allowlist controls which tools are visible. In `SCRIPT_DRAFT`, the agent sees `write_file`. In `PROVISIONING`, it sees `bash`.
- **The bash tool is special:** it is the ONLY tool that reaches outside the pipeline (Vast CLI, ffmpeg). All other tools operate within the pipeline's controlled environment.
```

---

## T4: "No timeout-based kills" contradicts 15-min deadman

**Current (7.2):**
```
- No timeout-based kills. Agent reasons about intervention.
```

**Current (2.2):**
```
- Monitors overseer heartbeat; reasons about whether to self-destruct (~15 min deadman)
```

**Current (7.7):**
```
- VMs self-destruct when overseer gone. ~15 min deadman.
```

**Problem:** A 15-minute deadman IS a timeout-based kill.

**Proposed:**
```
### 7.2 Agency
- **No timeout-based kills within the pipeline.** Agent turns run until the agent decides to stop.

### 7.7 VMs
- **VMs self-destruct on heartbeat loss.** The VM monitors the overseer's heartbeat. If no heartbeat for 15 minutes, the VM destroys itself. This is NOT an agent decision — it is a safety mechanism. The VM agent MAY decide to self-destruct earlier based on reasoning (e.g., "I have no work and the overseer told me to shut down").
```

---

## T5: "No environment variables" is unenforceable

**Current (7.6):**
```
- No environment variables. File reads or parameters only.
- No CLI arguments. Hardcoded entry point.
```

**Problem:** The Vast.ai CLI requires `VAST_API_KEY` as an env var. The B2 SDK requires env vars.

**Proposed:**
```
### 7.6 Configuration
- **No env vars for pipeline configuration.** Model, budget, output dir, stage order — all hardcoded.
- **External credentials are the ONLY exception.** Vast.ai API key (`VAST_API_KEY`), B2 credentials, LLM API keys — these MAY be env vars because the external tools require them. They are NOT configuration; they are credentials.
- **No CLI arguments.** The entry point is `python run_pipeline_v4.py`. The brief is read from stdin or a hardcoded file.
- **Hardcoded model:** `deepseek-v4-flash` everywhere. No override.
```

---

## T6: "Hardcoded model deepseek-v4-flash everywhere" contradicts parser/agent split

**Current (6.10):**
```
- DeepSeek v4-flash is the extraction model. The agent may run on deepseek-chat; the parser always uses v4-flash.
```

**Current (7.6):**
```
- Hardcoded model: deepseek-v4-flash everywhere.
```

**Problem:** If the agent "may run on deepseek-chat", then v4-flash is NOT everywhere.

**Proposed:**
```
### 7.6 Configuration
- **Hardcoded model for pipeline agents:** `deepseek-v4-flash`.
- **Hardcoded model for effect parser:** `deepseek-v4-flash`.
- **Both use the same model.** There is no "agent runs on a different model" exception. The entire pipeline uses one model.
- **Reason:** Adding a second model introduces failure modes (rate limits, different behavior, credential management). One model, one API key, one failure surface.
```

---

## T7: "Stateless agents" contradicts "conversation history NOT cleared"

**Current (5.1):**
```
- Stateless: Pipeline reconstructs state on every cycle. Agent receives full context each time.
```

**Current (6.4):**
```
- The agent's conversation history is NOT cleared on state change. Prior context remains.
```

**Problem:** If agents are stateless, there IS no conversation history. If history persists, they're not stateless.

**Proposed:**
```
### 5.1 Agent ↔ Pipeline
- **Stateless with respect to the pipeline.** The pipeline does not store agent state between turns. Each turn is a fresh invocation.
- **Stateful with respect to the LLM provider.** The LLM provider (DeepSeek) stores the conversation thread. The pipeline passes a `thread_id` or `session_id` on each call. The LLM provider retains context.
- **On state change:** The pipeline continues using the SAME thread_id. The LLM provider's conversation history persists. The agent's prior context is NOT cleared. The agent must reconcile old context with new instructions.
```

And update 6.4:
```
- **The LLM conversation thread is NOT cleared on state change.** Prior turns remain in the provider's history. The agent must reconcile old context with new instructions.
```

---

## T8: Who triggers state machine transitions?

**Current (6.1):**
```
- Transitions are triggered by effects appended to the event log.
```

**Current (6.10):**
```
script_approved = script_review.to(provisioning)
```

**Problem:** If transitions are "triggered by effects", then the effect parser or event store must trigger them. But the code shows transitions as Python attributes. Who calls `script_approved()`?

**Proposed:**
```
### 6.1 Lightweight State Machine Per Pipeline

**Transitions are triggered by the orchestrator, NOT by effects directly.**

The flow is:
1. Agent produces effects → effect parser extracts them → event store appends them
2. Projection handlers rebuild read-models from events
3. Orchestrator reads the state summary (from projections)
4. Orchestrator decides: "We should transition from SCRIPT_REVIEW to PROVISIONING"
5. Orchestrator calls the transition on the state machine
6. The transition fires, changing the state machine's current state
7. The state machine's new state changes the tool allowlist and prompt instructions
8. Next cycle: the agent is invoked with the new instructions

**Key invariant:** The state machine does NOT auto-transition. The orchestrator is the ONLY component that triggers transitions.
```

---

## T9: Remove the "Research Problem" section (6.3)

**Current (6.3):** Presents four strategies (A, B, C, D) as research alternatives.

**Problem:** This is architecture documentation, not a research paper. The architecture must make a definitive choice. Presenting alternatives creates ambiguity.

**Proposed:** Replace 6.3 with a single tight paragraph:

```
### 6.3 State Disclosure Strategy

**The agent never sees state names.** The state machine injects instructions implicitly. The agent receives new task descriptions without being told "the state changed."

**Escalation rule:** If the agent produces effects that don't match the current state (e.g., writing script text during AUDIO_PRODUCTION), the pipeline:
1. Rejects the effects with feedback: "Your instructions have changed. You are now in audio production."
2. On the next turn, APPENDS explicit state disclosure to the prompt: "Current state: AUDIO_PRODUCTION. Previous: SCRIPT_REVIEW."
3. Does NOT clear conversation history.

**Rationale:** Default implicit disclosure keeps the agent focused on the task. Explicit disclosure is a recovery mechanism for when the agent drifts.
```

---

## T10: "No procedural logic" contradicts transition conditions

**Current (7.2):**
```
- No procedural logic overriding agent decisions.
```

**Current (6.5):**
```
- Transition conditions: Require QA pass before exiting QA state.
```

**Problem:** "Require QA pass before exiting QA state" IS procedural logic overriding the orchestrator's decision.

**Proposed replace 6.5 transition conditions with:**
```
- **Transition conditions are NOT procedural gates.** They are PROMPT INJECTIONS.
  - Instead of: "If not QA pass, reject transition"
  - The state machine injects into the orchestrator's prompt: "The QA state has 3 clips pending review. Do not transition out of QA until all clips are reviewed."
  - The orchestrator (an LLM) reads this and decides: "Stay in QA."
  - The transition fires when the orchestrator decides to fire it.
- **There is no `if` statement blocking transitions.** The orchestrator is the only decider.
```

---

## T11: "No special endpoints" contradicts multiple server endpoints

**Current (2.7):**
```
- Hard rule: Only GET / and POST /. No other endpoints. No special paths for special actions.
```

**Problem:** The pipeline also has `/health`, `/status`, `/approval`, `/dashboard`, `/fleet`, etc.

**Proposed:**
```
### 2.7 HTTP Agent Surface
- **Agent-facing surface: ONLY GET / and POST /**. No exceptions.
- **Human-facing surface: whatever endpoints are needed.** Dashboard, approval gates, health checks — these are for operators, not agents. They do not appear in agent URLs.
- **Agent URLs are opaque.** The pipeline decides whether an agent is local (Python call) or remote (HTTP GET/POST). Agents do not know and do not care.
```

---

## T12: VM Agent as LLM is architecturally unsound

**Current (2.2):**
```
- Is an LLM. There are no "dumb inference endpoints."
```

**Problem:** Running an LLM on a GPU VM ($1.50-4.00/hr) to decide whether to retry a TTS job is economically absurd.

**Proposed:**
```
### 2.2 VM Agent

The VM agent is a **FastAPI server** with a **lightweight reasoning loop** (rule-based, not LLM-based):

- **Boots, installs deps, starts HTTP server.**
- **Receives job via POST /** (plain text describing the work).
- **Runs inference** (TTS or LTX model as a subprocess/tool).
- **QA's output** using deterministic checks (LUFS for audio, motion stability for video). If checks fail, retries with adjusted parameters (seed, guidance scale).
- **Self-destructs** on heartbeat loss (15 min timer, not LLM reasoning).
- **Reports JobCompleted or JobFailed** via POST to pipeline callback URL.
- **Uploads artifact to B2 immediately.** Does not wait for pipeline to pull.

**The VM agent is NOT an LLM.** It is a deterministic worker with rule-based retry logic. The pipeline's central LLM agents handle high-level reasoning. The VM handles low-level inference and deterministic QA.

**Rationale:** Running an LLM on every GPU VM would cost $1.50-4.00/hr per VM. The pipeline's central agents are cheaper (API calls at $0.001-0.01 per turn) and have full context.
```

---

## T13: "No other persistent state" contradicts agent_memory directory

**Current (2.4):**
```
- Hard rule: The event log is the ONLY source of truth. No other persistent state.
```

**Problem:** The code has `server/agent_memory/` which stores agent state across runs.

**Proposed:**
```
### 2.4 Event Store
- **Hard rule: The event log is the ONLY persistent source of truth for pipeline state.**
- **Agent memory is ephemeral caching, not truth.** The `agent_memory/` directory stores cached agent outputs for debugging. It is rebuilt from the event log if lost.
- **OTIO files are projections, not truth.** They are rebuilt from events. Direct OTIO edits are forbidden.
```

---

## T14: State machine "ephemeral" vs "reconstructed from events"

**Current (6.1):**
```
- The state machine is ephemeral — reconstructed from events on every cycle, never persisted independently.
```

**Problem:** "Ephemeral" usually means "lives for a short time and then disappears." But the state machine is reconstructed on EVERY cycle, so it exists continuously. The word "ephemeral" is misleading.

**Proposed:**
```
- **The state machine is reconstructed on every cycle.** It is not persisted between cycles. Each cycle:
  1. Read all events from the event log
  2. Instantiate a fresh state machine
  3. Replay all state-transition effects through the state machine
  4. The state machine's current state is now correct
  5. Use the current state to construct prompts
- **If the event log is lost, the state machine state is lost.** The event log is append-only JSONL with file-system persistence, so this is acceptable.
```

---

## T15: Flesh-out checklist — remove [x] marks for unimplemented items

**Current:** Items marked [x] that are NOT actually implemented:
- State machine definition [x] — NOT implemented
- Prompt construction logic [x] — NOT implemented
- pydantic-graph nodes [x] — NOT implemented (Strands used instead)

**Proposed:** All items should be [ ] until there is working code that passes tests.

---

## T16: "No JSON in prompts" vs state summary needing structure

**Current (7.3):**
```
- Plain text only between agents and pipeline. No JSON, no schemas in prompts.
```

**Problem:** The state summary includes structured data (queue counts, VM status, completed jobs). How is this conveyed without JSON?

**Proposed:**
```
### 7.3 Communication
- **Plain text only in agent prompts.** The state summary is rendered as human-readable text, not JSON.
- **Structured data is rendered as lists and tables.** Example:
  ```
  VM Status:
  - tts-worker-1: healthy, tts=yes, vram=2.1/24.0GB
  - video-worker-1: bootstrapping, ltx=no, vram=0.0/80.0GB

  Job Queue:
  - Audio: 5 pending, 12 completed, 2 failed
  - Video: 3 pending, 8 completed, 1 failed
  ```
- **No JSON in the prompt text.** The pipeline constructs the text. The agent reads it.
- **JSON is allowed inside the pipeline** (event store, effect parser, projections). It never crosses the agent boundary.
```

---

## T17: "No mocks, stubs, placeholders" vs "Produce movies, not perfect architecture"

**Current (7.8):**
```
- No mocks, stubs, placeholders. Every tool calls real services.
- No premature optimization. Produce movies, not perfect architecture.
```

**Problem:** Could be misread as "cut corners."

**Proposed:**
```
### 7.8 Quality
- **No mocks, stubs, placeholders.** Every tool calls real services.
- **No premature optimization.** The state machine can be flat. The prompts can be simple. The projection handlers can be slow. What matters is producing a movie. Optimize after the first successful run.
- **"Produce movies, not perfect architecture" means:** Ship a working pipeline with a 5-state flat machine before designing a 50-state Harel statechart. It does NOT mean skip QA or use fake video.
```

---

## Summary of Proposed Changes

| Section | Change |
|---|---|
| 3.2 | Exception returns to SAME agent, not separate Maintainer |
| 7.2 | Clarify that tool allowlists ARE constraints, but no procedural overrides |
| 6.10 | Clarify that effects ≠ tools; bash is the only external tool |
| 7.2 / 7.7 | Separate "no timeout kills" (pipeline) from "deadman timer" (VM safety) |
| 7.6 | Allow env vars ONLY for external credentials |
| 7.6 | One model everywhere: deepseek-v4-flash |
| 5.1 / 6.4 | Clarify stateless w.r.t. pipeline, stateful w.r.t. LLM provider |
| 6.1 / 6.10 | Orchestrator triggers transitions, not effects directly |
| 6.3 | Remove research alternatives; state the single strategy |
| 6.5 | Transition conditions are prompt injections, not procedural gates |
| 2.7 | Separate agent-facing surface (GET/POST only) from human surface |
| 2.2 | VM agent is deterministic worker, not LLM |
| 2.4 | Clarify event log is only persistent truth; agent_memory is cache |
| 6.1 | "Reconstructed on every cycle" instead of "ephemeral" |
| 8 | Remove [x] marks for unimplemented items |
| 7.3 | State summary is human-readable text, not JSON |
| 7.8 | Clarify "produce movies" does not mean "use fake data" |
