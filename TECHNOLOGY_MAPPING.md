> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Technology Mapping — Abstract Architecture vs. Actual Implementation

> **Date:** 2026-05-17  
> **Abstract version:** 2026-05-17 (904 lines)  
> **Codebase branch:** `strands-migration` (latest commit: `b04a28d`)  
> **Purpose:** Map every technology in `ABSTRACT_ARCHITECTURE.md` to actual files, identify gaps, mismatches, and missing pieces.

---

## 1. Executive Summary

| Dimension | Abstract Design | Actual Implementation | Fit |
|---|---|---|---|
| **Agent Framework** | `pydantic-ai` Agent with `prepare_tools`, `@system_prompt`, `deps_type` | **Strands** `Agent` + `GraphBuilder` | ❌ Mismatch — different framework entirely |
| **Graph/Orchestration** | `pydantic-graph` with `BaseNode`, `Decision`, broadcast/join | **Strands** `GraphBuilder` with `GraphNode`, `GraphEdge` | ❌ Mismatch — no Decision nodes, no broadcast/join |
| **State Machine** | `python-statemachine.StateChart` with compound/parallel states | **NOT USED** — flat function checks (`_scenario_not_completed`) | ❌ Missing — not implemented |
| **Effect Parser** | `instructor` + DeepSeek v4-flash, semantic extraction (`Mode.JSON`) | `effect_parser.py` exists but uses **simple text parsing**, not instructor semantic mode | ⚠️ Partial — file exists, mechanism wrong |
| **Event Store** | Append-only JSONL, `EventRecord` with Pydantic | **`event_store.py`** — matches abstract exactly | ✅ Fits |
| **Effects Schema** | Pydantic `BaseModel` discriminated union | **`effects.py`** — matches abstract exactly | ✅ Fits |
| **HTTP Agent Surface** | `GET /` + `POST /` only, plain text | Only `gpu_worker.py` implements this. Agents use **Python tool calls** | ⚠️ Partial — workers yes, agents no |
| **Orchestrator** | LLM agent that decides next agent | **`_build_orchestrator_prompt()`** function + hardcoded stage order | ⚠️ Partial — not an agent, just a prompt builder |
| **VM Agent** | IS an LLM (reasons about survival, QA, retry) | `gpu_worker.py` is a **FastAPI server** (dumb endpoint) | ❌ Mismatch — not an agent |
| **Model** | `deepseek-v4-flash` everywhere | `deepseek-v4-flash` in v4 pipeline, `deepseek-v4-flash` in Strands entry point | ⚠️ Partial — DEFAULTS dict has wrong value |
| **B2 Artifact Store** | Ground truth for all media | `tools/b2_checkpoint.py` exists with upload functions | ✅ Fits |
| **Projection Handlers** | Rebuild OTIO/queues/registry from events | **NOT USED** — code reads OTIO directly | ❌ Missing — never implemented |
| **State Summary Builder** | Human-readable summary from projected read models | `_build_state_summary()` in `run_pipeline_v4.py` | ⚠️ Partial — exists but reads OTIO directly |

---

## 2. Technology-by-Technology Mapping

### 2.1 Orchestrator

| Abstract Claim | Actual Code | File | Verdict |
|---|---|---|---|
| "Single input: State summary" | Reads OTIO + recovery shell | `strands_agents/graph_pipeline.py:1680` | ⚠️ Reads state, but not via event replay |
| "Single output: Decision — which agent runs next" | Hardcoded `STAGE_ORDER` list | `strands_agents/graph_pipeline.py:144` | ❌ No agent decision — fixed sequence |
| "No side effects. Read-only observer." | `build_documentary_graph()` wires agent nodes | `strands_agents/graph_pipeline.py:381` | ⚠️ Graph builder decides, not an agent |
| "Never runs tools directly" | Orchestrator is just a prompt string | `strands_agents/graph_pipeline.py:1680` | ✅ No tools |

**Gap:** The orchestrator is NOT an agent. It is a function that builds a prompt and the Strands graph has a hardcoded stage sequence (`STAGE_ORDER = [SCENARIO, AUDIO, VIDEO, ASSEMBLY]`). The abstract envisions an LLM that reasons about state and dynamically decides the next agent.

---

### 2.2 Agents (7 Types)

| Agent Type | Abstract Design | Actual Implementation | File | Verdict |
|---|---|---|---|---|
| **Scenario** | Writes narration text, produces `UpdateScript` effects | Strands `Agent` with `write_narration_script` tool | `strands_agents/scenario_agent.py` | ⚠️ Uses Strands, not pydantic-ai |
| **Audio** | Formulates TTS jobs, produces `GenerateNarrationAudio` effects | Strands `Agent` with `generate_scene_narration` tool | `strands_agents/graph_pipeline.py:976` | ⚠️ Uses Strands, not pydantic-ai |
| **Video** | Formulates LTX jobs, produces `RenderVideoSegment` effects | Strands `Agent` with `submit_gpu_production_job` tool | `strands_agents/graph_pipeline.py:1202` | ⚠️ Uses Strands, not pydantic-ai |
| **Provisioner** | Sees queues, provisions VMs via bash | Tools inside audio/video agents (not separate) | `agents/audio_provisioner_agent.py`, `agents/video_provisioner_agent.py` | ❌ Not a separate agent |
| **OTIO Gate** | **ONLY** component that writes the timeline | Multiple agents write to OTIO directly | `tools/otio_file_ops.py` | ❌ No gate — any agent can write |
| **Assembly** | Merges clips into final MP4 | `assembly_stage.py` with ffmpeg | `strands_agents/stages/assembly_stage.py` | ⚠️ Exists but not an agentic effect producer |
| **Maintainer** | Called on exceptions, diagnoses, proposes fixes | `maintainer.py` — `notify_maintainer()` function | `server/maintainer.py` | ⚠️ Exists as function, not an agent |

**Critical Gaps:**
- **No separate Provisioner agent** — provisioning tools are embedded in audio/video agents.
- **No OTIO Gate** — any agent with OTIO tools can mutate the timeline. There is no single gatekeeper.
- **No Maintainer agent** — `maintainer.py` is a notification function, not an LLM agent that produces effects.
- **Framework mismatch** — All agents use Strands `Agent`, not `pydantic-ai` Agent with `prepare_tools` / `@system_prompt`.

---

### 2.3 Effect Parser

| Abstract Claim | Actual Code | File | Verdict |
|---|---|---|---|
| "instructor + DeepSeek v4-flash extracts typed effects from raw text" | `effect_parser.py` has `parse_agent_text_multi()` | `effect_parser.py` | ⚠️ File exists |
| "Mode.JSON — semantic extraction, NOT tool calling" | `structured_extract.py` uses `instructor.Mode.JSON` | `structured_extract.py:41` | ✅ Used for research tools |
| "No regex pre-extraction" | `effect_parser.py` uses regex and markers | `effect_parser.py:15` import re | ❌ Uses regex |
| "Pydantic discriminated unions enforce schema" | `effects.py` has `Annotated[... Field(discriminator="type")]` | `effects.py` | ✅ Matches |
| "On exhaustion, emit ClarificationRequest" | `build_clarification_request()` exists | `effect_parser.py` | ✅ Exists |

**Gap:** The effect parser exists but does NOT use instructor semantic mode for agent output parsing. It uses simpler text-based parsing. The `instructor` library IS installed and IS used (in `structured_extract.py` for research tool output), but not for the core effect parsing pipeline.

---

### 2.4 Event Store

| Abstract Claim | Actual Code | File | Verdict |
|---|---|---|---|
| "Interface: append(effect) — atomic, append-only" | `EventStore.append()` writes JSONL | `event_store.py:70-124` | ✅ Exact match |
| "Storage: JSONL file" | `self.log_path` is JSONL | `event_store.py:71` | ✅ Exact match |
| "Immutable, ordered, replayable" | `EventRecord` with seq + Pydantic | `event_store.py:40-60` | ✅ Exact match |
| "Only source of truth" | Comment says so, but OTIO is read directly | `event_store.py:67` | ⚠️ Claimed but not enforced |

**Verdict:** The Event Store is one of the best-matching components. It implements the abstract design almost exactly.

---

### 2.5 Projection Handlers

| Abstract Claim | Actual Code | Verdict |
|---|---|---|
| "Rebuild read-models from event stream" | **NOT IMPLEMENTED** | ❌ Missing |
| "OTIO is a materialized view" | OTIO is read/written directly by agents | ❌ Not a projection |
| "Never read OTIO directly without projecting first" | Code reads OTIO directly everywhere | ❌ Violated |

**Gap:** Projection handlers are completely missing. The abstract envisions that OTIO, job queues, and VM registry are all rebuilt deterministically from the event log. In practice, agents read and write OTIO files directly, bypassing the event store entirely.

---

### 2.6 State Summary Builder

| Abstract Claim | Actual Code | File | Verdict |
|---|---|---|---|
| "Human-readable state summary from projected read-models" | `_build_state_summary()` reads OTIO directly | `run_pipeline_v4.py` | ⚠️ Exists but reads OTIO, not projections |
| "Includes VM observed status, queue counts, completed jobs" | Summary includes these | `run_pipeline_v4.py` | ✅ |

**Gap:** The state summary builder exists but reads OTIO and worker status directly rather than from projected read models.

---

### 2.7 HTTP Agent Surface

| Abstract Claim | Actual Code | File | Verdict |
|---|---|---|---|
| "GET / (probe), POST / (instruction)" | Implemented for VM workers | `scripts/gpu_worker.py:866-890` | ✅ Workers match |
| "Plain text in, plain text out" | Workers use plain text | `scripts/gpu_worker.py:884` | ✅ Workers match |
| "Only GET / and POST /. No other endpoints." | Workers match, but agents have many endpoints | `playground_server.py`, `server.py` | ❌ Agents violate |
| "Per agent" | Only workers have this surface | `scripts/gpu_worker.py` | ❌ Agents use Python calls |

**Gap:** The plain-text HTTP surface is only implemented for GPU VM workers (`gpu_worker.py`). The pipeline agents (scenario, audio, video) do NOT have HTTP surfaces — they are Python objects with tool registries that run in-process.

---

### 2.8 State Machine (`python-statemachine`)

| Abstract Claim | Actual Code | Verdict |
|---|---|---|
| "`python-statemachine.StateChart` with compound/parallel states" | **NOT USED ANYWHERE** | ❌ Missing |
| "States: INIT, SCRIPT_DRAFT, SCRIPT_REVIEW, PROVISIONING, AUDIO_PRODUCTION, VIDEO_PRODUCTION, QA, ASSEMBLY, DONE" | Hardcoded `STAGE_ORDER` list | ⚠️ Partial — flat list, not state machine |
| "Transitions triggered by effects" | No transitions — just sequential graph | ❌ Missing |
| "Ephemeral — reconstructed from events on every cycle" | `RecoveryShell` is used, not state machine | ⚠️ Different mechanism |

**Gap:** The state machine is entirely missing. The abstract envisions a `StateChart` with compound states, parallel regions, and transitions triggered by effects. The actual code uses a hardcoded stage sequence (`STAGE_ORDER`) and a `RecoveryShell` for resume logic.

---

### 2.9 pydantic-graph

| Abstract Claim | Actual Code | Verdict |
|---|---|---|
| "`BaseNode` subclasses with `PipelineGraphState`" | Uses Strands `GraphNode` | ❌ Different framework |
| "Beta broadcast/join for parallel media" | Not implemented — audio/video are sequential in graph | ❌ Missing |
| "`Decision` nodes for orchestrator routing" | Not implemented — hardcoded edges | ❌ Missing |

**Gap:** The abstract envisions `pydantic-graph` with typed state, broadcast/join for parallelism, and Decision nodes. The actual code uses Strands `GraphBuilder` with sequential node wiring.

---

### 2.10 pydantic-ai Agent Framework

| Abstract Claim | Actual Code | Verdict |
|---|---|---|
| `Agent('deepseek-v4-flash', deps_type=PipelineState)` | `Agent('deepseek-v4-flash', ...)` from Strands | ❌ Wrong framework |
| `prepare_tools=prepare_tools_for_state` | Not used — all tools visible to all agents | ❌ Missing |
| `@agent.system_prompt` decorators | Hardcoded string prompts | ⚠️ Similar concept, different mechanism |
| `tool_guard` / `FilteredToolset` | Not implemented | ❌ Missing |

**Gap:** The abstract specifies `pydantic-ai` as the agent framework with state-aware tool filtering. The actual code uses Strands, which does not support `prepare_tools` or per-state tool allowlists.

---

### 2.11 VM Workers (Vast.ai)

| Abstract Claim | Actual Code | File | Verdict |
|---|---|---|---|
| "VM agent IS an LLM" | `gpu_worker.py` is a FastAPI server | `scripts/gpu_worker.py` | ❌ Not an LLM |
| "Reasons about survival, output quality, retry strategy" | No reasoning — just inference endpoint | `scripts/gpu_worker.py` | ❌ No agentic behavior |
| "Self-monitors and self-destructs (~15 min deadman)" | `VMAgent` class has deadman timer | `scripts/gpu_worker.py:131` | ⚠️ Timer exists but no LLM reasoning |
| "Pulls jobs from queue" | Receives POST / with text | `scripts/gpu_worker.py:889` | ⚠️ Push, not pull |
| "QA's own output using reasoning" | No QA in worker — QA is pipeline-side | `scripts/gpu_worker.py` | ❌ Missing |
| "Uploads to B2 immediately" | Worker does not upload to B2 | `scripts/gpu_worker.py` | ❌ Missing |

**Critical Gap:** The abstract envisions that the VM worker IS an LLM agent that reasons about its own survival, quality, and retry strategy. The actual `gpu_worker.py` is a simple FastAPI inference server with no LLM reasoning capability.

---

### 2.12 Artifact Store (Backblaze B2)

| Abstract Claim | Actual Code | File | Verdict |
|---|---|---|---|
| "Ground truth for all media files" | `tools/b2_checkpoint.py` | `tools/b2_checkpoint.py` | ✅ Exists |
| "Upload immediately after creation" | `b2_checkpoint.py` has upload functions | `tools/b2_checkpoint.py` | ✅ Exists |
| "Pipeline never pulls artifacts from VM directly" | `gpu_worker.py` returns base64 in POST response | `scripts/gpu_worker.py:889` | ❌ Violated — pipeline pulls from VM |

**Gap:** B2 upload tools exist, but the worker returns artifacts directly to the pipeline via HTTP POST response (base64), violating the abstract principle that artifacts should only come from B2.

---

### 2.13 instructor Library

| Abstract Claim | Actual Code | Verdict |
|---|---|---|
| "Used for semantic effect parsing" | Used in `structured_extract.py` for GPU requirements | ⚠️ Used, but not for effects |
| "Mode.JSON" | `instructor.Mode.JSON` in `structured_extract.py` | ✅ Correct mode |
| "DeepSeek v4-flash as extraction model" | Used in `run_pipeline_v4.py:515` | ✅ Correct model |

**Gap:** instructor IS installed and IS used, but for research/structured extraction, not for the core effect parsing pipeline.

---

### 2.14 Model Selection

| Abstract Claim | Actual Code | Verdict |
|---|---|---|
| "Hardcoded: `deepseek-v4-flash` everywhere" | `run_pipeline_v4.py` uses v4-flash | ✅ v4 pipeline matches |
| | `run_strands.py` DEFAULTS has wrong value | ❌ Strands pipeline uses wrong model |

**Gap:** The abstract specifies `deepseek-v4-flash` everywhere. The v4 pipeline (`run_pipeline_v4.py`) complies. The Strands pipeline (`run_strands.py`) has a DEFAULTS dict with the wrong model.

---

## 3. Architecture Violations

### 3.1 Direct OTIO Reads/Writes (No Projection)

**Violation:** Agents read and write OTIO files directly.
- `tools/otio_file_ops.py` — direct read/write
- `agents/audio_provisioner_agent.py` — direct OTIO mutation
- `agents/video_provisioner_agent.py` — direct OTIO mutation

**Abstract rule:** "Never read OTIO or queues directly without projecting first."

### 3.2 No State Machine

**Violation:** No `python-statemachine.StateChart` exists. Stage progression is a hardcoded list.

**Abstract rule:** "The state machine is a control-flow engine under the hood."

### 3.3 Agent Framework Mismatch

**Violation:** Uses Strands instead of `pydantic-ai`.

**Abstract rule:** "Concrete implementation with pydantic-ai."

### 3.4 VM Worker Is Not an Agent

**Violation:** `gpu_worker.py` is a FastAPI server, not an LLM.

**Abstract rule:** "The VM agent is an LLM."

### 3.5 No Tool Guardrails

**Violation:** All agents see all tools. No per-state allowlists.

**Abstract rule:** "Per-state tool allow-lists."

### 3.6 Pipeline Pulls Artifacts from VM

**Violation:** Worker returns base64 audio/video in POST response.

**Abstract rule:** "Pipeline never pulls artifacts from VM directly. VM uploads to B2."

---

## 4. What's Missing (From Abstract Checklist)

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | **Agent prompts** (full text for 7 types) | ⚠️ Partial | Prompts exist in `graph_pipeline.py` but not as dynamic state-aware prompts |
| 2 | **Event store interface** (`append`, `read_all`, `replay`) | ✅ Done | `event_store.py` implements all three |
| 3 | **Projection handler functions** | ❌ Missing | No code rebuilds OTIO from events |
| 4 | **State summary builder** | ⚠️ Partial | `_build_state_summary()` exists but reads OTIO directly |
| 5 | **Orchestrator prompt** | ⚠️ Partial | Prompt exists but orchestrator is not an agent |
| 6 | **HTTP agent wrapper** (FastAPI, GET/POST) | ❌ Missing | Only workers have HTTP surface |
| 7 | **Bash tool** (for Vast CLI) | ⚠️ Partial | `ssh_run_command` exists, no general bash tool |
| 8 | **B2 upload tool** | ✅ Done | `tools/b2_checkpoint.py` |
| 9 | **Worker implementations** (TTS agent, video agent) | ⚠️ Partial | FastAPI servers, not LLM agents |
| 10 | **QA gates** | ⚠️ Partial | `qa_jury.py` exists but not integrated with event store |
| 11 | **Assembly pipeline** | ⚠️ Partial | `assembly_stage.py` exists but not as agent |
| 12 | **Maintainer hook** | ⚠️ Partial | `maintainer.py` is a function, not an agent |
| 13 | **Deadman switch** | ✅ Done | `gpu_worker.py` has `record_activity()` timer |
| 14 | **Skill system** | ⚠️ Partial | `~/.kimi/skills/vastai-provisioning/SKILL.md` exists but no runtime loader |
| 15 | **Entry point** (async main loop) | ⚠️ Partial | `run_pipeline_v4.py` and `run_strands.py` are separate entry points |

---

## 5. Flesh-Out Checklist Revisited

From abstract section 8:

| Checklist Item | Abstract Status | Actual Status |
|---|---|---|
| Effect schema | [x] | ✅ Implemented |
| Agent prompts | [ ] | ⚠️ Exist but not state-aware |
| State machine definition | [x] | ❌ Not implemented |
| Prompt construction logic | [x] | ❌ Not implemented |
| Event store interface | [ ] | ✅ Implemented |
| Projection handler functions | [ ] | ❌ Not implemented |
| State summary builder | [ ] | ⚠️ Partial |
| Effect parser | [x] | ⚠️ Partial (wrong mechanism) |
| Orchestrator prompt | [ ] | ⚠️ Partial |
| pydantic-graph nodes | [x] | ❌ Not implemented (Strands used instead) |
| HTTP agent wrapper | [ ] | ❌ Not implemented |
| Bash tool | [ ] | ⚠️ Partial |
| B2 upload tool | [ ] | ✅ Implemented |
| Worker implementations | [ ] | ⚠️ Partial (not LLM agents) |
| QA gates | [ ] | ⚠️ Partial |
| Assembly pipeline | [ ] | ⚠️ Partial |
| Maintainer hook | [ ] | ⚠️ Partial |
| Deadman switch | [ ] | ✅ Implemented |
| Skill system | [ ] | ⚠️ Partial |
| Entry point | [ ] | ⚠️ Partial |

---

## 6. Recommendations

### 6.1 Pick One Pipeline, Delete the Other

There are **two parallel pipeline implementations**:
- **`run_pipeline_v4.py`** — Uses `pydantic-ai`-style patterns, event store, effect parser, orchestrator
- **`strands_agents/run_strands.py`** — Uses Strands `GraphBuilder`, hardcoded stage order

**Recommendation:** Decide which architecture to keep. The abstract design aligns with `run_pipeline_v4.py`. If keeping v4, delete the Strands pipeline. If keeping Strands, the abstract needs to be rewritten.

### 6.2 Implement the State Machine

The `python-statemachine` library needs to be installed and a `StateChart` implemented. This is the central control-flow engine that the abstract design depends on.

### 6.3 Implement Projection Handlers

Code needs to exist that reads the event log and rebuilds OTIO, job queues, and VM registry. Currently agents mutate OTIO directly, which bypasses the event store.

### 6.4 Implement HTTP Agent Surface

If the abstract's plain-text agent protocol is desired, agents need FastAPI wrappers with `GET /` and `POST /`.

### 6.5 Implement pydantic-ai Agent Framework

The abstract's tool guardrails, dynamic prompts, and state-aware agents require `pydantic-ai`. Strands does not support these features.

### 6.6 Make VM Worker an Agent (or Update Abstract)

Either rewrite `gpu_worker.py` to be an LLM agent (major work) or update the abstract to reflect that workers are inference endpoints, not agents.

### 6.7 Fix B2 Upload Flow

Worker should upload artifacts to B2, and pipeline should read from B2 — not pull base64 from VM HTTP responses.

---

## 7. File Index

### Files That Match the Abstract
| File | Abstract Component | Match Quality |
|---|---|---|
| `server/event_store.py` | Event Store | ✅ Exact |
| `server/effects.py` | Effect Schema | ✅ Exact |
| `server/effect_parser.py` | Effect Parser | ⚠️ Wrong mechanism |
| `tools/b2_checkpoint.py` | Artifact Store | ✅ Exists |
| `scripts/gpu_worker.py` | VM Worker (HTTP surface) | ⚠️ Only HTTP part matches |
| `server/maintainer.py` | Maintainer | ⚠️ Function, not agent |

### Files That Violate the Abstract
| File | Violation |
|---|---|
| `strands_agents/graph_pipeline.py` | Uses Strands instead of pydantic-ai; hardcoded stage order instead of state machine |
| `strands_agents/run_strands.py` | DEFAULTS dict has wrong model; no event store |
| `scripts/gpu_worker.py` | Not an LLM agent; no B2 upload; returns artifacts directly |
| `tools/otio_file_ops.py` | Direct OTIO reads/writes bypass event store |
| `agents/audio_provisioner_agent.py` | No tool guardrails; no state machine |
| `agents/video_provisioner_agent.py` | No tool guardrails; no state machine |

### Files Missing From the Abstract
| Abstract Component | Missing File |
|---|---|
| State Machine | `state_machine.py` (does not exist) |
| Projection Handlers | `projection_handlers.py` (does not exist) |
| HTTP Agent Wrapper | `agent_http_server.py` (does not exist) |
| pydantic-ai Agent Factory | `agent_factory.py` (does not exist) |
| State Summary Builder | `state_summary.py` (does not exist) |

---

*Mapping produced by codebase analysis on 2026-05-17. See `ABSTRACT_ARCHITECTURE.md` for the abstract design.*
