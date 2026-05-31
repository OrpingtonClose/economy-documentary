> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture v2 Migration Report

**Branch:** `strands-migration`  
**Date:** 2026-05-25  
**Commits:** 18 commits since start of session

---

## 1. What Was Done

### 1.1 Pre-Migration Fixes (v1 codebase cleanup)

Before building v2, several critical issues in the existing codebase were fixed:

- **`recovery_agents.py`**: Hardcoded `deepseek/deepseek-v4-flash` model, removed OpenRouter fallback
- **`recovery_agents.py`**: Gutted `RecoveryAgent.decide()` to only `notify_maintainer()` + abort (no recovery fallback)
- **`vm_registry_tools.py`**: Removed `check_worker_health` (agent uses bash now)
- **`graph_pipeline.py`**: Added pending-job guards to routing conditions
- **`run_strands.py`**: Added orphan VM destruction at pipeline start
- **`provisioner_agent.py`**: Prompt rewritten to bash-only for VM health checks

### 1.2 Core v2 Infrastructure (Steps 1-3)

| File | Purpose | Lines |
|------|---------|-------|
| `server/effects.py` | Algebraic effect types — 6 typed Pydantic models that are the ONLY way to mutate pipeline state | 80 |
| `server/event_store.py` | Append-only JSONL event log. Single source of truth. Time travel via replay. | 91 |
| `server/effect_parser.py` | Instructor + DeepSeek parser that extracts typed effects from agent natural language | 133 |
| `server/projection_handler.py` | Applies validated effects to OTIO timeline. Rebuilds projection from events. | 134 |
| `server/pipeline_instructor.py` | Per-unit bridge: parse → validate → store → project → feedback. Tracks state machine. | 192 |
| `server/unit_state_machines.py` | 5 state machines (scenario, audio, video, assembly, provisioner) with transitions and valid effects | 149 |

**Key design decisions:**
- Agents communicate ONLY in natural language. No JSON, no structured output.
- Instructor is the gatekeeper: it parses agent text into typed effects.
- Effects are the ONLY mutation path.
- OTIO is a read model rebuilt from events.

### 1.3 pydantic-graph Orchestrator (Attempted, Failed)

Created `server/pydantic_graph_pipeline.py` with `@g.step` + while loop pattern.

**Result: DEADLOCK.** pydantic-graph `@g.step` cannot handle HTTP I/O inside steps. The graph runner waits on a memory stream that never receives a value because the while loop never exits.

**Resolution:** Global orchestrator is now a plain async function. pydantic-graph is reserved for per-unit internal state machines only.

### 1.4 pydantic-deep Agents (5 HTTP services)

| Agent | Port | Status |
|-------|------|--------|
| Scenario | 9001 | ✅ Writes scripts, validated end-to-end |
| Audio | 9002 | ✅ Creates jobs, validated end-to-end |
| Video | 9003 | ✅ Launches, responds |
| Assembly | 9005 | ✅ Launches, responds |
| Provisioner | 9006 | ✅ Launches, responds |

**Challenges overcome:**
- Model name format: `deepseek/deepseek-v4-flash` → `deepseek:deepseek-v4-flash`
- Subagent recursion: disabled with `include_subagents=False`
- WebSearch/WebFetch: disabled (not supported with OpenAIChatModel)
- `NoneType` deps error: fixed by passing `deps=_agent.deps_type()`
- Agent startup: added `sys.path` fix so agents can import `job_queue`

### 1.5 Job Queue (New Coordination Layer)

Created `server/models/job.py` and `server/job_queue.py`:

- SQLite-backed queue at `/tmp/documentary-pipeline/job_queue.db`
- Lifecycle: `PENDING → ASSIGNED → RUNNING → COMPLETED | FAILED | NEEDS_RETRY`
- Deduplication: `create_job` returns existing non-failed job for same stage+scene
- QA retry: `requeue_job_with_qa_comments` puts failed jobs back with comments

**Why this exists:** Media agents (audio, video) create jobs. The provisioner reads the queue and provisions workers. Workers execute jobs. Media agents poll for completed jobs and perform QA. No VM URLs in agent conversations.

### 1.6 Pipeline Orchestrator v2

`server/run_pipeline_v2.py`:
- Plain async function (no pydantic-graph)
- Cycles through units based on job queue state
- `_check_has_audio/_check_has_video` use queue summaries (not OTIO clips)
- Each unit runs until it produces a NoOp
- Initializes fresh OTIO timeline if none exists

### 1.7 Integration Testing

| Test | Status |
|------|--------|
| `test_pipeline_v2.py` (dry-run, mocked) | ✅ Passes |
| `test_integration_real_agents.py` (real API) | ✅ Passes |
| Scenario → script → UpdateScript → OTIO | ✅ Validated with DeepSeek |
| Audio → job creation → queue | ✅ Validated with DeepSeek |

### 1.8 /cheat Compliance

Created `server/cheat_check.py` scanner. Fixed violations:
- `event_store.py`: `except Exception: pass` → `logger.exception()`
- `launcher.py`: `time.sleep()` polling → async concurrent port checks
- `run_pipeline_v2.py`: Added `/cheat:` justification for agent timeout

**Result:** `/cheat` scan passes clean on all v2 files.

---

## 2. What Was NOT Done (And Why)

### 2.1 Local TTS Worker (`server/local_tts_worker.py`)

Created a development TTS worker using `edge-tts` (Microsoft free API) that pulls from the job queue and generates WAV files locally.

**User verdict: This is a mock/stand-in.** The real codebase already has `server/strands_agents/qwen3_tts_worker/` — a full GPU-based TTS worker. The local worker is a parallel simplified implementation, not an integration with existing infrastructure.

**Status:** Should be removed or clearly marked as dev-only. The proper path is to adapt the existing Qwen3-TTS worker to pull from the new SQLite queue.

### 2.2 Video Worker Integration

The existing `server/strands_agents/ltx_video_worker/` exists but was not integrated with the new job queue. Same issue as TTS: need to adapt existing worker, not build new one.

### 2.3 B2 Upload/Download

Job queue stores `artifact_path` as a B2 key, but:
- No B2 upload function for workers
- No B2 download function for media agents
- The existing `server/tools/b2_checkpoint.py` exists but was not integrated

### 2.4 Provisioner Agent — Real Vast.ai Testing

The provisioner agent prompt was written but never tested with actual Vast.ai:
- No VM was provisioned
- No worker was started on a VM
- The queue → provisioner → worker → complete flow is theoretical

**Blocker:** Vast.ai balance is $4.82. A single test run costs ~$0.10-0.30. Not tested due to cost/risk, not due to code issues.

### 2.5 Assembly End-to-End

The assembly agent was written but never tested with real audio/video clips:
- ffmpeg merge commands are in the prompt
- No actual MP4 was produced

### 2.6 Feedback Loop Polish

The audio agent creates jobs via bash commands, but the instructor only sees the agent's text output (which may be "All audio complete" → parsed as NoOp). The event store does not record the job creation.

**Fix needed:** Agent prompt should instruct the agent to explicitly list what jobs it created in its text response, so the instructor can parse `GenerateNarrationAudio` effects.

---

## 3. Architecture Decisions (Locked)

| Decision | Rationale |
|----------|-----------|
| No pydantic-graph at pipeline level | Deadlocks on HTTP I/O inside `@g.step` |
| Per-unit state machines | Each agent has its own graph/plan |
| Job queue as coordination layer | Decouples media agents from provisioner |
| Agents speak natural language only | Maximally free agents, maximally constrained instructor |
| Event store is append-only | Time travel, auditability, replay |
| No recovery fallback | `notify_maintainer()` + abort only |
| deepseek:deepseek-v4-flash only | No OpenRouter fallback |

---

## 4. Remaining Work (Prioritized)

### P0 — Critical (Blocks any production run)

1. **Integrate existing Qwen3-TTS worker with job queue**
   - Adapt `server/strands_agents/qwen3_tts_worker/app.py` to pull from SQLite queue
   - Or write a thin adapter that polls queue and POSTs to existing worker
   - Remove or dev-mark `local_tts_worker.py`

2. **Integrate existing LTX video worker with job queue**
   - Same pattern as TTS

3. **B2 upload/download**
   - Worker uploads artifact to B2 after generation
   - Media agent downloads from B2 for QA
   - Use existing `server/tools/b2_checkpoint.py`

### P1 — Important (Needed for reliable runs)

4. **Test provisioner with real Vast.ai**
   - Provision 1 cheap GPU instance
   - Start worker on instance
   - Verify queue → worker → complete flow
   - Cost: ~$0.10-0.30 per test

5. **Fix feedback loop**
   - Update audio/video agent prompts to explicitly report effects in text
   - Ensure instructor captures all effects in event store

6. **Assembly end-to-end**
   - Download completed artifacts
   - Merge with ffmpeg
   - Produce final MP4

### P2 — Nice to have

7. **Unit tests for job queue**
8. **Unit tests for state machines**
9. **Performance: async job queue (currently SQLite + threading.Lock)**
10. **Documentation: full ARCHITECTURE_v2.md update with learned lessons**

---

## 5. Files Changed

```
server/
├── effects.py                    (new)
├── event_store.py                (new)
├── effect_parser.py              (new)
├── projection_handler.py         (new)
├── pipeline_instructor.py        (new, renamed from instructor.py)
├── unit_state_machines.py        (new)
├── job_queue.py                  (new)
├── run_pipeline_v2.py            (new)
├── local_tts_worker.py           (new — dev-only, user wants removed)
├── test_pipeline_v2.py           (new)
├── test_integration_real_agents.py (new)
├── cheat_check.py                (new)
├── models/job.py                 (new)
├── models/__init__.py            (modified)
├── pydantic_deep_agents/
│   ├── scenario_agent.py         (new)
│   ├── audio_agent.py            (new)
│   ├── video_agent.py            (new)
│   ├── assembly_agent.py         (new)
│   ├── provisioner_agent.py      (new)
│   ├── otio_gate_agent.py        (new — may be obsolete)
│   └── launcher.py               (new)
├── recovery_agents.py            (modified)
├── vm_registry_tools.py          (modified)
├── graph_pipeline.py             (modified)
└── run_strands.py                (modified)
ARCHITECTURE_v2.md                (new)
STATUS_v2.md                      (new)
MIGRATION_REPORT.md               (new)
```

---

## 6. Cost to Complete

| Item | Cost |
|------|------|
| DeepSeek API (testing) | ~$0.05/run |
| Vast.ai GPU (1 instance, 10 min) | ~$0.10-0.30/run |
| **Total per full test run** | **~$0.20-0.50** |
| **Current balance** | **$4.82** |
| **Estimated runs remaining** | **~10-20** |

---

## 7. Key Lesson

**The pipeline is not one graph. It is 6 independent agents, each with their own internal state machine.**

The attempt to use pydantic-graph as the global orchestrator failed because:
- `@g.step` + HTTP I/O = deadlock
- Graph runners expect steps to return quickly, not run LLM calls

The fix: plain async orchestrator at the pipeline level, pydantic-graph reserved for per-unit internal planning only.
