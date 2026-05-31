> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture v2 — Current Status

**Branch:** `strands-migration`  
**Last validated:** 2026-05-25  
**DeepSeek API:** Working (deepseek:deepseek-v4-flash)  
**Vast.ai balance:** $4.82

---

## ✅ What's Working

### Core Infrastructure
| Component | Status | Validation |
|-----------|--------|------------|
| Algebraic effects (`effects.py`) | ✅ | 6 types, typed Pydantic models |
| Event store (`event_store.py`) | ✅ | JSONL append-only, time travel via replay |
| Effect parser (`effect_parser.py`) | ✅ | Instructor + DeepSeek extracts intent from text |
| Projection handler (`projection_handler.py`) | ✅ | Applies effects to OTIO, creates jobs in queue |
| Instructor bridge (`pipeline_instructor.py`) | ✅ | Validates, stores, projects, feedback per unit |
| Unit state machines (`unit_state_machines.py`) | ✅ | 5 machines: scenario, audio, video, assembly, provisioner |
| Job queue (`job_queue.py`) | ✅ | SQLite-backed, full lifecycle: PENDING → ASSIGNED → RUNNING → COMPLETED/FAILED/NEEDS_RETRY |
| Job models (`models/job.py`) | ✅ | Job, JobResult, QAResult, JobStatus, JobType |

### Agents (pydantic-deep HTTP services)
| Agent | Port | Status | Validation |
|-------|------|--------|------------|
| Scenario | 9001 | ✅ | Wrote rainbow script, parsed as UpdateScript, stored in OTIO |
| Audio | 9002 | ✅ | Reads OTIO, creates narration jobs via bash, queue verified |
| Video | 9003 | ✅ | Launches, responds to HTTP |
| Assembly | 9005 | ✅ | Launches, responds to HTTP |
| Provisioner | 9006 | ✅ | Launches, responds to HTTP |

### Orchestrator
| Component | Status | Validation |
|-----------|--------|------------|
| Launcher (`launcher.py`) | ✅ | Spawns 5 processes, TCP health check |
| Pipeline v2 (`run_pipeline_v2.py`) | ✅ | Dry-run test passes (mocked agents) |
| Integration test | ✅ | Real scenario + audio agents, DeepSeek API, jobs in queue |

---

## 🔧 What Needs Work

### 1. Worker Infrastructure (BLOCKS production)
**Status:** Not built  
**Impact:** Audio/video jobs are created but never executed

Workers need to:
1. Poll job queue for ASSIGNED jobs
2. Download TTS/video generation code
3. Execute on GPU
4. Upload artifacts to B2
5. Mark jobs COMPLETED

**Options:**
- **A.** Build workers as Python scripts that run on Vast.ai VMs
- **B.** Use existing GPU worker code from strands (if any)
- **C.** Simplify: run TTS locally on CPU (slower but no Vast.ai cost)

### 2. Provisioner Agent Logic
**Status:** Prompt exists, not tested with real Vast.ai  
**Impact:** Won't provision VMs until tested

The provisioner agent needs to:
1. Read queue depth
2. Search Vast.ai offers
3. Create instances
4. Start workers on instances
5. Monitor health

**Risk:** Vast.ai API changes, instance availability, cost

### 3. B2 Upload/Download
**Status:** Not integrated  
**Impact:** Workers can't return artifacts

Job queue stores `artifact_path` as a B2 key. Need:
- `b2_upload(local_path, b2_key)` for workers
- `b2_download(b2_key, local_path)` for media agents

### 4. Assembly Agent QA
**Status:** Not tested end-to-end  
**Impact:** Final merge untested

The assembly agent needs:
1. Download all completed artifacts from B2
2. Merge with ffmpeg
3. Normalize loudness
4. Produce final MP4

### 5. Feedback Loop Polish
**Status:** Basic feedback works  
**Impact:** Agents may need multiple turns to converge

Current behavior:
- Agent does bash work → creates side effects
- Agent returns summary text
- Instructor parses text → may miss bash side effects
- Agent receives feedback → adjusts

**Improvement:** Agent prompt should instruct it to explicitly state what effects it produced in its text output, so the instructor can record them.

---

## 💰 Cost Estimate for Full Run

| Step | Cost |
|------|------|
| Scenario script (1 LLM call) | ~$0.005 |
| Audio agent turns (2-3 cycles) | ~$0.02 |
| Video agent turns (2-3 cycles) | ~$0.02 |
| Provisioner agent turns (1-2 cycles) | ~$0.01 |
| Assembly agent turns (1-2 cycles) | ~$0.01 |
| **Total LLM API** | **~$0.06** |
| Vast.ai GPU (1-2 instances, ~10 min each) | ~$0.10-0.30 |
| **Total estimated** | **~$0.20-0.50** |

Current balance ($4.82) is sufficient for multiple test runs.

---

## 🚀 Next Steps (Prioritized)

### Option A: Local TTS (no Vast.ai cost)
1. Install a local TTS engine (e.g., `piper-tts`, `coqui-TTS`)
2. Modify audio agent to use local TTS instead of queue
3. Test full pipeline locally (scenario → audio → assembly)
4. Skip video for now

### Option B: Minimal Vast.ai Test
1. Test provisioner agent with 1 cheap GPU instance
2. Build minimal worker script (just sleeps and marks job complete)
3. Verify queue → provisioner → worker → complete flow
4. Then add real TTS/video generation

### Option C: Fix Feedback Loop
1. Update agent prompts to explicitly report effects in text
2. Ensure instructor captures all effects (not just NoOp)
3. Re-run integration test and verify event store has complete history

---

## 🏗️ Architecture Decisions (Locked)

1. **No pydantic-graph at pipeline level** — plain async orchestrator
2. **Per-unit state machines** — instructor enforces per agent
3. **Event store is append-only** — never mutate, only replay
4. **OTIO is a projection** — rebuilt from events on demand
5. **Job queue is coordination layer** — between media agents and provisioner
6. **Agents communicate in natural language only** — no JSON, no structured output
7. **No recovery fallback** — `notify_maintainer()` + abort only
8. **deepseek:deepseek-v4-flash only** — no OpenRouter fallback
