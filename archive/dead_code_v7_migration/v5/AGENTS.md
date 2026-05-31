> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Agent Specifications V5

> Architecture-only document. No code.
>
> Defines the behavior, prompts, and decision logic for all 4 pipeline agents.

---

## 1. Agent Framework (Abstract)

Every agent is an HTTP service with exactly two endpoints:

```
GET  /  → JSON: {name, status, last_error, current_task, idle_since, context_summary}
POST /  → Accepts: text instruction. Returns: {status: "accepted", task_id}
```

**No persistent memory.** Each POST / triggers a fresh LLM call. Context is rebuilt from the event log on every turn.

**Async pattern:**
1. State machine POSTs instruction → agent returns 202 immediately
2. Agent runs LLM call in background (minutes possible)
3. Agent parses its own output into Effects
4. Agent appends Effects to event store
5. State machine polls GET / to check status

---

## 2. Scenario Agent

### 2.1 Identity

- **Name:** `scenario`
- **Port:** 8001
- **Active states:** `init`, `script`
- **LLM:** deepseek-v4-flash
- **Tools:** none (produces effects only)

### 2.2 Prompt

```
System: You are the Scenario Agent. You write documentary narration scripts.
Your output is the creative foundation of the entire pipeline. Every scene
you write will be voiced by TTS and visualized by LTX-2.3.

System: Current state: {state_name}. {state_instruction}

System: The documentary brief:
{brief}

System: Current script state (from event log):
{otio_summary_json}

System: Failures requiring script revision (if any):
{script_errors_json}

User: Write or revise the narration script. Report your work with a kind marker.

Kind: update_script / delete_scene / reorder_scenes / noop

Describe what you wrote naturally. Include speaker, text, pronunciation hints,
visual notes, dopamine hook, and target duration for each scene.
```

### 2.3 State Instructions

| State | Instruction |
|---|---|
| `init` | Write the first draft. Create 3-7 scenes with distinct speakers. Target 20-40 seconds total narration. Mark each scene with a clear dopamine hook. |
| `script` | Refine existing scenes. Fix failures. Ensure speaker consistency. Adjust pacing. Do NOT produce media — only script effects. |

### 2.4 Decision Logic

1. Read current OTIO summary (rebuilt from events)
2. If gaps exist → write `UpdateScript` for missing scenes
3. If script_errors exist (from `ProductionFailed` with `gap_unexpected` or `voice_mismatch`) → revise offending scenes
4. If no gaps and no errors → `NoOp` (wait for state machine to transition)

### 2.5 Output Format

```
I've written Scene 3 with Speaker V1 narrating the market crash.

Kind: update_script
Scene: 3
Speaker: V1
Text: "The housing market collapsed in 2008, taking 8 trillion dollars with it."
Pronunciation hints: ["trillion: TRIL-yun"]
Visual notes: "Stock ticker falling, red numbers"
Dopamine hook: "The number that broke the world"
Duration: 6.5
```

---

## 3. Audio Agent

### 3.1 Identity

- **Name:** `audio`
- **Port:** 8002
- **Active states:** `audio_video`
- **LLM:** deepseek-v4-flash
- **Tools:** none (produces effects only)

### 3.2 Prompt

```
System: You are the Audio Agent. You own the narration reconciliation loop.
Your job is to produce TTS audio that matches the scripted durations.
You are the final authority on audio quality.

System: Current state: {state_name}. {state_instruction}

System: The reconciliation loop:
1. QueueJob (TTS) → VM generates WAV
2. WhisperX measures actual duration
3. Compare measured vs scripted (±15% or ±0.25s tolerance)
4. Within tolerance? → DurationAdjusted (update OTIO)
5. Outside tolerance? → ReconciliationFailed → JobRequeued (new text/pacing)
6. All blocks pass? → ReconciliationComplete (OTIO is now authoritative)

System: Current OTIO (scripted durations):
{otio_summary_json}

System: Job queue status:
{job_summary_json}

System: Reconciliation status:
{reconciliation_status_json}

System: VM inventory:
{vm_summary_json}

User: Produce audio. Formulate jobs. Judge output. Approve or reject.
Report with kind markers.

Kind: queue_job / job_approved / job_requeued / duration_adjusted
      / reconciliation_failed / reconciliation_complete / merge_into_otio / noop
```

### 3.3 State Instructions

| State | Instruction |
|---|---|
| `audio_video` | **Phase 1 — Reconciliation:** Run the TTS→WhisperX loop until all narration blocks pass tolerance. **Phase 2 — Video support:** Once ReconciliationComplete is emitted, video production begins. You may still approve/reject video jobs if they involve audio sync. |

### 3.4 Reconciliation Loop (Detailed)

This is the **intense iterative process** from the deep git history. The Audio Agent implements it directly:

```
For each narration block in A1_Narration track:
    1. Check if block already has measured_sec (from prior loop)
       → Yes: skip to next block
       → No: continue

    2. Formulate TTS parameters:
       - text: the narration phrase
       - voice: the speaker role
       - target_duration: scripted_sec (hint only, not constraint)

    3. Emit QueueJob(job_type="tts", ...)

    4. Wait for JobCompleted from Provisioner
       (Provisioner POSTs directly to this agent)

    5. On receipt:
       a. Emit AudioGenerated (record that TTS exists)
       b. Emit AudioMeasured (WhisperX result)

    6. Compare:
       delta = |measured_sec - scripted_sec|
       ratio = delta / scripted_sec

       IF delta <= 0.25s OR ratio <= 0.15:
           → Emit DurationAdjusted
           → Mark block as passed
       ELSE:
           → Emit ReconciliationFailed
           → Analyze: is the text too long? too short? pacing wrong?
           → Emit JobRequeued with new params (shorter text, different pauses, etc.)
           → Loop back to step 3 (max 5 attempts per block)

After all blocks pass:
    → Emit ReconciliationComplete
    → OTIO is now authoritative. Measured durations are LAW for video.
```

### 3.5 Tolerance Rules

```
PASS if: |measured - scripted| <= 0.25 seconds
     OR: |measured - scripted| / scripted <= 15%

FAIL otherwise.

Special cases:
- If scripted_sec < 1.0s: use absolute tolerance only (0.25s)
- If scripted_sec > 30.0s: use ratio only (15%)
```

### 3.6 Reconciliation Failure Analysis

When `ReconciliationFailed` is emitted, it must include:

```python
ReconciliationFailed(
    agent="audio",
    blocks_total=N,
    blocks_passed=K,
    blocks_failed=M,
    failures=[
        {
            "block_id": "scene_003_V1",
            "scene_num": 3,
            "phrase_idx": 0,
            "voice": "V1",
            "scripted_sec": 5.0,
            "measured_sec": 7.2,
            "delta_sec": 2.2,
            "ratio": 0.44,
            "message": "OUT OF TOLERANCE: measured=7.20s scripted=5.00s delta=+2.20s (+44.0%)"
        }
    ],
    worst_delta_sec=2.2,
    suggested_adjustments=[
        {"block_id": "scene_003_V1", "action": "shorten_text", "new_target_sec": 5.0}
    ]
)
```

**Suggested adjustment actions:**
- `shorten_text` — Reduce word count, remove filler
- `lengthen_text` — Add detail, expand contractions
- `change_pacing` — Add/remove commas, split/join sentences
- `accept_deviation` — Human override, accept the measured duration

### 3.7 Artistry Judgment

After `JobCompleted`, the Audio Agent judges quality before approving:

```
Check:
1. Audio file exists and is non-empty
2. Duration matches WhisperX measurement
3. No obvious artifacts (clipping, silence, noise)
4. Voice matches intended speaker role

If pass → JobApproved + MergeIntoOTIO
If fail → JobRequeued + reason
```

---

## 4. Video Agent

### 4.1 Identity

- **Name:** `video`
- **Port:** 8003
- **Active states:** `audio_video`
- **LLM:** deepseek-v4-flash
- **Tools:** none (produces effects only)

### 4.2 Prompt

```
System: You are the Video Agent. You produce video clips using LTX-2.3.
You use the authoritative OTIO (measured durations) as LAW.
You are the final authority on video quality.

System: Current state: {state_name}. {state_instruction}

System: The authoritative OTIO (measured durations are LAW):
{otio_summary_json}

System: Job queue status:
{job_summary_json}

System: VM inventory:
{vm_summary_json}

User: Produce video clips. Formulate jobs. Judge output. Approve or reject.
Report with kind markers.

Kind: queue_job / job_approved / job_requeued / merge_into_otio / noop
```

### 4.3 State Instructions

| State | Instruction |
|---|---|
| `audio_video` | Wait for ReconciliationComplete before starting video jobs. Once authoritative OTIO exists, generate LTX-2.3 clips matching measured durations exactly. Do not deviate from measured durations. |

### 4.4 Video Production Rules

1. **Do NOT start until `ReconciliationComplete`** — Video uses measured durations as LAW
2. **One clip per scene** — Each scene gets one V1_Video slot
3. **Duration must match measured exactly** — LTX-2.3 prompt includes frame count
4. **Visual description comes from script** — Use `visual_notes` and `dopamine_hook`

### 4.5 Artistry Judgment

```
Check:
1. Video file exists and is non-empty
2. Duration matches measured_sec exactly
3. Visual content matches scene description
4. No obvious artifacts (flicker, coherence breaks)
5. Audio sync points align (if audio track provided)

If pass → JobApproved + MergeIntoOTIO
If fail → JobRequeued + reason
```

### 4.6 Failure Types That Blame Script

If the Video Agent detects:
- `voice_mismatch` — Video narration doesn't match audio
- `gap_unexpected` — Silent visual where narration should exist

→ Emit `ProductionFailed` with appropriate `failure_type`
→ State machine will route back to SCRIPT state

---

## 5. Provisioner Agent

### 5.1 Identity

- **Name:** `provisioner`
- **Port:** 8004
- **Active states:** `audio_video`
- **LLM:** deepseek-v4-flash
- **Tools:** bash (vastai CLI, ssh, curl)

### 5.2 Prompt

```
System: You are the Provisioner Agent. You are the lackey.
Your job is to provision GPU VMs, dispatch jobs, and report results.
You do NOT judge quality. You do NOT make creative decisions.
You provision. You report. You deliver.

System: Current state: {state_name}. {state_instruction}

System: Pending jobs (from Job Projection):
{job_summary_json}

System: Active VMs (from VM Projection):
{vm_summary_json}

System: Vast.ai offers (live):
{vastai_offers_json}

User: Provision VMs. Dispatch jobs. Handle completions and failures.
Report with kind markers.

Kind: vm_allocated / vm_deallocated / vm_provision_failed
      / job_completed / job_failed / noop
```

### 5.3 State Instructions

| State | Instruction |
|---|---|
| `audio_video` | Monitor pending jobs. Provision VMs as needed. Dispatch jobs to VMs. Receive VM reports. Forward completions to media agents. Destroy idle VMs. |

### 5.4 Behavior

```
Every turn:
1. Read Job Projection → get pending jobs
2. Read VM Projection → get active VMs
3. For each pending job:
   a. Find or provision a VM with appropriate GPU
      - TTS jobs: any CUDA GPU (RTX 3090+, A4000+)
      - LTX jobs: high-VRAM GPU (A100, H100, RTX 4090+)
   b. If no suitable VM → VMAllocated (provision new)
   c. POST job to VM
   d. VM reports completion → JobCompleted
   e. VM reports failure → JobFailed
   f. Forward JobCompleted/JobFailed to media agent via POST /
4. For idle VMs (no jobs for 10+ minutes):
   a. VMDeallocated
5. For failed provisions:
   a. VMProvisionFailed
   b. Retry with different offer (max 3 attempts)
```

### 5.5 Return Path (Provisioner → Media Agent)

```
VM → POST / to Provisioner: "Job 123 done. File at /tmp/scene3.wav"
Provisioner → saves JobCompleted effect to event store
Provisioner → POST / to Audio Agent: "Job 123 completed. /tmp/scene3.wav. Duration 5.2s"
```

This is the **only** direct agent-to-agent communication allowed.

### 5.6 VM Bootstrap

The Provisioner generates on-start scripts for VMs:

**TTS VM:**
```bash
#!/bin/bash
# Install Qwen3-TTS
pip install qwen-tts
# Start VM agent
python vm_agent.py --role tts --port 9000
```

**Video VM:**
```bash
#!/bin/bash
# Install LTX-2.3
pip install ltx-video
# Start VM agent
python vm_agent.py --role ltx --port 9000
```

### 5.7 Credential Flow (Current — Unsafe)

```
Provisioner → writes DeepSeek API key into on-start script
VM on-start → exports DEEPSEEK_API_KEY
VM agent → uses key for LLM calls
```

**Note:** This will be amended later with a JWT broker. For now, the API key is sent to the VM in the on-start script.

---

## 6. VM Agent (Runs ON GPU Instance)

### 6.1 Identity

- **Name:** `vm_worker`
- **Port:** 9000+
- **Role:** TTS or LTX inference
- **LLM:** deepseek-v4-flash (via API, not local)

### 6.2 HTTP Surface

```
GET  /  → {role, status, current_job, progress_percent, last_output}
POST /  → {job_type, params} → accepts job, returns 202
```

### 6.3 Behavior

```
1. Receives job via POST /
2. Runs inference via bash command
   - TTS: python -m qwen_tts.generate --text "..." --output /tmp/out.wav
   - LTX: python -m ltx.generate --prompt "..." --frames N --output /tmp/out.mp4
3. Judges output via LLM call:
   - File size reasonable?
   - Duration matches expectation?
   - No obvious corruption?
4. Reports to Provisioner via POST /
   - Success: "Job 123 done. /tmp/out.wav. Duration 5.2s"
   - Failure: "Job 123 failed. OOM. Retryable: true"
5. Monitors heartbeat
   - GET /health from Provisioner every 60s
   - 15 min without heartbeat → self-destruct
     ```bash
     vastai destroy instance $INSTANCE_ID
     ```
```

### 6.4 Self-Destruction

```python
# In VM agent heartbeat monitor
if time_since_last_heartbeat > 900:  # 15 minutes
    subprocess.run(["vastai", "destroy", "instance", os.environ["INSTANCE_ID"]])
    sys.exit(1)
```

---

## 7. Agent Coordination Rules

### 7.1 No Direct Communication (Except One Exception)

| Source → Target | Allowed | Channel |
|---|---|---|
| Any agent → Event store | Yes | Effects |
| Event store → Projections | Yes | Notification |
| Projections → State machine | Yes | Guard reads |
| State machine → Agents | Yes | POST / instruction |
| Provisioner → Audio/Video agent | **Yes** | POST / job result |
| Audio → Video agent | No | — |
| Video → Scenario agent | No | — |
| VM → Any agent (except Provisioner) | No | — |

### 7.2 Context Rebuild

Every agent turn rebuilds context from the event log:

```
1. Replay all events for this run_id
2. Build OTIO summary (scenes, slots, durations, gaps)
3. Build job summary (pending, running, completed, failed)
4. Build VM summary (active, idle, destroyed)
5. Build reconciliation status (complete? failed? attempts?)
6. Inject into prompt as JSON
```

### 7.3 Human Intervention

```
1. Human GETs agent → sees full context, status, errors
2. Human POSTs text → "Scene 3 is too long, cut the second paragraph"
3. Next agent turn includes human instruction in context
4. Agent responds to human instruction
5. Effects appended normally
```

---

## 8. Prompt Construction (T/M/D/R/W Pattern)

Every agent prompt follows the T/M/D/R/W pattern from earlier architectures:

| Slot | Content | Source |
|---|---|---|
| **T** (Task) | What to do this turn | State machine based on current state |
| **M** (Memory) | Previous effects for this agent | Replayed from event log |
| **D** (Data) | Current projections (OTIO, jobs, VMs) | Rebuilt from event log |
| **R** (Rules) | State-specific instructions | Hardcoded per state |
| **W** (Weights) | Budget/emphasis adjustments | State machine when over budget |

### 8.1 Example: Audio Agent Prompt

```
--- T: Task ---
You are the Audio Agent. Produce TTS audio for the documentary.
Current pipeline state: audio_video (Phase 1: Reconciliation)

--- M: Memory ---
Your previous effects:
- QueueJob(j1, tts, scene 1, "Hello world")
- QueueJob(j2, tts, scene 2, "The market crashed")

--- D: Data ---
OTIO Summary:
{
  "A1_Narration": {
    "slots": [
      {"slot_id": "A1:1:0", "speaker": "V1", "text": "Hello world", "scripted_sec": 5.0, "measured_sec": null, "status": "pending"},
      {"slot_id": "A1:2:0", "speaker": "V2", "text": "The market crashed", "scripted_sec": 3.5, "measured_sec": null, "status": "pending"}
    ]
  }
}

Job Summary:
{"pending": 2, "running": 0, "completed": 0, "failed": 0}

VM Summary:
{"active": 1, "role": "tts", "url": "http://vm-1:9000"}

--- R: Rules ---
Run the reconciliation loop:
1. QueueJob (TTS) → VM generates WAV
2. WhisperX measures actual duration
3. Compare measured vs scripted (±15% or ±0.25s)
4. Within tolerance? → DurationAdjusted
5. Outside tolerance? → ReconciliationFailed → JobRequeued
6. All blocks pass? → ReconciliationComplete

--- W: Weights ---
Current budget: $6.17 on Vast.ai. Prefer cheaper offers. No budget alarm.

User: Run your turn. Formulate jobs. Judge output. Report with kind markers.
```

---

*Version: 2026-05-17 v5*
