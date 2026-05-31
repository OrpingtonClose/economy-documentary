> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture V5.1 — Documentary Pipeline (Refined & Complete)

> **Date:** 2026-05-26  
> **Status:** Production-ready specification (implementation in progress)  
> **Version:** 5.1 (supersedes V5 2026-05-17)  
> **Branch:** `strands-migration`  
> **Location:** `server/v5/`  
>
> This is the **canonical and complete** V5 architecture. It refines the original V5 document by closing all identified gaps, adding missing operational details, full guard implementations, an Assembly Agent, explicit data models, deployment guidance, and production hardening while preserving every core principle.

---

## 1. Core Philosophy (Unchanged)

### 1.1 The Event Log Is the Only Source of Truth
### 1.2 Effects Are the Only Legal Mutations
### 1.3 The Orchestrator Is Destroyed
### 1.4 No Timeouts Anywhere
### 1.5 No Mocks in Production

All principles remain absolute. No hidden state. No simulation in prod.

---

## 2. System Topology (Updated)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            HUMAN / OVERSEER                              │
│  GET / on any agent or state machine for observation.                    │
│  POST / with natural-language instruction for correction.                │
│  Recommended: Streamlit dashboard (see §13.4)                            │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    WATCHER + STATE MACHINE (self-operating)              │
│  python-statemachine + asyncio. Tick every 1s.                           │
│  All guards read only from projections. No central orchestrator.         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
           ┌────────────────────────┼────────────────────────┐
           │                        │                        │
           ▼                        ▼                        ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│  SCENARIO     │  │  AUDIO        │  │  VIDEO        │  │  PROVISIONER  │
│  Agent        │  │  Agent        │  │  Agent        │  │  Agent        │
│  port 8001    │  │  port 8002    │  │  port 8003    │  │  port 8004    │
└───────────────┘  └───────────────┘  └───────────────┘  └───────────────┘
       │                  │◄─────────────────┘                  │
       │                  │   (Provisioner POSTs job results)   │
       │                  │                                     │
       ▼                  ▼                                     ▼
┌───────────────┐
│  ASSEMBLY     │  (NEW in V5.1 — port 8005)
│  Agent        │
└───────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              EVENT STORE (SQLite, append-only, single writer)            │
│  Table: events(id, run_id, sequence, kind, payload_json, version, created_at) │
│  BEGIN IMMEDIATE + asyncio.Queue single writer.                          │
└─────────────────────────────────────────────────────────────────────────┘
       ▲                  ▲                  ▲                  ▲
       │                  │                  │                  │
       ▼                  ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        PROJECTIONS (incremental, tick-driven)            │
│  OTIOProjection | JobProjection | VMProjection | StateProjection         │
│  Each tracks last_sequence; processes only new events.                   │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     VM WORKERS (ephemeral GPU instances via Vast.ai)     │
│  port 9000+  GET /health  POST /job                                      │
│  TTS: Qwen3-TTS. Video: LTX-2.3. LLM fallback: deepseek-v4-flash.        │
│  Self-destruct on 15min heartbeat loss.                                  │
└─────────────────────────────────────────────────────────────────────────┘
```

**Endpoint Rule (strict):** Every component exposes **exactly** `GET /` (status + summary) and `POST /` (instruction or job payload). No other routes.

---

## 3. Effect Type Family (Updated — 31 Types)

All effects are Pydantic `BaseModel` with `kind: Literal[...]`, `run_id: str`, `version: int = 1`, and domain fields. Single discriminated union `EffectUnion`.

### 3.1 Script Effects (3)
`UpdateScript`, `DeleteScene`, `ReorderScenes` — unchanged.

### 3.2 Job Effects (5)
`QueueJob`, `JobCompleted`, `JobFailed`, `JobRequeued`, `JobApproved` — unchanged.

### 3.3 Reconciliation Effects (6) — **+1 new**
`AudioGenerated`, `AudioMeasured`, `DurationAdjusted`, `ReconciliationFailed`, `ReconciliationComplete`, **`MeasurementRequested`** (new — Audio agent triggers WhisperX explicitly if needed).

### 3.4 VM Effects (4)
Unchanged.

### 3.5 OTIO Effects (2)
Unchanged.

### 3.6 Pipeline Effects (5) — **+1 new**
`PipelineStarted`, `TransitionState`, `PipelineComplete`, `PipelineAborted`, **`AssemblyStarted`**.

### 3.7 Bash / Human / Fallback Effects (5)
Unchanged.

### 3.8 Production Failure Effect (1)
Unchanged.

**Total: 31** (added `MeasurementRequested` and `AssemblyStarted` for clarity; original count was off-by-one).

**New Effect Examples:**

```python
class MeasurementRequested(Effect):
    kind: Literal["measurement_requested"] = "measurement_requested"
    job_id: str
    block_id: str
    artifact_path: str
    requested_by: str = "audio_agent"

class AssemblyStarted(Effect):
    kind: Literal["assembly_started"] = "assembly_started"
    output_path: str
    tracks_summary: dict

class AssembleFinal(Effect):  # Produced by Assembly Agent
    kind: Literal["assemble_final"] = "assemble_final"
    command: str
    expected_duration_sec: float
    output_path: str
```

---

## 4. State Machine (Refined)

### 4.1 States (Unchanged)

`INIT` → `SCRIPT` → `AUDIO_VIDEO` → `ASSEMBLY` → `DONE` (final)  
(with back-edge `AUDIO_VIDEO` ↔ `SCRIPT` on script errors)

### 4.2 Transitions (tick-driven) — **clarified**

```python
tick = (
    init.to(script, cond="_pipeline_started")
    | script.to.itself()
    | script.to(audio_video, cond="_audio_reconciled_and_no_gaps")
    | audio_video.to.itself()
    | audio_video.to(script, cond="_has_script_errors")
    | audio_video.to(assembly, cond="_all_media_produced_and_reconciled")
    | assembly.to.itself()
    | assembly.to(done, cond="_assembly_valid_and_complete")
    | done.to.itself()  # no-op after final
)
```

### 4.3 Guard Details — **COMPLETE IMPLEMENTATIONS** (V5.1 addition)

All guards are pure functions on projection state. No side effects.

```python
# In PipelineStateMachine class

def _pipeline_started(self, event, source, target) -> bool:
    return bool(getattr(self.state, "pipeline_started", False))

def _audio_reconciled_and_no_gaps(self, event, source, target) -> bool:
    if not self._reconciliation_complete():
        return False
    if self._has_pending_or_running_jobs("tts"):
        return False
    tracks = getattr(self.otio, "tracks", {})
    return _all_slots_filled(tracks) and not _has_gaps(tracks)

def _has_script_errors(self, event, source, target) -> bool:
    failures = getattr(self.jobs, "production_failures", [])
    script_blaming = {"gap_unexpected", "voice_mismatch"}
    return any(f.get("failure_type") in script_blaming for f in failures)

def _all_media_produced_and_reconciled(self, event, source, target) -> bool:
    if not self._reconciliation_complete():
        return False
    if self._has_pending_or_running_jobs():  # any type
        return False
    tracks = getattr(self.otio, "tracks", {})
    return _all_slots_filled(tracks) and self._no_unresolved_production_failures()

def _assembly_valid_and_complete(self, event, source, target) -> bool:
    output_path = getattr(self.state, "output_path", "/artifacts/{run_id}/final_documentary.mp4")
    if not os.path.exists(output_path):
        return False
    if getattr(self.jobs, "production_failures", []):
        return False
    ok, _ = self.otio.validate_no_overlaps()
    if not ok: return False
    ok, _ = self.otio.validate_track_alignment()
    if not ok: return False
    ok, _ = self.otio.validate_clip_media()
    return ok

def _reconciliation_complete(self) -> bool:
    return getattr(self.jobs, "reconciliation_complete", False)

def _has_pending_or_running_jobs(self, job_type: str | None = None) -> bool:
    jobs = getattr(self.jobs, "jobs", {})
    for j in jobs.values():
        if j.get("status") in ("pending", "running"):
            if job_type is None or j.get("job_type") == job_type:
                return True
    return False

def _no_unresolved_production_failures(self) -> bool:
    return len(getattr(self.jobs, "production_failures", [])) == 0
```

**Additional helper (in OTIOProjection):**

```python
def _all_slots_filled(self, tracks: dict) -> bool:
    for track_name, track in tracks.items():
        for slot in track.get("slots", []):
            if not slot.get("media_path"):
                return False
    return True
```

### 4.4 Watcher Loop (Hardened)

```python
async def watcher(machine, projections, event_store, agents):
    consecutive_errors = 0
    while True:
        try:
            for proj in projections:
                proj.tick(event_store)  # idempotent via last_sequence
            await machine.tick()
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Watcher tick failed: {e}")
            if consecutive_errors > 5:
                await event_store.append(AgentLoopDetected(agent="watcher", reason=str(e)))
        await asyncio.sleep(1)
```

### 4.5 State Instruction Injection (Clarified)

On every transition (or every tick while in state), the state machine (via watcher) POSTs a structured payload to **all active agents**:

```json
{
  "instruction": "Current state: AUDIO_VIDEO. Focus on ...",
  "context": {
    "otio_summary": {...},
    "job_status": {...},
    "recent_failures": [...],
    "measured_durations": {...}
  }
}
```

Agents incorporate this into their next LLM turn prompt.

---

## 5. Event Store (Minor Update)

Schema now includes `version INTEGER DEFAULT 1`.

Single-writer queue + `BEGIN IMMEDIATE` unchanged. Replay unchanged.

---

## 6. Projections (Enhanced)

### 6.1 OTIOProjection — **Data Model Added**

**Track Structure (authoritative after ReconciliationComplete):**

- `A1_Narration`: Audio track. Slots keyed by `block_id = f"{scene_num}:{phrase_idx}"`. Each slot has `scripted_sec`, `measured_sec` (after reconciliation), `media_path` (WAV), `voice_role`.
- `V1_Video`: Video track. Slots reference same `block_id`, use `measured_sec` as duration LAW. `media_path` = MP4 clip from LTX-2.3.
- Optional future: `M1_Music`, `S1_SFX`.

Validation methods expanded with exact error messages for ProductionFailed routing.

### 6.2 JobProjection — **Enhanced**

Now tracks `retry_budget` per job (default 5). Emits `AgentLoopDetected` if any job exceeds budget.

### 6.3 VMProjection — **Polling Added**

`poll_vastai()` runs every 30s in background task. Emits `VMObserved` on drift. Auto-deallocates stale VMs.

### 6.4 StateProjection — Unchanged.

---

## 7. Agents (Updated with Assembly + Clarifications)

### 7.1 Base Agent (Hardened)

- Added `/health` endpoint (returns 200 + uptime + last_error).
- Structured logging (JSON) + correlation via `run_id`.
- Automatic `ClarificationRequest` emission after 3 consecutive parse failures.

### 7.2 Scenario Agent (8001)
Unchanged role. Now receives `measured_durations` in context for better pacing suggestions.

### 7.3 Audio Agent (8002) — **Reconciliation Owner (Clarified)**

**Exact Flow (V5.1):**

1. On entering AUDIO_VIDEO or new narration slot → emit `QueueJob` (tts).
2. Provisioner notifies via POST / → Audio agent downloads artifact from shared `/artifacts/{run_id}/tts/{job_id}.wav`.
3. Audio agent runs:
   ```bash
   whisperx --model large-v3 --language en --output_format json {artifact} --output_dir /tmp/whisperx
   ```
   (Assumes whisperx installed on host; runs in subprocess with 300s soft limit — operator intervenes if longer.)
4. Parse JSON → emit `AudioMeasured` (with `whisperx_confidence`).
5. Compare:
   ```python
   tolerance = max(0.25, 0.15 * scripted_sec)
   if abs(measured_sec - scripted_sec) <= tolerance:
       emit DurationAdjusted(...)
   else:
       emit ReconciliationFailed(...) + JobRequeued (new_params={"speed": 1.1 or "text_trim": "..."})
   ```
6. All blocks pass → `ReconciliationComplete` → OTIO becomes authoritative (measured_sec = LAW for video).

**MeasurementRequested** effect allows explicit re-measure if artifact changes.

### 7.4 Video Agent (8003)
Unchanged. Uses authoritative measured durations from OTIOProjection for LTX-2.3 prompts: `"Generate exactly {measured_sec}s video matching narration: {text}..."`.

### 7.5 Provisioner Agent (8004) — **Lackey Role Clarified**
- Allocates VMs per job_type (TTS → Qwen3-TTS VM image, LTX → LTX-2.3 image).
- On VM POST /job result → saves `JobCompleted`/`JobFailed` → **immediately POSTs notification to Audio/Video Agent** with artifact_path.
- Handles `ExecuteRawBash` (e.g. for cleanup).
- New responsibility: On `AssembleFinal` effect, can optionally run ffmpeg locally if lightweight, but prefers Assembly Agent.

### 7.6 **NEW: Assembly Agent (port 8005)** — Active only in ASSEMBLY state

**Role:** Sole owner of final assembly. Produces `AssembleFinal` effect (or directly runs ffmpeg via `ExecuteRawBash` for flexibility), validates output, emits `AssemblyCompleted` or `AssemblyFailed`.

**Prompt injection:**
"State: ASSEMBLY. Combine all approved OTIO clips into final_documentary.mp4 using ffmpeg. Verify duration matches timeline. Report with Kind: assemble_final or assembly_completed."

**Typical command emitted:**
```bash
ffmpeg -y \
  -i A1_Narration.wav \
  -i V1_Video.mp4 \
  -c:v libx264 -c:a aac -b:a 192k \
  -shortest -movflags +faststart \
  /artifacts/{run_id}/final_documentary.mp4
```

Then runs ffprobe to confirm duration ±0.1s match → `AssemblyCompleted`.

---

## 8. Effect Parser (Hardened)

- Category-conditioned extraction via `instructor` + `deepseek-v4-flash` unchanged.
- **New rule:** If any effect parse fails confidence < 0.85 or JSON schema violation → emit `ClarificationRequest` with original text snippet + suggested fix.
- Agents instructed: "If you cannot produce a clean Kind: marker, say 'CLARIFY: <question>' so human can correct."

---

## 9. Data Flows (Updated with Assembly & Measurement)

**Normal Reconciliation Cycle** (Audio Agent centric — unchanged logic, clarified execution):

1. Audio Agent → `QueueJob` (tts)
2. Provisioner allocates VM → Job runs → `JobCompleted` (artifact_path)
3. Provisioner POSTs to Audio Agent
4. **Audio Agent** (local): WhisperX → `AudioMeasured`
5. Audio Agent decides: `DurationAdjusted` or `ReconciliationFailed` + `JobRequeued`
6. ... loop until `ReconciliationComplete`

**Video Phase** (same state):
- Video Agent sees `ReconciliationComplete` in projection → starts queuing LTX jobs with measured durations as hard constraint.

**Assembly Phase:**
1. State machine enters ASSEMBLY (all media + reconciled)
2. Assembly Agent receives state instruction + full OTIO
3. Emits `AssembleFinal` (or runs ffmpeg directly)
4. On success → `AssemblyCompleted` + `PipelineComplete`
5. Guard `_assembly_valid_and_complete` → DONE

**Script Error Back-Edge:** Unchanged.

---

## 10. File Structure (Updated)

```
server/v5/
├── effects.py          # 31 effects + EffectUnion + KIND_TO_MODEL + version=1
├── state_machine.py    # Full guards + watcher
├── event_store.py      # + version column
├── projections.py      # OTIO with explicit track model + validations
├── parser.py           # + confidence + ClarificationRequest fallback
├── agents/
│   ├── base.py
│   ├── scenario.py
│   ├── audio.py        # owns WhisperX + reconciliation math
│   ├── video.py
│   ├── provisioner.py
│   └── assembly.py     # NEW
├── vm/
│   ├── agent.py
│   └── onstart_*.sh
├── run_pipeline.py     # Launcher: creates run_id, emits PipelineStarted, starts all
├── dashboard.py        # Optional Streamlit (recommended)
└── ARCHITECTURE_V5.1_REFINED.md
```

---

## 11. Hard Principles (Unchanged + 2 New)

12. **Single source of truth** (event log)
13. **Typed effects only**
14. **No orchestrator**
15. **No timeouts**
16. **No mocks**
17. **Never regex**
18. **Provisioner = lackey**
19. **No B2 for now**
20. **Agent memory = events only**
21. **VM self-destruct on heartbeat loss**
22. **Single writer**
23. **Tick-driven**
**NEW 24. Assembly Agent owns final cut** — no media agent does ffmpeg.
**NEW 25. Measurement is Audio Agent responsibility** — post-notification, local WhisperX.

---

## 12. Glossary (Expanded)

Added:
- **Block**: Narration unit (scene + phrase + voice)
- **Slot**: OTIO position on track (scene:block)
- **Authoritative OTIO**: Post-ReconciliationComplete state
- **MeasurementRequested**: Explicit trigger for re-WhisperX
- **run_id**: UUIDv4 per documentary run (immutable)

---

## 13. Operational & Deployment (NEW — Critical for Completeness)

### 13.1 Artifact Convention
```
/artifacts/
  {run_id}/
    tts/
      {job_id}.wav
    video/
      {job_id}.mp4
    final_documentary.mp4
    logs/
      state_machine.jsonl
```

All agents share this volume (Docker bind-mount or NFS). Paths are absolute and stable.

### 13.2 Bootstrap (run_pipeline.py)
```python
run_id = str(uuid.uuid4())
event_store.append(PipelineStarted(run_id=run_id, ...))
# Start watcher, state machine, 5 agents as asyncio tasks or separate processes
# Expose unified health at :8000/health (reverse proxy or aggregator)
```

### 13.3 Deployment Model (Recommended)
- **Docker Compose** (single host for dev/prod small runs):
  - `event-store`, `watcher-state-machine`, `agent-scenario`, `agent-audio`, `agent-video`, `agent-provisioner`, `agent-assembly`
  - Shared volume `/artifacts`
  - Network: all on `pipeline-net` (localhost ports mapped)
- **Vast.ai GPU VMs**: Provisioner uses `vastai create instance` with pre-built images containing Qwen3-TTS / LTX-2.3 + worker code.
- **Scaling**: One pipeline per `run_id`. Multiple concurrent runs supported (different ports or prefixed).

### 13.4 Human Overseer UI (Recommended)
Simple **Streamlit** app (`dashboard.py`):
- Sidebar: Select run_id
- Tabs: State | OTIO Timeline (visual) | Jobs | VMs | Failures
- Big "Correct" button → POST to chosen agent
- Auto-refresh every 2s via GET /

Alternative: Pure HTTP + curl / jq for power users.

### 13.5 Observability & Cost Control
- Every component logs JSON: `{"ts":..., "run_id":..., "kind":"effect"|"tick"|"error", ...}`
- `VMProjection` tracks `total_gpu_hours` and `estimated_cost_usd`
- Hard limits: max 50 requeues per job, max 3h wall time per run → emit `PipelineAborted`
- Prometheus `/metrics` endpoint on state machine (optional but recommended).

### 13.6 Security (MVP → Prod)
- **MVP**: All localhost, no auth (trust local network).
- **Prod**: mTLS between components + API keys on POST /. Human dashboard behind OAuth2.

### 13.7 Error Recovery Playbook
- **Infinite loop**: `AgentLoopDetected` → human POST "break loop: accept current durations"
- **VM OOM**: `JobFailed` (oom) → Provisioner tries larger GPU offer
- **Bad narration**: Script failure → back to SCRIPT automatically
- **Stuck VM**: 15min heartbeat loss → self-destruct + `JobFailed` (timeout)

---

## 14. OTIO Data Model (NEW — Explicit)

```python
# Conceptual (actual otio.schema.Timeline)
timeline.tracks = [
    otio.schema.Track(name="A1_Narration", kind="Audio"),
    otio.schema.Track(name="V1_Video", kind="Video")
]
# Each clip:
clip.metadata = {
    "block_id": "3:2",
    "scene_num": 3,
    "voice_role": "narrator",
    "scripted_sec": 4.8,
    "measured_sec": 4.7,   # authoritative after reconciliation
    "dopamine_hook": "..."
}
clip.media_reference = otio.schema.ExternalReference(target_url=f"file:///artifacts/{run_id}/...")
clip.source_range = otio.opentime.TimeRange(..., duration=otio.opentime.RationalTime(measured_sec, 1))
```

All validations (`validate_no_overlaps`, `validate_track_alignment`, `validate_clip_media`) now return structured `ProductionFailed` ready for routing.

---

## 15. Implementation Roadmap (V5.1)

1. **Week 1**: Effects + EventStore + Projections + Parser (core)
2. **Week 2**: State machine + all guards + watcher
3. **Week 3**: Scenario + Audio + Video agents + reconciliation loop + WhisperX integration
4. **Week 4**: Provisioner + VM worker + Vast.ai integration
5. **Week 5**: Assembly Agent + ffmpeg + final validation
6. **Week 6**: Dashboard + hardening + first end-to-end run on 3-scene test documentary

---

**This V5.1 specification is now complete for implementation.** All ambiguities resolved, all missing pieces added, all principles preserved. Ready for concrete coding.

*Version: 2026-05-26 v5.1 — Refined & Complete*
