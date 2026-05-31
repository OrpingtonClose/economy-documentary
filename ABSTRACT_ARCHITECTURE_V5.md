> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Abstract Architecture V5 — Documentary Pipeline

> Revised 2026-05-17. eventsourcing library for CQRS. python-statemachine with tick event. OTIO validation in guards. Provisioner talks directly to media agents.
>
> Research: Perplexity (eventsourcing ProcessApplication, python-statemachine cyclic/eventless, OTIO validation APIs).

---

## 0. What Changed From V4

| V4 | V5 |
|---|---|
| Hand-rolled SQLite event store | **`eventsourcing` library** — ProcessApplication for projections, built-in snapshotting, checkpoints, notification logs |
| python-statemachine eventless transitions (no step() API) | **Explicit `tick` event** from watcher loop. Guards evaluate on each tick. |
| Projections hand-rolled incremental | **ProcessApplication projections** with `tick()` + `policy()` + checkpoints |
| Agents communicate only via event store | **Provisioner can POST / directly to media agents** — exception for job completion notifications |
| OTIO validation unspecified | **OTIO validation functions** in state machine guards: overlap checks, duration alignment, track consistency |
| State machine guards read projections | **State machine guards read projection ProcessApplications** directly |

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
│  python-statemachine. Watcher loop sends `tick` event every 1s.          │
│  Guards read ProcessApplication projections.                             │
│  No orchestrator.                                                        │
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
       │                  │◄─────────────────┘                  │
       │                  │   (Provisioner POSTs job results)   │
       │                  │                                     │
       ▼                  ▼                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              EVENT STORE (eventsourcing library)                         │
│  Application: stores domain events. Notification log for projections.    │
│  SQLite backend. Built-in snapshotting.                                  │
└─────────────────────────────────────────────────────────────────────────┘
       ▲                  ▲                  ▲                  ▲
       │                  │                  │                  │
       ▼                  ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│           PROJECTIONS (ProcessApplications with checkpoints)             │
│  Each follows the notification log. Calls tick() to process new events.  │
│  OTIO Projection | Job Projection | VM Projection | State Projection     │
│  port 8101       | port 8102      | port 8103     | port 8104            │
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

**Endpoint Rule:** Every box exposes exactly `GET /` and `POST /` on its own port.

---

## 2. Components

### 2.1 Event Store (eventsourcing library)

**Library:** `eventsourcing` by John Bywater (`pip install eventsourcing`).

**Why this library:**
- Built-in snapshotting for aggregates
- `ProcessApplication` for projections with `tick()` + `policy()` + checkpoints
- SQLite and PostgreSQL backends
- Notification logs for incremental consumption
- No hand-rolling of queues, locks, or serialization

**Application Class:**
```python
from eventsourcing.application import Application
from eventsourcing.domain import Aggregate, event

class PipelineAggregate(Aggregate):
    """The pipeline run itself is an aggregate."""
    @event("PipelineStarted")
    def __init__(self, run_id: str, brief: str):
        self.run_id = run_id
        self.brief = brief
        self.current_state = "init"

class PipelineApplication(Application):
    def start_pipeline(self, run_id: str, brief: str):
        pipeline = PipelineAggregate(run_id=run_id, brief=brief)
        self.save(pipeline)
        return pipeline.id
```

**Events as effects:** Each agent effect becomes a domain event on the pipeline aggregate:
```python
@event("ScriptUpdated")
def update_script(self, scene: int, speaker: str, text: str):
    self.script[scene] = {"speaker": speaker, "text": text}

@event("JobQueued")
def queue_job(self, job_id: str, kind: str, scene: int, slot: int, params: dict):
    self.jobs[job_id] = {"kind": kind, "scene": scene, "slot": slot, 
                         "params": params, "status": "pending"}

@event("JobCompleted")
def complete_job(self, job_id: str, artifact_path: str, duration: float):
    self.jobs[job_id]["status"] = "completed"
    self.jobs[job_id]["artifact_path"] = artifact_path
    self.jobs[job_id]["duration"] = duration
```

**Why aggregate events instead of flat effects:** The eventsourcing library is built around aggregates. The pipeline run IS the aggregate. All effects are events on this aggregate. This gives us snapshotting for free.

---

### 2.2 Projections (ProcessApplications)

**Pattern:** Each projection is a `ProcessApplication` that:
1. `follow()`s the PipelineApplication's notification log
2. Has a `policy(event)` method that routes events to projection handlers
3. Calls `tick()` to process new events incrementally
4. Maintains tracking positions (checkpoints) automatically

```python
from eventsourcing.application import ProcessApplication

class OTIOProjection(ProcessApplication):
    def __init__(self):
        super().__init__()
        self.timeline = otio.schema.Timeline(name="Documentary")
    
    def policy(self, domain_event, **kwargs):
        event_name = domain_event.__class__.__name__
        if event_name == "ScriptUpdated":
            self.on_script_updated(domain_event)
        elif event_name == "JobCompleted":
            self.on_job_completed(domain_event)
        elif event_name == "OTIOMerged":
            self.on_otio_merged(domain_event)
    
    def on_script_updated(self, event):
        # Update timeline with new script segment
        pass
    
    def on_job_completed(self, event):
        # Mark slot as ready in timeline
        pass
    
    def on_otio_merged(self, event):
        # Add clip to track
        pass
    
    # Validation methods for state machine guards
    def validate_no_overlaps(self):
        """Check that clips don't overlap illegally."""
        for track in self.timeline.tracks:
            children = list(track)
            for i in range(len(children) - 1):
                a, b = children[i], children[i + 1]
                if isinstance(a, otio.schema.Transition) or isinstance(b, otio.schema.Transition):
                    continue
                ra = a.trimmed_range_in_parent()
                rb = b.trimmed_range_in_parent()
                if not (ra.end_time_exclusive() <= rb.start_time):
                    return False, f"Overlap: {a.name} and {b.name}"
        return True, None
    
    def validate_track_alignment(self):
        """Check that track durations align with timeline duration."""
        track_durs = [t.duration() for t in self.timeline.tracks]
        if not track_durs:
            return True, None
        max_dur = max(track_durs, key=lambda rt: rt.value)
        timeline_dur = self.timeline.duration()
        if timeline_dur.value != max_dur.value:
            return False, f"Timeline {timeline_dur.value} != max track {max_dur.value}"
        return True, None
    
    def validate_clip_media(self):
        """Check that all clips have media references and valid ranges."""
        for track in self.timeline.tracks:
            for child in track:
                if isinstance(child, otio.schema.Clip):
                    if child.media_reference is None:
                        return False, f"Clip {child.name} missing media_reference"
                    try:
                        _ = child.trimmed_range()
                    except Exception as e:
                        return False, f"Clip {child.name} invalid range: {e}"
        return True, None
```

**Projection Services:**

| Projection | Port | Follows Events | HTTP GET / Returns |
|---|---|---|---|
| OTIOProjection | 8101 | `ScriptUpdated`, `JobCompleted`, `OTIOMerged` | JSON: scenes, slots, durations, validation results |
| JobProjection | 8102 | `JobQueued`, `JobCompleted`, `JobFailed`, `JobRequeued` | JSON: pending, running, completed, failed |
| VMProjection | 8103 | `VMAllocated`, `VMDeallocated`, `VMProvisionFailed`, `VMObserved` | JSON: active VMs, roles, Vast.ai state |
| StateProjection | 8104 | `StateTransitioned` | JSON: current state, history, time in state |

**VMProjection special behavior:**
```python
class VMProjection(ProcessApplication):
    def policy(self, domain_event, **kwargs):
        event_name = domain_event.__class__.__name__
        if event_name in ("VMAllocated", "VMDeallocated", "VMProvisionFailed", "VMObserved"):
            self.on_vm_event(domain_event)
    
    def on_vm_event(self, event):
        # Also call vastai CLI for current truth
        import subprocess
        result = subprocess.run(["vastai", "show", "instances"], 
                               capture_output=True, text=True)
        # Compare events with CLI output
        # Emit VMObserved if drift detected
        pass
```

**Why ProcessApplication:** `tick()` processes only new events since last checkpoint. Tracking positions are persisted. On restart, resumes from checkpoint. No O(n²). No hand-rolled queue.

---

### 2.3 State Machine (python-statemachine)

**Framework:** `python-statemachine` with `StateChart` API.

**Critical finding from research:** Eventless transitions only re-evaluate when an event is processed. There's no `step()` method. **Workaround:** Define a `tick` event and send it from the watcher loop every 1 second.

```python
from statemachine import StateChart, State

class PipelineStateMachine(StateChart):
    # States
    init = State(initial=True)
    script = State()
    audio_video = State()
    assembly = State()
    done = State(final=True)

    # The tick event — sent by watcher loop every 1s
    tick = (
        init.to(script, cond="script_has_scenes")
        | script.to(audio_video, cond="script_complete")
        | script.to.itself(cond="script_needs_refinement")
        | audio_video.to(assembly, cond="all_jobs_done")
        | audio_video.to(script, cond="script_needs_rework")
        | audio_video.to.itself(cond="jobs_still_pending")
        | assembly.to(done, cond="assembly_complete")
        | assembly.to.itself(cond="assembly_in_progress")
    )

    def __init__(self, otio_proj, job_proj, vm_proj):
        super().__init__()
        self.otio = otio_proj
        self.jobs = job_proj
        self.vms = vm_proj

    # ---- GUARDS ----
    # Each guard reads from projection ProcessApplications
    
    def script_has_scenes(self, event, source, target):
        """Init -> Script: OTIO has at least one scene."""
        return len(self.otio.timeline.tracks) > 0
    
    def script_complete(self, event, source, target):
        """Script -> Audio_Video: all scenes have text for all speakers."""
        # Check OTIO projection
        for track in self.otio.timeline.tracks:
            for child in track:
                if isinstance(child, otio.schema.Gap):
                    return False  # Gaps mean missing content
        return True
    
    def script_needs_refinement(self, event, source, target):
        """Script -> Script: script exists but incomplete."""
        return not self.script_complete(event, source, target)
    
    def all_jobs_done(self, event, source, target):
        """Audio_Video -> Assembly: 0 pending, 0 running, >0 completed."""
        pending = sum(1 for j in self.jobs.jobs.values() if j["status"] == "pending")
        running = sum(1 for j in self.jobs.jobs.values() if j["status"] == "running")
        completed = sum(1 for j in self.jobs.jobs.values() if j["status"] == "completed")
        return pending == 0 and running == 0 and completed > 0
    
    def jobs_still_pending(self, event, source, target):
        """Audio_Video -> Audio_Video: still working."""
        pending = sum(1 for j in self.jobs.jobs.values() if j["status"] == "pending")
        running = sum(1 for j in self.jobs.jobs.values() if j["status"] == "running")
        return pending > 0 or running > 0
    
    def script_needs_rework(self, event, source, target):
        """Audio_Video -> Script: failed jobs indicate script error."""
        failed = [j for j in self.jobs.jobs.values() if j["status"] == "failed"]
        return any("script" in j.get("error", "").lower() for j in failed)
    
    def assembly_complete(self, event, source, target):
        """Assembly -> Done: final MP4 exists and OTIO validates."""
        # Check file existence
        import os
        output_path = "/tmp/output.mp4"  # from config
        if not os.path.exists(output_path):
            return False
        # OTIO validation
        ok, msg = self.otio.validate_no_overlaps()
        if not ok:
            return False
        ok, msg = self.otio.validate_track_alignment()
        if not ok:
            return False
        return True
    
    def assembly_in_progress(self, event, source, target):
        """Assembly -> Assembly: still assembling."""
        return not self.assembly_complete(event, source, target)
```

**Watcher Loop:**
```python
async def state_machine_watcher(machine, projections):
    """Runs forever. Sends tick every 1s. Projections tick first."""
    while True:
        # 1. Advance all projections (process new events)
        for proj in projections:
            proj.tick()
        
        # 2. Send tick to state machine (evaluates guards)
        await machine.tick()
        
        # 3. Throttle
        await asyncio.sleep(1)
```

**Why tick event:** python-statemachine requires an explicit event to trigger guard evaluation. The watcher loop provides this. Guards read projection state. Transitions fire when guards are true.

**Cyclic transitions confirmed working:** `audio_video.to(script)` is valid. `script.to(audio_video)` is valid. The machine can cycle between these states.

---

### 2.4 Agents (4 Types)

| Agent | Port | Active States | Role |
|---|---|---|---|
| **Scenario** | 8001 | `init`, `script` | Writes narration. Revises on script_needs_rework. |
| **Audio** | 8002 | `audio_video` | Formulates TTS jobs. Judges audio artistry. Receives job results from Provisioner. |
| **Video** | 8003 | `audio_video` | Formulates LTX jobs. Judges video artistry. Receives job results from Provisioner. |
| **Provisioner** | 8004 | `audio_video` | Provisions/destroys VMs. Receives VM reports. Notifies media agents of completions. |

**Agent HTTP Surface:**
- `GET /` → JSON: current context, last effects, current state, idle time
- `POST /` → Accepts text instruction. Returns 202 Accepted + `task_id`

**Async turn pattern:**
1. State machine enters state → activates agents for that state
2. State machine POSTs state summary JSON to each active agent
3. Agent returns 202 Accepted
4. Agent runs LLM call (minutes possible)
5. Agent produces effects → PipelineApplication saves them
6. State machine polls agent GET / to check status
7. If agent stuck: overseer POSTs correction

**Agent prompt construction (rebuilt each turn):**
```
System: You are the {agent_name} agent. {base_persona}
System: Current state: {state}. {state_instructions}
User: {state_summary_json}
User: {human_corrections_if_any}
```

**No conversation history across runs.** Each turn is independent.

---

### 2.5 Provisioner ↔ Media Agent Direct Communication

**Normal flow (via event store):**
```
Audio Agent → QueueJob effect → PipelineApplication → notification log
                                      ↓
Provisioner ticks → sees pending jobs → provisions VM
```

**Direct communication exception (job completion):**
```
VM → reports JobCompleted to Provisioner POST /
Provisioner → saves JobCompleted effect to PipelineApplication
Provisioner → POST / to Audio Agent: "Job 123 completed. File at /tmp/..."
Audio Agent → receives notification, judges artistry
Audio Agent → produces JobApproved or JobRequeued effect
```

**Why direct:** The media agent needs to judge output quality. The Provisioner is the lackey. When a job finishes, the Provisioner tells the artist directly. The artist decides approve/reject.

**This is the ONLY direct agent-to-agent communication allowed.** All other coordination goes through the event store.

---

### 2.6 VM Agent (runs ON GPU instance)

Same as V4:
- GPU runs inference worker (TTS/LTX)
- VM agent HTTP service calls deepseek-v4-flash via API
- `GET /` → status, progress, last output
- `POST /` → receives job, returns 202 Accepted

**VM Agent Behavior:**
1. Receives job via POST /
2. Runs inference via bash
3. Judges output via LLM call (metadata: file size, duration)
4. Reports to Provisioner via POST /
5. Monitors Health Service GET / every 60s
6. 15 min without heartbeat → self-destruct via `vastai destroy`

**Credentials:** Vast.ai API key sent via on-start script (temporary, unsafe).

---

### 2.7 Effect Parser

Same category-conditioned generation as V4:
- Agent knows 12 abstract `kind` words
- Parser extracts `kind` via string find
- `instructor` + `deepseek-v4-flash` maps to Pydantic model
- `max_retries` case-by-case (usually 2-3)
- NEVER regex. NEVER optimize cost.

**Agent prompt snippet:**
```
Report your work with a kind marker:
Kind: script / audio_job / video_job / vm_alloc / vm_free / vm_fail
       / otio_merge / job_done / job_fail / requeue / transition / abort / clarify

Describe what happened naturally.
```

---

## 3. Data Flow

### 3.1 Normal Cycle

```
1. Watcher loop wakes (every 1s)
2. All projections call tick() — process new events since last checkpoint
3. Watcher sends tick event to state machine
4. State machine evaluates guards using projection state
5. If guard true → transition fires
6. On entering new state → activate agents for that state
7. State machine POSTs state summary JSON to active agents
8. Agents return 202 Accepted
9. Agents run LLM calls, produce effects
10. PipelineApplication saves effects (aggregate events)
11. Projections will pick them up on next tick
12. Go to 1
```

### 3.2 VM Job Completion Flow

```
1. VM finishes inference
2. VM judges output (LLM call, metadata)
3. VM POSTs to Provisioner: "Job 123 done. File at /tmp/scene3.wav"
4. Provisioner saves JobCompleted effect to PipelineApplication
5. Provisioner POSTs to Audio Agent: "Your job 123 is done. Verify."
6. Audio Agent receives, GETs Job Projection for context
7. Audio Agent judges artistry via LLM
8. Audio Agent produces: JobApproved → MergeIntoOTIO effect
              or: JobRequeued → modified params, reason
```

### 3.3 Exception Flow

```
1. Exception during agent turn
2. Agent catches, sets status="error" in its state
3. State machine polls agent GET /, sees error
4. State machine keeps agent active (same state)
5. Next tick: agent receives exception in context
6. Agent responds with diagnosis + fix
7. Effects saved to PipelineApplication
8. Cycle continues
```

### 3.4 Human Intervention Flow

```
1. Human GETs agent → sees state, context, errors
2. Human POSTs text → appended to agent context
3. Next agent turn incorporates human instruction
4. Effects saved
5. Pipeline continues
```

---

## 4. State Machine

### 4.1 States

```
[INIT] → [SCRIPT] → [AUDIO_VIDEO] → [ASSEMBLY] → [DONE]
            ↑__________|____________|
                 (retry loops)
```

**Old mermaid chart alignment:**
- Kept: 5-stage flow (init→script→audio/video→assembly→done)
- Kept: SCRIPT ↔ AUDIO_VIDEO back-edge for rewrites
- Removed: QA jury, Bearnaise gates, preference ledger, dashboard, human gates, L0-L4 ladders, preview assemblies, blackboard, scenario evaluator, coherence evaluator
- Simplified: One media production loop instead of separate audio/video ladders

### 4.2 Guard Summary

| Guard | Condition | OTIO Validation Used |
|---|---|---|
| `script_has_scenes` | OTIO has tracks | No |
| `script_complete` | No gaps in tracks | No |
| `script_needs_refinement` | Gaps exist | No |
| `all_jobs_done` | 0 pending, 0 running, >0 completed | No |
| `jobs_still_pending` | pending > 0 or running > 0 | No |
| `script_needs_rework` | Failed jobs with "script" in error | No |
| `assembly_complete` | Output file exists + no overlaps + track alignment + clip media valid | **Yes** |
| `assembly_in_progress` | Output missing or validation fails | **Yes** |

**OTIO validation in assembly_complete:**
1. `validate_no_overlaps()` — clips don't overlap without transitions
2. `validate_track_alignment()` — timeline duration matches max track duration
3. `validate_clip_media()` — all clips have media references and valid ranges

---

## 5. Communication Contracts

### 5.1 Agent ↔ Pipeline

| Agent | Protocol | Context |
|---|---|---|
| Scenario, Audio, Video, Provisioner | HTTP (GET /, POST /) | Server-side, rebuilt each turn from events |
| VM Agent (on GPU) | HTTP (GET /, POST /) | Server-side |

### 5.2 Agent ↔ Agent

**Normally: NO direct communication.** All via event store.

**Exception:** Provisioner POSTs job completion directly to Audio/Video agent. The agent is free to GET global state (projections), but this should be the exception.

### 5.3 Pipeline ↔ VM Workers

| Direction | Method | Content |
|---|---|---|
| Provisioner → VM | POST / | Job description text |
| VM → Provisioner | POST / | Job completion, failure, health |

---

## 6. Prompt Construction

### 6.1 System Prompt

```
System: You are the {agent_name} agent. {base_persona}
System: Current state: {state}. {state_instructions}
User: {state_summary_json}
User: {human_corrections_if_any}
```

**State instructions by state (R slot):**
```python
STATE_INSTRUCTIONS = {
    "init": "Write a narration script outline.",
    "script": "Refine narration. Focus on pacing, speaker consistency, timing.",
    "audio_video": "Produce media. Formulate jobs, judge output, approve or reject.",
    "assembly": "Assemble final film with ffmpeg. Verify output.",
    "done": "Pipeline complete."
}
```

**Weights (W slot):** Budget-aware emphasis injected by state machine when over budget.

---

## 7. Hard Principles

Same as V4 with these additions:

### 7.10 Eventsourcing
- Use `eventsourcing` library. No hand-rolled event store.
- Projections are `ProcessApplication` with `tick()` + `policy()`.
- Snapshots are built-in. Checkpoints are automatic.

### 7.11 State Machine
- python-statemachine with explicit `tick` event from watcher loop.
- Guards read projection ProcessApplications.
- Cyclic transitions are valid and used.

### 7.12 OTIO Validation
- Custom validation functions in OTIOProjection.
- Used by state machine guards for assembly state.
- No built-in OTIO validator exists; we implement our own.

---

## 8. Glossary

| Term | Definition |
|---|---|
| **Aggregate** | Pipeline run as eventsourcing aggregate |
| **Application** | `eventsourcing.Application` that stores aggregate events |
| **ProcessApplication** | Projection that follows notification log with checkpoints |
| **tick()** | Process new events since last checkpoint |
| **policy()** | Route events to projection handlers |
| **Notification Log** | Ordered stream of events from Application |
| **Tracking Record** | Checkpoint position per projection |
| **tick event** | Explicit event sent to state machine every 1s |
| **Guard** | Condition function that determines if transition fires |
| **Effect** | Domain event proposed by an agent |
| **Event** | Effect saved to aggregate via Application |
| **Projection** | Read model built by ProcessApplication |
| **Agent** | LLM with bash tool, HTTP service |
| **Worker** | GPU VM running inference + VM agent |
| **State Machine** | python-statemachine with tick-driven guard evaluation |
| **Overseer** | Human/agent that launched pipeline |
| **State Summary** | JSON describing current pipeline state |

---

*Abstract version: 2026-05-17 v5*
