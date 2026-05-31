> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture V6 — Documentary Pipeline

> **Date:** 2026-05-27
> **Status:** LOCKED — Prompt-based rules, no state machine, pydantic-deepagents, emergent phases
> **Replaces:** ARCHITECTURE_V5.md
> **Location:** `server/v6/`
>
> This document is the canonical V6 architecture. Pipeline phases are emergent, not enforced. Agents read projections and decide. Prioritization lives in agent system prompts. There is no state machine, no `RulesEngine` Python class, and no `TransitionState` effect.

---

## 1. Core Philosophy

Six foundational commitments govern the pipeline. The eleven hard principles in §1.9 enumerate every invariant and its enforcement mechanism.

### 1.1 Event Log as Sole Source of Truth

#### 1.1.1 All state derived from events; replay reconstructs everything

Every fact is an **Effect** — a typed Pydantic model — appended to an append-only event log. The OTIO timeline, job queue, VM inventory, and pipeline phase are **projections**: read models rebuilt by pure fold functions. Replay from sequence `0` reconstructs everything exactly.

#### 1.1.2 Event store is only persistent storage; all other state is ephemeral projection

The SQLite `events` table `(id, run_id, sequence, kind, payload_json, created_at)` is the sole durable storage. Agents hold no session state. VM workers are ephemeral. Projections are in-memory folds processing only new events since their last checkpoint.

### 1.2 Effects as Only Legal Mutations

#### 1.2.1 Typed Pydantic models; parser extracts from agent text

A **category-conditioned parser** (§9.6) extracts Effects from agent text using `instructor` + `deepseek-v4-flash`. Every Effect carries `kind: Literal[...]`, `run_id: str`, `effect_id: UUID` (UUIDv7 — §3.1), `agent: str`, and `timestamp: datetime`. Invalid payloads are rejected before reaching the event store.

#### 1.2.2 No direct state mutation outside event store append

The event store is a single asyncio queue with `BEGIN IMMEDIATE` (§5.2). Every state change enters through this one aperture. Agents do not call projection methods.

### 1.3 No State Machine — Prompt-Based Rules

**No state machine.** Pipeline "state" is emergent from projection state (e.g., "all audio blocks clean" emerges from OTIOProjection, not from a state variable). Agents read projection-derived narratives and decide what to do. Rules live in the agent's system prompt, not in code.

Prioritization, filtering, and response selection happen **inside the agent via prompt instruction**. The watcher passes projection-derived narratives to the agent; the agent's system prompt contains the rules for what to prioritize and how to respond.

This follows the principle: *whenever something can be done via prompt, do so — cut code complexity.*

### 1.4 No Timeouts in Code

#### 1.4.1 No setTimeout, threading.Timer, or asyncio.timeout anywhere in pipeline code

No pipeline code calls `setTimeout`, `threading.Timer`, `asyncio.timeout`, or any timer primitive. HTTP requests and subprocess calls run to completion. This is architecture policy.

#### 1.4.2 Stale-job detection via projection-based TimeoutObserved effect (not a timer)

Hung jobs are detected by **observation**. The watcher queries `JobProjection` every tick: "which jobs have been `running` longer than the threshold?" On detection, it emits a `TimeoutObserved` effect — a normal log event. The Provisioner routes to VM cleanup and job requeue. The V5 VM-side 15-minute heartbeat self-destruct has been eliminated; it violated this principle.

### 1.5 Real Engines Only

#### 1.5.1 Qwen3-TTS, LTX-2.3, DeepSeek API; no simulation layers

TTS uses **Qwen3-TTS** on GPU VMs. Video uses **LTX-2.3**. Agent LLM inference uses **DeepSeek API** (`deepseek-v4-flash`). No mocks, no stubs. Unavailable engines trigger `ClarificationRequest`.

### 1.6 Never Regex

#### 1.6.1 Category-conditioned extraction via instructor + deepseek-v4-flash

No regex extracts structured data from agent output. The parser uses the agent's current role to determine valid Effect subtypes and constrains the LLM to schema-compliant JSON via `instructor`. If extraction fails, the prompt is adjusted — the schema is not weakened.

### 1.7 Situation-Driven Agent Tasking

Agents read projections directly via `ctx.deps.projections`. They scan OTIO, Job, VM, and Budget state themselves. Their system prompt contains situation-type guidance and prioritization rules. Agents decide what to do.

### 1.8 pydantic-deepagents

Agents use `pydantic-deepagents` (built on pydantic-ai). Context compaction is implemented as a **pydantic-ai Capability** (`OTIOAwareCompactionCap`) that hooks `before_model_request`. It queries the OTIO projection to determine the agent's current task/focus, then compacts the message history preserving task-relevant details. The native `ContextManagerCapability` tracks token usage and triggers compaction at configurable thresholds.

**Why capabilities, not watcher-side compaction:** The watcher should not know about LLM token budgets. Token management is an agent-internal concern. pydantic-deepagents provides the hook infrastructure; we provide the OTIO-aware compaction logic.

### 1.9 Principles at a Glance

#### 1.9.1 Table of 11 hard principles with enforcement mechanism per principle

| # | Principle | Enforcement | V5→V6 Change |
|---|---|---|---|
| 1 | **Event log is sole source of truth** | All state derived from events. No hidden state. No projection writes independently. | None |
| 2 | **Effects are only legal mutations** | Only Pydantic models enter event store. Parser validates against `EffectUnion`. | Added `effect_id: UUIDv7` |
| 3 | **No state machine — prompt-based rules** | Prioritization lives in agent system prompts. Agents scan projections and decide. | **NEW: Removed state machine** |
| 4 | **No timeouts in code** | No `setTimeout`, `threading.Timer`, or `asyncio.timeout` in pipeline code. | `TimeoutObserved` is projection-based |
| 5 | **Real engines only** | Qwen3-TTS, LTX-2.3, DeepSeek API. No mocks, no stubs, no simulation. | Renamed from "No mocks" |
| 6 | **Never regex** | Category-conditioned extraction via `instructor` + `deepseek-v4-flash`. | None |
| 7 | **Provisioner is deterministic lackey** | Python service, no LLM. Provisions, reports, delivers. Media agents judge artistry. | Added "deterministic" |
| 8 | **Agent memory does not persist** | Each turn rebuilt from projection summaries. No session state between POSTs. | None |
| 9 | **Stale-state detection is projection-based** | Watcher queries `JobProjection`; `TimeoutObserved` on threshold. No VM-side timers. | Replaces V5 P10 "Kill switch VM-side" |
| 10 | **Single writer** | Event store: asyncio queue + `BEGIN IMMEDIATE`. One writer coroutine. | None |
| 11 | **Tick-driven** | Watcher ticks every 10 seconds. Projections advance; agents run. No async transitions. | Changed from 1s to 10s tick |

---

## 2. System Topology

### 2.1 Architecture Diagram

#### 2.1.1 ASCII topology

```
                     Human / Overseer
                    (instruction, GET state)
                            │
                            ▼
                    ┌───────────────┐
                    │   Watcher     │ port 8080
                    │   (10s tick)  │
                    └───────┬───────┘
                            │ effects
                            ▼
                    ┌───────────────┐
                    │  Event Store  │ port 8079
                    │  (append-only)│
                    └───────┬───────┘
                            │ events
        ┌──────────┬────────┼────────┬──────────┐
        ▼          ▼        ▼        ▼          ▼
   ┌─────────┐ ┌────────┐ ┌──────┐ ┌────────┐ ┌──────────┐
   │  OTIO   │ │  Job   │ │  VM  │ │ State  │ │ Budget   │
   │  Proj.  │ │ Proj.  │ │ Proj.│ │ Proj.  │ │ Proj.    │
   └────┬────┘ └───┬────┘ └──────┘ └────┬───┘ └────┬─────┘
        │          │                    │          │
        └──────────┼────────────────────┼──────────┘
                   │    projections     │
                   ▼                    ▼
            ┌──────────────┐      ┌──────────────┐
            │  Scenario    │      │   Audio      │
            │   Agent      │      │   Agent      │
            │(main agent)  │      │(main agent)  │
            └──────────────┘      └──────────────┘
            ┌──────────────┐      ┌──────────────┐
            │   Video      │      │  Assembly    │
            │   Agent      │      │   Agent      │
            │(main agent)  │      │(main agent)  │
            └──────────────┘      └──────────────┘

         ════════ DETERMINISTIC SERVICES ════════
                    ┌──────────────┐
                    │  Provisioner │  (in watcher loop)
                    │(deterministic│
                    │   service)   │
                    └──────────────┘
                            │
                            ▼
                    ┌──────────────┐
                    │  VM Worker   │  port 9000+
                    │ (ephemeral   │
                    │   GPU)       │
                    └──────────────┘
```

**Data flow.** The Watcher ticks every 10 seconds. It advances all projections by reading new effects from the Event Store. Each agent (main agent, not subagent) is invoked via `agent.run()` with projections injected as dependencies. Agents emit effects, which the watcher appends to the Event Store. The deterministic Provisioner runs inside the watcher loop, reading `JobProjection` to match pending jobs to VMs. VM Workers receive jobs via HTTP POST, execute inference, and report results back to the Provisioner.

#### 2.1.2 Provisioner — deterministic watcher-loop service

The Provisioner is a deterministic Python service, not an LLM agent. It executes inside the Watcher loop. On each tick, it reads `JobProjection` to find pending jobs, matches Vast.ai offers by deterministic criteria, and dispatches work packages to VM Workers via `POST /execute` on port 9000+. Because it has no model weights, it runs in-process rather than as a standalone HTTP agent. Full implementation in §10.

---

### 2.2 Component Inventory

#### 2.2.1 Component table

Every agent exposes exactly `GET /` (health) and `POST /` (primary endpoint) on its own port.

| Component | Port | Type | Effects Produced | Effects Consumed |
|---|---|---|---|---|
| Watcher | 8080 | service (loop) | `TimeoutObserved`, `AgentLoopDetected`, `PipelineAborted` | all effects (reads) |
| Scenario Agent | 8001 | main agent | `UpdateScript`, `DeleteScene`, `ReorderScenes` | projection state |
| Audio Agent | 8002 | main agent | `QueueJob`, `JobApproved`, `JobRequeued`, `DurationAdjusted`, `ReconciliationFailed`, `ReconciliationPartial`, `ReconciliationComplete` | projection state |
| Video Agent | 8003 | main agent | `QueueJob`, `JobApproved`, `JobRequeued`, `MergeIntoOTIO` | projection state |
| Assembly Agent | 8005 | main agent | `PipelineComplete`, `ProductionFailed` | projection state |
| Provisioner | 8080 | service (in-loop) | `VMAllocated`, `VMDeallocated`, `VMProvisionFailed`, `VMObserved`, `JobCompleted`, `JobFailed` | `QueueJob` |
| Event Store | 8079 | service | — | all effects |
| Projections (5) | in-memory | read models | — | all effects |
| VM Workers | 9000+ | service | `JobResult` (to Provisioner) | `JobRequest` |

**V6 delta.** The Provisioner was an LLM agent on port 8004; it is now a deterministic in-loop service. There is no state machine. Agents are main agents (not subagents) using `pydantic-deepagents`. The watcher tick interval is 10 seconds.

---

### 2.3 Emergent Pipeline Phases

#### 2.3.1 Seven phases (six operational + ABORTED)

These are not states. They are descriptive labels for human observation. No code enforces transitions — they emerge from what agents do.

```
    INIT ──► SCRIPT ──► AUDIO_RECONCILE ──► VIDEO_PRODUCTION ──► ASSEMBLY ──► DONE
              ▲                ▲
              │                │
       gap_unexpected    voice_mismatch
```

| Phase | Emergent Condition | Active Agents |
|---|---|---|
| **INIT** | No `PipelineStarted` effect | None |
| **SCRIPT** | `PipelineStarted` exists, OTIO has unfilled slots | Scenario |
| **AUDIO_RECONCILE** | OTIO has dirty audio blocks | Audio |
| **VIDEO_PRODUCTION** | All audio clean, video slots unfilled | Video |
| **ASSEMBLY** | All slots filled, final MP4 missing | Assembly |
| **DONE** | Final MP4 exists and validates | None |
| **ABORTED** | `PipelineAborted` emitted | None |

#### 2.3.2 Back-edges

| Back-Edge | Trigger | From | To | Handler |
|---|---|---|---|---|
| `gap_unexpected` | Narration scene count ≠ scene list | `AUDIO_RECONCILE` or later | `SCRIPT` | Scenario Agent rewrites script |
| `voice_mismatch` | Final audio speaker ≠ scenario voice tag | `VIDEO_PRODUCTION` | `SCRIPT` | Scenario Agent fixes voice tag |

Back-edges are triggered when an agent emits `ProductionFailed` with `failure_type` in `{gap_unexpected, voice_mismatch}`. The Scenario Agent receives the failure context on its next turn and emits `UpdateScript` to fix the problem. Prior effects remain immutable in the Event Store. Downstream Projectors rebuild read models from the full log. This makes recovery a new forward path, not a mutation.


---

## 3. Effect Type Family — Complete Schemas

All pipeline mutations pass through the event store as **effects** — Pydantic v2 models serialized to a single row in SQLite. Every effect carries `run_id` (identifies the pipeline run), `effect_id` (UUIDv7 for client-side idempotency), `agent` (which component produced it), and `timestamp` (when it was created, in seconds since the epoch). The `kind` field serves as the discriminant for parsing and union dispatch.

This section defines 30 concrete effect types organized into 8 families, plus the base `Effect` model and the `ReconciliationFailureDetail` and `SuggestedFix` sub-models. All together, 33 Pydantic models. Every model is a complete, runnable schema with type annotations, `Literal` discriminants, and `Field` constraints. The section closes with the `EffectUnion` discriminated union definition and the `KIND_TO_MODEL` routing table used by the parser.

Naming convention: **imperative** for agent requests (`QueueJob`, `ExecuteRawBash`, `MergeIntoOTIO`), **past-tense** for system-reported outcomes (`JobCompleted`, `AudioMeasured`, `PipelineComplete`).

---

### 3.1 Base Effect Model

#### 3.1.1 Effect base class

```python
from __future__ import annotations

import time
from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field
from uuid_extensions import uuid7


class Effect(BaseModel):
    """Base for all effect types. NEVER instantiated directly.

    Fields present on every effect emitted into the event store:
    - run_id:        pipeline run identifier (opaque string)
    - effect_id:     UUIDv7 generated client-side for idempotent retries
    - kind:          Literal discriminant string (overridden per subclass)
    - agent:         component that produced the effect (e.g. "scenario")
    - timestamp:     seconds since epoch at creation time

    The EventStore deduplicates on (run_id, effect_id) via INSERT OR IGNORE
    so that retrying an append with the same effect_id is a safe no-op.
    """
    run_id: str
    effect_id: UUID = Field(default_factory=uuid7)
    kind: str = "effect"  # overridden per subclass via Literal
    agent: str
    timestamp: float = Field(default_factory=time.time)
```

`effect_id` uses UUIDv7 because it encodes a timestamp in the high bits, making event logs naturally time-sortable without leaking sequence gaps. Client-side generation means an agent can retry a failed `append()` with the same `effect_id` and the duplicate is silently dropped by the store.

#### 3.1.2 EventStore deduplication

The events table enforces uniqueness on `(run_id, effect_id)`:

```sql
CREATE UNIQUE INDEX idx_events_run_effect_id ON events(run_id, effect_id);
```

The writer loop uses `INSERT OR IGNORE` so that duplicate `effect_id` values — from retries, replays, or idempotent re-submissions — are silently discarded without raising.

| Field | Type | Source | Purpose |
|---|---|---|---|
| `run_id` | `str` | caller | scopes all effects to one pipeline run |
| `effect_id` | `UUID` | `uuid7()` client-side | idempotency key; survives retry |
| `kind` | `str` (Literal per subclass) | agent/parser | discriminant for `EffectUnion` |
| `agent` | `str` | caller | attribution for loop detection |
| `timestamp` | `float` | `time.time()` | wall-clock ordering aid |

---

### 3.2 Script Effects

Produced by the Scenario Agent (port 8001). These effects mutate the OTIO timeline's narrative track.

#### 3.2.1 UpdateScript, DeleteScene, ReorderScenes

```python
class UpdateScript(Effect):
    """Write or revise a scene's narration slot.

    Each UpdateScript defines one narration block: speaker, text content,
    timing target, and optional production hints. The OTIOProjection merges
    this into the A1_Narration track, creating or overwriting the slot for
    (scene_num, block_id).
    """
    kind: Literal["update_script"] = "update_script"
    scene_num: int = Field(..., ge=1, description="1-based scene index")
    block_id: str = Field(..., description="stable identifier for this narration block")
    speaker: str = Field(..., description="voice role: narrator, guest_a, etc.")
    text: str = Field(..., min_length=1, description="narration text")
    pronunciation_hints: list[str] = Field(default_factory=list)
    visual_notes: str = ""
    dopamine_hook: str = ""
    duration_sec: float = Field(..., gt=0.0, description="target duration in seconds")


class DeleteScene(Effect):
    """Remove a scene and all its narration blocks from the timeline."""
    kind: Literal["delete_scene"] = "delete_scene"
    scene_num: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1)


class ReorderScenes(Effect):
    """Change scene order. new_order[i] is the scene_num that should occupy position i+1.

    The OTIOProjection resequences the top-level timeline tracks so that
    scene N moves to the position specified. All narration and video slots
    attached to a scene move with it.
    """
    kind: Literal["reorder_scenes"] = "reorder_scenes"
    new_order: list[int] = Field(..., min_length=1)
```

---

### 3.3 Job Effects

Produced by the Audio Agent (port 8002), Video Agent (port 8003), and the deterministic Provisioner service. These effects manage the lifecycle of media-generation work units.

#### 3.3.1 QueueJob, JobCompleted, JobFailed, JobRequeued, JobApproved

```python
class QueueJob(Effect):
    """Demand creation of a media artifact by a VM worker.

    The Provisioner reads QueueJob from the JobProjection and matches it to
    a Vast.ai offer (job_type="tts" -> Qwen3-TTS GPU; job_type="ltx" -> LTX GPU).
    Once a VM is allocated, the job is considered "pending".
    """
    kind: Literal["queue_job"] = "queue_job"
    job_id: str = Field(..., description="stable unique job identifier")
    job_type: Literal["tts", "ltx"]
    scene_num: int = Field(..., ge=1)
    block_id: str
    slot_id: str = Field(..., description="OTIO slot where the result belongs")
    params: dict = Field(default_factory=dict, description="type-specific generation params")


class JobCompleted(Effect):
    """VM worker finished successfully; artifact is ready for quality review."""
    kind: Literal["job_completed"] = "job_completed"
    job_id: str
    artifact_path: str = Field(..., description="absolute path to generated file")
    duration_sec: float = Field(..., ge=0.0, description="actual media duration")
    vm_instance_id: str


class JobFailed(Effect):
    """VM worker failed. failure_category drives retry vs escalation policy."""
    kind: Literal["job_failed"] = "job_failed"
    job_id: str
    error_message: str
    failure_category: Literal[
        "oom",           # GPU out of memory
        "timeout",       # VM-side timeout (legacy; see TimeoutObserved for pipeline-side)
        "bad_prompt",    # malformed generation params
        "model_load_error",  # model weights failed to load
        "disk_full",     # VM out of disk
        "network",       # network error during model download or upload
        "cuda_error",    # CUDA runtime failure
        "unknown",       # uncategorized failure
    ]
    vm_instance_id: str
    retryable: bool = True
    retry_count: int = Field(default=0, ge=0, description="how many times this job has been retried")


class JobRequeued(Effect):
    """Artistry rejection: previous output did not meet quality bar.

    The Audio or Video Agent emits this when a JobCompleted artifact fails
    quality review. new_params carries adjusted generation parameters for
    the retry attempt (e.g., different voice speed, revised prompt).
    """
    kind: Literal["job_requeued"] = "job_requeued"
    job_id: str
    reason: str = Field(..., min_length=1, description="why the previous attempt was rejected")
    new_params: dict | None = None


class JobApproved(Effect):
    """Artistry approval: artifact passes quality review, ready for OTIO merge."""
    kind: Literal["job_approved"] = "job_approved"
    job_id: str
    artifact_path: str
    quality_notes: str = ""
    reviewed_by: str = Field(default="agent", description="'agent' or human name")
```

#### 3.3.2 JobFailed.failure_category routing

| Category | Meaning | Default Action | Retryable |
|---|---|---|---|
| `oom` | GPU out of memory | requeue with lower batch size | yes |
| `timeout` | VM-side timeout (legacy) | handled by TimeoutObserved instead | yes |
| `bad_prompt` | Malformed params | fix params, requeue | yes |
| `model_load_error` | Weights load failure | requeue on fresh VM | yes |
| `disk_full` | VM disk exhausted | deallocate VM, requeue | yes |
| `network` | Transient network error | retry with backoff | yes |
| `cuda_error` | CUDA runtime failure | requeue on different GPU | yes |
| `unknown` | Uncategorized | emit `ClarificationRequest` | no |

The `retryable` field is a hint. The deterministic Provisioner may override it based on retry count (e.g., force `retryable=False` after 3 consecutive failures of the same job).

---

### 3.4 Reconciliation Effects

Produced by the Audio Agent and the deterministic Provisioner during audio reconciliation. These effects implement the tight TTS-measure-adjust loop.

#### 3.4.1 AudioGenerated, AudioMeasured, DurationAdjusted

```python
class AudioGenerated(Effect):
    """TTS WAV produced by a VM worker. Artifact awaits WhisperX measurement."""
    kind: Literal["audio_generated"] = "audio_generated"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    artifact_path: str


class AudioMeasured(Effect):
    """WhisperX measured the actual spoken duration of a generated WAV.

    Three independent WhisperX runs produce three measurements (decision C2).
    The median of `measurements` is the authoritative measured duration used
    for tolerance checking. All three values are stored for debugging variance.
    """
    kind: Literal["audio_measured"] = "audio_measured"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    measured_sec: float = Field(..., description="median of measurements (authoritative)")
    measurements: list[float] = Field(
        default_factory=list,
        description="all three WhisperX measurements, unsorted",
    )
    whisperx_confidence: float = Field(..., ge=0.0, le=1.0)


class DurationAdjusted(Effect):
    """Measured duration within tolerance; OTIO slot updated with authoritative value.

    The Audio Agent computes delta = measured_sec - scripted_sec. If
    |delta| <= max(15% * scripted_sec, 0.25s) the block passes. The OTIO
    Projection updates the slot's source_range to match measured_sec.
    """
    kind: Literal["duration_adjusted"] = "duration_adjusted"
    block_id: str
    scene_num: int
    voice_role: str
    scripted_sec: float
    measured_sec: float
    delta_sec: float
    tolerance_sec: float
```

#### 3.4.2 ReconciliationFailed, ReconciliationPartial, ReconciliationComplete

```python
class ReconciliationFailed(Effect):
    """One or more blocks failed the tolerance check. Retry or escalate.

    The failure_type field determines routing:
    - duration_mismatch -> requeue with adjusted TTS params (normal retry)
    - duration_unrecoverable -> per-block attempt limit exceeded
      This triggers a back-edge to SCRIPT because the target duration is
      physically impossible for the given text.
    """
    kind: Literal["reconciliation_failed"] = "reconciliation_failed"
    agent: str
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    failures: list[ReconciliationFailureDetail] = Field(default_factory=list)
    worst_delta_sec: float
    suggested_adjustments: list[dict] = Field(default_factory=list)
    failure_type: Literal["duration_mismatch", "duration_unrecoverable"] = "duration_mismatch"


class ReconciliationPartial(Effect):
    """After a script back-edge, some blocks remain authoritative while others
    are marked dirty and must be re-reconciled.

    The Audio Agent computes the dirty set by comparing the new script against
    the authoritative OTIO: blocks whose text, speaker, or duration target
    changed are dirty; all others keep their AudioMeasured values.
    """
    kind: Literal["reconciliation_partial"] = "reconciliation_partial"
    agent: str
    dirty_block_ids: list[str] = Field(default_factory=list)
    clean_block_ids: list[str] = Field(default_factory=list)
    reason: str = Field(..., description="e.g. 'voice_mismatch back-edge from VIDEO_PRODUCTION'")
    blocks_dirty: int
    blocks_clean: int


class ReconciliationComplete(Effect):
    """All narration blocks pass tolerance. OTIO is now authoritative.

    This effect is the gateway from AUDIO_RECONCILE to VIDEO_PRODUCTION.
    Agents check for ReconciliationComplete and clean blocks to decide
    whether to begin video generation.
    """
    kind: Literal["reconciliation_complete"] = "reconciliation_complete"
    agent: str
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    worst_delta_sec: float
    total_measured_sec: float
```

#### 3.4.3 ReconciliationFailureDetail sub-model

```python
class ReconciliationFailureDetail(BaseModel):
    """Per-block failure diagnostic embedded in ReconciliationFailed.

    Not a top-level effect — has no `kind` field and is NOT in EffectUnion.
    """
    block_id: str
    scene_num: int
    phrase_idx: int = Field(..., description="index of phrase within block")
    voice: str
    scripted_sec: float
    measured_sec: float
    delta_sec: float
    ratio: float = Field(..., description="measured / scripted")
    message: str = Field(..., description="human-readable diagnostic")
    attempt_number: int = Field(default=1, ge=1, description="which reconciliation attempt this was")
```

| Reconciliation Effect | Producer | Next Action |
|---|---|---|
| `AudioGenerated` | Provisioner / VM Worker | Run WhisperX (3x), emit `AudioMeasured` |
| `AudioMeasured` | Audio Agent | Audio Agent computes tolerance, emits `DurationAdjusted` or `ReconciliationFailed` |
| `DurationAdjusted` | Audio Agent | OTIOProjection updates slot; block passes |
| `ReconciliationFailed` | Audio Agent | Requeue with adjusted params, or escalate if `duration_unrecoverable` |
| `ReconciliationPartial` | Audio Agent | JobProjection marks dirty blocks; Audio Agent re-reconciles only dirty set |
| `ReconciliationComplete` | Audio Agent | Video Agent may begin VIDEO_PRODUCTION when all blocks clean |


---

### 3.5 VM Effects

Produced by the deterministic Provisioner service. These effects track the lifecycle of ephemeral GPU instances rented from Vast.ai.

#### 3.5.1 VMAllocated, VMDeallocated, VMProvisionFailed, VMObserved

```python
class VMAllocated(Effect):
    """GPU instance created and ready for job assignment.

    The Provisioner emits this after successfully creating a Vast.ai instance
    and verifying that the worker HTTP endpoint (port 9000+) responds to GET /.
    """
    kind: Literal["vm_allocated"] = "vm_allocated"
    instance_id: str = Field(..., description="Vast.ai instance ID")
    role: Literal["tts", "ltx"] = Field(..., description="worker role determines GPU type")
    offer_id: str = Field(..., description="Vast.ai offer ID that was accepted")
    worker_url: str = Field(..., description="full URL including port, e.g. http://1.2.3.4:9000")
    gpu_type: str = Field(..., description="GPU model, e.g. 'RTX 4090'")
    cost_per_hour: float = Field(..., gt=0.0)
    vm_login_name: str = "root"


class VMDeallocated(Effect):
    """GPU instance destroyed. Final cost is recorded for budget tracking."""
    kind: Literal["vm_deallocated"] = "vm_deallocated"
    instance_id: str
    reason: Literal[
        "job_done",       # worker finished all assigned jobs
        "cost_limit",     # exceeded per-VM cost threshold
        "stale",          # TimeoutObserved triggered cleanup
        "provision_failed",  # never reached healthy state
        "manual",         # human overseer destroyed via instruction
    ]
    final_cost: float = Field(default=0.0, ge=0.0)
    runtime_sec: float = Field(default=0.0, ge=0.0)


class VMProvisionFailed(Effect):
    """Provisioner could not create a VM for a pending job.

    On repeated failures (configurable threshold, default 3), the Provisioner
    halts and emits `ClarificationRequest` for human intervention. It does not
    attempt creative recovery — deterministic lackey behavior.
    """
    kind: Literal["vm_provision_failed"] = "vm_provision_failed"
    offer_id: str = ""
    job_id: str = ""
    error_message: str
    failure_category: Literal[
        "no_offers",      # no Vast.ai offers match requirements
        "offer_taken",    # offer was rented by another user
        "payment_failed", # billing issue
        "boot_timeout",   # instance created but never became healthy
        "ssh_failed",     # cannot reach worker endpoint
        "unknown",
    ]
    retryable: bool = True
    consecutive_failures: int = Field(default=1, ge=1)


class VMObserved(Effect):
    """Provisioner detected drift between event-derived VM state and Vast.ai reality.

    The Provisioner polls Vast.ai API and compares reported
    instance status against the VMProjection's internal model. When drift is
    detected, it emits VMObserved so the projection can reconcile.
    """
    kind: Literal["vm_observed"] = "vm_observed"
    instance_id: str
    observed_status: Literal[
        "running", "offline", "not_found", "unknown"
    ] = Field(..., description="what Vast.ai API reports")
    expected_status: Literal[
        "running", "offline", "not_found", "unknown"
    ] = Field(..., description="what VMProjection believes")
    drift_description: str = Field(..., description="human-readable drift summary")
    corrective_action: Literal[
        "none",           # minor drift, no action
        "deallocate",     # VM gone from Vast.ai, clean up projection
        "refresh_state",  # transient, re-poll next cycle
        "escalate",       # unresolvable, emit ClarificationRequest
    ] = "none"
```

---

### 3.6 OTIO Effects

Produced by the Audio Agent and Video Agent after artistry approval. These effects merge approved media artifacts into the OTIO timeline.

#### 3.6.1 MergeIntoOTIO, DeleteFromOTIO

```python
class MergeIntoOTIO(Effect):
    """Approved clip enters the OTIO timeline at the specified track and slot.

    The OTIOProjection creates (or replaces) an `otio.schema.Clip` with a
    media reference pointing to `artifact_path` and a source range of
    `duration_sec`. The clip is placed on `track_name` at `start_time`.
    """
    kind: Literal["merge_into_otio"] = "merge_into_otio"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str = Field(..., description="OTIO slot identifier")
    artifact_path: str
    track_name: Literal["A1_Narration", "V1_Video"] = Field(..., description="audio or video track")
    start_time: float = Field(..., ge=0.0, description="timeline start in seconds")
    duration_sec: float = Field(..., gt=0.0)
    transition_type: Literal["cut", "dissolve", "none"] = "cut"
    transition_duration_sec: float = Field(default=0.0, ge=0.0)


class DeleteFromOTIO(Effect):
    """Remove a clip from the OTIO timeline. Used when a block is re-reconciled
    (its old audio becomes invalid) or when a scene is deleted.
    """
    kind: Literal["delete_from_otio"] = "delete_from_otio"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str
    track_name: Literal["A1_Narration", "V1_Video", "both"]
    reason: str = Field(..., min_length=1)
```

---

### 3.7 Pipeline Effects

Produced by the Launcher, the watcher loop, and agents. These effects record pipeline lifecycle events.

#### 3.7.1 PipelineStarted, PipelineComplete, PipelineAborted, TimeoutObserved

```python
class PipelineStarted(Effect):
    """Launcher emitted this to signal that a new pipeline run has begun.

    Agents check for the presence of a PipelineStarted effect
    to determine whether the run has begun.
    """
    kind: Literal["pipeline_started"] = "pipeline_started"
    agent: str = "launcher"
    config: dict = Field(default_factory=dict, description="pipeline configuration snapshot")
    max_tts_budget_usd: float = Field(default=2.0, gt=0.0)
    max_attempts_per_block: int = Field(default=5, ge=1)
    max_run_budget_usd: float = Field(default=10.0, gt=0.0)
    output_path: str = Field(default="/tmp/final_documentary.mp4")


class PipelineComplete(Effect):
    """Assembly finished. Final MP4 validated and ready."""
    kind: Literal["pipeline_complete"] = "pipeline_complete"
    agent: str = "assembly"
    run_id: str  # duplicate of base field for convenience in queries
    output_path: str
    duration_sec: float = Field(..., ge=0.0)
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    validation_passed: bool = True


class PipelineAborted(Effect):
    """Unrecoverable stop. The watcher loop ceases sending ticks.

    Reasons include budget exhaustion, repeated VM provision
    failures beyond threshold, or human instruction to abort.
    """
    kind: Literal["pipeline_aborted"] = "pipeline_aborted"
    agent: str = Field(..., description="agent or component that triggered abort")
    reason: Literal[
        "budget_exceeded",
        "vm_unavailable",
        "human_request",
        "loop_detected",
        "unknown",
    ]
    error_log: list[str] = Field(default_factory=list)
    spent_usd: float = Field(default=0.0, ge=0.0)


class TimeoutObserved(Effect):
    """Watcher detected a job pending longer than threshold.

    The watcher loop checks JobProjection on every tick: if any job has
    status "pending" or "running" for more than `stale_threshold_min`,
    this effect is emitted. The Provisioner routes it to VM cleanup
    (deallocates the stuck VM) + job requeue.

    This is a projection-based observation — no `threading.Timer`, no
    `asyncio.timeout`. Principle 4 ("no timeouts") is preserved because
    the timeout is observed externally, not enforced by the VM.
    """
    kind: Literal["timeout_observed"] = "timeout_observed"
    agent: str = "watcher"
    job_id: str
    vm_instance_id: str = ""
    pending_since: float = Field(..., description="timestamp when job entered pending/running")
    elapsed_min: float = Field(..., gt=0.0, description="how long the job has been pending")
    stale_threshold_min: float = Field(default=10.0, gt=0.0)
    action_taken: Literal["deallocate_vm", "requeue_job", "escalate"] = "deallocate_vm"
```

---

### 3.8 Bash / Human / Fallback Effects

Escape hatches, human intervention requests, and meta-effects that don't fit other families.

#### 3.8.1 ExecuteRawBash, HumanInstruction, ClarificationRequest

```python
class ExecuteRawBash(Effect):
    """Escape hatch: run a shell command.

    Security model: pre-approved commands (`ffmpeg`, `ffprobe`,
    `whisperx`, `vastai`, `python3` with known scripts) run without gating.
    Non-allowlisted commands are replaced by the parser with a
    `ClarificationRequest` ("Agent wants to run `curl ...` — approve?").
    Human approval produces a `HumanInstruction` containing the cleared command.

    The `approved_by_human` flag is set only after explicit human approval.
    Commands without this flag are rejected by the execution handler.
    """
    kind: Literal["execute_raw_bash"] = "execute_raw_bash"
    command: str = Field(..., min_length=1)
    working_dir: str = "/tmp"
    approved_by_human: bool = False
    approved_by: str = ""  # human name or empty
    allowlisted: bool = Field(
        default=False,
        description="True if command matches ALLOWLISTED_COMMANDS in config",
    )
    expected_artifacts: list[str] = Field(
        default_factory=list,
        description="files this command is expected to produce",
    )


class HumanInstruction(Effect):
    """Human overseer posted a directive to a specific agent.

    The overseer POSTs to the agent's endpoint with free text. The agent
    parses it on its next turn. Instructions can override parameters, approve
    blocked commands, or redirect the pipeline (e.g. "skip scene 5").
    """
    kind: Literal["human_instruction"] = "human_instruction"
    agent: str = Field(..., description="target agent name or 'all'")
    instruction: str = Field(..., min_length=1)
    from_human: str = Field(..., description="human identifier")
    posted_at: float = Field(default_factory=time.time)
    priority: Literal["normal", "urgent", "blocking"] = "normal"
    expires_at: float | None = None  # if set, instruction is ignored after this timestamp
    action: Literal["budget_override", "emergency_abort", "approve_command", "generic"] = "generic"
    action_params: dict = Field(default_factory=dict)


class ClarificationRequest(Effect):
    """Parser or agent needs human input to proceed.

    Triggers include: parser confidence below threshold, non-allowlisted
    bash command, unresolvable VM provision failures, or agent loop detection.
    The pipeline halts (no ticks) until a `HumanInstruction` resolves the
    request.
    """
    kind: Literal["clarification_request"] = "clarification_request"
    agent: str = Field(default="overseer", description="usually 'overseer' for human routing")
    parser_category: str = ""  # which parser category triggered this (if any)
    raw_text: str = ""  # original agent output that caused the problem
    failure_reason: str = Field(..., description="why clarification is needed")
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    suggested_resolution: str = ""
    question: str = ""           # human-readable question
    referenced_text: str = ""    # text that caused the issue
```

#### 3.8.2 AgentLoopDetected, NoOp

```python
class AgentLoopDetected(Effect):
    """Watcher detected an agent stuck in a loop.

    Two independent detection modes, both with configurable threshold N
    (default 5):
    1. Duplicate effects: the last N effects from this agent are identical.
    2. No progress: N ticks have elapsed without any projection state change.

    When either fires, the watcher halts and emits `ClarificationRequest`
    for human review.
    """
    kind: Literal["agent_loop_detected"] = "agent_loop_detected"
    agent: str = Field(..., description="agent that is looping")
    loop_signature: str = Field(..., description="concatenated kind sequence, e.g. 'queue_job:queue_job:queue_job'")
    effect_sequence: list[str] = Field(default_factory=list, description="last N effect kinds")
    detection_mode: Literal["duplicate_effects", "no_progress", "both"] = "both"
    detection_count: int = Field(..., ge=1, description="how many times loop pattern repeated")
    threshold: int = Field(default=5, ge=1, description="configured threshold that was exceeded")
    projection_delta: dict = Field(
        default_factory=dict,
        description="snapshot of projection changes (empty = no progress)",
    )


class NoOp(Effect):
    """Informational effect carrying no state mutation.

    The parser emits NoOp as a fallback when an agent's response contains
    no extractable effects. It is also used for heartbeat pings and logging.
    No projection applies NoOp — it passes through the store untouched.
    """
    kind: Literal["noop"] = "noop"
    reason: str = Field(default="no_effects_extracted")
    agent_context: str = ""  # free-text context from the agent
```


---

### 3.9 Production Failure Effect

Produced by the Audio Agent, Video Agent, or Assembly agent when media generation or final assembly fails in a way that requires explicit routing.

#### 3.9.1 ProductionFailed with failure_type routing table

```python
class ProductionFailed(Effect):
    """Media production or assembly failure with structured suggested fix.

    The failure_type field is the routing key. Agents read failure_type
    to decide: back-edge to SCRIPT, requeue in current phase, or halt
    for human intervention.
    """
    kind: Literal["production_failed"] = "production_failed"
    failure_type: Literal[
        "overlap",
        "duration_mismatch",
        "gap_unexpected",
        "voice_mismatch",
        "visual_incoherence",
        "artistic_reject",
        "missing_media",
        "invalid_range",
        "track_misalignment",
        "audio_lufs",
    ]
    slot_id: str = ""
    expected: str = ""  # human-readable expected value
    actual: str = ""    # human-readable actual value
    suggested_fix: SuggestedFix = Field(default_factory=SuggestedFix)
    vm_instance_id: str = ""
    attempt_number: int = Field(default=1, ge=1)


class SuggestedFix(BaseModel):
    """Structured fix proposal. Not a top-level effect — embedded in ProductionFailed."""
    fix_type: Literal[
        "requeue",             # retry same job with adjusted params
        "rewrite_script",      # back-edge to SCRIPT, fix narration text
        "adjust_params",       # tweak generation parameters
        "manual_intervention", # halt, human must fix
        "skip",                # skip this block and continue
    ] = "requeue"
    target_scene: int | None = None
    target_block: str | None = None
    new_params: dict = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    retry_count_suggestion: int = Field(default=3, ge=0, description="try N more times then escalate")
    rationale: str = ""
```

**Failure type routing table:**

| failure_type | Routing Action | Target Phase | Rationale |
|---|---|---|---|
| `gap_unexpected` | Back-edge to SCRIPT | SCRIPT | Narration text doesn't fit target duration |
| `voice_mismatch` | Back-edge to SCRIPT | SCRIPT | Wrong speaker or voice role assigned |
| `overlap` | Requeue with adjusted timing | AUDIO_RECONCILE | Clip overlaps neighbor in timeline |
| `duration_mismatch` | Requeue with new params | AUDIO_RECONCILE | TTS output duration outside tolerance |
| `visual_incoherence` | Requeue with revised prompt | VIDEO_PRODUCTION | LTX output doesn't match narration |
| `artistic_reject` | Requeue with adjusted params | VIDEO_PRODUCTION | Quality bar not met |
| `audio_lufs` | Requeue with gain adjustment | AUDIO_RECONCILE | Audio loudness out of spec |
| `track_misalignment` | Requeue assembly | ASSEMBLY | A/V tracks don't align after merge |
| `missing_media` | Retry artifact delivery | current phase | Artifact file not found at expected path |
| `invalid_range` | Requeue with corrected timing | current phase | OTIO source range is invalid |

The Scenario Agent checks for `failure_type in {"gap_unexpected", "voice_mismatch"}` to trigger the SCRIPT back-edge. All other failure types either requeue in the current phase or halt with `ClarificationRequest`.

---

### 3.10 EffectUnion and KIND_TO_MODEL

#### 3.10.1 Discriminated union definition

```python
from typing import Annotated
from pydantic import Field

EffectUnion = Annotated[
    Union[
        # 3.2 Script Effects (3)
        UpdateScript,
        DeleteScene,
        ReorderScenes,
        # 3.3 Job Effects (5)
        QueueJob,
        JobCompleted,
        JobFailed,
        JobRequeued,
        JobApproved,
        # 3.4 Reconciliation Effects (6)
        AudioGenerated,
        AudioMeasured,
        DurationAdjusted,
        ReconciliationFailed,
        ReconciliationPartial,
        ReconciliationComplete,
        # 3.5 VM Effects (4)
        VMAllocated,
        VMDeallocated,
        VMProvisionFailed,
        VMObserved,
        # 3.6 OTIO Effects (2)
        MergeIntoOTIO,
        DeleteFromOTIO,
        # 3.7 Pipeline Effects (4)
        PipelineStarted,
        PipelineComplete,
        PipelineAborted,
        TimeoutObserved,
        # 3.8 Bash / Human / Fallback (5)
        ExecuteRawBash,
        HumanInstruction,
        ClarificationRequest,
        AgentLoopDetected,
        NoOp,
        # 3.9 Production Failure (1)
        ProductionFailed,
    ],
    Field(discriminator="kind"),
]
```

`EffectUnion` is the only type accepted by `EventStore.append()`. Pydantic validates that the `kind` field matches the declared `Literal` value on the subclass. Any JSON payload with an unknown `kind` fails validation at the parser level, before reaching the event store.

#### 3.10.2 Complete KIND_TO_MODEL mapping

The parser uses `KIND_TO_MODEL` to resolve a `kind` string (extracted from agent output via category-conditioned string find) to the correct Pydantic model for validation:

```python
KIND_TO_MODEL: dict[str, type[Effect]] = {
    # 3.2 Script Effects
    "update_script":      UpdateScript,
    "delete_scene":       DeleteScene,
    "reorder_scenes":     ReorderScenes,
    # 3.3 Job Effects
    "queue_job":          QueueJob,
    "job_completed":      JobCompleted,
    "job_failed":         JobFailed,
    "job_requeued":       JobRequeued,
    "job_approved":       JobApproved,
    # 3.4 Reconciliation Effects
    "audio_generated":    AudioGenerated,
    "audio_measured":     AudioMeasured,
    "duration_adjusted":  DurationAdjusted,
    "reconciliation_failed":    ReconciliationFailed,
    "reconciliation_partial":   ReconciliationPartial,
    "reconciliation_complete":  ReconciliationComplete,
    # 3.5 VM Effects
    "vm_allocated":       VMAllocated,
    "vm_deallocated":     VMDeallocated,
    "vm_provision_failed": VMProvisionFailed,
    "vm_observed":        VMObserved,
    # 3.6 OTIO Effects
    "merge_into_otio":    MergeIntoOTIO,
    "delete_from_otio":   DeleteFromOTIO,
    # 3.7 Pipeline Effects
    "pipeline_started":   PipelineStarted,
    "pipeline_complete":  PipelineComplete,
    "pipeline_aborted":   PipelineAborted,
    "timeout_observed":   TimeoutObserved,
    # 3.8 Bash / Human / Fallback
    "execute_raw_bash":   ExecuteRawBash,
    "human_instruction":  HumanInstruction,
    "clarification_request": ClarificationRequest,
    "agent_loop_detected":   AgentLoopDetected,
    "noop":               NoOp,
    # 3.9 Production Failure
    "production_failed":  ProductionFailed,
}
```

The parser's `_extract_kind_markers` function performs case-insensitive substring search for each key in `KIND_TO_MODEL`. The category-conditioned prompt narrows the model set to the kinds relevant to the agent's current role, reducing false positives. The resulting kind string is looked up in `KIND_TO_MODEL`, and the corresponding model validates the extracted JSON.

#### 3.10.3 Naming convention summary

| Convention | Pattern | Examples |
|---|---|---|
| Imperative (agent requests) | Verb-noun, present tense | `QueueJob`, `ExecuteRawBash`, `MergeIntoOTIO`, `DeleteScene` |
| Past-tense (system outcomes) | Noun-verb or noun-adjective, past tense | `JobCompleted`, `AudioMeasured`, `PipelineComplete`, `VMDeallocated` |
| State descriptors | Adjective or participle | `ReconciliationComplete`, `ReconciliationPartial`, `TimeoutObserved` |
| Meta / diagnostic | Descriptive phrase | `AgentLoopDetected`, `ClarificationRequest`, `ProductionFailed` |

The naming convention is enforced by code review, not by the type system. When adding a new effect type, place it in the family section matching its producer, follow the naming convention based on whether it is an agent request or a system outcome, add it to `EffectUnion`, and register it in `KIND_TO_MODEL`.


---

## 4. Rules as Prompt (No State Machine, No Rules Engine Code)

There is no state machine and no `RulesEngine` Python class. Prioritization, filtering, and response selection happen **inside the agent via prompt instruction**. The watcher passes projection-derived narratives to the agent; the agent's system prompt contains the rules for what to prioritize and how to respond.

This follows the principle: *whenever something can be done via prompt, do so — cut code complexity.*

### 4.1 Agent System Prompt: Embedded Rules

Each agent's `instructions` (system prompt) includes a **RULES block** that tells it how to prioritize situations:

```
=== YOUR ROLE ===
You are the {role} agent. You produce effects. You decide what to do.

=== RULES ===
1. Prioritize safety situations (budget critical, loop detected) above all else.
2. Prioritize blocked situations (stale VM, job queued long) next.
3. Prioritize work situations (dirty block, measurement needed) last.
4. If multiple work situations, pick the one with the lowest slot_id.
5. If no situations apply, emit NoOp with reason.
6. Never emit effects outside your permitted kinds: {permitted_effects}.

=== CURRENT SITUATIONS ===
{situation_narratives}

=== YOUR MEMORY ===
{memory}

=== AVAILABLE EFFECTS ===
{effect_schema}
```

### 4.2 Emergent Pipeline Phases

| Phase | Emergent Condition | Active Agents |
|---|---|---|
| **INIT** | No `PipelineStarted` effect | None |
| **SCRIPT** | `PipelineStarted` exists, OTIO has unfilled slots | Scenario |
| **AUDIO_RECONCILE** | OTIO has dirty audio blocks | Audio |
| **VIDEO_PRODUCTION** | All audio clean, video slots unfilled | Video |
| **ASSEMBLY** | All slots filled, final MP4 missing | Assembly |
| **DONE** | Final MP4 exists and validates | None |
| **ABORTED** | `PipelineAborted` emitted | None |

These are not states. They are descriptive labels for human observation. No code enforces transitions — they emerge from what agents do.

### 4.3 Rules Block (Agent System Prompt Text)

Rules live in the agent's system prompt. They are not code. Each agent receives the same RULES block; only the `PERMITTED EFFECTS` section differs by role.

```
=== RULES ===
1. If agent_loop_detected -> emit ClarificationRequest and stop.
2. If pipeline_budget_critical -> emit PipelineAborted and stop.
3. If block_at_max_attempts -> handle escalation (accept, human, abort).
4. If measurement_complete_fail -> requeue with adjusted params.
5. If fresh_dirty_block -> do the work (queue job, measure, judge).
6. If vm_stale -> note it (Provisioner handles VM cleanup deterministically).
7. If noop_all_clean -> emit NoOp, nothing to do.

Pick the highest-priority rule that applies. Only one action per turn.
```

---

## 5. Event Store

The event store is a single SQLite database opened through `aiosqlite`. Every effect (Section 3.1) is appended as one row. The store enforces idempotency on `(run_id, effect_id)` via `INSERT OR IGNORE` — retrying an `append()` with the same `effect_id` is a safe no-op. A single asyncio queue serializes all writes behind `BEGIN IMMEDIATE`, eliminating WAL-mode lock contention. Projections query the store through `read_since()` and `replay()`; no projection mutates the database.

The database file is the only durable artifact of a pipeline run. All state — OTIO timeline, job queue, VM inventory, pipeline phase — is rebuilt from events. Losing the database means losing the run. Section 5.4.2 defines the backup strategy.

---

### 5.1 Schema

#### 5.1.1 events table with UNIQUE(run_id, effect_id) constraint

```sql
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    sequence      INTEGER NOT NULL,
    effect_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    created_at    REAL NOT NULL
);

-- Idempotency constraint: duplicate (run_id, effect_id) is silently
-- discarded by INSERT OR IGNORE.  This survives retries, replays, and idempotent
-- re-submissions from agents.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_effect_id
    ON events(run_id, effect_id);

-- Ordering index: replay and read_since both filter by run_id and ORDER BY sequence.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_seq
    ON events(run_id, sequence);

-- Projection filtering: the OTIO, Job, and VM projections often query by kind
-- to locate specific effect types (e.g. all queue_job events).
CREATE INDEX IF NOT EXISTS idx_events_kind
    ON events(kind);

-- Time-range queries for operational inspection and backup windows.
CREATE INDEX IF NOT EXISTS idx_events_run_created
    ON events(run_id, created_at);
```

| Column | SQLite Type | Null | Source | Purpose |
|---|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | NO | store | Monotonic row identifier; never exposed to agents |
| `run_id` | `TEXT` | NO | caller | Scopes all rows to one pipeline run |
| `sequence` | `INTEGER` | NO | store | Per-run ordering; increments from 1 per run |
| `effect_id` | `TEXT` | NO | client (`uuid7()`) | Idempotency key; survives retry and replay |
| `kind` | `TEXT` | NO | agent/parser | Discriminant for `EffectUnion` dispatch |
| `payload_json` | `TEXT` | NO | `effect.model_dump_json()` | Full Pydantic serialization; source of truth |
| `created_at` | `REAL` | NO | `time.time()` | Seconds since epoch; operational ordering aid |

**Constraint design rationale:** `UNIQUE(run_id, effect_id)` is a unique index, not a table-level constraint, to allow `INSERT OR IGNORE` to skip duplicates without raising. The `id` column is an auto-incrementing integer for human-readable row ordering but is never referenced by application code. The `sequence` column is the application-visible ordering key; it is assigned inside the single writer loop (Section 5.2.1) and is unique per `(run_id, sequence)` pair via the `idx_events_run_seq` unique index.

---

#### 5.1.2 Indexes for replay performance

The four indexes serve distinct query patterns:

| Index | Query Pattern | Used By |
|---|---|---|
| `idx_events_run_effect_id` | `WHERE run_id = ? AND effect_id = ?` (implicit in `INSERT OR IGNORE`) | Writer loop — deduplication |
| `idx_events_run_seq` | `WHERE run_id = ? ORDER BY sequence` | `replay()`, `read_since()` |
| `idx_events_kind` | `WHERE kind = ?` | Projection state rebuilds, analytics |
| `idx_events_run_created` | `WHERE run_id = ? AND created_at > ?` | Operational queries, backup windows |

All indexes are created in `IF NOT EXISTS` form so that `EventStore.__init__` can safely re-run the DDL on an existing database file. The schema does not use foreign keys, triggers, or views — application-level logic (projections) owns all derived state.

---

### 5.2 Single Writer Pattern

#### 5.2.1 Asyncio queue + BEGIN IMMEDIATE

All writes pass through a single `asyncio.Queue`. One coroutine, `_writer_loop`, consumes from this queue and holds an `aiosqlite` connection open for the lifetime of the store. Each write is wrapped in `BEGIN IMMEDIATE` / `COMMIT` so that SQLite acquires the reserved lock immediately, preventing reader-writer deadlocks that can occur with deferred transactions under WAL mode.

```python
import asyncio
import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import aiosqlite


class EventStore:
    """Append-only SQLite event store. Single writer via asyncio queue.

    Guarantees:
    - Every append returns a monotonic sequence number per run_id.
    - Duplicate (run_id, effect_id) is silently dropped (idempotent retry).
    - All writes are serialized: no concurrent transactions.
    - Reader methods (read_since, replay) use separate connections.

    Initialization:
        store = EventStore("/data/events.db")
        # queue is running; schema is created if missing

    Shutdown:
        await store.close()
    """

    # -- public API ---------------------------------------------------------

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._queue: asyncio.Queue = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._seq_cache: dict[str, int] = {}  # run_id -> last sequence
        self._closed = False

    async def start(self):
        """Create schema and start the single writer coroutine.

        Called automatically on first append if not already started.
        """
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        await self._init_schema()
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name="event-store-writer"
        )

    async def append(self, run_id: str, effect_id: UUID, kind: str,
                     payload_json: str) -> int:
        """Queue an effect for writing. Returns the assigned sequence number.

        If (run_id, effect_id) already exists, the call returns the original
        sequence number without inserting a duplicate row.
        """
        if self._closed:
            raise RuntimeError("EventStore is closed")
        if self._writer_task is None:
            await self.start()

        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        await self._queue.put((run_id, str(effect_id), kind, payload_json, future))
        return await future

    async def read_since(self, run_id: str, sequence: int) -> list[dict[str, Any]]:
        """Return all events for run_id with sequence > given value.

        Used by projections for incremental updates. Each projection tracks
        its own 'last_sequence' and calls read_since() on every tick.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT sequence, effect_id, kind, payload_json, created_at "
                "FROM events WHERE run_id = ? AND sequence > ? ORDER BY sequence",
                (run_id, sequence),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def replay(self, run_id: str) -> list[dict[str, Any]]:
        """Return all events for a run_id, ordered by sequence.

        Full replay for state reconstruction (e.g. restarting a projection
        from scratch or rebuilding a read model after a schema change).
        """
        return await self.read_since(run_id, sequence=0)

    async def close(self):
        """Signal the writer loop to exit and wait for it to finish."""
        self._closed = True
        if self._writer_task and not self._writer_task.done():
            await self._queue.put(None)  # sentinel
            try:
                await asyncio.wait_for(self._writer_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._writer_task.cancel()

    # -- internal -----------------------------------------------------------

    async def _init_schema(self):
        """Execute DDL. Safe to call on an existing database."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_DDL)
            await db.commit()

    async def _writer_loop(self):
        """Single writer coroutine. Runs until sentinel (None) is received."""
        async with aiosqlite.connect(self.db_path) as db:
            while True:
                item = await self._queue.get()
                if item is None:
                    break  # sentinel — shutdown

                run_id, effect_id, kind, payload_json, future = item
                try:
                    seq = await self._write_one(db, run_id, effect_id,
                                                kind, payload_json)
                    future.set_result(seq)
                except Exception as exc:
                    future.set_exception(exc)

    async def _write_one(self, db: aiosqlite.Connection, run_id: str,
                         effect_id: str, kind: str,
                         payload_json: str) -> int:
        """Execute one idempotent write inside BEGIN IMMEDIATE.

        1. Acquire reserved lock immediately.
        2. Check cache for next sequence; fall back to MAX(sequence) query.
        3. INSERT OR IGNORE — duplicates are silently dropped.
        4. If insert was ignored (no new row), look up the original sequence.
        5. COMMIT and return the sequence number.
        """
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Resolve next sequence
            cached = self._seq_cache.get(run_id, 0)
            if cached == 0:
                cursor = await db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id = ?",
                    (run_id,),
                )
                row = await cursor.fetchone()
                cached = row[0] if row else 0
            next_seq = cached + 1

            created_at = time.time()
            cursor = await db.execute(
                "INSERT OR IGNORE INTO events "
                "(run_id, sequence, effect_id, kind, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, next_seq, effect_id, kind, payload_json, created_at),
            )

            if cursor.rowcount == 0:
                # Duplicate (run_id, effect_id) — look up original sequence
                cur = await db.execute(
                    "SELECT sequence FROM events WHERE run_id = ? AND effect_id = ?",
                    (run_id, effect_id),
                )
                row = await cur.fetchone()
                seq = row[0] if row else next_seq
            else:
                seq = next_seq
                self._seq_cache[run_id] = seq

            await db.commit()
            return seq
        except Exception:
            await db.rollback()
            raise

    @property
    def _db_path(self) -> str:
        return self.db_path


_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    sequence      INTEGER NOT NULL,
    effect_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    created_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_effect_id
    ON events(run_id, effect_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_seq
    ON events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_kind
    ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_run_created
    ON events(run_id, created_at);
"""
```

**Key implementation decisions:**

| Decision | Rationale |
|---|---|
| `BEGIN IMMEDIATE` | Acquires the reserved lock at transaction start, preventing "database is locked" errors from deferred-lock upgrades under concurrent readers |
| `INSERT OR IGNORE` | Duplicate `(run_id, effect_id)` is silently dropped — no exception, no need for application-level try/catch |
| `_seq_cache` dict | Avoids `MAX(sequence)` query on hot runs; cache is per-process and correct because the single writer is the only sequence allocator |
| Sentinel shutdown | `None` placed on the queue signals the writer to exit cleanly, committing any pending work before stopping |
| Separate reader connections | `read_since()` and `replay()` open their own `aiosqlite.connect()` context managers; they never contend with the writer for the connection lock |
| Lazy start | `append()` calls `start()` automatically on first use; callers may also call `start()` explicitly for deterministic startup timing |


---

#### 5.2.2 Deduplication on effect_id

The deduplication flow when an agent retries `append()`:

```
Agent                    EventStore                    SQLite
  |                         |                            |
  |-- append(effect_id=X) ->|                            |
  |   (network drops)       |-- BEGIN IMMEDIATE          |
  |                         |-- INSERT (X) -> row 47     |
  |   (no response)         |-- COMMIT                   |
  |                         |   (response lost)          |
  |                         |                            |
  |-- append(effect_id=X) ->|  (retry, same effect_id)   |
  |                         |-- BEGIN IMMEDIATE          |
  |                         |-- INSERT OR IGNORE (X)     |
  |                         |   rowcount == 0 (ignored)  |
  |                         |-- SELECT sequence WHERE    |
  |                         |   run_id=R AND effect_id=X |
  |<-- returns 47 ----------|   -> 47                    |
  |                         |-- COMMIT                   |
```

The agent receives the same sequence number (47) on both calls. The event log contains exactly one row for `effect_id=X`. This property holds across process restarts, network partitions, and agent crashes because the unique constraint is in the database, not in memory.

---

### 5.3 Replay

#### 5.3.1 read_since() method for incremental projection updates

Every projection tracks `last_sequence` — the highest sequence number it has already processed. On each tick (Section 12), the projection calls `read_since(run_id, last_sequence)` and receives only new events. This avoids re-reading the entire event log on every tick.

```python
class OTIOProjection:
    """Example: incremental update via read_since()."""

    def __init__(self):
        self.timeline = otio.schema.Timeline(name="Documentary")
        self.tracks: dict[str, Any] = {}
        self.last_sequence = 0

    async def tick(self, event_store: EventStore, run_id: str):
        """Process only events newer than last_sequence."""
        rows = await event_store.read_since(run_id, self.last_sequence)
        for row in rows:
            effect = _parse_payload(row["kind"], row["payload_json"])
            self._apply(effect)
            self.last_sequence = row["sequence"]
        return len(rows)
```

The return type of `read_since()` is `list[dict[str, Any]]` where each dict has keys: `sequence`, `effect_id`, `kind`, `payload_json`, `created_at`. Projections deserialize `payload_json` into the appropriate Pydantic model via `EffectUnion` dispatch (Section 3.10). The `sequence` field in the response is the same integer that `append()` returned when the effect was first written.

**Invariants maintained by read_since():**
- Results are strictly ordered by `sequence` ascending.
- The `sequence` value in each row is greater than the input `sequence` parameter.
- If no new events exist, returns an empty list.
- The method is read-only: it never mutates the database.

---

#### 5.3.2 Full replay for state reconstruction

`replay(run_id)` is a convenience wrapper that calls `read_since(run_id, 0)` — returning every event for the run, from sequence 1 to the highest assigned. Full replay is used in three operational scenarios:

| Scenario | Trigger | Action |
|---|---|---|
| Projection schema change | New field added to a projection's state model | Rebuild the projection from sequence 0 |
| Process restart | Watcher loop or agent crashes and restarts | Replay to restore in-memory state |
| Run audit | Human operator inspects a completed run | Replay returns full event history |

The cost of a full replay is `O(N)` where `N` is the event count for the run. Typical documentary runs produce 500–2000 events; SQLite with the `idx_events_run_seq` index replays this in under 10ms on NVMe storage.

```python
# Full replay example: rebuild a JobProjection from scratch
async def rebuild_job_projection(store: EventStore, run_id: str) -> JobProjection:
    """Construct a fresh JobProjection by replaying all events."""
    proj = JobProjection()
    events = await store.replay(run_id)
    for row in events:
        effect = _parse_payload(row["kind"], row["payload_json"])
        proj.apply(effect)
    return proj
```

Both `read_since()` and `replay()` return row dictionaries with the `payload_json` string unparsed. The caller is responsible for deserializing via `json.loads()` and `EffectUnion` model validation (Section 3.10). This keeps the event store agnostic of effect type definitions.

---

### 5.4 Operational Concerns

#### 5.4.1 Disk usage monitoring (>80% → ClarificationRequest)

The event store is the only persistent data structure. If the host disk fills, all appends fail, pipeline state cannot advance, and the run is effectively dead. A background coroutine polls disk usage every 30 seconds and emits a `ClarificationRequest` effect when usage exceeds 80%.

```python
import shutil


async def disk_monitor(event_store: EventStore, run_id: str,
                       threshold: float = 0.80, interval_sec: float = 30.0):
    """Poll disk usage. Emit ClarificationRequest if above threshold.

    The event store catches the emitted effect through its own append()
    mechanism, so the alert itself is logged and visible to overseers.
    """
    while True:
        await asyncio.sleep(interval_sec)
        usage = shutil.disk_usage(event_store.db_path)
        ratio = usage.used / usage.total
        if ratio > threshold:
            payload = {
                "run_id": run_id,
                "effect_id": str(uuid7()),
                "agent": "disk_monitor",
                "kind": "clarification_request",
                "timestamp": time.time(),
                "message": (
                    f"Disk usage {ratio:.1%} exceeds threshold {threshold:.0%}. "
                    f"Database path: {event_store.db_path}. "
                    f"Used: {usage.used // (1024**3)} GB / "
                    f"Total: {usage.total // (1024**3)} GB. "
                    "Free disk or abort the pipeline."
                ),
                "severity": "critical",
                "category": "disk_full",
            }
            await event_store.append(
                run_id=run_id,
                effect_id=UUID(payload["effect_id"]),
                kind="clarification_request",
                payload_json=json.dumps(payload),
            )
```

| Parameter | Default | Description |
|---|---|---|
| `threshold` | `0.80` | Fraction of total disk used before alert fires |
| `interval_sec` | `30.0` | Seconds between polls |
| `severity` | `"critical"` | Embedded in the ClarificationRequest payload |
| `category` | `"disk_full"` | Routing hint for agents |

The monitor runs as a coroutine in the same process as the watcher loop. It is started when the pipeline launches and stops when `event_store.close()` is called. The threshold is configurable per deployment; documentary runs with large video artifacts may need a lower threshold (e.g., 0.60) to leave headroom for LTX-2.3 output files.

---

#### 5.4.2 Backup strategy: copy SQLite file (not event log streaming)

The event store is a single SQLite file on local disk. Backup is a straightforward file copy, not a streaming replication protocol. Two backup modes are supported:

**Online backup (hot copy):** SQLite's `VACUUM INTO` creates a consistent snapshot without locking the database for reads or writes. The event store exposes this as a method:

```python
async def backup_to(self, dest_path: str) -> str:
    """Create a consistent snapshot via VACUUM INTO.

    Returns the destination path. Safe to call while the writer
    loop is active — readers and writers are not blocked.
    """
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(self.db_path) as db:
        await db.execute(f"VACUUM INTO ?", (dest_path,))
    return dest_path
```

**Offline backup (stop-and-copy):** For scheduled backups (e.g., every 10 minutes), the recommended approach is:

```python
async def scheduled_backup(event_store: EventStore, backup_dir: str):
    """Create a timestamped backup copy of the database.

    Called by the watcher loop on a 10-minute interval.
    """
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dest = f"{backup_dir}/events_{ts}.db"
    await event_store.backup_to(dest)
    # Retain last N backups; delete older ones
    _prune_old_backups(backup_dir, keep=10)
```

| Backup Type | Method | Lock Impact | Use Case |
|---|---|---|---|
| Online | `VACUUM INTO` | None (readers/writers proceed) | Ad-hoc snapshots, pre-transition checkpoints |
| Scheduled | `VACUUM INTO` + pruning | None | Automated 10-minute backups during long runs |
| Full replay | `replay()` → JSON export | Read-only | Run archival, external audit |

**Why not WAL archive streaming?** SQLite WAL mode supports archive replication via `sqlite3_wal_checkpoint()`, but this adds operational complexity (separate WAL file management, checkpoint timing, archive cleanup) for marginal benefit. The `VACUUM INTO` approach produces a single, self-contained file that can be moved, copied, or rsync'd without tool dependencies. Documentary pipeline runs are hours long, not days; a 10-minute backup interval with 10 retained copies provides sufficient recovery granularity.

**Recovery procedure:** If the active database is corrupted or lost, stop the pipeline, copy the most recent backup to the active path, and restart. The event store's `INSERT OR IGNORE` semantics mean that any effects re-emitted by agents after the backup point are silently deduplicated — the pipeline self-heals from the backup timestamp forward.


---

## 6. Projections

Projections are **incremental read models** rebuilt from the event log. Each projection tracks `last_sequence` and processes only new events on every `tick`. If the SQLite database is wiped, replaying the event log through every projection reconstructs the entire pipeline state. Projections never emit events — they are pure consumers (Section 6.1 enforces this absolutely).

---

### 6.1 Projection Base Class

#### 6.1.1 Abstract base with tick(event_store) and apply(effect) interface

All projections inherit from `Projection`, an abstract base class that defines two operations:

- `tick(event_store)`: fetch events newer than `last_sequence`, apply each, increment `last_sequence`.
- `apply(event)`: mutate the projection's internal state in response to a single event.

```python
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Protocol


class EventStore(Protocol):
    """Protocol for the event store — projections depend only on read_since."""

    async def read_since(self, run_id: str, sequence: int) -> list[dict]: ...


class Effect(Protocol):
    """Protocol for effects — projections read kind and payload fields."""

    kind: str
    sequence: int


class Projection(ABC):
    """Abstract base for all incremental read models.

    Subclasses implement ``apply()`` to define how each event kind mutates state.
    The ``tick()`` method is final — it handles event fetching and sequence tracking.
    """

    def __init__(self) -> None:
        self.last_sequence: int = 0

    async def tick(self, event_store: EventStore, run_id: str) -> int:
        """Fetch events since ``last_sequence`` and apply each.

        Returns the number of events processed.
        """
        events = await event_store.read_since(run_id, self.last_sequence)
        processed = 0
        for event in events:
            self.apply(event)
            self.last_sequence = event["sequence"]
            processed += 1
        return processed

    @abstractmethod
    def apply(self, event: Effect) -> None:
        """Mutate projection state in response to a single event.

        Must be implemented by every concrete projection.
        """
        ...

    def summary(self) -> str:
        """Return a human-readable summary for agent prompts.

        Subclasses override to produce O(1) summaries regardless of event log length.
        """
        return f"{self.__class__.__name__}(last_sequence={self.last_sequence})"
```

#### 6.1.2 last_sequence tracking for incremental updates

`last_sequence` is the waterline. On each `tick`, the projection calls `event_store.read_since(self.last_sequence)`, which returns all events with `sequence > last_sequence` ordered by `sequence`. After applying each event, `last_sequence` advances to that event's sequence number. If `tick` processes zero events, `last_sequence` is unchanged and no state mutation occurs.

This design guarantees idempotent `tick` calls: calling `tick` twice with no new events is a no-op. It also makes projections deterministic and replay-safe: reconstructing a projection from an empty state by calling `tick` in a loop until no events remain produces the same state as a projection that has been incrementally updated since run start.

---

### 6.2 OTIO Projection

#### 6.2.1 Timeline construction from script + merge + adjust events

`OTIOProjection` builds an OpenTimelineIO `schema.Timeline` from three event families:

- **Script events** (`UpdateScript`, `DeleteScene`, `ReorderScenes`): define narration blocks with speaker, text, and target duration.
- **Merge events** (`MergeIntoOTIO`): insert approved media clips into timeline slots.
- **Adjust events** (`DurationAdjusted`): update a slot's duration after measured audio passes tolerance.

```python
from typing import Optional
import opentimelineio as otio


class OTIOProjection(Projection):
    """Builds and validates an OpenTimelineIO timeline from events.

    The timeline is the authoritative structure for the documentary.
    It contains one or more tracks (e.g., "A1_Narration", "V1_Video"),
    each composed of clips aligned to scene slots.
    """

    def __init__(self) -> None:
        super().__init__()
        self.timeline: otio.schema.Timeline = otio.schema.Timeline(
            name="Documentary", global_start_time=otio.opentime.RationalTime(0, 24)
        )
        self.slots: dict[str, dict] = {}  # slot_addr -> {scene_num, speaker, text, duration}

    def apply(self, event: Effect) -> None:
        match event.kind:
            case "update_script" | "script_updated":
                self._build_from_script(event)
            case "merge_into_otio":
                self._merge_clip(event)
            case "duration_adjusted":
                self._adjust_slot_duration(event)
            case "delete_scene":
                self._delete_scene(event)
            case "reorder_scenes":
                self._reorder_scenes(event)
            case "delete_from_otio":
                self._remove_clip(event)
            case "reconciliation_partial":
                self._mark_dirty_slots(event)

    def _build_from_script(self, event: Effect) -> None:
        """Rebuild narration track from script event.

        Creates one clip per narration block with the scripted duration.
        Media reference is initially ``MissingReference`` — filled by
        ``MergeIntoOTIO`` when audio/video production completes.
        """
        track_name = "A1_Narration"
        track = otio.schema.Sequence(name=track_name)
        self.timeline.tracks.clear()
        self.timeline.tracks.append(track)
        self.slots.clear()

        for block in getattr(event, "blocks", []):
            slot_addr = f"{track_name}:{block.scene_num}:{block.phrase_idx}"
            rate = 24  # Working rate; adjusted in ASSEMBLY
            duration_rt = otio.opentime.RationalTime(
                block.duration_sec * rate, rate
            )
            clip = otio.schema.Clip(
                name=slot_addr,
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, rate),
                    duration=duration_rt,
                ),
                media_reference=otio.schema.MissingReference(),
            )
            track.append(clip)
            self.slots[slot_addr] = {
                "scene_num": block.scene_num,
                "phrase_idx": block.phrase_idx,
                "speaker": block.speaker,
                "text": block.text,
                "scripted_sec": block.duration_sec,
                "measured_sec": None,
                "status": "scripted",
                "artifact_path": None,
            }

    def _merge_clip(self, event: Effect) -> None:
        """Replace MissingReference with an ExternalReference to the produced artifact."""
        slot_addr = event.slot_id
        clip = self._find_clip_by_name(slot_addr)
        if clip is None:
            return
        clip.media_reference = otio.schema.ExternalReference(
            target_url=f"file://{event.artifact_path}",
            available_range=clip.source_range,
        )
        if slot_addr in self.slots:
            self.slots[slot_addr]["status"] = "delivered"
            self.slots[slot_addr]["artifact_path"] = event.artifact_path

    def _adjust_slot_duration(self, event: Effect) -> None:
        """Update a slot's duration after reconciliation passes tolerance."""
        slot_addr = event.block_id
        clip = self._find_clip_by_name(slot_addr)
        if clip is None:
            return
        rate = 24
        new_duration = otio.opentime.RationalTime(event.measured_sec * rate, rate)
        clip.source_range = otio.opentime.TimeRange(
            start_time=clip.source_range.start_time,
            duration=new_duration,
        )
        if slot_addr in self.slots:
            self.slots[slot_addr]["measured_sec"] = event.measured_sec
            self.slots[slot_addr]["status"] = "measured"

    def _delete_scene(self, event: Effect) -> None:
        """Remove all slots belonging to a scene."""
        scene_num = event.scene_num
        to_remove = [
            addr for addr, slot in self.slots.items()
            if slot["scene_num"] == scene_num
        ]
        for addr in to_remove:
            clip = self._find_clip_by_name(addr)
            if clip is not None:
                track = clip.parent()
                if track is not None:
                    track.remove(clip)
            self.slots.pop(addr, None)

    def _reorder_scenes(self, event: Effect) -> None:
        """Reorder tracks — applied via script rebuild (rebuilds from new order)."""
        pass

    def _remove_clip(self, event: Effect) -> None:
        """Remove a clip from the timeline (e.g., rejected media)."""
        clip = self._find_clip_by_name(event.slot_id)
        if clip is not None:
            track = clip.parent()
            if track is not None:
                track.remove(clip)

    def _mark_dirty_slots(self, event: Effect) -> None:
        """Mark slots as dirty on ReconciliationPartial — reset to scripted state."""
        for slot_addr in getattr(event, "dirty_block_ids", []):
            if slot_addr in self.slots:
                self.slots[slot_addr]["status"] = "dirty"
                self.slots[slot_addr]["measured_sec"] = None
                self.slots[slot_addr]["artifact_path"] = None
        for slot_addr in getattr(event, "clean_block_ids", []):
            if slot_addr in self.slots:
                self.slots[slot_addr]["status"] = "clean"

    def _find_clip_by_name(self, name: str) -> Optional[otio.schema.Clip]:
        for track in self.timeline.tracks:
            for child in track:
                if isinstance(child, otio.schema.Clip) and child.name == name:
                    return child
        return None

    def all_slots_filled(self) -> bool:
        """Return True if every narration slot has a delivered audio clip."""
        slots = getattr(self, "slots", {})
        if not slots:
            return False
        return all(s.get("status") == "delivered" for s in slots.values())

    def get_timeline_duration_sec(self) -> float:
        """Return timeline duration in seconds."""
        dur = self.timeline.duration()
        return dur.value / dur.rate if dur and dur.rate else 0.0

    def summary(self) -> str:
        total = len(self.slots)
        measured = sum(1 for s in self.slots.values() if s["status"] == "measured")
        delivered = sum(1 for s in self.slots.values() if s["status"] == "delivered")
        dirty = sum(1 for s in self.slots.values() if s["status"] == "dirty")
        scenes = len({s["scene_num"] for s in self.slots.values()})
        return (
            f"OTIO: {scenes} scenes, {total} slots, "
            f"{measured} measured, {delivered} delivered, {dirty} dirty"
        )
```

#### 6.2.2 Validation: no_overlaps, track_alignment, clip_media

Three validation methods support agent decision-making. Each returns `(bool, Optional[str])`: `True` with no message on success, `False` with a descriptive error on failure.

```python
    def validate_no_overlaps(self) -> tuple[bool, Optional[str]]:
        """Check that no two clips on the same track overlap in time.

        Transitions are skipped — overlapping a Transition with a Clip
        is valid OTIO behavior.
        """
        for track in self.timeline.tracks:
            children = list(track)
            for i in range(len(children) - 1):
                a, b = children[i], children[i + 1]
                if isinstance(a, otio.schema.Transition) or isinstance(
                    b, otio.schema.Transition
                ):
                    continue
                try:
                    ra = a.trimmed_range_in_parent()
                    rb = b.trimmed_range_in_parent()
                except Exception:
                    continue
                if ra is None or rb is None:
                    continue
                if not (ra.end_time_inclusive() <= rb.start_time):
                    return (
                        False,
                        f"Overlap on {track.name}: {a.name} ({ra}) vs {b.name} ({rb})",
                    )
        return True, None

    def validate_track_alignment(self) -> tuple[bool, Optional[str]]:
        """Check that all tracks have the same duration.

        A documentary has one coherent timeline — all tracks must span
        the same time range. Returns False if the max track duration
        differs from the timeline duration.
        """
        if not self.timeline.tracks:
            return True, None
        track_durations = []
        for track in self.timeline.tracks:
            try:
                d = track.duration()
                if d is not None:
                    track_durations.append(d)
            except Exception:
                continue
        if not track_durations:
            return True, None
        max_dur = max(track_durations, key=lambda rt: rt.value)
        timeline_dur = self.timeline.duration()
        if timeline_dur is None:
            return False, "Timeline has no duration"
        if abs(timeline_dur.value - max_dur.value) > 0.5:
            return (
                False,
                f"Track misalignment: timeline {timeline_dur.value:.2f}s "
                f"!= max track {max_dur.value:.2f}s",
            )
        return True, None

    def validate_clip_media(self) -> tuple[bool, Optional[str]]:
        """Check that every clip has a valid media reference.

        A clip passes if it has a non-MissingReference media target
        and its trimmed_range resolves without exception.
        """
        for track in self.timeline.tracks:
            for child in track:
                if not isinstance(child, otio.schema.Clip):
                    continue
                if isinstance(child.media_reference, otio.schema.MissingReference):
                    return False, f"Clip {child.name} has no media reference"
                try:
                    _ = child.trimmed_range()
                except Exception as e:
                    return False, f"Clip {child.name} invalid range: {e}"
        return True, None
```

#### 6.2.3 Slot addressing scheme (track:scene:slot)

Every slot in the timeline has a canonical address of the form `track_name:scene_num:phrase_idx`. Example: `"A1:3:2"` identifies the third phrase in scene 3 on the A1 (audio narration) track. This addressing scheme is used in:

- `QueueJob.slot_id` — the slot a job targets
- `MergeIntoOTIO.slot_id` — where to insert the produced clip
- `DurationAdjusted.block_id` — which slot's duration changed
- `ReconciliationPartial.dirty_block_ids` and `clean_block_ids` — which slots need re-reconciliation

The `OTIOProjection._find_clip_by_name()` method resolves a slot address to its `otio.schema.Clip` by iterating tracks and matching `clip.name == slot_addr`.


---

### 6.3 Job Projection

#### 6.3.1 Job lifecycle tracking (pending → running → completed/failed)

`JobProjection` tracks the state of every job in the pipeline. A job passes through the lifecycle: `pending` → `running` → `completed` or `failed`. Jobs can be requeued (return to `pending` with updated parameters).

```python
from collections import defaultdict


class JobState:
    """Mutable record for a single job's current state."""

    def __init__(self, job_id: str, job_type: str, slot_id: str) -> None:
        self.job_id: str = job_id
        self.job_type: str = job_type          # "tts" | "video" | "whisperx" | "ffmpeg"
        self.slot_id: str = slot_id
        self.status: str = "pending"           # pending | running | completed | failed
        self.params: dict[str, Any] = {}
        self.artifact_path: Optional[str] = None
        self.duration_sec: Optional[float] = None
        self.error_message: Optional[str] = None
        self.requeue_count: int = 0
        self.created_at: float = 0.0
        self.completed_at: Optional[float] = None


class JobProjection(Projection):
    """Tracks job lifecycle, reconciliation state, budget, and production failures.

    V6 additions:
    - ``dirty_blocks`` / ``clean_blocks``: per-block authority tracking
    - ``block_attempts``: per-block retry counter, bounded by max_attempts
    - ``spent_usd``: cumulative budget accumulator
    - ``production_failures``: list of unrecoverable production failures
    """

    def __init__(self) -> None:
        super().__init__()
        self.jobs: dict[str, JobState] = {}
        self.reconciliation_complete: bool = False
        self.dirty_blocks: set[str] = set()
        self.clean_blocks: set[str] = set()
        self.block_attempts: dict[str, int] = defaultdict(int)
        self.spent_usd: float = 0.0
        self.production_failures: list[dict[str, Any]] = []

    def apply(self, event: Effect) -> None:
        match event.kind:
            # --- Job lifecycle ---
            case "queue_job":
                self._on_queue(event)
            case "job_started":
                self._on_start(event)
            case "job_completed":
                self._on_complete(event)
            case "job_failed":
                self._on_fail(event)
            case "job_requeued":
                self._on_requeue(event)
            # --- Reconciliation state ---
            case "reconciliation_complete":
                self.reconciliation_complete = True
                self.dirty_blocks.clear()
            case "reconciliation_failed":
                self.reconciliation_complete = False
            case "reconciliation_partial":
                self._on_partial(event)
            # --- Budget ---
            case "cost_incurred":
                self.spent_usd += getattr(event, "amount_usd", 0.0)
            case "pipeline_aborted":
                pass  # Guard reads spent_usd, not the projection
            # --- Production failures ---
            case "production_failed":
                self.production_failures.append({
                    "slot_id": getattr(event, "slot_id", ""),
                    "failure_type": getattr(event, "failure_type", ""),
                    "expected": getattr(event, "expected", ""),
                    "actual": getattr(event, "actual", ""),
                    "suggested_fix": getattr(event, "suggested_fix", ""),
                })
            # --- Timeout observation ---
            case "timeout_observed":
                job = self.jobs.get(event.job_id)
                if job and job.status == "running":
                    job.status = "failed"
                    job.error_message = f"TimeoutObserved after {event.elapsed_min}min"

    def _on_queue(self, event: Effect) -> None:
        job_id = event.job_id
        if job_id not in self.jobs:
            job = JobState(
                job_id=job_id,
                job_type=event.job_type,
                slot_id=getattr(event, "slot_id", ""),
            )
            job.params = getattr(event, "params", {})
            job.created_at = getattr(event, "timestamp", 0.0)
            self.jobs[job_id] = job
            # Track attempt for TTS jobs (block-level retry counting)
            block_id = getattr(event, "slot_id", None)
            if block_id and event.job_type == "tts":
                self.block_attempts[block_id] += 1

    def _on_start(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "running"

    def _on_complete(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "completed"
            job.artifact_path = getattr(event, "artifact_path", None)
            job.duration_sec = getattr(event, "duration_sec", None)
            job.completed_at = getattr(event, "timestamp", None)
            # Mark block clean on successful completion
            block_id = job.slot_id
            if block_id in self.dirty_blocks:
                self.dirty_blocks.discard(block_id)
                self.clean_blocks.add(block_id)

    def _on_fail(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "failed"
            job.error_message = getattr(event, "error_message", "unknown")

    def _on_requeue(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "pending"
            job.requeue_count += 1
            job.error_message = None
            if getattr(event, "new_params", None):
                job.params.update(event.new_params)
            # Requeueing a block resets it to dirty
            if job.slot_id:
                self.dirty_blocks.add(job.slot_id)
                self.clean_blocks.discard(job.slot_id)

    def _on_partial(self, event: Effect) -> None:
        """Handle ReconciliationPartial: dirty/clean marking on script back-edge.

        Blocks in ``dirty_block_ids`` lose their authority and must be
        re-reconciled. Blocks in ``clean_block_ids`` retain measured
        durations and do not need re-TTS.
        """
        self.reconciliation_complete = False
        for block_id in getattr(event, "dirty_block_ids", []):
            self.dirty_blocks.add(block_id)
            self.clean_blocks.discard(block_id)
        for block_id in getattr(event, "clean_block_ids", []):
            self.clean_blocks.add(block_id)
            self.dirty_blocks.discard(block_id)

    # --- Query methods for agents ---

    def has_pending_or_running_jobs(self, job_type: Optional[str] = None) -> bool:
        """Return True if any job matches status and optional type filter."""
        for job in self.jobs.values():
            if job.status in ("pending", "running"):
                if job_type is None or job.job_type == job_type:
                    return True
        return False

    def pending_jobs(self, job_type: Optional[str] = None) -> list[JobState]:
        """Return all pending jobs, optionally filtered by type."""
        return [
            j for j in self.jobs.values()
            if j.status == "pending" and (job_type is None or j.job_type == job_type)
        ]

    def block_attempts_exceeded(self, block_id: str, max_attempts: int = 5) -> bool:
        """Check if a block has exceeded its per-block attempt limit."""
        return self.block_attempts.get(block_id, 0) >= max_attempts

    def budget_exceeded(self, max_budget_usd: float = 10.0) -> bool:
        """Check if cumulative spend exceeds the per-run budget."""
        return self.spent_usd >= max_budget_usd

    def is_block_clean(self, block_id: str) -> bool:
        """Return True if a block has measured audio and is authoritative."""
        return block_id in self.clean_blocks

    def all_blocks_clean(self, block_ids: list[str]) -> bool:
        """Return True if every block in the list is clean."""
        return all(self.is_block_clean(bid) for bid in block_ids)

    def summary(self) -> str:
        by_status: dict[str, int] = defaultdict(int)
        by_type: dict[str, int] = defaultdict(int)
        for job in self.jobs.values():
            by_status[job.status] += 1
            by_type[job.job_type] += 1
        return (
            f"Jobs: {len(self.jobs)} total, "
            f"pending={by_status['pending']}, running={by_status['running']}, "
            f"completed={by_status['completed']}, failed={by_status['failed']} | "
            f"tts={by_type['tts']}, video={by_type['video']} | "
            f"reconciled={'yes' if self.reconciliation_complete else 'no'} | "
            f"dirty={len(self.dirty_blocks)} clean={len(self.clean_blocks)} | "
            f"spent=${self.spent_usd:.4f}"
        )
```

#### 6.3.2 Reconciliation state: complete flag, dirty/clean block tracking

The `reconciliation_complete` flag is set by `ReconciliationComplete` and cleared by `ReconciliationFailed` or `ReconciliationPartial`. Agents read this flag to determine whether to begin video generation. Transition from `AUDIO_RECONCILE` to `VIDEO_PRODUCTION` is emergent: when `reconciliation_complete == True` and no dirty blocks remain, the Video Agent may begin work.

Dirty/clean tracking enables partial reconciliation after script back-edges. When `voice_mismatch` routes from `VIDEO_PRODUCTION` back to `SCRIPT`, the Scenario Agent fixes the script and emits `ReconciliationPartial`. Blocks whose text, speaker, or duration target changed are marked **dirty** (need re-TTS). Blocks that didn't change keep their `AudioMeasured` values as **clean**. This avoids discarding the entire audio pipeline for a single-scene typo fix.

| Field | Type | Meaning |
|---|---|---|
| `reconciliation_complete` | `bool` | `True` when all blocks have measured audio within tolerance |
| `dirty_blocks` | `set[str]` | Slot addresses needing re-reconciliation |
| `clean_blocks` | `set[str]` | Slot addresses with authoritative measured audio |

#### 6.3.3 Attempt counter per block, budget accumulator per run

Per-block attempt counting prevents any single narration block from consuming infinite retries. Each time a `QueueJob` event targets a TTS slot, `block_attempts[slot_id]` increments. When `block_attempts[slot_id] >= max_attempts` (default 5), the Audio Agent emits `ReconciliationFailed` with `failure_type="duration_unrecoverable"`, triggering a back-edge to `SCRIPT`.

Per-run budget tracking prevents aggregate runaway. `spent_usd` accumulates from `CostIncurred` events (emitted by the Provisioner when VM hours are consumed or API calls are made). When `spent_usd >= max_run_budget_usd` (default $10.00), the watcher emits `PipelineAborted` with `reason="budget_exceeded"`.

#### 6.3.4 Production failures list

`production_failures` collects all `ProductionFailed` events. Each entry is a dictionary with `slot_id`, `failure_type`, `expected`, `actual`, and `suggested_fix`. Agents use this list to detect unrecoverable errors: failures with `failure_type` in `{gap_unexpected, voice_mismatch}` trigger the script back-edge; all other types either requeue in the current phase or halt with `ClarificationRequest`.

---

### 6.4 VM Projection

#### 6.4.1 VM inventory: instance_id → {status, role, cost, worker_url}

`VMProjection` maintains a pure read model of the VM fleet. It applies `VMAllocated`, `VMDeallocated`, `VMObserved`, and `VMProvisionFailed` events. Each VM record tracks:

| Field | Type | Meaning |
|---|---|---|
| `status` | `str` | `active`, `destroyed`, `provisioning`, `failed` |
| `role` | `str` | `tts`, `video`, or `whisperx` — the job type this VM serves |
| `offer_id` | `str` | Vast.ai offer ID used for provisioning |
| `worker_url` | `str` | HTTP endpoint of the VM agent process |
| `hourly_rate_usd` | `float` | Cost per hour for this instance |
| `started_at` | `float` | Unix timestamp of allocation |
| `observed_status` | `str` | Last status from `VMObserved` (may differ from event-derived status) |

```python
from dataclasses import dataclass, field


@dataclass
class VMRecord:
    """Read-only record of a single VM's state."""

    instance_id: str
    status: str = "active"
    role: str = ""
    offer_id: str = ""
    worker_url: str = ""
    hourly_rate_usd: float = 0.0
    started_at: float = 0.0
    observed_status: Optional[str] = None


class VMProjection(Projection):
    """Pure read model of the VM fleet. No polling, no event emission.

    The Provisioner emits ``VMObserved`` effects when Vast.ai state diverges
    from event-derived state; this projection applies them passively.
    """

    def __init__(self) -> None:
        super().__init__()
        self.vms: dict[str, VMRecord] = {}

    def apply(self, event: Effect) -> None:
        match event.kind:
            case "vm_allocated":
                self.vms[event.instance_id] = VMRecord(
                    instance_id=event.instance_id,
                    status="active",
                    role=getattr(event, "role", ""),
                    offer_id=getattr(event, "offer_id", ""),
                    worker_url=getattr(event, "worker_url", ""),
                    hourly_rate_usd=getattr(event, "cost_per_hour", 0.0),
                    started_at=getattr(event, "timestamp", 0.0),
                )
            case "vm_deallocated":
                rec = self.vms.get(event.instance_id)
                if rec:
                    rec.status = "destroyed"
            case "vm_observed":
                rec = self.vms.get(event.instance_id)
                if rec:
                    rec.observed_status = getattr(event, "observed_status", None)
                    # If Vast.ai reports the instance gone but events say active,
                    # update status to reflect reality (Provisioner handles cleanup).
                    if rec.observed_status == "not_found" and rec.status == "active":
                        rec.status = "observed_gone"
            case "vm_provision_failed":
                # No VM record created — failure is logged by the Provisioner.
                pass

    def active_vms(self, role: Optional[str] = None) -> list[VMRecord]:
        """Return VMs with status == 'active', optionally filtered by role."""
        return [
            v for v in self.vms.values()
            if v.status == "active" and (role is None or v.role == role)
        ]

    def estimated_hourly_cost(self) -> float:
        """Sum of hourly rates for all active VMs."""
        return sum(v.hourly_rate_usd for v in self.active_vms())

    def summary(self) -> str:
        active = len(self.active_vms())
        total = len(self.vms)
        cost_hr = self.estimated_hourly_cost()
        roles: dict[str, int] = defaultdict(int)
        for v in self.active_vms():
            roles[v.role] += 1
        role_str = ", ".join(f"{k}={v}" for k, v in roles.items())
        return f"VMs: {active}/{total} active, ${cost_hr:.4f}/hr ({role_str})"
```

#### 6.4.2 Pure read model — no polling, no event emission

`VMProjection` has no `poll_vastai()` method. Vast.ai drift detection lives in the deterministic Provisioner service. The Provisioner runs `vastai show instances` at its own cadence, compares Vast.ai reality against `VMProjection` state (read via the shared projection registry), and emits `VMObserved` effects when divergence is detected. This preserves the projection invariant: projections are read models only; they consume events, they do not produce them.

The watcher loop advances `VMProjection` like all other projections — via `tick(event_store)` — which applies any `VMObserved` events emitted by the Provisioner since the last tick.


---

### 6.5 State Projection

#### 6.5.1 Current phase + transition history (descriptive only)

`StateProjection` tracks the emergent pipeline phase for human observation and the full history of phase changes. It does not enforce anything — agents decide what to do based on their own reading of projections.

```python
@dataclass
class PhaseChangeRecord:
    """A single phase change (descriptive, not a transition)."""

    from_phase: str
    to_phase: str
    reason: str
    at_sequence: int


class StateProjection(Projection):
    """Tracks emergent pipeline phase and phase change history.

    Also maintains a ring buffer of recent effects per agent for loop
    detection. The watcher loop checks this buffer on every tick.
    """

    def __init__(self, loop_buffer_size: int = 5) -> None:
        super().__init__()
        self.current_phase: str = "init"
        self.phase_history: list[PhaseChangeRecord] = []
        self.run_id: Optional[str] = None
        # Ring buffer: last N effects per agent (agent_name -> deque[Effect])
        self.recent_effects: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=loop_buffer_size)
        )
        self.loop_buffer_size: int = loop_buffer_size

    def apply(self, event: Effect) -> None:
        # Record effect in agent's ring buffer for loop detection
        agent = getattr(event, "agent", None) or getattr(event, "source_agent", "unknown")
        if agent:
            self.recent_effects[agent].append(event)

        match event.kind:
            case "pipeline_started":
                self.run_id = getattr(event, "run_id", None)
                self.current_phase = "init"
                self.phase_history.clear()
                self.recent_effects.clear()
            case "reconciliation_complete":
                if self.current_phase != "audio_reconcile":
                    self._record_phase_change("audio_reconcile")
            case "pipeline_complete":
                self._record_phase_change("done")
            case "pipeline_aborted":
                self._record_phase_change("aborted")
            case "merge_into_otio":
                # Crude phase inference for observation
                if event.track_name == "V1_Video" and self.current_phase == "audio_reconcile":
                    self._record_phase_change("video_production")

    def _record_phase_change(self, to_phase: str, reason: str = "") -> None:
        rec = PhaseChangeRecord(
            from_phase=self.current_phase,
            to_phase=to_phase,
            reason=reason,
            at_sequence=getattr(self, "last_sequence", 0),
        )
        self.phase_history.append(rec)
        self.current_phase = to_phase

    def get_recent_events(self, n: int) -> list[Effect]:
        """Return the last N effects across all agents."""
        all_events = []
        for agent_deque in self.recent_effects.values():
            all_events.extend(list(agent_deque))
        all_events.sort(key=lambda e: getattr(e, "timestamp", 0), reverse=True)
        return all_events[:n]

    def summary(self) -> str:
        tx_count = len(self.phase_history)
        return (
            f"Phase: {self.current_phase}, "
            f"{tx_count} phase changes, "
            f"{len(self.recent_effects)} agents tracked"
        )
```

#### 6.5.2 Loop detection buffer (last N effects per agent)

The `recent_effects` dictionary maps agent name to a `deque` of that agent's last `loop_buffer_size` effects (default 5). On every `apply`, the effect is appended to the deque for its agent. Because `deque` has `maxlen`, old effects are automatically evicted — the buffer is a fixed-size ring buffer with O(1) append and no allocation on overflow.

The watcher loop uses this buffer to detect two loop conditions:

1. **Duplicate effects**: all N entries in a deque are the same `kind` with the same key parameters.
2. **No progress**: after N effects from an agent, no projection state has changed (OTIO, jobs, or VM state delta is empty).

```python
    def detect_duplicate_loop(self, agent: str, threshold: int = 5) -> tuple[bool, str]:
        """Check if an agent has emitted the same effect kind N times in a row.

        Returns (is_looping, reason).
        """
        buf = self.recent_effects.get(agent, deque())
        if len(buf) < threshold:
            return False, "insufficient history"
        kinds = [getattr(e, "kind", "") for e in buf]
        if len(set(kinds)) == 1:
            return True, f"{agent} emitted {kinds[0]} {len(buf)} times"
        return False, "effects vary"

    def get_recent_kinds(self, agent: str) -> list[str]:
        """Return the list of recent effect kinds for an agent."""
        return [getattr(e, "kind", "") for e in self.recent_effects.get(agent, [])]
```

When either condition triggers, the watcher loop emits `AgentLoopDetected` with context (agent name, effect history, projection delta) and halts, emitting `ClarificationRequest` for human review. The threshold is configurable per agent (default 5) via the agent's config table.

---

## 7. Situation Types (Agent Guidance)

These are the situation types an agent should look for when reading projections. They are not a Python class — they are guidance text embedded in the agent's system prompt. The agent scans `ctx.deps.projections["otio"]` directly and decides which situations apply.

### 7.1 Situation Types

| Type | Trigger | Description |
|---|---|---|
| `fresh_dirty_block` | Block exists, dirty, attempts < max | New/requeued block needs work |
| `measurement_complete_pass` | Block measured, within tolerance | Block passed reconciliation |
| `measurement_complete_fail` | Block measured, outside tolerance | Block failed, needs retry |
| `block_at_max_attempts` | Block dirty, attempts == max | Exhausted, needs escalation |
| `vm_stale` | VM last_seen > threshold | VM not reporting, may be dead |
| `vm_provision_failed` | `VMProvisionFailed` exists | Could not create VM |
| `job_queued_long` | Job queued > threshold | Job waiting too long for VM |
| `reconciliation_complete_all` | All blocks pass | Audio pipeline done |
| `reconciliation_partial_some` | Some pass, some dirty | Partial progress, continue loop |
| `assembly_ready` | All video approved | Ready for final assembly |
| `pipeline_budget_warning` | Spent > 80% of limit | Warning level |
| `pipeline_budget_critical` | Spent > 95% of limit | Critical, may abort |
| `agent_loop_detected` | Duplicate effects or no progress | Agent stuck |
| `human_instruction_pending` | `HumanInstruction` unread | Human input waiting |
| `noop_all_clean` | Nothing dirty, nothing queued | Idle, waiting |

### 7.2 Narrative Template Format

```
=== SITE: {slot_id} ===
{text_snippet}
TARGET: {target_sec}s | MEASURED: {measured_sec}s | DELTA: {delta_sec}s
ATTEMPTS: {attempts}/{max_attempts} | VERDICT: {verdict}

WHAT'S HAPPENING:
{situation_narrative}
===
```

### 7.3 Rules for Narrative Generation

1. **Dirty blocks get full narrative** — all fields, history, guidance
2. **Clean blocks get one line** — slot_id, verdict, measured duration
3. **Failed blocks get extra context** — previous attempts, error history
4. **Max-attempt blocks get escalation options** — accept, human, abort
5. **VM issues get infrastructure narrative** — not artistic
6. **Budget issues get fiscal narrative** — spent, limit, remaining

---

## 8. Agent Architecture — pydantic-deepagents

All agents share a common infrastructure built on `pydantic-deepagents`. Each agent is a **main agent** (not a subagent) created via `create_deep_agent()` with capabilities for context compaction. They differ only in role, permitted effects, and the focus function used for compaction.

### 8.1 pydantic-deepagents Layer Stack

pydantic-deepagents provides a layered context management system. We use all layers, configured for pipeline agents:

```
Message history (situation narratives + memory + prior turns)
│
▼
┌─────────────────────────────┐
│ EvictionProcessor           │ Saves large tool outputs (>20K tokens) to files
│ (default: on)               │
├─────────────────────────────┤
│ PatchToolCallsProcessor     │ Fixes orphaned effect pairs (e.g. QueueJob without
│ (default: on)               │ JobCompleted due to interrupt)
├─────────────────────────────┤
│ SlidingWindowProcessor      │ Hard fallback: trims oldest messages if compaction
│ (trigger: fraction 0.95)    │ capability fails
├─────────────────────────────┤
│ OTIOAwareCompactionCap      │ OUR CAPABILITY: queries OTIO, determines focus,
│ (before_model_request hook) │ LLM-compacts history preserving task context
├─────────────────────────────┤
│ ContextManagerCapability    │ Token tracking + auto-trigger at 90% threshold
│ (default: on)               │
└─────────────────────────────┘
│
▼
Model request
```

| Layer | Source | Config | Purpose |
|---|---|---|---|
| `EvictionProcessor` | pydantic-deepagents | `eviction_token_limit=20_000` | Bash outputs, ffprobe results, WhisperX JSON — saved to `/tmp/` |
| `PatchToolCallsProcessor` | pydantic-deepagents | `patch_tool_calls=True` | Prevents orphaned `QueueJob` when agent is interrupted mid-turn |
| `SlidingWindowProcessor` | pydantic-deepagents | `trigger=("fraction", 0.95), keep=("fraction", 0.5)` | Last-resort hard trim; never splits causal pairs |
| `OTIOAwareCompactionCap` | **Our code** | `compaction_model="deepseek-v4-flash"` | Queries OTIO projection → determines focus → LLM compacts |
| `ContextManagerCapability` | pydantic-deepagents | `context_manager_max_tokens=128_000` | Tracks tokens, calls `on_context_update`, triggers at 90% |

### 8.2 Agent Construction

```python
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_deep import create_deep_agent
from pydantic_deep.processors import create_sliding_window_processor

class OTIOAwareCompactionCap(AbstractCapability):
    """Capability that queries OTIO projection before each model call.

    Hooks before_model_request to inspect message history, query OTIO state
    via ctx.deps.projections, determine focus, and compact if over budget.
    """

    async def before_model_request(self, ctx, request_context):
        # 1. Query OTIO projection via dependency injection
        otio = ctx.deps.projections["otio"]
        jobs = ctx.deps.projections["jobs"]

        # 2. Determine focus from current task
        focus = _determine_focus(ctx.deps.agent_role, otio, jobs)

        # 3. Check if compaction needed (ContextManager already tracks tokens)
        if request_context.estimated_tokens > ctx.deps.max_tokens * 0.85:
            request_context.messages = await self._compact(
                request_context.messages,
                focus,
                ctx.deps.compaction_model
            )
            # Inject focus marker so agent knows what was preserved
            request_context.messages.insert(0, SystemMessage(
                content=f"[Context compacted. Focus: {focus}]"
            ))

        return request_context

    async def _compact(self, messages, focus, model):
        """Call compaction LLM with OTIO-derived focus."""
        flat = _render_messages(messages)
        system = (
            f"Compress this agent context. Preserve everything related to: {focus}. "
            f"Keep all IDs, numbers, durations, verdicts, failure reasons. "
            f"Remove redundant pleasantries, old success details, clean blocks. "
            f"Output ONLY compressed context."
        )
        compressed = await llm_complete(system=system, user=flat, model=model)
        return [SystemMessage(content="[Compacted]"), UserMessage(content=compressed)]

def create_pipeline_agent(role: str, config: Config, projections: dict):
    """Factory: create pydantic-deepagents agent with pipeline capabilities."""
    return create_deep_agent(
        model=config.agent_models[role],
        instructions=ROLE_INSTRUCTIONS[role],
        capabilities=[
            OTIOAwareCompactionCap(),
            # ContextManagerCapability is auto-added by create_deep_agent
        ],
        history_processors=[
            create_sliding_window_processor(
                trigger=("fraction", 0.95),
                keep=("fraction", 0.5),
                max_input_tokens=config.max_tokens,
            ),
        ],
        eviction_token_limit=20_000,
        patch_tool_calls=True,
        context_manager=True,
        context_manager_max_tokens=config.max_tokens,
        deps_type=PipelineDeps,  # carries projections, agent_role, max_tokens
    )
```

### 8.3 Prompt Construction

The watcher constructs the **initial user prompt** containing the situation narrative. The agent's `instructions` (system prompt) contains the role description and effect format. Memory is injected via `message_history` from prior `AgentMemoryUpdated` effects.

```python
async def run_agent_turn(agent, agent_situations, memory, projections):
    """Build prompt and run agent via pydantic-deepagents."""
    # Build situation narrative
    narrative = "\n\n".join(
        SITUATION_TEMPLATES[s.type].format(**s.facts)
        for s in agent_situations
    )

    # Memory from AgentMemoryUpdated effects
    history = [
        UserMessage(content=f"[MEMORY] {m}")
        for m in memory[-5:]
    ]

    # Run agent with deps carrying projections for compaction
    result = await agent.run(
        user_prompt=narrative,
        message_history=history,
        deps=PipelineDeps(
            projections=projections,
            agent_role=agent.role,
            max_tokens=config.max_tokens,
            compaction_model=config.compaction_model,
        ),
    )

    # Parse effects from result.output
    return parse_effects(result.output)
```

### 8.4 Context Compaction (Agent-Internal, via Capability)

**No watcher-side compaction.** The watcher passes the full narrative. The agent's `OTIOAwareCompactionCap` handles compaction internally:

1. **ContextManagerCapability** counts tokens before each model call
2. If > 90% of budget, it calls `before_model_request` on all capabilities
3. `OTIOAwareCompactionCap` queries OTIO via `ctx.deps.projections`
4. Determines focus (e.g., "block A1:3:2 reconciliation, attempt 2/3")
5. Calls compaction LLM with focus-prompt
6. Replaces message history with compressed version + focus marker
7. If compaction fails, `SlidingWindowProcessor` hard-trims oldest messages (never splitting causal pairs)

**Causal pair preservation:** The `SlidingWindowProcessor` uses a "safe cutoff" algorithm that walks backward from the cutoff point to find the nearest point between complete effect pairs. A pair is:
- `QueueJob` → `JobCompleted`/`JobFailed`
- `MeasurementRequested` → `AudioMeasured`
- `ScriptGenerated` → any effect referencing that script

This is identical to pydantic-ai-skills' safe cutoff but applied to pipeline effect pairs.

### 8.5 How the Capability Queries OTIO

```python
def _determine_focus(role: str, otio: OTIOProjection, jobs: JobProjection) -> str:
    """Read OTIO state to determine what the agent is working on."""
    if role == "audio":
        dirty = otio.dirty_blocks()
        if dirty:
            b = dirty[0]
            return (
                f"audio reconciliation of block {b.slot_id}, "
                f"attempt {b.attempts}/{b.max_attempts}, "
                f"measured {b.measured_sec}s vs target {b.target_sec}s"
            )
        return "audio pipeline — all blocks clean, awaiting instructions"

    if role == "video":
        pending = [j for j in jobs.active() if j.job_type == "ltx"]
        if pending:
            return f"video generation for {len(pending)} pending LTX jobs"
        return "video pipeline — awaiting approved audio"

    if role == "scenario":
        gaps = otio.gap_slots()
        if gaps:
            return f"script writing: {len(gaps)} unfilled slots"
        return "script refinement — all slots filled"

    if role == "assembly":
        return "final assembly — merging approved clips"

    return f"{role} agent — no active task"
```

---

## 9. Agents — Per-Agent Implementations

All agents are main agents (not subagents) constructed via `create_pipeline_agent()` (§8.2). They share the same compaction capability and sliding-window fallback. Each differs in:
- `ROLE_INSTRUCTIONS[role]` — system prompt with persona + RULES block
- `_determine_focus()` — focus extraction for compaction
- `_permitted_effects` — which effect kinds the parser will extract

### 9.1 Scenario Agent

```python
SCENARIO_INSTRUCTIONS = """
=== YOUR ROLE ===
You are the Scenario agent. You write and revise narration scripts.
Every block must specify speaker, duration_sec, and scene_num.

=== RULES ===
1. If agent_loop_detected -> emit ClarificationRequest and stop.
2. If pipeline_budget_critical -> emit PipelineAborted and stop.
3. If production_failed with failure_type in {gap_unexpected, voice_mismatch} ->
   rewrite affected scenes.
4. If gaps exist in OTIO -> emit UpdateScript to fill them.
5. If noop_all_clean -> emit NoOp, nothing to do.

Pick the highest-priority rule that applies. Only one action per turn.

=== PERMITTED EFFECTS ===
update_script, delete_scene, reorder_scenes, noop, clarification_request
"""
```

**Port:** 8001
**Effects:** `UpdateScript`, `DeleteScene`, `ReorderScenes`
**Focus:** Unfilled slots, script gaps, voice mismatches

### 9.2 Audio Agent

```python
AUDIO_INSTRUCTIONS = """
=== YOUR ROLE ===
You are the Audio agent. You own the narration reconciliation loop:
(1) Queue TTS jobs for dirty blocks. (2) On JobCompleted, run WhisperX ->
measure duration. (3) Compare measured vs scripted (±15% or ±0.25s):
within tolerance -> DurationAdjusted; outside -> ReconciliationFailed -> requeue.
(4) When all blocks clean, emit ReconciliationComplete.

=== RULES ===
1. If agent_loop_detected -> emit ClarificationRequest and stop.
2. If pipeline_budget_critical -> emit PipelineAborted and stop.
3. If block_at_max_attempts -> emit ReconciliationFailed(duration_unrecoverable).
4. If measurement_complete_fail -> requeue with adjusted params.
5. If fresh_dirty_block -> do the work (queue job, measure, judge).
6. If noop_all_clean -> emit NoOp, nothing to do.

Pick the highest-priority rule that applies. Only one action per turn.

=== PERMITTED EFFECTS ===
queue_job, job_approved, job_requeued, duration_adjusted,
reconciliation_failed, reconciliation_partial, reconciliation_complete,
noop, clarification_request
"""
```

**Port:** 8002
**Effects:** `QueueJob`, `JobApproved`, `JobRequeued`, `DurationAdjusted`, `ReconciliationFailed`, `ReconciliationPartial`, `ReconciliationComplete`
**Focus:** Dirty block reconciliation, attempt counts, tolerance checks
**Tolerance:** `max(scripted_sec × 0.15, 0.25)`
**Bounds:** Max 5 attempts per block, $2.00 TTS budget

### 9.3 Video Agent

```python
VIDEO_INSTRUCTIONS = """
=== YOUR ROLE ===
You are the Video agent. Generate LTX-2.3 clips using measured audio durations
as LAW. Queue ltx jobs, judge for visual coherence and artistic quality.
Emit JobApproved or JobRequeued. Merge approved clips via MergeIntoOTIO.
Continue until all video slots filled.

=== RULES ===
1. If agent_loop_detected -> emit ClarificationRequest and stop.
2. If pipeline_budget_critical -> emit PipelineAborted and stop.
3. If fresh_dirty_block (video slot unfilled) -> queue LTX job.
4. If job completed -> judge quality, approve or requeue.
5. If noop_all_clean -> emit NoOp, nothing to do.

Pick the highest-priority rule that applies. Only one action per turn.

=== PERMITTED EFFECTS ===
queue_job, job_approved, job_requeued, merge_into_otio,
noop, clarification_request
"""
```

**Port:** 8003
**Effects:** `QueueJob`, `JobApproved`, `JobRequeued`, `MergeIntoOTIO`
**Focus:** Pending LTX jobs, video slot fill rate

### 9.4 Assembly Agent

```python
ASSEMBLY_INSTRUCTIONS = """
=== YOUR ROLE ===
You are the Assembly agent. Run ffmpeg to compose all approved audio and video
clips from OTIO into final_documentary.mp4. Validate OTIO before assembly and
verify output after. If all checks pass, emit PipelineComplete.
If any check fails, emit ProductionFailed with failure_type.

=== RULES ===
1. If agent_loop_detected -> emit ClarificationRequest and stop.
2. If pipeline_budget_critical -> emit PipelineAborted and stop.
3. If validation fails -> emit ProductionFailed with appropriate failure_type.
4. If all checks pass -> emit PipelineComplete.
5. If noop_all_clean -> emit NoOp, nothing to do.

Pick the highest-priority rule that applies. Only one action per turn.

=== PERMITTED EFFECTS ===
pipeline_complete, production_failed, noop, clarification_request
"""
```

**Port:** 8005
**Effects:** `PipelineComplete`, `ProductionFailed`
**Focus:** OTIO validation, ffmpeg composition, output verification


---

## 10. Provisioner Service

The Provisioner is the most significant architectural change in V6. In V5 it was an LLM agent with its own HTTP server (port 8004); it received instructions via `POST /`, reasoned about VM state with an LLM, and produced effects through the parser. In V6 it is a deterministic Python service with no LLM, no HTTP surface, and no autonomy. It runs inside the watcher loop as a plain Python class, reads `JobProjection` to find pending work, matches Vast.ai offers by deterministic criteria, and emits effects directly. This change eliminates an entire LLM call per provisioning cycle, removes a failure mode (parser mis-extraction of VM effects), and makes VM operations predictable and testable.

### 10.1 Architecture

#### 10.1.1 Deterministic Python service (not LLM agent) running in watcher loop

`ProvisionerService` is a plain Python class instantiated once at pipeline startup. The watcher loop calls `provisioner.tick(projections)` on every cycle, after advancing all projections and before running agents. The Provisioner never initiates communication; it reacts to projection state.

```python
import asyncio
import json
import time
from typing import Optional
from urllib.parse import urljoin

import httpx

class ProvisionerService:
    """Deterministic VM provisioning service. No LLM. No HTTP endpoint.

    Runs inside the watcher loop. Reads JobProjection to find pending jobs,
    matches Vast.ai offers deterministically, manages VM lifecycle, delivers
    jobs to VM workers, and emits effects via EventStore.
    """

    # Retry configuration
    MAX_CONSECUTIVE_PROVISION_FAILURES: int = 3
    STALE_VM_THRESHOLD_MIN: float = 15.0
    POLL_VASTAI_INTERVAL_SEC: float = 60.0

    def __init__(
        self,
        event_store: "EventStore",
        config: "PipelineConfig",
    ) -> None:
        self.event_store = event_store
        self.config = config
        self._last_poll_time: float = 0.0
        self._consecutive_failures: dict[str, int] = {}  # job_id -> count
        self._job_to_vm: dict[str, str] = {}        # job_id -> instance_id
        self._result_queue: asyncio.Queue = asyncio.Queue()

    async def tick(self, projections: dict[str, "Projection"]) -> None:
        """Called by watcher every cycle. Main entry point."""
        job_proj: "JobProjection" = projections["jobs"]
        vm_proj: "VMProjection" = projections["vms"]

        # 1. Poll Vast.ai for drift detection
        if time.time() - self._last_poll_time >= self.POLL_VASTAI_INTERVAL_SEC:
            await self._poll_vastai(vm_proj)
            self._last_poll_time = time.time()

        # 2. Process pending jobs
        await self._process_pending_jobs(job_proj, vm_proj)

        # 3. Collect job results from running VMs
        await self._collect_job_results(job_proj, vm_proj)

        # 4. Deallocate idle VMs
        await self._cleanup_idle_vms(vm_proj, job_proj)
```

#### 10.1.2 No POST / endpoint; reads JobProjection, acts on pending jobs

The Provisioner has no HTTP server, no `GET /`, no `POST /`. It reads `job_proj.jobs` directly — a dictionary of `JobState` objects keyed by `job_id`. A job with `status == "pending"` and no assigned VM is a provisioning candidate. The Provisioner selects a matching Vast.ai offer, allocates the VM, and records the assignment in an internal `dict[job_id, str]` mapping `job_id` → `instance_id`. This mapping is ephemeral; if the process restarts, it is rebuilt from `VMObserved` effects and re-queued jobs on the next tick.

#### 10.1.3 No direct agent-to-agent communication; all via event store

V5's Provisioner agent directly `POST`ed job-completion notifications to the Audio and Video agents. V6 eliminates this: the Provisioner appends `JobCompleted` or `JobFailed` effects to the event store. The Audio Agent and Video Agent read these via their own `JobProjection` subscriptions on subsequent ticks. There is no direct HTTP call between any two agents. The event store is the only communication channel.

---

### 10.2 VM Lifecycle Management

#### 10.2.1 Offer matching: GPU type, VRAM, price thresholds (deterministic criteria)

Offer selection is a pure function of job requirements and available offers. No LLM reasoning, no fuzzy matching.

| Criterion | TTS Job (`job_type="tts"`) | LTX Job (`job_type="ltx"`) |
|---|---|---|
| GPU type | RTX 4090 or A6000 | RTX 4090 or A6000 |
| Min VRAM | 24 GB | 24 GB |
| Max price/hr | `$config.max_tts_cost_hr` (default $0.80) | `$config.max_ltx_cost_hr` (default $1.20) |
| Disk | ≥ 30 GB | ≥ 50 GB |
| Sort key | cheapest hourly rate | cheapest hourly rate |

```python
    def _match_offer(self, job_type: str) -> Optional[dict]:
        """Deterministic offer selection. Returns best-matching offer dict or None.

        Executes `vastai search offers` with job-type-specific filters,
        parses JSON output, and selects cheapest offer meeting all criteria.
        """
        if job_type == "tts":
            min_vram = 24
            max_price = getattr(self.config, "max_tts_cost_hr", 0.80)
            min_disk = 30
        else:  # ltx
            min_vram = 24
            max_price = getattr(self.config, "max_ltx_cost_hr", 1.20)
            min_disk = 50

        # Build vastai CLI query — deterministic filter string
        query = (
            f"vastai search offers 'gpu_ram >= {min_vram} "
            f"and dph <= {max_price} and disk_space >= {min_disk}' "
            f"--raw --storage {min_disk}"
        )

        result = self._run_vastai_cli(query)
        if not result:
            return None

        offers = json.loads(result)
        # Sort by cost per hour ascending; cheapest first
        offers.sort(key=lambda o: o.get("dph_total", float("inf")))

        for offer in offers:
            gpu_name = offer.get("gpu_name", "").upper()
            if any(g in gpu_name for g in ("RTX 4090", "A6000")):
                return offer
        return None

    def _run_vastai_cli(self, cmd: str) -> Optional[str]:
        """Execute vastai CLI command. Return stdout or None on failure."""
        import subprocess
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                return None
            return result.stdout
        except Exception:
            return None
```

#### 10.2.2 VM allocation via Vast.ai CLI

Once an offer is selected, the Provisioner creates the instance, waits for the worker HTTP endpoint to respond, then emits `VMAllocated`.

```python
    async def _allocate_vm(
        self,
        offer: dict,
        job_type: str,
        job_id: str,
    ) -> Optional[str]:
        """Create Vast.ai instance from offer. Return instance_id or None.

        Emits VMAllocated on success, VMProvisionFailed on failure.
        """
        offer_id = str(offer.get("id", ""))
        if not offer_id:
            return None

        create_cmd = (
            f"vastai create instance {offer_id} "
            f"--image vastai/worker:{job_type} "
            f"--disk {offer.get('disk_space', 30)} "
            f"--env WORKER_PORT=9000"
        )

        result = self._run_vastai_cli(create_cmd)
        if not result:
            await self._emit_provision_failed(
                offer_id=offer_id,
                job_id=job_id,
                category="unknown",
                error="vastai create instance returned no output",
            )
            return None

        try:
            instance_data = json.loads(result)
            instance_id = str(instance_data.get("id", ""))
        except (json.JSONDecodeError, KeyError):
            await self._emit_provision_failed(
                offer_id=offer_id,
                job_id=job_id,
                category="unknown",
                error=f"cannot parse instance creation response: {result[:200]}",
            )
            return None

        # Wait for worker HTTP endpoint to become healthy
        worker_url = await self._await_vm_health(instance_id)
        if not worker_url:
            await self._emit_provision_failed(
                offer_id=offer_id,
                job_id=job_id,
                category="boot_timeout",
                error=f"instance {instance_id} did not become healthy",
            )
            return None

        # Success — emit VMAllocated
        effect = VMAllocated(
            instance_id=instance_id,
            role=job_type,
            offer_id=offer_id,
            worker_url=worker_url,
            gpu_type=offer.get("gpu_name", "unknown"),
            cost_per_hour=float(offer.get("dph_total", 0.0)),
            run_id=self.config.run_id,
        )
        await self.event_store.append(effect)
        return instance_id

    async def _await_vm_health(
        self,
        instance_id: str,
        max_wait_sec: float = 300.0,
        poll_interval_sec: float = 5.0,
    ) -> Optional[str]:
        """Poll Vast.ai for instance IP, then probe GET / on worker.

        Returns worker URL (e.g. http://1.2.3.4:9000) or None.
        """
        start = time.time()
        while time.time() - start < max_wait_sec:
            info = self._run_vastai_cli(
                f"vastai show instance {instance_id} --raw"
            )
            if not info:
                await asyncio.sleep(poll_interval_sec)
                continue

            try:
                data = json.loads(info)
                public_ip = data.get("public_ipaddr") or data.get("public_ip")
                if public_ip:
                    worker_url = f"http://{public_ip}:9000"
                    # Probe health endpoint
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.get(worker_url)
                        if resp.status_code == 200:
                            return worker_url
            except (json.JSONDecodeError, httpx.RequestError):
                pass

            await asyncio.sleep(poll_interval_sec)
        return None
```

#### 10.2.3 Heartbeat monitoring via Vast.ai polling (not VM-side)

V6 moved Vast.ai polling from the VM-side agent into the Provisioner. The Provisioner runs `vastai show instances` every `POLL_VASTAI_INTERVAL_SEC` (60 s). It compares the Vast.ai API response against `VMProjection.active_vms()`. When divergence is detected, it emits `VMObserved` with the appropriate `corrective_action`.

```python
    async def _poll_vastai(self, vm_proj: "VMProjection") -> None:
        """Compare Vast.ai reality with event-derived state. Emit VMObserved on drift."""
        result = self._run_vastai_cli("vastai show instances --raw")
        if not result:
            return

        try:
            instances = json.loads(result)
            if not isinstance(instances, list):
                instances = [instances]
        except json.JSONDecodeError:
            return

        vast_ids = {str(i.get("id", "")) for i in instances if i.get("id")}

        for vm in vm_proj.active_vms():
            if vm.instance_id not in vast_ids:
                # VM is gone from Vast.ai but still active in projection
                effect = VMObserved(
                    instance_id=vm.instance_id,
                    observed_status="not_found",
                    expected_status="running",
                    drift_description=(
                        f"VM {vm.instance_id} ({vm.role}) not found in "
                        f"Vast.ai but active in projection"
                    ),
                    corrective_action="deallocate",
                    run_id=self.config.run_id,
                )
                await self.event_store.append(effect)
```

#### 10.2.4 TimeoutObserved emission for stale VMs

The Provisioner checks the `created_at` timestamp of every job in `running` status. If `elapsed_min > STALE_VM_THRESHOLD_MIN`, it emits `TimeoutObserved` with `action_taken="deallocate_vm"`. The watcher loop (§12) also performs a similar check; the Provisioner's check is a secondary safety net specifically for VMs that have gone unresponsive.

```python
    async def _check_stale_vms(
        self,
        job_proj: "JobProjection",
        vm_proj: "VMProjection",
    ) -> None:
        """Emit TimeoutObserved for jobs running on VMs longer than threshold."""
        now = time.time()
        for job in job_proj.jobs.values():
            if job.status != "running" or job.created_at == 0.0:
                continue
            elapsed_min = (now - job.created_at) / 60.0
            if elapsed_min > self.STALE_VM_THRESHOLD_MIN:
                # Find the VM running this job
                instance_id = self._job_to_vm.get(job.job_id, "")
                effect = TimeoutObserved(
                    job_id=job.job_id,
                    vm_instance_id=instance_id,
                    pending_since=job.created_at,
                    elapsed_min=elapsed_min,
                    stale_threshold_min=self.STALE_VM_THRESHOLD_MIN,
                    action_taken="deallocate_vm",
                    run_id=self.config.run_id,
                )
                await self.event_store.append(effect)
```

#### 10.2.5 VM deallocation on job completion or failure

After `_collect_job_results()` processes completions and failures, any VM with no remaining assigned jobs is deallocated.

```python
    async def _cleanup_idle_vms(
        self,
        vm_proj: "VMProjection",
        job_proj: "JobProjection",
    ) -> None:
        """Destroy VMs that have no pending or running jobs assigned."""
        active_job_vms = {
            self._job_to_vm.get(j.job_id)
            for j in job_proj.jobs.values()
            if j.status in ("pending", "running")
        }

        for vm in vm_proj.active_vms():
            if vm.instance_id not in active_job_vms:
                await self._deallocate_vm(vm.instance_id, reason="job_done")

    async def _deallocate_vm(
        self,
        instance_id: str,
        reason: str,
    ) -> None:
        """Destroy Vast.ai instance and emit VMDeallocated."""
        self._run_vastai_cli(f"vastai destroy instance {instance_id}")

        # Calculate approximate runtime and cost
        effect = VMDeallocated(
            instance_id=instance_id,
            reason=reason,
            final_cost=0.0,
            runtime_sec=0.0,
            run_id=self.config.run_id,
        )
        await self.event_store.append(effect)
```

---

### 10.3 Job Delivery

#### 10.3.1 POST job to VM worker

Once a VM is allocated and healthy, the Provisioner POSTs the job payload to the worker's `POST /` endpoint. The payload is a JSON dict containing `job_id`, `job_type`, `params`, and `slot_id`. The worker runs inference and will later POST the result back.

```python
    async def _process_pending_jobs(
        self,
        job_proj: "JobProjection",
        vm_proj: "VMProjection",
    ) -> None:
        """Find pending jobs, match offers, allocate VMs, deliver jobs."""
        for job in job_proj.jobs.values():
            if job.status != "pending":
                continue
            if job.job_id in self._job_to_vm:
                continue  # already has a VM assigned

            # Check consecutive failure count
            if self._consecutive_failures.get(job.job_id, 0) >= \
                    self.MAX_CONSECUTIVE_PROVISION_FAILURES:
                continue  # handled in _handle_repeated_failure

            # Match offer
            offer = self._match_offer(job.job_type)
            if not offer:
                await self._emit_provision_failed(
                    offer_id="",
                    job_id=job.job_id,
                    category="no_offers",
                    error=f"no offers match {job.job_type} requirements",
                )
                self._consecutive_failures[job.job_id] = \
                    self._consecutive_failures.get(job.job_id, 0) + 1
                continue

            # Allocate VM
            instance_id = await self._allocate_vm(offer, job.job_type, job.job_id)
            if not instance_id:
                self._consecutive_failures[job.job_id] = \
                    self._consecutive_failures.get(job.job_id, 0) + 1
                continue

            # Reset failures on success
            self._consecutive_failures[job.job_id] = 0
            self._job_to_vm[job.job_id] = instance_id

            # Deliver job to VM
            vm = vm_proj.vms.get(instance_id)
            if vm and vm.worker_url:
                await self._post_job_to_vm(vm.worker_url, job)

    async def _post_job_to_vm(
        self,
        worker_url: str,
        job: "JobState",
    ) -> None:
        """POST job payload to VM worker."""
        payload = {
            "job_id": job.job_id,
            "job_type": job.job_type,
            "slot_id": job.slot_id,
            "params": job.params,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    urljoin(worker_url, "/"),
                    json=payload,
                )
                if resp.status_code != 202:
                    # Worker rejected — will retry next tick
                    pass
        except httpx.RequestError:
            # Network failure — VM may still be booting, retry next tick
            pass
```

#### 10.3.2 Receive completion/failure, emit JobCompleted/JobFailed

The VM worker POSTs results back to the Provisioner (direct HTTP call to the coordinator's exposed result endpoint). The Provisioner's result handler emits the appropriate effect.

```python
    async def _collect_job_results(
        self,
        job_proj: "JobProjection",
        vm_proj: "VMProjection",
    ) -> None:
        """Poll VM workers for completed jobs and emit JobCompleted/JobFailed.

        In practice, VM workers POST results to a coordinator endpoint.
        This method processes a result queue populated by those callbacks.
        """
        while not self._result_queue.empty():
            result = await self._result_queue.get()
            await self._handle_job_result(result, job_proj)

    async def _handle_job_result(
        self,
        result: dict,
        job_proj: "JobProjection",
    ) -> None:
        """Process a single job result and emit the corresponding effect."""
        job_id = result.get("job_id", "")
        success = result.get("success", False)
        instance_id = result.get("instance_id", "")

        if success:
            effect = JobCompleted(
                job_id=job_id,
                artifact_path=result.get("artifact_path", ""),
                duration_sec=result.get("duration_sec", 0.0),
                vm_instance_id=instance_id,
                run_id=self.config.run_id,
            )
        else:
            effect = JobFailed(
                job_id=job_id,
                error_message=result.get("error_message", "unknown"),
                failure_category=result.get("failure_category", "unknown"),
                vm_instance_id=instance_id,
                retryable=result.get("retryable", True),
                retry_count=job_proj.jobs.get(job_id, {}).requeue_count
                if hasattr(job_proj.jobs.get(job_id), "requeue_count")
                else 0,
                run_id=self.config.run_id,
            )

        await self.event_store.append(effect)
```

---

### 10.4 Failure Handling

#### 10.4.1 VMProvisionFailed → retry with different offer → ClarificationRequest on repeated failure

Each provisioning failure increments a per-`job_id` counter. On reaching `MAX_CONSECUTIVE_PROVISION_FAILURES` (default 3), the Provisioner emits `ClarificationRequest` and stops retrying that job. Human intervention is required.

```python
    async def _emit_provision_failed(
        self,
        *,
        offer_id: str,
        job_id: str,
        category: str,
        error: str,
    ) -> None:
        """Emit VMProvisionFailed and check for repeated-failure escalation."""
        self._consecutive_failures[job_id] = \
            self._consecutive_failures.get(job_id, 0) + 1

        effect = VMProvisionFailed(
            offer_id=offer_id,
            job_id=job_id,
            error_message=error,
            failure_category=category,
            retryable=True,
            consecutive_failures=self._consecutive_failures[job_id],
            run_id=self.config.run_id,
        )
        await self.event_store.append(effect)

        if self._consecutive_failures[job_id] >= \
                self.MAX_CONSECUTIVE_PROVISION_FAILURES:
            await self._escalate_to_human(job_id, category, error)

    async def _escalate_to_human(
        self,
        job_id: str,
        category: str,
        error: str,
    ) -> None:
        """Emit ClarificationRequest after repeated provisioning failures."""
        effect = ClarificationRequest(
            agent="overseer",
            failure_reason=(
                f"VM provisioning failed {self.MAX_CONSECUTIVE_PROVISION_FAILURES} "
                f"times for job {job_id}. Last error: {error} "
                f"(category: {category}). "
                f"Human intervention required to adjust budget, criteria, or "
                f"manually provision a VM."
            ),
            suggested_resolution=(
                "Check Vast.ai account balance, relax cost/VRAM constraints, "
                "or manually allocate a GPU instance."
            ),
            run_id=self.config.run_id,
        )
        await self.event_store.append(effect)
```

The escalation flow preserves the deterministic lackey model: the Provisioner never improvises recovery. Three strikes and it calls for the overseer. This matches V6's principle that agents are autonomous within their domain but defer to humans when boundaries are exceeded.


---

## 11. VM Worker

The VM Worker is a stateless FastAPI server on ephemeral GPU instances (port 9000+). It receives inference jobs from the Provisioner (Section 10.3.1), executes TTS or video inference, measures output with WhisperX (3 runs), validates via LLM call to `deepseek-v4-flash`, and posts results back to the Provisioner. The worker has no event store access, no local state beyond the current job, and no timeout or self-destruct logic per Principle 4 (Section 1.4).

---

### 11.1 HTTP Surface

#### 11.1.1 GET / (health), POST / (receive job)

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/` | `GET` | None | `VMHealthResponse` JSON |
| `/` | `POST` | `JobRequest` JSON | `202 Accepted` (or `409` if busy) |

```python
from pydantic import BaseModel, Field
from typing import Literal
from uuid import UUID

class VMHealthResponse(BaseModel):
    status: Literal["idle", "busy"] = "idle"
    instance_id: str
    gpu_type: str
    current_job_id: str | None = None

class JobRequest(BaseModel):
    job_id: UUID
    job_type: Literal["tts", "ltx"]
    params: dict = Field(default_factory=dict)
    provisioner_url: str
    whisperx_model: str = "large-v3"
```

`GET /` returns the worker's current status. The Provisioner polls this before dispatch. `POST /` returns `202 Accepted` immediately and spawns the job in a `BackgroundTasks`. If the worker is `busy`, it returns `409 Conflict`. The worker holds no queue — exactly one job runs at a time.

---

### 11.2 Job Execution

#### 11.2.1 TTS: Qwen3-TTS inference pipeline

For `job_type="tts"`, the worker invokes Qwen3-TTS via subprocess. The model is loaded into VRAM once at VM boot.

```python
async def _run_tts(params: dict) -> str:
    cmd = [
        "python3", "-m", "qwen3_tts",
        "--text", params["text"],
        "--voice", params.get("voice_id", "default"),
        "--output", params.get("output_path", f"/tmp/{uuid4()}.wav"),
        "--format", "wav",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise InferenceError(f"Qwen3-TTS failed: {stderr.decode()}")
    return params.get("output_path", f"/tmp/{uuid4()}.wav")
```

OOM errors propagate as `InferenceError` and are reported with `failure_category="oom"`.

#### 11.2.2 Video: LTX-2.3 inference pipeline

For `job_type="ltx"`, the worker invokes LTX-2.3 via subprocess. The diffusion model is VRAM-resident from VM boot.

```python
async def _run_ltx(params: dict) -> str:
    output = params.get("output_path", f"/tmp/{uuid4()}.mp4")
    cmd = [
        "python3", "-m", "ltx_video",
        "--prompt", params["prompt"],
        "--duration", str(params.get("duration_sec", 5.0)),
        "--width", str(params.get("width", 1280)),
        "--height", str(params.get("height", 720)),
        "--output", output, "--steps", "30",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise InferenceError(f"LTX-2.3 failed: {stderr.decode()}")
    return output
```

#### 11.2.3 WhisperX: 3× measurement, return all to Audio Agent

After inference, the worker runs WhisperX three times per decision C2. All three measurements are returned; the Audio Agent computes the median (Section 9.2).

```python
async def _measure_with_whisperx(
    audio_path: str, model: str = "large-v3", num_runs: int = 3,
) -> list[float]:
    measurements: list[float] = []
    for _ in range(num_runs):
        cmd = ["whisperx", audio_path, "--model", model,
               "--language", "en", "--output_format", "json",
               "--output_dir", "/tmp/"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise MeasurementError(f"WhisperX failed: {stderr.decode()}")
        segments = json.loads(stdout.decode()).get("segments", [])
        measurements.append(segments[-1]["end"] if segments else 0.0)
    return measurements   # e.g. [5.12, 5.08, 5.15]
```

Runs execute sequentially to avoid CPU contention. For TTS jobs WhisperX always runs. For LTX jobs it runs only when `ffprobe` detects an audio track in the MP4.

---

### 11.3 Quality Check

#### 11.3.1 LLM-based output validation (file size, duration, corruption check)

Before reporting success, the worker classifies the artifact via a single-turn call to `deepseek-v4-flash`. This catches gross failures — zero-byte files, extreme duration mismatch, container corruption — before they reach the Audio/Video Agent.

```python
async def _quality_check(
    path: str, job_type: str, expected: float | None,
) -> tuple[Literal["pass", "fail"], str]:
    size = os.path.getsize(path)
    actual = _ffprobe_duration(path) if job_type == "ltx" else None
    prompt = (
        f"File: {path}\nType: {job_type}\nSize: {size} bytes\n"
        f"Expected: {expected or 'N/A'}s\nActual: {actual or 'N/A'}s\n\n"
        "Classify as: pass | fail_file_empty | fail_duration_mismatch "
        "| fail_corrupt. Respond with label only."
    )
    label = (await _call_deepseek_flash(prompt)).strip().lower()
    if label == "pass":
        return "pass", "ok"
    reasons = {
        "fail_file_empty": "Output file is empty",
        "fail_duration_mismatch": f"Duration mismatch (expected {expected}s)",
        "fail_corrupt": "Container corruption detected",
    }
    return "fail", reasons.get(label, f"QC failed: {label}")
```

Unrecognized labels default to `pass` — false negatives are more expensive than false positives. This check is not a substitute for Audio Agent reconciliation (Section 9.2) or Video Agent artistry judgment.

---

### 11.4 Reporting

#### 11.4.1 POST result to Provisioner

The worker posts a `JobResult` to the `provisioner_url` from the inbound `JobRequest`:

```python
class JobResult(BaseModel):
    job_id: UUID
    status: Literal["completed", "failed"]
    artifact_path: str | None = None
    duration_sec: float | None = None
    measurements: list[float] = Field(default_factory=list)
    file_size_bytes: int | None = None
    vm_instance_id: str
    failure_category: str | None = None
    failure_reason: str | None = None
```

The orchestrator `_execute_job` ties all phases: inference → measure → quality-check → report. On any exception, it posts `status="failed"` with the error detail and resets to `idle`. The worker does not retry failed POSTs — the Provisioner's `TimeoutObserved` (Section 12) handles undelivered results.

#### 11.4.2 No VM-side timeout; no self-destruct

The VM Worker contains no `asyncio.timeout`, `threading.Timer`, `signal.alarm`, heartbeat loop, or self-destruct call. This is the V5 → V6 change mandated by Principle 4.

| Aspect | V5 | V6 |
|---|---|---|
| Heartbeat | VM polls every 60s | None — VM is passive |
| Stale detection | 15 min → `vastai destroy` | Provisioner detects via `TimeoutObserved` |
| Timer code | `threading.Timer` in VM | No timer code in VM |
| Recovery path | VM self-destructs | Provisioner deallocates + requeues job |

If a subprocess hangs (Qwen3-TTS or LTX-2.3 never returns), the VM remains occupied until the Provisioner's projection-based stale-job detection reclaims it. The VM has no awareness of its own lifecycle — it processes jobs until terminated externally.

## 12. Data Flows

This chapter traces the four principal data flow patterns through the V6 pipeline: the normal tick cycle, the reconciliation loop with VM-mediated TTS, the script back-edge with partial re-reconciliation, and human intervention. Each flow is presented as a text-based sequence diagram showing actor interactions, followed by a step-by-step specification.

---

### 12.1 Normal Cycle

#### 12.1.1 11-step tick-driven cycle with projection updates

```
  +----------+     +--------------+     +--------------+     +--------------+
  | Watcher  |     |Projections   |     |  Watcher Loop |     |  Agents      |
  | (1s loop)|     |(OTIO,Job,VM, |     | (prompt-based)|     |(Scenario,   |
  |          |     | State)       |     |               |     | Audio,Video,|
  |          |     |              |     |               |     | Assembly)   |
  +----+-----+     +------+-------+     +-------+-------+     +------+-------+
       |                  |                     |                    |
       | 1. wake          |                     |                    |
       |----------------->|                     |                    |
       |                  | 2. tick(es)         |                    |
       |                  |--read new events----|                     |
       |                  |   since last_seq    |                    |
       |                  |------------------->|                     |
       |                  |                     | 3. agents read     |
       |                  |                     |    projections     |
       |                  |                     |    and decide      |
       |                  |                     |    actions         |
       |                  |                     | 4. emit effects    |
       |                  |                     |    (QueueJob,      |
       |                  |                     |    UpdateScript,   |
       |                  |                     |    etc.)           |
       |                  |                     |------------------->|
       |                  |                     |    (POST / with    |
       |                  |                     |     instruction +  |
       |                  |                     |     snapshot)      |
       |                  |                     | 5. Agent 202       |
       |                  |                     |<-------------------|
       |                  |                     | 6. LLM runs,       |
       |                  |                     |    produces text   |
       |                  |                     | 7. parser extracts |
       |                  |                     |    effects         |
       |                  |     8. append      |                    |
       |                  |<---effects---------|                    |
       |                  |     (EventStore)   |                    |
       |                  |     INSERT OR      |                    |
       |                  |     IGNORE         |                    |
       | 9. sleep 1s      |                     |                    |
       |<-----------------|                     |                    |
       | (repeat)         |                     |                    |
```

**Step-by-step specification:**

| Step | Actor | Action | Specification |
|---|---|---|---|
| 1 | Watcher | Wakes | `asyncio.sleep(1)` expires; loop body begins |
| 2 | Projections | Tick | Each projection calls `tick(event_store)`, which invokes `read_since(last_sequence)` to fetch new events |
| 3 | Watcher Loop | Agents observe projections | Each agent reads relevant projection state (OTIO, Job, VM, Config) via injected references (§4.3) |
| 4 | Watcher Loop | Agents decide and act | Agents see conditions in projection and emit effects (QueueJob, UpdateScript, etc.) directly to event store |
| 5 | Watcher Loop → Agent | POST / | HTTP POST to agent's port (8001, 8002, 8003, 8005) with `InstructionPayload` (§7.1.1). Agent returns `202 Accepted` immediately |
| 6 | Agent | LLM execution | Agent runs `_call_llm()` with `_build_prompt()` (persona + instruction + projection summary). No timeout (§1.4) |
| 7 | Agent | Effect parsing | `_parse_effects()` via instructor extracts typed Pydantic models from LLM output (§7.6) |
| 8 | Event Store | Append | Effects written to SQLite with `INSERT OR IGNORE` on `(run_id, effect_id)`. Single writer queue (§5.2) |
| 9 | Watcher | Sleep | `asyncio.sleep(1)`; loop repeats. New effects are picked up on next tick at step 2 |

The cycle is **strictly sequential** — the watcher does not send a new tick until the previous one has fully settled (all projections processed, all agent decisions made). This prevents race conditions between projection state and agent reads. The 1-second throttle is a minimum, not a maximum: if step 6 (LLM execution) takes 30 seconds, ticks simply resume afterward.

---

### 12.2 Reconciliation Loop (Detailed)

The reconciliation loop is the most complex flow in the pipeline. It spans four physical components — Audio Agent, Event Store, Provisioner, and VM Worker — and iterates until every narration block passes the tolerance check or exhausts its attempt budget.

#### 12.2.1 Audio Agent ↔ Event Store ↔ Provisioner ↔ VM Worker (TTS path)

```
Audio Agent (8002)          Event Store            Provisioner           VM Worker (9000)
      |                         |                        |                      |
      |-- QueueJob (TTS) ----->|                        |                      |
      |  job_type=tts           |                        |                      |
      |  block_id=A1:1:1       |                        |                      |
      |  text="In 1924..."     |                        |                      |
      |                         |<-- tick() -------------|                      |
      |                         |  Provisioner reads     |                      |
      |                         |  JobProjection: 1      |                      |
      |                         |  pending (tts)         |                      |
      |                         |                        |-- offer matching     |
      |                         |                        |  GPU/VRAM/price      |
      |                         |                        |  criteria (§8.2.1)   |
      |                         |                        |                      |
      |                         |<-- VMAllocated -------|                      |
      |                         |  instance_id=vast-42   |                      |
      |                         |                        |-- POST / (job) ----->|
      |                         |                        |  JobRequest JSON     |
      |                         |                        |                      |
      |                         |                        |<-- 202 Accepted -----|
      |                         |                        |                      |
      |                         |                        |<-- POST result ------|
      |                         |                        |  JobResult JSON      |
      |                         |                        |  measurements=[5.12, |
      |                         |                        |  5.08, 5.15]         |
      |                         |                        |                      |
      |                         |<-- JobCompleted ------|                      |
      |                         |  artifact=/tmp/...     |                      |
      |                         |  duration_median=5.12  |                      |
      |                         |  measurements=[5.12,  |                      |
      |                         |    5.08, 5.15]        |                        |
      |                         |                        |                      |
      |<-- POST / from          |                        |                      |
      |    Provisioner          |                        |                      |
      |   "Job done, verify"    |                        |                      |
      |                         |                        |                      |
      |-- AudioMeasured ------->|                        |                      |
      |   block=A1:1:1         |                        |                      |
      |   measurements=[5.12,  |                        |                      |
      |    5.08, 5.15]         |                        |                      |
      |   median=5.12           |                        |                      |
      |                         |                        |                      |
```

#### 12.2.2 3× WhisperX measurement flow

The VM Worker executes WhisperX three times sequentially (decision C2). Each run loads the WhisperX model, transcribes the generated WAV, and reports the end timestamp of the final segment. All three values are returned in `JobResult.measurements` as raw floats. The Audio Agent computes the median client-side.

```python
# Inside VM Worker (Section 9.2.3)
measurements: list[float] = []
for run in range(3):
    segments = await _whisperx_transcribe(audio_path, model="large-v3")
    measurements.append(segments[-1]["end"] if segments else 0.0)
# Returns: [5.12, 5.08, 5.15]

# Inside Audio Agent
import statistics
median_sec = statistics.median(job_result.measurements)  # 5.12
```

The three runs execute sequentially to avoid CPU contention on the shared WhisperX process. Runs are not parallelized across GPU — the model is CPU-bound for transcription.

#### 12.2.3 Within tolerance → DurationAdjusted

```
Audio Agent (8002)          Event Store            Provisioner           VM Worker (9000)
      |                         |                        |                      |
      | [ median=5.12,          |                        |                      |
      |   scripted=5.0,         |                        |                      |
      |   delta=+0.12s,         |                        |                      |
      |   tolerance=max(        |                        |                      |
      |     5.0*0.15=0.75,     |                        |                      |
      |     0.25)=0.75s ]       |                        |                      |
      |   delta < tolerance     |                        |                      |
      |   → PASS                |                        |                      |
      |                         |                        |                      |
      |-- DurationAdjusted ---->|                        |                      |
      |   block=A1:1:1         |                        |                      |
      |   measured=5.12         |                        |                      |
      |   scripted=5.0          |                        |                      |
      |   delta=+0.12           |                        |                      |
      |   within_tolerance=true |                        |                      |
      |                         |                        |                      |
      | [ Next tick: OTIO       |                        |                      |
      |   Projection merges     |                        |                      |
      |   5.12s into slot       |                        |                      |
      |   A1:1:1. Block marked  |                        |                      |
      |   measured. ]           |                        |                      |
      |                         |                        |                      |
      |-- QueueJob (TTS) ----->|   [ proceed to next    |                        |
      |   block=A1:1:2         |     block A1:1:2 ]     |                        |
```

The tolerance rule (§7.3.3) is **max(15% of scripted duration, 0.25s)**. For a 5.0s target, tolerance = max(0.75, 0.25) = 0.75s. A measured 5.12s (delta +0.12s) passes. The `DurationAdjusted` effect updates the OTIO Projection, which on the next tick applies the measured duration to the corresponding slot.

#### 12.2.4 Outside tolerance → ReconciliationFailed → JobRequeued → retry

```
Audio Agent (8002)          Event Store            Provisioner           VM Worker (9000)
      |                         |                        |                      |
      | [ median=7.2,           |                        |                      |
      |   scripted=5.0,         |                        |                      |
      |   delta=+2.2s,          |                        |                      |
      |   tolerance=0.75s ]     |                        |                      |
      |   delta > tolerance     |                        |                      |
      |   → FAIL                |                        |                      |
      |   attempt=2/5           |                        |                      |
      |                         |                        |                      |
      |-- ReconciliationFailed >|                        |                      |
      |   block=A1:1:2         |                        |                      |
      |   measured=7.2          |                        |                      |
      |   scripted=5.0          |                        |                      |
      |   delta=+2.2            |                        |                      |
      |   failure_type=         |                        |                      |
      |     duration_mismatch   |                        |                      |
      |                         |                        |                      |
      |-- JobRequeued --------->|                        |                      |
      |   job_id=<old>          |                        |                      |
      |   reason="too long      |                        |                      |
      |     by 2.2s"            |                        |                      |
      |   adjusted_text=        |                        |                      |
      |     "In '24..."         |                        |                      |
      |   (shortened)           |                        |                      |
      |                         |                        |                      |
      |-- QueueJob (TTS v2) -->|                        |                      |
      |   block=A1:1:2          |                        |                      |
      |   text="In '24..."      |                        |                      |
      |   attempt=2             |                        |                      |
      |                         |                        |                      |
      | [ Next tick:            |                        |                      |
      |   Provisioner sees      |                        |                      |
      |   new QueueJob,         |                        |                      |
      |   allocates VM,         |                        |                      |
      |   loop repeats... ]     |                        |                      |
```

When the measured duration exceeds tolerance, the Audio Agent computes an adjusted text (shortening or splitting the phrase) and requeues. Each block has a maximum of **5 attempts** (§7.3.4). If attempts are exhausted, the Audio Agent emits `ReconciliationFailed` with `failure_type="duration_unrecoverable"`, which triggers a back-edge to SCRIPT (the text is physically unachievable at the target duration).

#### 12.2.5 All pass → ReconciliationComplete

When every block in the narration has been measured and passes tolerance, the Audio Agent emits `ReconciliationComplete`:

```
Audio Agent (8002)          Event Store
      |                         |
      | [ All blocks measured:   |
      |   A1:1:1=5.12s PASS     |
      |   A1:1:2=4.89s PASS     |
      |   ...                     |
      |   A1:3:5=3.01s PASS ]   |
      |                         |
      |-- ReconciliationComplete >|
      |   blocks_total=14       |
      |   blocks_passed=14      |
      |   blocks_failed=0       |
      |   otio_authoritative=   |
      |     true                |
      |                         |
      | [ Next tick:            |
      |   _reconciliation_      |
      |   _complete guard       |
      |   returns True →        |
      |   AUDIO_RECONCILE →     |
      |   VIDEO_PRODUCTION ]    |
```

The `ReconciliationComplete` effect is the **gateway** from `AUDIO_RECONCILE` to `VIDEO_PRODUCTION`. Agents check for `ReconciliationComplete` and clean blocks to decide whether to begin video generation. The OTIO Projection's measured durations become **authoritative** — the Video Agent uses them as LAW for LTX-2.3 clip generation.

| Parameter | Value | Source |
|---|---|---|
| Tolerance | ±15% or ±0.25s (whichever is larger) | §7.3.3 |
| Max attempts per block | 5 | §7.3.4, `max_attempts_per_block` config |
| Max TTS budget | $2.00 USD | §7.3.4, `max_tts_budget_usd` config |
| WhisperX runs per measurement | 3 | §9.2.3, decision C2 |
| Median computation | Client-side (Audio Agent) | §7.3.2 |

---

### 12.3 Script Failure → Back-Edge with ReconciliationPartial

V6 introduces `ReconciliationPartial` (§3.4.2) to handle the case where a script revision invalidates only some blocks. Blocks that were unchanged keep their measured durations; only dirty blocks are re-reconciled.

#### 12.3.1 voice_mismatch in VIDEO_PRODUCTION → Transition to SCRIPT

```
Video Agent (8003)          Event Store           Scenario Agent (8001)
      |                         |                        |
      | [ Generates LTX-2.3     |                        |
      |   clip for scene 3.     |                        |
      |   LLM judges: "Voice    |                        |
      |   is baritone, script   |                        |
      |   says soprano." ]      |                        |
      |                         |                        |
      |-- ProductionFailed --->|                        |
      |   failure_type=         |                        |
      |     voice_mismatch      |                        |
      |   scene=3               |                        |
      |   detail="baritone      |                        |
      |     vs soprano"         |                        |
      |                         |                        |
      | [ Next tick:            |                        |
      |   Scenario Agent reads  |                        |
      |   production_failures   |                        |
      |   and sees voice_mismatch|
      |   in SCRIPT_ERROR_TYPES]|                        |
      |                         |                        |
      |                         |<-- UpdateScript -------|
      |                         |   scenes=[...,         |
      |                         |     {scene:3, voice:   |
      |                         |      "baritone", text: |
      |                         |      "In 1924..."} ]   |
```

The Scenario Agent checks for `ProductionFailed` effects with `failure_type in {"gap_unexpected", "voice_mismatch"}`. These are the only two failure types that trigger a back-edge to `SCRIPT`; all others either requeue in-place or halt with `ClarificationRequest`.

#### 12.3.2 Scenario Agent fixes script → UpdateScript

```
Scenario Agent (8001)         Event Store           State Projection
      |                           |                         |
      | [ Receives instruction     |                         |
      |   with audio_mismatch      |                         |
      |   context. LLM revises     |                         |
      |   scene 3: changes voice   |                         |
      |   tag to "baritone",       |                         |
      |   adjusts narration text   |                         |
      |   to match. ]              |                         |
      |                           |                         |
      |-- UpdateScript ----------> |                         |
      |   scenes=[...,             |                         |
      |     {scene:3, voice:       |                         |
      |      "baritone", text:     |                         |
      |      "In 1924..."} ]      |                         |
      |                           |                         |
      | [ Next tick: SCRIPT →     |                         |
      |   AUDIO_RECONCILE via     |                         |
      |   _reconciliation_ready    |                         |
      |   check. Audio Agent       |                         |
      |   begins reconciliation. ] |                         |
```

The Scenario Agent's system prompt includes the failure context so the LLM understands what changed. The emitted `UpdateScript` effect contains the full revised scene list.

#### 12.3.3 Audio Agent computes dirty/clean blocks → ReconciliationPartial

```
Audio Agent (8002)            Event Store           OTIO Projection
      |                           |                         |
      | [ Receives state summary   |                         |
      |   in AUDIO_RECONCILE.      |                         |
      |   Compares new script      |                         |
      |   against authoritative    |                         |
      |   OTIO: ]                  |                         |
      |                           |                         |
      |   Block A1:1:1: unchanged  |                         |
      |     → CLEAN (keep 5.12s)   |                         |
      |   Block A1:1:2: unchanged  |                         |
      |     → CLEAN (keep 4.89s)   |                         |
      |   Block A1:3:1: voice      |                         |
      |     changed baritone→      |                         |
      |     soprano → DIRTY        |                         |
      |   Block A1:3:2: text       |                         |
      |     shortened → DIRTY      |                         |
      |   Block A1:3:3: unchanged  |                         |
      |     → CLEAN (keep 3.01s)   |                         |
      |                           |                         |
      |-- ReconciliationPartial -> |                         |
      |   dirty_block_ids=[        |                         |
      |     "A1:3:1", "A1:3:2"]   |                         |
      |   clean_block_ids=[        |                         |
      |     "A1:1:1", "A1:1:2",    |                         |
      |     "A1:3:3"]             |                         |
      |   blocks_dirty=2           |                         |
      |   blocks_clean=3           |                         |
      |   reason="voice_mismatch   |                         |
      |     back-edge from         |                         |
      |     VIDEO_PRODUCTION"      |                         |
      |                           |                         |
      | [ OTIO Projection marks    |                         |
      |   dirty slots: status=     |                         |
      |   "dirty", measured_sec=   |                         |
      |   None, artifact_path=     |                         |
      |   None. Clean slots        |                         |
      |   retain their values. ]   |                         |
```

The dirty-block computation is performed by the Audio Agent by comparing each block's `(text, speaker, duration_target)` tuple between the new script and the authoritative OTIO. Any change in any field marks the block dirty.

#### 12.3.4 Only dirty blocks re-reconciled; clean blocks remain authoritative

```
Audio Agent (8002)         Event Store         Provisioner        VM Worker (9000)
      |                        |                      |                   |
      | [ Loop starts: only    |                      |                   |
      |   dirty blocks queued  |                      |                   |
      |   for TTS. Clean       |                      |                   |
      |   blocks skipped. ]    |                      |                   |
      |                        |                      |                   |
      |-- QueueJob (TTS) ----->|                      |                   |
      |   block=A1:3:1         |                      |                   |
      |   text="In 1924..."    |                      |                   |
      |   voice="baritone"     |                      |                   |
      |   (was "soprano")      |                      |                   |
      |                        |                      |                   |
      |-- QueueJob (TTS) ----->|                      |                   |
      |   block=A1:3:2         |                      |                   |
      |   text="He then..."    |                      |                   |
      |   (shortened)          |                      |                   |
      |                        |                      |                   |
      | [ Block A1:1:1         |                      |                   |
      |   (clean, 5.12s) is    |                      |                   |
      |   NOT queued. Block    |                      |                   |
      |   A1:1:2 (clean,       |                      |                   |
      |   4.89s) is NOT        |                      |                   |
      |   queued. ]            |                      |                   |
      |                        |                      |                   |
      | [ Reconciliation proceeds|                     |                   |
      |   for A1:3:1 and       |                      |                   |
      |   A1:3:2 only.         |                      |                   |
      |   Clean blocks remain  |                      |                   |
      |   authoritative. ]     |                      |                   |
      |                        |                      |                   |
      |-- ReconciliationComplete>|                     |                   |
      |   (when all dirty pass) |                     |                   |
```

The Audio Agent keeps emitting effects for dirty blocks until they are all clean. Clean blocks are never re-measured — their `AudioMeasured` values from the previous reconciliation pass remain LAW. This avoids redundant TTS spend on unchanged content.

| Block | Status | Action | Previous Measurement |
|---|---|---|---|
| A1:1:1 | clean | Skipped, retained | 5.12s (authoritative) |
| A1:1:2 | clean | Skipped, retained | 4.89s (authoritative) |
| A1:3:1 | dirty | Re-queued for TTS | Reset to `None` |
| A1:3:2 | dirty | Re-queued for TTS | Reset to `None` |
| A1:3:3 | clean | Skipped, retained | 3.01s (authoritative) |

---

### 12.4 Human Intervention

Human operators interact with agents via HTTP GET/POST. There is no dedicated dashboard — the agent's own endpoints serve as the observation and control surface.

#### 12.4.1 GET agent status, POST instruction

```
Human Operator            Audio Agent (8002)          Event Store
      |                          |                          |
      |-- GET / ---------------->|                          |
      |                          |                          |
      |<-- AgentStatus ----------|                          |
      |   name="audio"           |                          |
      |   status="working"       |                          |
      |   current_task=          |                          |
      |     "reconcile block     |                          |
      |      A1:3:1, attempt    |                          |
      |      3/5"               |                          |
      |   last_error=null       |                          |
      |                          |                          |
      | [ Human decides text    |                          |
      |   is fine at 5.5s,      |                          |
      |   override tolerance. ]  |                          |
      |                          |                          |
      |-- POST / --------------->|                          |
      |   instruction: "Accept   |                          |
      |     the 5.5s duration   |                          |
      |     for block A1:3:1.   |                          |
      |     It is close enough."|                          |
      |                          |                          |
      |<-- 202 Accepted ---------|                          |
      |                          |                          |
      | [ Next agent turn:       |                          |
      |   instruction appears    |                          |
      |   in prompt context.     |                          |
      |   LLM emits: ]           |                          |
      |                          |                          |
      |                          |-- DurationAdjusted ----->|
      |                          |   block=A1:3:1           |
      |                          |   measured=5.5           |
      |                          |   human_override=true    |
      |                          |   reason="operator       |
      |                          |     approved"            |
```

The `AgentStatus` response (§7.1.1) includes `status`, `current_task`, `last_error`, and `idle_since`. A human reading this can determine if the agent is stuck (e.g., "attempt 4/5 on same block") and issue corrective instructions.

#### 12.4.2 ExecuteRawBash allowlist approval flow

When an agent needs to run a shell command, it emits `ExecuteRawBash`. Non-allowlisted commands are blocked by the parser and converted to `ClarificationRequest`, which halts the pipeline until human approval.

```
Audio Agent (8002)          Parser            Event Store         Human Operator
      |                        |                      |                    |
      |-- ExecuteRawBash ----->|                      |                    |
      |   command="ffmpeg      |                      |                    |
      |     -i /tmp/x.wav      |                      |                    |
      |     -af volume=1.5     |                      |                    |
      |     /tmp/x_loud.wav"   |                      |                    |
      |                        |                      |                    |
      |                        | [ Command NOT in     |                    |
      |                        |   allowlist. Replace |                    |
      |                        |   with: ]            |                    |
      |                        |                      |                    |
      |<-- ClarificationRequest|                      |                    |
      |   (agent sees this     |                      |                    |
      |   as its own effect)   |                      |                    |
      |                        |                      |                    |
      |                        |-- ClarificationRequest|                   |
      |                        |   -> Event Store     |                    |
      |                        |                      |                    |
      | [ Pipeline HALTS.      |                      |                    |
      |   No ticks until       |                      |                    |
      |   resolved. ]          |                      |                    |
      |                        |                      |                    |
      |                        |                      |-- (human observes  |
      |                        |                      |   via GET / )      |
      |                        |                      |                    |
      |                        |                      |<-- POST / --------|
      |                        |                      |   HumanInstruction |
      |                        |                      |   "Approve ffmpeg  |
      |                        |                      |   volume adjust.   |
      |                        |                      |   Run: ffmpeg -i   |
      |                        |                      |   /tmp/x.wav -af   |
      |                        |                      |   volume=1.5       |
      |                        |                      |   /tmp/x_loud.wav" |
      |                        |                      |                    |
      |<-- HumanInstruction ---|                      |                    |
      |   (appears in prompt)  |                      |                    |
      |                        |                      |                    |
      | [ LLM re-emits: ]      |                      |                    |
      |                        |                      |                    |
      |-- ExecuteRawBash ----->|                      |                    |
      |   (allowlist now       |                      |                    |
      |    approved)           |                      |                    |
      |                        |-- allowed through -->|                    |
      |                        |                      |                    |
      |                        |                      |-- (shell exec)     |
```

The allowlist is a static set of command patterns stored in the parser configuration (§7.6.2). A command matches if its base executable and flags are in the list. The `HumanInstruction` effect carries the approved command string, which the agent includes in its next turn context and re-emits as `ExecuteRawBash` — now passing the allowlist check.

| Step | Effect | Actor | Meaning |
|---|---|---|---|
| 1 | `ExecuteRawBash` | Agent | Request to run non-allowlisted command |
| 2 | `ClarificationRequest` | Parser | Blocked; pipeline halts |
| 3 | `HumanInstruction` | Human | Operator approves the command |
| 4 | `ExecuteRawBash` (re-emitted) | Agent | Re-parsed, now allowed |
| 5 | Shell execution | System | Command runs, output captured |

#### 12.4.3 Budget override and emergency abort

```
Human Operator          Any Agent (8001-8005)       Event Store        Watcher
      |                          |                      |                    |
      | [ Human observes run     |                      |                    |
      |   is approaching $10     |                      |                    |
      |   budget via GET /.      |                      |                    |
      |   Decides to increase. ] |                      |                    |
      |                          |                      |                    |
      |-- POST / --------------->|                      |                    |
      |   instruction: "Raise    |                      |                    |
      |     budget to $25.00.    |                      |                    |
      |     Reason: narration    |                      |                    |
      |     is longer than       |                      |                    |
      |     expected."           |                      |                    |
      |                          |                      |                    |
      |                          |-- HumanInstruction ->|                    |
      |                          |   action=             |                    |
      |                          |     "budget_override" |                    |
      |                          |   new_limit=25.00     |                    |
      |                          |   reason=...          |                    |
      |                          |                      |                    |
      |                          |                      |-- (next tick:      |
      |                          |                      |   _budget_exceeded |
      |                          |                      |   reads new limit, |
      |                          |                      |   guard False,     |
      |                          |                      |   run continues)   |
      |                          |                      |                    |
      | [ Emergency abort: ]     |                      |                    |
      |                          |                      |                    |
      |-- POST / --------------->|                      |                    |
      |   instruction: "ABORT    |                      |                    |
      |     RUN IMMEDIATELY.     |                      |                    |
      |     Reason: wrong        |                      |                    |
      |     pipeline started."   |                      |                    |
      |                          |                      |                    |
      |                          |-- HumanInstruction ->|                    |
      |                          |   action=             |                    |
      |                          |     "emergency_abort" |                    |
      |                          |                      |                    |
      |                          |                      |-- PipelineAborted >|
      |                          |                      |   reason=           |
      |                          |                      |     "human_abort"   |
      |                          |                      |                     |
      |                          |                      |-- (all VMs          |
      |                          |                      |   deallocated via   |
      |                          |                      |   VMDeallocated)    |
```

The `HumanInstruction` effect carries an `action` field that agents inspect on their next turn. Valid actions are `"budget_override"` (requires `new_limit` float), `"emergency_abort"`, and `"approve_command"` (for allowlist flows). An emergency abort emits `PipelineAborted`, which the watcher detects and halts, followed by VM deallocation via the Provisioner's cleanup path (§10.2.5).

| Action Field | Required Params | Response |
|---|---|---|
| `budget_override` | `new_limit: float` | Updates `max_run_budget_usd` in config; budget check re-evaluates |
| `emergency_abort` | `reason: str` | Emits `PipelineAborted`; watcher halts; Provisioner deallocates all VMs |
| `approve_command` | `command: str` | Clears pending `ClarificationRequest`; command is re-injected into agent prompt |


---

## 13. Security Model

### 13.1 ExecuteRawBash Allowlist

**Threat.** `ExecuteRawBash` grants agents arbitrary shell access on worker VMs. A compromised or misdirected agent can execute destructive commands or exfiltrate data.

**Defense.** A strict allowlist gates every command. Pre-approved binaries execute without human intervention; all others raise a `ClarificationRequest`.

| Command | Permitted Arguments | Rejected Patterns |
|---|---|---|
| `ffmpeg` | Input/output paths, codec flags, filter graphs | Network URLs (`http://`, `sftp://`) |
| `ffprobe` | `-print_format json`, `-show_streams`, file paths | `--execute`, shell metacharacters |
| `whisperx` | `--model`, `--language`, `--output_format`, file paths | `--download-root` with absolute paths |
| `vastai` | `create instance`, `destroy instance`, `show instances` | `ssh`, `scp`, any file transfer |
| `python3` | Script file path + literal arguments only | `-c` (inline code) |

Validation is two-pass: command name against the allowlist, then argument tokens against a per-command regex denylist. Network egress from `ffmpeg` is blocked at the VM firewall as defense-in-depth (see 13.4).

**Escape hatch.** Non-allowlisted commands surface a `ClarificationRequest` with the full command string. The operator may approve (one-time), approve (pattern), or deny. Denial returns `CommandDisallowed`; the agent must select an alternative strategy.

```python
from pydantic import BaseModel, Field
from typing import Literal, List

class BashCommand(BaseModel):
    """Shell command submitted for allowlist validation."""
    command: str
    agent_id: str

class ValidationResult(BaseModel):
    """Outcome of allowlist validation."""
    decision: Literal["approved", "blocked_pending_approval", "denied"]
    matched_rule: str
    blocked_args: List[str] = Field(default_factory=list)
```

### 13.2 Budget Enforcement

**Threat.** LLM API calls, GPU rental, and storage accumulate cost without bound. A runaway pipeline can consume hundreds of dollars in minutes.

**Defense.** Every pipeline run carries a monotonically-increasing cost accumulator checked against a per-run budget cap. Default: $10.00 USD, configurable per-run via `budget_usd`. The accumulator tracks LLM tokens, GPU rental (per-second), and egress bandwidth (per-GB).

```python
class BudgetLedger(BaseModel):
    """Cumulative spend against a per-run budget ceiling."""
    budget_usd: float = Field(default=10.0, ge=0.01, le=1000.0)
    spent_llm_usd: float = Field(default=0.0)
    spent_gpu_usd: float = Field(default=0.0)
    spent_egress_usd: float = Field(default=0.0)

    @property
    def remaining_usd(self) -> float:
        return self.budget_usd - (
            self.spent_llm_usd + self.spent_gpu_usd + self.spent_egress_usd
        )

    def check(self, next_charge_usd: float) -> bool:
        return (self.remaining_usd - next_charge_usd) >= 0.0
```

**Escape hatch.** If a projected charge exceeds remaining budget, the watcher emits `PipelineAborted` with `reason=budget_exceeded` and a final ledger. All non-committed GPU instances are destroyed immediately. Partial outputs are retained for inspection.

### 13.3 Agent Loop Detection

**Threat.** An agent may enter an infinite loop: repeatedly calling the same tool with identical arguments, or cycling through strategies without progress.

**Defense.** Dual detection runs against every agent's turn history:

1. **Duplicate-effects detection.** Hashes observable side effects (files written, API calls, VMs launched) after each turn. Same hash twice within the window fires the detector.
2. **No-progress detection.** If the task-state score (completed checklist items) does not increase for `N` consecutive turns, the detector fires. Default `N=5`, configurable per agent type.

| Detector | Signal | Threshold | Action |
|---|---|---|---|
| Duplicate effects | Identical side-effect hash | 2 occurrences | `LoopDetected` → `ClarificationRequest` |
| No progress | Task-state delta = 0 | `N` turns (default 5) | `LoopDetected` → `ClarificationRequest` |

**Escape hatch.** Either trigger pauses the agent and surfaces a `ClarificationRequest` with the last `N` turns of context. The operator may resume with guidance, terminate the agent, or reassign.

```python
class LoopDetectorConfig(BaseModel):
    """Per-agent loop detection parameters."""
    progress_threshold_turns: int = Field(default=5, ge=2, le=20)
    effect_dedup_window: int = Field(default=10, ge=2, le=50)
    enabled_detectors: List[Literal["duplicate_effects", "no_progress"]] = Field(
        default_factory=lambda: ["duplicate_effects", "no_progress"]
    )
```

### 13.4 VM Isolation

**Threat.** GPU worker VMs execute arbitrary code. A compromised VM could exfiltrate secrets, persist malware, or attack the coordinator.

**Defense.** Three isolation layers:

1. **Ephemeral lifecycle.** VMs are created per pipeline stage and destroyed within 60 seconds of completion. Root disks are provisioned from a golden image; no writable overlay persists.
2. **No secrets on workers.** API keys reside exclusively on the coordinator. Workers authenticate via short-lived JWTs (5-minute expiry, single-use refresh) granting access only to the stage's input/output buckets.
3. **Network egress restriction.** Outbound connections are limited to the coordinator control plane and object-storage endpoint. All other egress is blocked at the hypervisor firewall.

```python
class VMIsolationConfig(BaseModel):
    """Security parameters for ephemeral GPU worker VMs."""
    jwt_ttl_seconds: int = Field(default=300, ge=60, le=3600)
    destroy_after_stage_seconds: int = Field(default=60, ge=0, le=300)
    allowed_egress_hosts: List[str] = Field(
        default_factory=lambda: ["coordinator.internal", "storage.internal"]
    )
    enable_process_monitoring: bool = Field(default=True)
```

**Escape hatch.** Anomalous behavior (failed health check, unexpected process, unauthorized connection) triggers immediate VM destruction and stage retry on a fresh instance. Anomaly events are logged to a security audit stream.

---

## 14. Configuration

The `Config` Pydantic model (§14.1) is the single source of truth for all tunable pipeline parameters. It is instantiated once at coordinator startup from a `config.py` module and passed read-only into every downstream component. No environment-variable fallbacks or runtime mutation are permitted; changing a value requires a code change and redeployment.

### 14.1 Pipeline Config

#### 14.1.1 max_run_budget_usd, max_attempts_per_block, max_tts_budget_usd

`max_run_budget_usd` (`float`, default `10.00`) defines the hard upper bound on cloud-spend for a single pipeline run. This is a *post-approval* gate: the watcher aborts the run if projected cumulative cost exceeds this threshold. `max_attempts_per_block` (`int`, default `5`) is the per-block retry ceiling. Each block may be retried up to this many times before the watcher marks the run FAILED and enters cleanup. `max_tts_budget_usd` (`float`, default `2.00`) caps TTS-specific spend per run, evaluated independently of the overall run budget because TTS is billed per-character via a separate provider API.

#### 14.1.2 tolerance_percent, tolerance_abs_sec

`tolerance_percent` (`float`, default `0.15`) and `tolerance_abs_sec` (`float`, default `0.25`) are the dual-threshold acceptance criteria for assembly-stage duration validation. A generated segment passes if its actual duration deviates from the target by no more than 15 % *and* no more than 0.25 s. Both conditions must hold. These values are chosen to accommodate natural speech-rate variation (the percent guard) while preventing sub-frame timing errors in 24 fps video (the absolute guard).

#### 14.1.3 loop_detection_threshold, stale_job_threshold_minutes

`loop_detection_threshold` (`int`, default `5`) triggers loop-detection logic in the watcher. When the same block transitions to FAILED and back to PENDING more than 5 times within a single run, the watcher raises a `LoopDetectedError` and aborts. `stale_job_threshold_minutes` (`int`, default `10`) is the VM-agent heartbeat timeout. If a VM agent's last heartbeat is older than 10 minutes, the watcher declares the job stale, releases the VM, and reschedules the block on a fresh instance.

#### 14.1.4 ALLOWLISTED_COMMANDS list

`ALLOWLISTED_COMMANDS` (`list[str]`, default `["ffmpeg", "ffprobe", "whisperx", "vastai", "python3"]`) is the explicit permit-list of shell commands that the VM agent may invoke via `subprocess.run`. Any command string whose basename is not in this list is rejected with `SecurityError` before execution. The list is intentionally short; adding a command requires a code review and version bump.

```python
from pydantic import BaseModel, Field
from typing import Literal

class Config(BaseModel):
    """Single source of truth for all tunable pipeline parameters.

    Instantiated once at coordinator startup and passed read-only
    into all downstream components. No runtime mutation permitted.
    """

    # 14.1.1 — Pipeline limits
    max_run_budget_usd: float = Field(default=10.00, ge=0.0)
    max_attempts_per_block: int = Field(default=5, ge=1)
    max_tts_budget_usd: float = Field(default=2.00, ge=0.0)

    # 14.1.2 — Assembly tolerance (dual threshold)
    tolerance_percent: float = Field(default=0.15, ge=0.0, le=1.0)
    tolerance_abs_sec: float = Field(default=0.25, ge=0.0)

    # 14.1.3 — Health & loop detection
    loop_detection_threshold: int = Field(default=5, ge=1)
    stale_job_threshold_minutes: int = Field(default=10, ge=1)

    # 14.1.4 — VM-agent command allowlist
    allowlisted_commands: list[str] = Field(
        default_factory=lambda: [
            "ffmpeg", "ffprobe", "whisperx", "vastai", "python3"
        ]
    )

    # 14.2 — VM sizing (see subsections)
    tts_gpu_type: Literal["RTX_4090"] = "RTX_4090"
    tts_vram_gb: int = 24
    tts_cpu_cores: int = 8
    tts_disk_gb: int = 100

    video_gpu_type: Literal["RTX_A6000", "RTX_4090"] = "RTX_A6000"
    video_vram_gb: int = 48
    video_cpu_cores: int = 16
    video_disk_gb: int = 200

    coord_vcpu: int = 2
    coord_ram_gb: int = 4
    coord_disk_gb: int = 100

    # 14.3 — Rate limits
    tick_interval_sec: float = 10.0
    llm_requests_per_minute: int = 60
    llm_tokens_per_minute: int = 200_000
    vastai_requests_per_minute: int = 30

    # Agent models
    scenario_model: str = "deepseek-v4-flash"
    audio_model: str = "deepseek-v4-flash"
    video_model: str = "deepseek-v4-flash"
    assembly_model: str = "deepseek-v4-flash"
    compaction_model: str = "deepseek-v4-flash"

    # Token budgets
    max_tokens: int = 8000
    compaction_threshold: float = 0.85

    # VMs
    vastai_api_key: str = ""
    vm_tts_image: str = "vastai/worker:tts"
    vm_ltx_image: str = "vastai/worker:ltx"
```

### 14.2 VM Sizing

#### 14.2.1 TTS VM: GPU type, VRAM, CPU, disk

The TTS VM (§11.1) runs speaker-cloning inference and requires 24 GB VRAM for the Qwen3-TTS model in float16. Specification: GPU `RTX_4090` (24 GB VRAM), 8 CPU cores, 100 GB SSD. The 100 GB disk accommodates the base model weights (~4 GB), speaker reference uploads (~50 MB each), and generated WAV output (~10 MB/min at 48 kHz). No swap is configured; inference fails fast with `OutOfMemoryError` if the model does not fit.

#### 14.2.2 Video VM: GPU type, VRAM, CPU, disk

The Video VM (§11.2) runs LTX-Video inference at 720p and requires 48 GB VRAM for the unquantized model. Specification: GPU `RTX_A6000` (48 GB VRAM), 16 CPU cores, 200 GB SSD. The larger disk stores the diffusion model weights (~24 GB), input conditioning frames, and output MP4 segments. Fallback to `RTX_4090` (24 GB) is permitted only when the model is quantized to int8 and quality checks (§11.3) still pass.

#### 14.2.3 Coordinator VM: 2 vCPU, 4 GB RAM, 100 GB disk

The coordinator is a control-plane process and does not run GPU workloads. Specification: 2 vCPU, 4 GB RAM, 100 GB SSD. The disk hosts the SQLite event-store file, projection tables, and log files. RAM is sized for the Pydantic models, SQLAlchemy session cache, and in-memory job queue; typical working set is <512 MB.

### 14.3 Rate Limits

#### 14.3.1 Tick interval (10 s), LLM API rate limits, Vast.ai API rate limits

The coordinator event loop ticks once every 10 seconds (`tick_interval_sec: 10.0`). On each tick it polls the event store for new effects, evaluates health checks, and dispatches VM commands. LLM API calls are throttled to 60 requests per minute and 200 000 tokens per minute to stay within provider tier-1 quotas. Vast.ai API calls (search, create, destroy) are limited to 30 requests per minute to avoid IP-level rate-limiting. All three limits are enforced by an in-memory token-bucket scheduler in the coordinator; requests that exceed the bucket are queued and retried on the next tick.

---

## 15. File Structure

### 15.1 Directory Layout

#### 15.1.1 Complete tree: server/v6/ with all files

The repository root is `server/v6/`. Files and directories are grouped by responsibility: top-level modules for orchestration, `agents/` for domain-specific generation logic, `provisioner/` for cloud VM lifecycle, and `vm/` for the on-instance agent runtime.

```text
server/v6/
├── README.md                          # Project overview and quick-start
├── ARCHITECTURE_V6.md                 # This document
├── config.py                          # Pydantic Config model (§14.1)
├── effects.py                         # 30 effect types + EffectUnion + KIND_TO_MODEL
├── event_store.py                     # SQLite-backed event log (§5)
├── projections.py                     # Read-model builders: OTIO, Job, VM, State, Budget
├── parser.py                          # Category-conditioned effect parser (§9.6)
├── run_pipeline.py                    # Coordinator entry point and watcher loop (§12)
├── watcher.py                         # Watcher loop: tick projections, run agents, safety checks
├── agent_base.py                      # PipelineAgent base + create_pipeline_agent factory
├── situations.py                      # SITUATION_TEMPLATES text blocks for agent prompts
├── rules.py                           # RULES text blocks for agent system prompts
├── agents/
│   ├── __init__.py
│   ├── scenario.py                    # ScenarioAgent: role instructions + focus
│   ├── audio.py                       # AudioAgent: tolerance + reconciliation focus
│   ├── video.py                       # VideoAgent: quality judgment + LTX focus
│   └── assembly.py                    # AssemblyAgent: ffmpeg + validation
├── provisioner/
│   ├── __init__.py
│   ├── service.py                     # VM lifecycle orchestrator (§10)
│   └── vastai.py                      # Vast.ai REST client and search filters
└── vm/
    ├── __init__.py
    ├── agent.py                         # On-instance daemon: fetch, execute, report (§11)
    ├── onstart_tts.sh                   # TTS VM bootstrap: conda env + model download
    └── onstart_ltx.sh                   # Video VM bootstrap: conda env + model download
```

**Top-level modules.** `config.py` contains the `Config` Pydantic model and is imported by `run_pipeline.py`, `agent_base.py`, and `provisioner/service.py`. `effects.py` defines the 30 `Effect` dataclass hierarchies and the `EffectUnion` discriminated union used by `parser.py`. `event_store.py` and `projections.py` form the persistence layer: the former appends domain events, the latter rebuilds read models. `run_pipeline.py` is the executable entry point; it instantiates `Config`, creates the SQLite tables, and enters the tick loop. `watcher.py` implements the 10-second watcher loop with safety checks.

**`agents/` package.** Each module defines role instructions, focus functions, and permitted effects for one agent type. `assembly.py` performs FFmpeg muxing, WhisperX transcript alignment, and dual-threshold duration validation.

**`provisioner/` package.** `service.py` exposes `tick(projections)` and manages VM lifecycle. `vastai.py` wraps the Vast.ai REST API with typed request/response models and exponential-backoff retry.

**`vm/` package.** `agent.py` is the only Python process running on rented instances. It receives tasks via POST, executes the command allowlist (§14.1.4), streams stdout/stderr back, and sends results to the Provisioner. The `onstart_*.sh` scripts are rendered as Vast.ai "on-start" scripts; they install Miniconda, create the environment, and download model weights.

### 15.2 Python Dependencies

```text
pydantic>=2.0
aiosqlite>=0.20.0
opentimelineio>=0.16.0
instructor>=1.0.0
openai>=1.0.0
httpx>=0.27.0
fastapi>=0.110.0
uvicorn>=0.29.0
python-multipart>=0.0.9
pydantic-deepagents>=0.1.0
uuid_extensions>=0.0.10
```

Install: `pip install -r requirements.txt`

### 15.3 API Key Management

| Key | Environment Variable | Used By |
|---|---|---|
| DeepSeek API | `DEEPSEEK_API_KEY` | All agents (LLM calls), parser |
| Vast.ai | `VASTAI_API_KEY` | Provisioner (VM allocation) |

Keys are read from environment variables at startup. Never commit keys to version control.

---

## 16. Glossary

### 16.1 Term Definitions

#### 16.1.1 All terms with precise definitions

| Term | Definition |
|------|------------|
| **Block** | A unit of work in the pipeline: one of `scenario`, `audio`, `video`, or `assembly`. Each block has a dedicated agent, VM type, and retry budget. |
| **Coordinator** | The central control process (2 vCPU, 4 GB RAM) that owns the event loop, watcher, and VM provisioner. Runs continuously on a fixed host. |
| **Deep agent** | A pydantic-deepagents main agent with capabilities (OTIOAwareCompactionCap) and sliding-window fallback. Not a subagent. |
| **Dual-threshold validation** | Assembly acceptance criteria requiring both a relative (15 %) and an absolute (0.25 s) duration check to pass. |
| **Effect** | A typed Pydantic model representing a pipeline mutation. The only legal way to change state. 30 types in 8 families. |
| **Emergent phase** | A descriptive pipeline label (SCRIPT, AUDIO_RECONCILE, etc.) that emerges from projection state, not enforced by any state machine. |
| **Event** | An immutable, append-only record describing a state change. Stored in SQLite with monotonic `sequence` and epoch `timestamp`. |
| **Event loop** | The coordinator's 10-second tick cycle: advance projections, run agents, check safety, sleep. |
| **Event store** | SQLite-backed append-only log of all domain events. Source of truth for pipeline state; no in-memory mirrors. |
| **Generation plan** | The JSON output of the ScenarioAgent containing shot list, speaker assignments, script, and duration targets per segment. |
| **LTX-Video** | The diffusion-based video-generation model running on the Video VM. Requires 48 GB VRAM at full precision. |
| **Projection** | A read-optimized Python dataclass built by folding (reducing) the event stream. Rebuilt incrementally on every tick. |
| **Prompt-based rules** | Prioritization and decision logic embedded in agent system prompts, not in Python code. |
| **Run** | A single end-to-end invocation of the pipeline for one screenplay, from approval through assembly to final MP4 delivery. |
| **Scenario** | A screenplay excerpt (typically 3–8 pages) selected for adaptation. The input artifact to the pipeline. |
| **SD-JSON** | "Screenplay Data — JSON". The normalized JSON representation of a screenplay after parsing, containing scenes, dialogue, and slug lines. |
| **Shot plan** | The per-segment visual plan generated by the ScenarioAgent: shot type, motion, characters, props, duration. |
| **Situation** | A narrative template describing a condition an agent should respond to (e.g., `fresh_dirty_block`, `vm_stale`). |
| **TTS** | Text-to-Speech. The Qwen3-TTS model running on the TTS VM that converts dialogue lines into character-specific WAV audio. |
| **VM agent** | The Python daemon (`vm/agent.py`) executing on rented GPU instances. Receives jobs, runs allowlisted commands, reports results. |
| **Vast.ai** | The cloud-GPU marketplace used for on-demand rental of TTS and Video VMs. Billed per-second. |
| **WhisperX** | The forced-alignment tool (via `whisperx` CLI) that produces word-level timestamps for transcript-to-audio synchronization. |

---

*V6 Architecture — pydantic-deepagents, agent-scans-projections, prompt-based rules, deterministic Provisioner, 10-second tick, no state machine.*
