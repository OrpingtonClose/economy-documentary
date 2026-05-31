> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture V5 — Documentary Pipeline

> **Date:** 2026-05-17  
> **Status:** Concrete implementation in progress  
> **Branch:** `strands-migration`  
> **Location:** `server/v5/` >
> This document is the canonical V5 architecture. It subsumes all prior architecture docs.

---

## 1. Core Philosophy

### 1.1 The Event Log Is the Only Source of Truth

Everything else — OTIO timeline, job queue, VM inventory, pipeline state — is a **read model** rebuilt from the event log. If the SQLite database vanishes, replay the event log and the entire pipeline state reconstructs.

### 1.2 Effects Are the Only Legal Mutations

Agents produce free text. A parser extracts **Effects** (typed Pydantic models). The event store appends validated effects. Nothing else mutates state.

### 1.3 No Orchestrator

No central orchestrator agent. **Agents are free, autonomous peers.** They scan projections, maintain private cognitive maps, and emit effects independently. The watcher loop ticks projections and gives agents turns — it does not schedule, assign, or enforce.

### 1.4 Bash-Agentic Over Determinism

Infrastructure and irregular failures are handled by LLM agents that bash CLI tools (Vast.ai CLI, ssh, nvidia-smi) and query web search (Exa, Perplexity, Brave). Deterministic code for irregular domains is brittle 100% of the time. Agents reason, retry creatively, search for solutions, and escalate when stuck.

### 1.5 No Timeouts in Pipeline Code

No `setTimeout`, `threading.Timer`, `asyncio.timeout`, `signal.alarm` anywhere. Stale-state detection is agentic: domain agents probe and judge stalls via ssh, nvidia-smi, logs. VM-side kill switches (heartbeat loss → self-destruct) are safety nets, not timeouts. Projections do not poll infrastructure.

### 1.6 Real Engines Only

Qwen3-TTS, LTX-2.3, WhisperX. No mocks. Unavailable engines trigger `ClarificationRequest`.

### 1.7 Never Regex

Category-conditioned extraction via `instructor` + `deepseek-v4-flash`. No regex.

---

## 2. System Topology

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            HUMAN / OVERSEER                              │
│  Observes any agent via GET /. Corrects via POST /.                     │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         WATCHER LOOP (tick every 1s)                     │
│  1. Tick all projections (rebuild from new events)                       │
│  2. Give each agent a turn (scan → reason → emit effects)               │
│  No state machine. No orchestrator. Agents are autonomous peers.         │
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
       │                  │                                     │
       │                  │                                     │
       ▼                  ▼                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              EVENT STORE (SQLite, append-only, single writer)            │
│  Table: events(id, run_id, sequence, kind, payload_json, created_at)     │
│  BEGIN IMMEDIATE. Single writer via asyncio queue.                       │
└─────────────────────────────────────────────────────────────────────────┘
       ▲                  ▲                  ▲                  ▲
       │                  │                  │                  │
       ▼                  ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        PROJECTIONS (incremental, tick-driven)            │
│  OTIO Projection | Job Projection | VM Projection | State Projection     │
│  Each tracks `last_sequence` and processes only new events.              │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     VM WORKERS (ephemeral GPU instances)                 │
│  port 9000+  GET /  POST /                                              │
│  TTS: Qwen3-TTS. Video: LTX-2.3. LLM: deepseek-v4-flash via API.        │
└─────────────────────────────────────────────────────────────────────────┘

AGENT PRIVATE COGNITIVE SUBSTRATES (not shown):
  Each agent maintains its own Private Task Atlas — a self-modifying,
  semantic, lineage-aware memory structure. Persisted in the event log
  via Update{Agent}Map effects. Advisory only.
```

**Endpoint Rule:** Every box exposes exactly `GET /` and `POST /` on its own port.

---

## 3. Commands vs. Events

Commands are intents that may be rejected. Events are facts that are immutable.

| Aspect | Command | Event |
|---|---|---|
| Meaning | "I want to..." | "...happened" |
| Stored? | NO — ephemeral | YES — append-only |
| Naming | Imperative (`QueueJob`) — accepted intent, not guaranteed outcome |

Agents emit commands. The coordinator validates. Valid commands become events in the log.

---

## 4. Agent Architecture: Autonomous Peers with Private Cognitive Maps

### 4.1 No State Machine

There is **no global state machine**. No state graph. No prescribed workflow. The pipeline "phase" is an emergent property of the event log — computed by `PhaseTracker` for human observation only.

### 4.2 Agents Are Autonomous

Each agent:
1. Reads the event log (shared truth)
2. Reads all projections (shared read models)
3. Reads **its own Private Task Atlas** (private working memory)
4. Reasons about what to do
5. Emits effects freely

Agents may:
- Act on any block at any time
- Skip blocks and come back later
- Retry with creative parameter changes
- Invent new strategies
- Ignore their own past advice
- Ask other agents (via `HumanInstruction`) for help

### 4.3 Private Task Atlas: The Agent's Cognitive Substrate

Each agent maintains a **private, self-modifying, semantic memory structure** — its "task map." This is the answer to the core architectural problem: how does an autonomous agent navigate a complex, evolving task space where objects split, merge, and mutate?

**Core design (informed by BDI architectures, Semantic Intention Compass, and Narrative Field Architecture):**

The atlas is **not** a shared whiteboard, state machine, dependency graph, or computed projection. It is the agent's own cognitive artifact — persisted in the event log via `Update{Agent}Map` effects, advisory in nature, and freely revisable by the agent itself.

**Structure:**

```yaml
# Private Task Atlas — agent-owned, self-modifying, semantic

high_level_orientation:
  narrative_arc: "Script Draft → Audio Reconciliation → Video Production → Assembly"
  current_position: "audio_reconciliation_phase_2"
  confidence: 0.85

intent_threads:
  - intent_id: "INT-001"
    title: "Reconcile narration for Scene 3 kitchen sequence"
    status: active
    semantic_anchors:
      - "the kitchen dialogue after the market scene"
      - "Block A1:3:1 and descendants"
    resolution_heuristic:
      - "+10 confidence if AudioMeasured within tolerance"
      - "-5 if ReconciliationFailed for same block"
      - "-20 if retry_count > 3"
    priority: 0.92
    retry_count: 2
    personal_notes: "Text is always 2s too long. Try speed=1.2 first."

belief_base:
  - belief: "Scene 3 narration tends to run long"
    confidence: 0.8
    source: "past 3 reconciliation failures"
  - belief: "WhisperX underestimates by ~0.1s for fast speech"
    confidence: 0.6
    source: "manual measurement comparison"

learned_heuristics:
  - "When block splits, duplicate intent and re-score semantically"
  - "Always check phase cancellation when audio splits"
  - "Speed=1.2 before text shortening for long blocks"

revision_history:
  - v1: "Focused on Scene 1-2"
  - v2: "Shifted to Scene 3 after ScenarioAgent rewrote opening"
```

**Key properties:**

| Property | How it's realized |
|---|---|
| **Private** | Each agent has its own atlas. Agents read each other's atlases only via the shared event log (they're just effects). |
| **Self-modifying** | The agent emits `Update{Agent}Map` effects to revise its atlas. Reflection cycles rewrite sections proactively. |
| **Advisory** | The atlas is consulted as context when deciding what to do. The LLM can override, abandon, or reinterpret anything. |
| **Survives object mutation** | Associations are **semantic anchors** ("the kitchen dialogue") and **lineage queries** (`descendants_of(Event_142)`) — not hard object IDs. When a block splits, the agent's intent probabilistically rebinds to descendants. |
| **Long-term memory** | Persisted in the event log. Survives agent restarts. The agent reads its own past atlases on startup. |
| **Learning** | `learned_heuristics` and `belief_base` accumulate over time. The agent gets smarter. |

**Lineage-based anchors (the key insight):**

Instead of `Target: block_99` (fragile — breaks on split), the agent stores:

```
Target: descendants_of(Event_142_Scene_Creation) AND lacking(Event_Type_Audio_Mastered)
```

When `block_99` splits into `block_99a/b/c`, the agent re-evaluates its lineage query against the current event log and naturally discovers the three new leaf nodes. The map survives mutation because it relies on the **log's history**, not the **present state**.

**Ephemeral binding (the workbench):**

The only place the agent stores current object IDs is a **transient cache** populated just-in-time by running lineage queries. If the agent restarts, the cache clears. On wake, it re-queries and discovers mutations naturally.

### 4.4 Agent Base Class

```python
class Agent(ABC):
    """Autonomous agent. Scans projections, reads its own atlas, emits effects."""

    name: str = ""

    def __init__(self, event_store: Any) -> None:
        self.event_store = event_store
        self.atlas: dict = {}  # loaded from past Update{Agent}Map effects

    def tick(self, projections: dict[str, Any]) -> list[Any]:
        """One autonomous turn."""
        # 1. Read shared projections
        otio = projections["otio"]
        jobs = projections["jobs"]
        vms = projections["vms"]

        # 2. Load/reload private atlas from event log
        self.atlas = self._load_atlas()

        # 3. Build prompt with projections + atlas
        prompt = self._build_prompt(otio, jobs, vms)

        # 4. Call LLM (deepseek-v4-flash)
        raw_response = self._call_llm(prompt)

        # 5. Parse effects
        effects = self._parse_effects(raw_response)

        # 6. Include atlas update if agent revised its strategy
        atlas_update = self._maybe_update_atlas(raw_response)
        if atlas_update:
            effects.append(atlas_update)

        return effects

    def _load_atlas(self) -> dict:
        """Read all past Update{Agent}Map effects for this agent."""
        past = self.event_store.query(
            kind="update_audio_map" if self.name == "audio" else f"update_{self.name}_map"
        )
        # Merge by version; latest wins
        atlas = {}
        for effect in sorted(past, key=lambda e: e.get("version", 0)):
            atlas.update(effect.get("atlas_delta", {}))
        return atlas

    def _build_prompt(self, otio, jobs, vms) -> str:
        return (
            f"You are the {self.name} agent.\n\n"
            f"=== SHARED PROJECTIONS ===\n"
            f"OTIO: {otio.summary()}\n"
            f"Jobs: {jobs.summary()}\n"
            f"VMs: {vms.summary()}\n\n"
            f"=== YOUR PRIVATE TASK ATLAS ===\n"
            f"{json.dumps(self.atlas, indent=2)}\n\n"
            f"You are autonomous. No one tells you what to do. "
            f"Scan the state, consult your atlas, reason from first principles, "
            f"and emit effects to advance the pipeline. "
            f"You may ignore your atlas if you have a better idea. "
            f"You may invent new strategies. "
            f"You may skip work and come back later. "
            f"You may ask other agents for help via HumanInstruction."
        )
```

### 4.5 Inter-Agent Communication

Agents communicate **only** through the event store:

- **ScenarioAgent** emits `UpdateScript` → **AudioAgent** sees new narration slots on next tick
- **AudioAgent** emits `QueueJob(tts)` → **ProvisionerAgent** sees pending job on next tick
- **ProvisionerAgent** emits `AudioCompleted` → **AudioAgent** sees completed audio on next tick
- **AudioAgent** emits `ReconciliationFailed` → **ScenarioAgent** may see script-level failure and emit new `UpdateScript`

**No direct POST between agents.** The event log is the only channel.

---

## 5. Effect Type Family

### 5.1 Script Effects (3)

| Effect | Producer | Meaning |
|---|---|---|
| `UpdateScript` | Scenario | Write or revise scene narration |
| `DeleteScene` | Scenario | Remove a scene by number |
| `ReorderScenes` | Scenario | Change scene order |

```python
class UpdateScript(Effect):
    kind: Literal["update_script"] = "update_script"
    scene_num: int
    voices: list[dict]  # {voice, text, tone, duration_sec}
    visual_description: str = ""
    duration_sec: float = 0.0
    dopamine_hook: str = ""
    pronunciation_hints: str = ""

class DeleteScene(Effect):
    kind: Literal["delete_scene"] = "delete_scene"
    scene_num: int
    reason: str = ""

class ReorderScenes(Effect):
    kind: Literal["reorder_scenes"] = "reorder_scenes"
    new_order: list[int]
```

### 5.2 Job Effects (6)

| Effect | Producer | Meaning |
|---|---|---|
| `QueueJob` | Audio/Video | Demand TTS or LTX generation |
| `AudioCompleted` | Provisioner | TTS job finished, WAV at `artifact_path` |
| `VideoCompleted` | Provisioner | LTX job finished, MP4 at `artifact_path` |
| `JobFailed` | Provisioner | VM failed |
| `JobRequeued` | Audio/Video | Artistry rejection → retry with new params |
| `JobApproved` | Audio/Video | Artistry approval → ready for OTIO |

```python
class QueueJob(Effect):
    kind: Literal["queue_job"] = "queue_job"
    job_id: str
    job_type: Literal["tts", "ltx"]
    scene_num: int
    phrase_idx: int = 0
    voice: str = ""
    text: str = ""
    prompt: str = ""
    lora_id: str = ""
    duration_sec: float = 5.0
    priority: int = 0

class AudioCompleted(Effect):
    kind: Literal["audio_completed"] = "audio_completed"
    job_id: str
    vm_id: str
    artifact_path: str
    duration_actual: float
    format: Literal["wav", "mp3"] = "wav"
    sample_rate: int = 48000
    channels: int = 1
    file_size_bytes: int = 0
    generation_params: dict = {}
    generation_time_sec: float = 0.0
    cost_usd: float = 0.0

class VideoCompleted(Effect):
    kind: Literal["video_completed"] = "video_completed"
    job_id: str
    vm_id: str
    artifact_path: str
    duration_actual: float
    format: Literal["mp4", "webm"] = "mp4"
    width: int = 1280
    height: int = 720
    fps: float = 24.0
    file_size_bytes: int = 0
    generation_params: dict = {}
    generation_time_sec: float = 0.0
    cost_usd: float = 0.0

class JobFailed(Effect):
    kind: Literal["job_failed"] = "job_failed"
    job_id: str
    vm_id: str
    job_type: Literal["tts", "ltx"]
    error: str
    retryable: bool = True
    failure_category: Literal["oom", "timeout", "bad_prompt", "model_load_error",
                               "disk_full", "network", "cuda_error", "unknown"]

class JobRequeued(Effect):
    kind: Literal["job_requeued"] = "job_requeued"
    job_id: str
    reason: str
    modified_params: dict = {}

class JobApproved(Effect):
    kind: Literal["job_approved"] = "job_approved"
    job_id: str
    artistic_notes: str = ""
```

### 5.3 Reconciliation Effects (6)

| Effect | Producer | Meaning |
|---|---|---|
| `AudioGenerated` | Provisioner | TTS WAV produced (artifact exists) |
| `AudioMeasured` | Audio Agent | WhisperX measured actual duration |
| `DurationAdjusted` | Audio | Measured within tolerance, OTIO updated |
| `ReconciliationFailed` | Audio | Measured outside tolerance, retry needed |
| `ReconciliationPartial` | Audio | After script back-edge, dirty/clean blocks |
| `ReconciliationComplete` | Audio | All blocks pass, OTIO authoritative |

```python
class AudioGenerated(Effect):
    kind: Literal["audio_generated"] = "audio_generated"
    job_id: str
    scene_num: int
    phrase_idx: int
    voice: str
    artifact_path: str

class AudioMeasured(Effect):
    kind: Literal["audio_measured"] = "audio_measured"
    job_id: str
    scene_num: int
    phrase_idx: int
    voice: str
    scripted_sec: float
    measured_sec: float
    delta_sec: float
    ratio: float
    tolerance_sec: float
    verdict: Literal["pass", "fail", "skip"]
    message: str = ""

class DurationAdjusted(Effect):
    kind: Literal["duration_adjusted"] = "duration_adjusted"
    scene_num: int
    phrase_idx: int
    voice: str
    old_duration_sec: float
    new_duration_sec: float
    reason: str = ""

class ReconciliationFailed(Effect):
    kind: Literal["reconciliation_failed"] = "reconciliation_failed"
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    failures: list[dict]
    worst_delta_sec: float
    suggested_adjustments: list[dict]

class ReconciliationPartial(Effect):
    kind: Literal["reconciliation_partial"] = "reconciliation_partial"
    dirty_block_ids: list[str]
    clean_block_ids: list[str]
    reason: str

class ReconciliationComplete(Effect):
    kind: Literal["reconciliation_complete"] = "reconciliation_complete"
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    worst_delta_sec: float
    total_measured_sec: float
```

### 5.4 VM Effects (4)

| Effect | Producer | Meaning |
|---|---|---|
| `VMAllocated` | Provisioner | GPU instance created |
| `VMDeallocated` | Provisioner | GPU instance destroyed |
| `VMProvisionFailed` | Provisioner | Could not create VM |
| `VMObserved` | Observer | Vast.ai truth differs from events |

```python
class VMAllocated(Effect):
    kind: Literal["vm_allocated"] = "vm_allocated"
    vm_id: str
    offer_id: str
    worker_url: str
    role: Literal["tts", "ltx"]
    vastai_json: dict = {}

class VMDeallocated(Effect):
    kind: Literal["vm_deallocated"] = "vm_deallocated"
    vm_id: str
    reason: str

class VMProvisionFailed(Effect):
    kind: Literal["vm_provision_failed"] = "vm_provision_failed"
    offer_id: str
    error: str

class VMObserved(Effect):
    kind: Literal["vm_observed"] = "vm_observed"
    vm_id: str
    status: str
    vastai_json: dict = {}
```

### 5.5 OTIO Effects (2)

| Effect | Producer | Meaning |
|---|---|---|
| `MergeIntoOTIO` | Audio/Video | Approved clip enters timeline |
| `DeleteFromOTIO` | Audio/Video | Remove clip from timeline |

```python
class MergeIntoOTIO(Effect):
    kind: Literal["merge_into_otio"] = "merge_into_otio"
    track: Literal["V1_Video", "A1_Narration", "A2_Music"]
    scene_num: int
    phrase_idx: int = 0
    job_id: str
    artifact_path: str
    duration_sec: float
    trim_start: float = 0.0
    trim_end: float = 0.0

class DeleteFromOTIO(Effect):
    kind: Literal["delete_from_otio"] = "delete_from_otio"
    track: Literal["V1_Video", "A1_Narration", "A2_Music"]
    scene_num: int
    phrase_idx: int = 0
    reason: str = ""
```

### 5.6 Pipeline Effects (4)

| Effect | Producer | Meaning |
|---|---|---|
| `PipelineStarted` | Launcher | Run begins |
| `TransitionState` | Watcher | Phase changed (for observation only) |
| `PipelineComplete` | Assembly | Final MP4 done |
| `PipelineAborted` | Any | Unrecoverable stop |

```python
class PipelineStarted(Effect):
    kind: Literal["pipeline_started"] = "pipeline_started"
    run_id: str
    brief: str

class TransitionState(Effect):
    kind: Literal["transition_state"] = "transition_state"
    from_state: str
    to_state: str
    reason: str

class PipelineComplete(Effect):
    kind: Literal["pipeline_complete"] = "pipeline_complete"
    output_path: str
    duration_sec: float

class PipelineAborted(Effect):
    kind: Literal["pipeline_aborted"] = "pipeline_aborted"
    reason: str
```

### 5.7 Bash / Human / Fallback Effects (5)

| Effect | Producer | Meaning |
|---|---|---|
| `ExecuteRawBash` | Any | Escape hatch — run arbitrary command |
| `HumanInstruction` | Overseer | Human posted to agent |
| `ClarificationRequest` | Parser | Parse failed, needs human |
| `AgentLoopDetected` | Watcher | Agent stuck in loop |
| `NoOp` | Any | Informational, no mutation |

```python
class ExecuteRawBash(Effect):
    kind: Literal["execute_raw_bash"] = "execute_raw_bash"
    command: str
    reason: str
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""

class HumanInstruction(Effect):
    kind: Literal["human_instruction"] = "human_instruction"
    target_agent: str
    text: str

class ClarificationRequest(Effect):
    kind: Literal["clarification_request"] = "clarification_request"
    target_agent: str
    question: str
    raw_text: str = ""

class AgentLoopDetected(Effect):
    kind: Literal["agent_loop_detected"] = "agent_loop_detected"
    agent: str
    effect_kind: str
    count: int

class NoOp(Effect):
    kind: Literal["noop"] = "noop"
    reason: str = ""
```

### 5.8 Production Failure Effect (1)

```python
class ProductionFailed(Effect):
    kind: Literal["production_failed"] = "production_failed"
    job_id: str
    failure_type: Literal["overlap", "duration_mismatch", "gap_unexpected",
                           "voice_mismatch", "visual_incoherence", "artistic_reject",
                           "missing_media", "invalid_range", "track_misalignment", "audio_lufs"]
    slot_id: str
    expected: str = ""
    actual: str = ""
    suggested_fix: str = ""
```

**Failure type routing (advisory — agents decide):**
- `gap_unexpected`, `voice_mismatch` → Scenario agent may rewrite script
- `overlap`, `duration_mismatch`, `visual_incoherence`, `artistic_reject` → Requeue in current domain
- `track_misalignment` → Assembly agent retries
- `missing_media`, `invalid_range` → Retry in current state
- `audio_lufs` → Requeue with adjusted params

### 5.9 Private Task Atlas Effects (1 per agent)

Each agent emits its own atlas update effect:

| Effect | Producer | Meaning |
|---|---|---|
| `UpdateAudioMap` | Audio Agent | Audio agent revised its private atlas |
| `UpdateVideoMap` | Video Agent | Video agent revised its private atlas |
| `UpdateScenarioMap` | Scenario Agent | Scenario agent revised its private atlas |
| `UpdateProvisionerMap` | Provisioner Agent | Provisioner agent revised its private atlas |

```python
class UpdateAudioMap(Effect):
    kind: Literal["update_audio_map"] = "update_audio_map"
    agent: str = "audio"
    version: int
    atlas_delta: dict  # merged into agent's private atlas
    reflections: str = ""  # agent's reasoning for this update

# Similar for UpdateVideoMap, UpdateScenarioMap, UpdateProvisionerMap
```

---

## 6. Watcher Loop (No State Machine)

```python
async def run_watcher(
    projections: dict[str, Any],
    agents: list[Agent],
    event_store: Any,
    tick_interval: float = 1.0,
) -> None:
    phase_tracker = PhaseTracker()
    while True:
        # 1. Tick all projections
        for proj in projections.values():
            proj.tick()

        # 2. Compute phase (human observation only)
        phase = phase_tracker.tick(projections["otio"], projections["jobs"])
        logger.debug("Phase: %s", phase)

        # 3. Let agents act autonomously
        for agent in agents:
            try:
                effects = agent.tick(projections)
                for effect in effects:
                    event_store.append(effect)
            except Exception:
                logger.exception("Agent %s failed", agent.name)

        await asyncio.sleep(tick_interval)
```

**No state machine. No guards. No transitions. Agents are free peers.**

---

## 7. Projections

### 7.1 OTIO Projection

Builds OpenTimelineIO timeline from script + merge + adjust events.

| Method | Purpose |
|---|---|
| `validate_no_overlaps()` | Check clips don't overlap on same track |
| `validate_track_alignment()` | Check track duration matches timeline |
| `validate_clip_media()` | Check every clip has valid media reference |
| `all_slots_filled()` | True if every non-gap slot is delivered |

### 7.2 Job Projection

Tracks job lifecycle, reconciliation state, dirty/clean blocks.

| Field | Type | Meaning |
|---|---|---|
| `jobs` | dict | job_id → {status, type, block_id, ...} |
| `reconciliation_complete` | bool | All narration blocks pass tolerance |
| `dirty_blocks` | set[str] | Block IDs needing re-reconciliation |
| `clean_blocks` | set[str] | Block IDs with authoritative measured audio |
| `production_failures` | list | Unresolved ProductionFailed effects |

### 7.3 VM Projection

Pure read model of VM fleet. No polling, no event emission.

| Field | Type |
|---|---|
| `vms` | dict[instance_id, {status, role, cost, worker_url}] |

### 7.4 PhaseTracker

Emergent phase for human observation only.

```python
class PhaseTracker:
    def tick(self, otio, jobs) -> str:
        # Returns one of: script, audio_production, video_production,
        #                  media_production, assembly, done
        # NOT used for control. Agents do not read this.
```

---

## 8. Agents

### 8.1 Scenario Agent (port 8001)

**Role:** Writes and revises narration.

**Atlas vocabulary:**
- Narrative arcs, scene structure, pacing
- Speaker consistency, voice roles, emotional beats
- Scene-level duration targets

**Actions:**
- Reads OTIO for existing narration gaps
- Emits `UpdateScript`, `DeleteScene`, `ReorderScenes`

### 8.2 Audio Agent (port 8002)

**Role:** Owns audio reconciliation. Manages TTS generation, WhisperX measurement, and tolerance checking.

**Atlas vocabulary:**
- Blocks, measurements, tolerances (±15% or ±0.25s)
- Retry strategies, text variants, speed adjustments
- WhisperX confidence trends

**Actions:**
- Scans for scripted-but-unqueued narration blocks → `QueueJob(tts)`
- Scans for completed TTS with no measurement → `AudioMeasured`
- Compares measured vs scripted → `DurationAdjusted` or `ReconciliationFailed`
- All blocks pass → `ReconciliationComplete`

### 8.3 Video Agent (port 8003)

**Role:** Generates LTX-2.3 video clips.

**Atlas vocabulary:**
- Clips, visual descriptions, style consistency
- Lora/seed history, prompt variants
- Measured durations as LAW

**Actions:**
- Waits for `ReconciliationComplete` (via projection)
- Scans authoritative OTIO for unqueued video slots → `QueueJob(ltx)`
- Judges output → `JobApproved` or `JobRequeued`

### 8.4 Provisioner Agent (port 8004)

**Role:** Bash-agentic VM management. Uses CLI tools and web search.

**Atlas vocabulary:**
- VM offers, GPU types, costs, worker health
- Job-to-VM assignments, retry strategies
- Vast.ai market conditions

**Actions:**
- Reads `QueueJob` effects → provisions VMs via Vast.ai CLI
- Monitors worker health via ssh / nvidia-smi
- Emits `VMAllocated`, `VMDeallocated`, `VMObserved`, `AudioCompleted`, `VideoCompleted`, `JobFailed`
- Troubleshoots creatively using web search

### 8.5 Assembly Agent (port 8005)

**Role:** ffmpeg composition and final validation.

**Atlas vocabulary:**
- ffmpeg commands, track composition
- OTIO validation, duration matching
- Progressive assembly checks

**Actions:**
- Reads all OTIO tracks merged → runs ffmpeg
- Validates output → `PipelineComplete` or `ProductionFailed`

---

## 9. Hard Principles

| # | Principle | Enforcement |
|---|---|---|
| 1 | **Event log is sole source of truth** | All state derived from events |
| 2 | **Effects are only legal mutations** | Pydantic models only |
| 3 | **No orchestrator** | Agents are free peers; watcher ticks but does not schedule |
| 4 | **No timeouts in code** | No `setTimeout`, `threading.Timer`, `asyncio.timeout` |
| 5 | **Real engines only** | No mocks, no stubs |
| 6 | **Never regex** | Category-conditioned extraction via `instructor` |
| 7 | **Agent cognitive maps are private and advisory** | Each agent owns its own Task Atlas; agents read shared log but maintain private beliefs |
| 8 | **No shared whiteboards** | No computed map all agents write to |
| 9 | **No state machine** | Pipeline phase is emergent, not prescribed |
| 10 | **Bash-agentic over brittle determinism** | Agents reason, search, and bash CLI for irregular domains |
| 11 | **Provisioner is agentic** | LLM-driven, not deterministic; uses web search + CLI |
| 12 | **Agent memory = events + private atlas** | Events are shared truth; atlas is private interpretation |
| 13 | **No B2 for now** | Artifacts local. Event store is only external store |
| 14 | **Single writer** | Event store: asyncio queue + `BEGIN IMMEDIATE` |
| 15 | **Tick-driven** | Projections tick every 1s; agents act on their own schedule |
| 16 | **Assembly Agent owns final cut** | No media agent does ffmpeg |
| 17 | **Measurement is Audio Agent responsibility** | Post-notification, local WhisperX |

---

## 10. File Structure

```
server/v5/
├── effects.py          # All effect types + EffectUnion + KIND_TO_MODEL
├── watcher.py          # Watcher loop + PhaseTracker + Agent base class
├── projections.py      # OTIO, Job, VM projections
├── agents/
│   ├── base.py         # Agent base class + Private Task Atlas loading
│   ├── scenario.py     # ScenarioAgent
│   ├── audio.py        # AudioAgent (WhisperX + reconciliation)
│   ├── video.py        # VideoAgent
│   ├── provisioner.py  # ProvisionerAgent (bash-agentic)
│   └── assembly.py     # AssemblyAgent (ffmpeg)
├── event_store.py      # SQLite event store
├── parser.py           # Category-conditioned effect parser
├── run_pipeline.py     # Launcher
└── ARCHITECTURE_V5.md  # This document
```

---

## 11. Glossary

| Term | Definition |
|---|---|
| **Block** | Narration unit (scene + phrase + voice). Slot address: `A1:3:2` |
| **Private Task Atlas** | Agent's self-modifying, semantic, lineage-aware cognitive map |
| **Semantic Anchor** | Soft reference ("the kitchen dialogue") instead of hard object ID |
| **Lineage Query** | Anchor to historical event (`descendants_of(Event_142)`) instead of current state |
| **Intent Thread** | Agent's ongoing concern, not a task with a fixed target |
| **Resolution Heuristic** | Agent's private criteria for judging completion |
| **Ephemeral Binding** | Transient cache of current object IDs, repopulated each turn |
| **Event Log** | Immutable append-only record of all effects |
| **Projection** | Read model rebuilt from event log |
| **Effect** | Typed Pydantic model — the only legal mutation |
| **Phase** | Emergent pipeline stage computed from projections; advisory only |
| **Reconciliation** | TTS → WhisperX → compare → adjust/requeue loop |
| **Authoritative OTIO** | Post-ReconciliationComplete state; measured durations are LAW |
| **Slot** | OTIO position on track (scene:block) |
