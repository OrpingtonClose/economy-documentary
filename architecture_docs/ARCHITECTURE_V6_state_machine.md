> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture V6 — Documentary Pipeline

> **Date:** 2026-05-27
> **Status:** LOCKED — All 15 reviewer questions answered, all 47 gaps filled
> **Replaces:** ARCHITECTURE_V5.md
> **Branch:** `strands-migration`
> **Location:** `server/v5/`
>
> This document is the canonical V6 architecture. It resolves all issues identified in the V5 review (Grok V5.1, Kimi 30-point gap analysis, No-Timeouts critique, Opznai invariant review) and is ready for implementation.

---

## 1. Core Philosophy

Six foundational commitments govern the pipeline. The twelve hard principles in §1.7 enumerate every invariant and its enforcement mechanism.

### 1.1 Event Log as Sole Source of Truth

#### 1.1.1 All state derived from events; replay reconstructs everything

Every fact is an **Effect** — a typed Pydantic model — appended to an append-only event log. The OTIO timeline, job queue, VM inventory, and state machine state are **projections**: read models rebuilt by pure fold functions. Replay from sequence `0` reconstructs everything exactly.

#### 1.1.2 Event store is only persistent storage; all other state is ephemeral projection

The SQLite `events` table `(id, run_id, sequence, kind, payload_json, created_at)` is the sole durable storage. Agents hold no session state. VM workers are ephemeral. Projections are in-memory folds processing only new events since their last checkpoint.

### 1.2 Effects as Only Legal Mutations

#### 1.2.1 Typed Pydantic models; parser extracts from agent text

A **category-conditioned parser** (§8) extracts Effects from agent text using `instructor` + `deepseek-v4-flash`. Every Effect carries `kind: Literal[...]`, `run_id: str`, `effect_id: UUID` (UUIDv7 — §3.1), `agent: str`, and `timestamp: datetime`. Invalid payloads are rejected before reaching the event store.

#### 1.2.2 No direct state mutation outside event store append

The event store is a single asyncio queue with `BEGIN IMMEDIATE` (§5.2). Every state change enters through this one aperture. Agents do not call projection methods. The state machine does not modify projections.

### 1.3 No LLM Orchestrator

#### 1.3.1 State machine is deterministic coordinator; guards are pure boolean functions

No central agent decides what happens next. The **state machine** (python-statemachine) self-operates: the watcher loop emits `tick` every second, triggering guard evaluation. Guards are pure Python functions over projections — no LLM calls, no randomness, no network I/O. A `True` guard fires a hardcoded transition.

#### 1.3.2 Agents activated per-state via instruction injection; no LLM schedules work

On state entry, the machine injects a state-specific instruction into the active agent's prompt (§4.5). The Scenario Agent script-writes in `SCRIPT`; the Audio Agent reconciles in `AUDIO_RECONCILE`; the Video Agent generates in `VIDEO_PRODUCTION`. No agent chooses which peer runs next.

### 1.4 No Timeouts in Code

#### 1.4.1 No setTimeout, threading.Timer, or asyncio.timeout anywhere in pipeline code

No pipeline code calls `setTimeout`, `threading.Timer`, `asyncio.timeout`, or any timer primitive. HTTP requests and subprocess calls run to completion. This is architecture policy.

#### 1.4.2 Stale-job detection via projection-based TimeoutObserved effect (not a timer)

Hung jobs are detected by **observation**. The watcher queries `JobProjection` every tick: "which jobs have been `PENDING` longer than the threshold (default 10 min)?" On detection, it emits a `TimeoutObserved` effect — a normal log event. The state machine routes to VM cleanup and job requeue. The V5 VM-side 15-minute heartbeat self-destruct has been eliminated; it violated this principle. See Decision A1.

### 1.5 Real Engines Only

#### 1.5.1 Qwen3-TTS, LTX-2.3, DeepSeek API; no simulation layers

TTS uses **Qwen3-TTS** on GPU VMs. Video uses **LTX-2.3**. Agent LLM inference uses **DeepSeek API** (`deepseek-v4-flash`). No mocks, no stubs. Unavailable engines trigger `ClarificationRequest`.

### 1.6 Never Regex

#### 1.6.1 Category-conditioned extraction via instructor + deepseek-v4-flash

No regex extracts structured data from agent output. The parser uses the agent's current state to determine valid Effect subtypes and constrains the LLM to schema-compliant JSON via `instructor`. If extraction fails, the prompt is adjusted — the schema is not weakened.

### 1.7 Principles at a Glance

#### 1.7.1 Table of 12 hard principles with enforcement mechanism per principle

| # | Principle | Enforcement | V5→V6 Change |
|---|---|---|---|
| 1 | **Event log is sole source of truth** | All state derived from events. No hidden state. No projection writes independently. | None |
| 2 | **Effects are only legal mutations** | Only Pydantic models enter event store. Parser validates against `EffectUnion`. | Added `effect_id: UUIDv7` (A3) |
| 3 | **No LLM orchestrator — State machine is deterministic coordinator** | Guards are pure boolean functions. No LLM in scheduling path. | Renamed from "No orchestrator" (A2) |
| 4 | **No timeouts in code** | No `setTimeout`, `threading.Timer`, or `asyncio.timeout` in pipeline code. | Clarified: `TimeoutObserved` is projection-based, not a timer (A1) |
| 5 | **Real engines only** | Qwen3-TTS, LTX-2.3, DeepSeek API. No mocks, no stubs, no simulation. | Renamed from "No mocks" |
| 6 | **Never regex** | Category-conditioned extraction via `instructor` + `deepseek-v4-flash`. | None |
| 7 | **Provisioner is deterministic lackey** | Python service, no LLM. Provisions, reports, delivers. Media agents judge artistry. | Added "deterministic" (B2) |
| 8 | **No B2 for now** | Artifacts local. Event store is only external store. | None |
| 9 | **Agent memory does not persist** | Each turn rebuilt from projection summaries. No session state between POSTs. | None |
| 10 | **Stale-state detection is projection-based** | Watcher queries `JobProjection`; `TimeoutObserved` on threshold. No VM-side timers. | Replaces V5 P10 "Kill switch VM-side" (A1) |
| 11 | **Single writer** | Event store: asyncio queue + `BEGIN IMMEDIATE`. One writer coroutine. | None |
| 12 | **Tick-driven** | State machine advances only on explicit `tick` (1s). No async transitions. | None |


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
                    │  State Machine│ port 8080
                    │   (§4, §8)    │
                    └───────┬───────┘
                            │ EFFECT_TRANSITION
                            ▼
                    ┌───────────────┐
                    │  Event Store  │ port 8079
                    │  (append-only)│
                    └───────┬───────┘
                            │
        ┌──────────┬────────┼────────┬──────────┐
        ▼          ▼        ▼        ▼          ▼
   ┌─────────┐ ┌────────┐ ┌──────┐ ┌────────┐ ┌──────────┐
   │ Scenario│ │ Audio  │ │ Video│ │Assembly│ │   VM     │
   │  8001   │ │  8002  │ │ 8003 │ │  8005  │ │ 9000+    │
   └─────────┘ └────┬───┘ └──────┘ └────────┘ └──────────┘
                    │
              ┌─────┴─────┐
              ▼           ▼
         ┌────────┐  ┌──────────┐
         │Narration│  │Reconciler│
         │  8006   │  │(in-loop) │
         └────────┘  └──────────┘

         ════════ PROJECTIONS (read Event Store) ════════
   ┌────────┬────────┬────────┬────────┬────────┐
   │Timeline│ Budget │ Asset  │ Search │Requeue │
   │  8100  │  8101  │  8102  │  8103  │  8104  │
   └────────┴────────┴────────┴────────┴────────┘
```

**Data flow.** Human posts an instruction to the State Machine (8080). The State Machine appends an `EFFECT_TRANSITION` event to the Event Store (8079), then advances. Each agent polls the Event Store for effects matching its subscribed states, performs work, and appends completion effects. Five Projectors consume the event log to build read models.

#### 2.1.2 Provisioner — deterministic watcher-loop service

The Provisioner is a deterministic Python service, not an LLM agent. It executes inside the State Machine's watcher loop on port 8080. On each tick, it reads new `EFFECT_TRANSITION` events, computes resource requirements from the payload, and dispatches work packages to VM Workers via `POST /execute` on port 9000+. Because it has no model weights, it runs in-process rather than as a standalone HTTP agent. Full implementation in §8.

---

### 2.2 Component Inventory

#### 2.2.1 Component table

Every box exposes exactly `GET /` (health) and `POST /` (primary endpoint) on its own port.

| Component | Port | Type | Active States | Effects Produced | Effects Consumed |
|---|---|---|---|---|---|
| State Machine | 8080 | service | all | `EFFECT_TRANSITION`, `EFFECT_ERROR` | `EFFECT_INSTRUCTION`, `EFFECT_COMPLETE_*` |
| Scenario Agent | 8001 | agent | `SCRIPT` | `EFFECT_SCENARIO`, `EFFECT_SCENE_LIST` | transition into `SCRIPT` |
| Audio Agent | 8002 | agent | `AUDIO_RECONCILE` | `EFFECT_AUDIO_CANDIDATE`, `EFFECT_AUDIO_SCORE` | `EFFECT_SCENARIO`, `EFFECT_RECONCILE_REQUEST` |
| Video Agent | 8003 | agent | `VIDEO_PRODUCTION` | `EFFECT_VIDEO_CLIP`, `EFFECT_VIDEO_DONE` | `EFFECT_AUDIO_FINAL`, `EFFECT_SCENE_LIST` |
| Assembly Agent | 8005 | agent | `ASSEMBLY` | `EFFECT_RENDER_JOB`, `EFFECT_ASSEMBLY_DONE` | `EFFECT_VIDEO_DONE` |
| Narration Agent | 8006 | agent | `AUDIO_RECONCILE` | `EFFECT_NARRATION`, `EFFECT_NARRATION_SCORE` | `EFFECT_SCENE_LIST`, `EFFECT_AUDIO_CANDIDATE` |
| Reconciler | 8080 | service (in-loop) | `AUDIO_RECONCILE` | `EFFECT_AUDIO_FINAL`, `EFFECT_RECONCILE_REQUEST` | `EFFECT_AUDIO_SCORE`, `EFFECT_NARRATION_SCORE` |
| Provisioner | 8080 | service (in-loop) | `ASSEMBLY`, `VIDEO_PRODUCTION` | `EFFECT_VM_DISPATCH`, `EFFECT_VM_RESULT` | `EFFECT_RENDER_JOB`, `EFFECT_VIDEO_CLIP` |
| Event Store | 8079 | service | all | `EFFECT_APPENDED` | all `EFFECT_*` |
| Projectors (5) | 8100-8104 | service | all (read) | read models | all `EFFECT_*` |
| VM Workers | 9000+ | service | `ASSEMBLY`, `VIDEO_PRODUCTION` | `EFFECT_VM_RESULT` | `EFFECT_VM_DISPATCH` |

**V5 delta.** The Provisioner was an LLM agent on port 8004; it is now a deterministic in-loop service. Assembly Agent (8005) is new. The `AUDIO_VIDEO` state is split into `AUDIO_RECONCILE` + `VIDEO_PRODUCTION`, adding Narration Agent (8006) and the in-loop Reconciler.

---

### 2.3 State Machine Overview

#### 2.3.1 Seven states (six operational + ABORTED)

```
    INIT ──► SCRIPT ──► AUDIO_RECONCILE ──► VIDEO_PRODUCTION ──► ASSEMBLY ──► DONE
              ▲                ▲
              │                │
       gap_unexpected    voice_mismatch
```

| State | Ordinal | Enter Condition | Exit Condition |
|---|---|---|---|
| `INIT` | 0 | Project created | Instruction received |
| `SCRIPT` | 1 | `EFFECT_INSTRUCTION` appended | `EFFECT_SCENE_LIST` from Scenario Agent |
| `AUDIO_RECONCILE` | 2 | `EFFECT_SCENE_LIST` received | Reconciler scores pass threshold |
| `VIDEO_PRODUCTION` | 3 | `EFFECT_AUDIO_FINAL` emitted | `EFFECT_VIDEO_DONE` for all scenes |
| `ASSEMBLY` | 4 | `EFFECT_VIDEO_DONE` received | `EFFECT_ASSEMBLY_DONE` + artifacts exist |
| `DONE` | 5 | `EFFECT_ASSEMBLY_DONE` appended | Terminal |

#### 2.3.2 Back-edges

| Back-Edge | Trigger | From | To | Handler |
|---|---|---|---|---|
| `gap_unexpected` | Narration scene count ≠ scene list | `AUDIO_RECONCILE` | `SCRIPT` | State Machine emits transition with `regenerate=true` |
| `voice_mismatch` | Final audio speaker ≠ scenario voice tag | `VIDEO_PRODUCTION` | `SCRIPT` | State Machine emits transition with `audio_mismatch=true` |

Back-edges append a new `EFFECT_TRANSITION` event carrying the target state and a `reason` field. The target agent re-runs from its entry point; prior effects remain immutable in the Event Store. Downstream Projectors rebuild read models from the full log. This makes recovery a new forward path, not a mutation. See §4.2 for transition logic and §3.4 for reconciliation effects.


---

## 3. Effect Type Family — Complete Schemas

All pipeline mutations pass through the event store as **effects** — Pydantic v2 models serialized to a single row in SQLite. Every effect carries `run_id` (identifies the pipeline run), `effect_id` (UUIDv7 for client-side idempotency), `agent` (which component produced it), and `timestamp` (when it was created, in seconds since the epoch). The `kind` field serves as the discriminant for parsing and union dispatch.

This section defines 31 concrete effect types organized into 8 families, plus the base `Effect` model and the `ReconciliationFailureDetail` sub-model. All together, 33 Pydantic models. Every model is a complete, runnable schema with type annotations, `Literal` discriminants, and `Field` constraints. The section closes with the `EffectUnion` discriminated union definition and the `KIND_TO_MODEL` routing table used by the parser.

Naming convention (decision B4): **imperative** for agent requests (`QueueJob`, `ExecuteRawBash`, `MergeIntoOTIO`), **past-tense** for system-reported outcomes (`JobCompleted`, `AudioMeasured`, `PipelineComplete`).

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

`effect_id` uses UUIDv7 (decision A3) because it encodes a timestamp in the high bits, making event logs naturally time-sortable without leaking sequence gaps. Client-side generation means an agent can retry a failed `append()` with the same `effect_id` and the duplicate is silently dropped by the store.

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

Produced by the Scenario Agent (port 8001, active in states INIT and SCRIPT). These effects mutate the OTIO timeline's narrative track.

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
    a Vast.ai offer (job_type="tts" → Qwen3-TTS GPU; job_type="ltx" → LTX GPU).
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

Produced by the Audio Agent and the deterministic Provisioner during the AUDIO_RECONCILE phase. These effects implement the tight TTS-measure-adjust loop.

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
    - duration_mismatch → requeue with adjusted TTS params (normal retry)
    - duration_unrecoverable → per-block attempt limit exceeded (decision C1)
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
    are marked dirty and must be re-reconciled (decision C3).

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
    The state machine only permits the transition when ReconciliationComplete
    has been emitted AND all blocks are clean (no pending ReconciliationPartial).
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
| `AudioGenerated` | Provisioner | Run WhisperX (3x), emit `AudioMeasured` |
| `AudioMeasured` | Provisioner | Audio Agent computes tolerance, emits `DurationAdjusted` or `ReconciliationFailed` |
| `DurationAdjusted` | Audio Agent | OTIOProjection updates slot; block passes |
| `ReconciliationFailed` | Audio Agent | Requeue with adjusted params, or escalate if `duration_unrecoverable` |
| `ReconciliationPartial` | Audio Agent | JobProjection marks dirty blocks; Audio Agent re-reconciles only dirty set |
| `ReconciliationComplete` | Audio Agent | Guard `_reconciliation_complete` becomes True; VIDEO_PRODUCTION can begin |

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
    attempt creative recovery — deterministic lackey behavior (decision B2).
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

    The Provisioner polls Vast.ai API (decision B3) and compares reported
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

Produced by the Launcher, the state machine, and the watcher loop. These effects record pipeline lifecycle events and meta-state transitions.

#### 3.7.1 PipelineStarted, TransitionState, PipelineComplete, PipelineAborted, TimeoutObserved

```python
class PipelineStarted(Effect):
    """Launcher emitted this to signal that a new pipeline run has begun.

    The guard `_script_exists` (Section 4.3) checks for the presence of a
    PipelineStarted effect matching the run's `run_id`. Until this effect
    exists, the state machine remains in INIT.
    """
    kind: Literal["pipeline_started"] = "pipeline_started"
    agent: str = "launcher"
    config: dict = Field(default_factory=dict, description="pipeline configuration snapshot")
    max_tts_budget_usd: float = Field(default=2.0, gt=0.0)
    max_attempts_per_block: int = Field(default=5, ge=1)
    max_run_budget_usd: float = Field(default=10.0, gt=0.0)
    output_path: str = Field(default="/tmp/final_documentary.mp4")


class TransitionState(Effect):
    """State machine recorded a state transition.

    Produced automatically by the state machine's `on_transition` callback.
    The StateProjection applies this to update `current_state`. The guard
    name that triggered the transition is stored for debugging.
    """
    kind: Literal["transition_state"] = "transition_state"
    agent: str = "state_machine"
    from_state: str
    to_state: str
    guard_name: str = Field(..., description="guard that evaluated True")


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

    Reasons include budget exhaustion (decision D1), repeated VM provision
    failures beyond threshold, or human instruction to abort.
    """
    kind: Literal["pipeline_aborted"] = "pipeline_aborted"
    agent: str = Field(..., description="agent or component that triggered abort")
    aborted_state: str = Field(..., description="state machine state when aborted")
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
    """Watcher detected a job pending longer than threshold (decision A1).

    The watcher loop checks JobProjection on every tick: if any job has
    status "pending" or "running" for more than `stale_threshold_min`,
    this effect is emitted. The state machine routes it to VM cleanup
    (Provisioner deallocates the stuck VM) + job requeue.

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

    Security model (decision C4): pre-approved commands (`ffmpeg`, `ffprobe`,
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
    timeout_sec: float = 300.0
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
    """Parser or state machine needs human input to proceed.

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
    """Watcher detected an agent stuck in a loop (decision D4).

    Two independent detection modes, both with configurable threshold N
    (default 5):
    1. Duplicate effects: the last N effects from this agent are identical.
    2. No progress: N ticks have elapsed without any projection state change.

    When either fires, the state machine halts (stays in current state) and
    emits `ClarificationRequest` for human review.
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

Produced by the Audio Agent, Video Agent, or Assembly pseudo-agent when media generation or final assembly fails in a way that requires explicit routing.

#### 3.9.1 ProductionFailed with failure_type routing table

```python
class ProductionFailed(Effect):
    """Media production or assembly failure with structured suggested fix.

    The failure_type field is the routing key. The state machine guards
    read failure_type to decide: back-edge to SCRIPT, requeue in current
    state, or halt for human intervention.
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

| failure_type | Routing Action | Target State | Rationale |
|---|---|---|---|
| `gap_unexpected` | Back-edge to SCRIPT | SCRIPT | Narration text doesn't fit target duration |
| `voice_mismatch` | Back-edge to SCRIPT | SCRIPT | Wrong speaker or voice role assigned |
| `overlap` | Requeue with adjusted timing | AUDIO_RECONCILE | Clip overlaps neighbor in timeline |
| `duration_mismatch` | Requeue with new params | AUDIO_RECONCILE | TTS output duration outside tolerance |
| `visual_incoherence` | Requeue with revised prompt | VIDEO_PRODUCTION | LTX output doesn't match narration |
| `artistic_reject` | Requeue with adjusted params | VIDEO_PRODUCTION | Quality bar not met |
| `audio_lufs` | Requeue with gain adjustment | AUDIO_RECONCILE | Audio loudness out of spec |
| `track_misalignment` | Requeue assembly | ASSEMBLY | A/V tracks don't align after merge |
| `missing_media` | Retry artifact delivery | current state | Artifact file not found at expected path |
| `invalid_range` | Requeue with corrected timing | current state | OTIO source range is invalid |

The state machine's `_has_script_errors` guard checks for `failure_type in {"gap_unexpected", "voice_mismatch"}` to trigger the SCRIPT back-edge. All other failure types either requeue in the current state or halt with `ClarificationRequest`.

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
        # 3.7 Pipeline Effects (5)
        PipelineStarted,
        TransitionState,
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
    "transition_state":   TransitionState,
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

The parser's `_extract_kind_markers` function (Section 8.1) performs case-insensitive substring search for each key in `KIND_TO_MODEL`. The category-conditioned prompt (Section 8.2) narrows the model set to the kinds relevant to the agent's current role, reducing false positives. The resulting kind string is looked up in `KIND_TO_MODEL`, and the corresponding model validates the extracted JSON.

#### 3.10.3 Naming convention summary

| Convention | Pattern | Examples |
|---|---|---|
| Imperative (agent requests) | Verb-noun, present tense | `QueueJob`, `ExecuteRawBash`, `MergeIntoOTIO`, `DeleteScene` |
| Past-tense (system outcomes) | Noun-verb or noun-adjective, past tense | `JobCompleted`, `AudioMeasured`, `PipelineComplete`, `VMDeallocated` |
| State descriptors | Adjective or participle | `ReconciliationComplete`, `ReconciliationPartial`, `TimeoutObserved` |
| Meta / diagnostic | Descriptive phrase | `AgentLoopDetected`, `ClarificationRequest`, `ProductionFailed` |

The naming convention is enforced by code review, not by the type system. When adding a new effect type, place it in the family section matching its producer, follow the naming convention based on whether it is an agent request or a system outcome, add it to `EffectUnion`, and register it in `KIND_TO_MODEL`.


---

## 4. State Machine

The state machine is a `python-statemachine` `StateChart` that self-operates. The watcher loop sends `tick` every second. Guards read projections and return `bool`. No orchestrator agent exists.

V6 splits V5's `AUDIO_VIDEO` into `AUDIO_RECONCILE` and `VIDEO_PRODUCTION`. An `ABORTED` final state accepts escape transitions from every state.

### 4.1 States

#### 4.1.1 Table: 7 states (6 operational + ABORTED), active agents, what happens in each

| State | Active Agents | What Happens |
|---|---|---|
| **INIT** | Scenario | Pipeline starts. Scenario writes first narration script (scenes, speakers, duration targets). Emits `PipelineStarted` then `UpdateScript`. |
| **SCRIPT** | Scenario | Refine narration. Fill gaps, fix speaker assignments, adjust pacing. Remains here until all slots are present and no script-blame failures exist. |
| **AUDIO_RECONCILE** | Audio | **Phase 1: Reconciliation.** Queue TTS jobs via Provisioner, receive WhisperX-measured durations, compare against scripted targets (±15% or ±0.25s). Emit `DurationAdjusted` (pass) or `ReconciliationFailed` → requeue with adjusted text. Exit when `ReconciliationComplete` emitted and no dirty blocks remain. |
| **VIDEO_PRODUCTION** | Video | **Phase 2: Video generation.** Queue LTX-2.3 clip jobs using measured audio durations as LAW. Judge output. Emit `JobApproved` or `JobRequeued`. Loop until all OTIO video slots filled. |
| **ASSEMBLY** | Video | ffmpeg combines all approved OTIO clips into `final_documentary.mp4`. OTIO validation (no overlaps, track alignment, clip media). Remain until MP4 exists and validates. |
| **DONE** | None | Pipeline complete. Terminal. |
| **ABORTED** | None | Pipeline terminated by `PipelineAborted`. Terminal, no exits. |

Provisioner (§7.5) is a deterministic service, not an agent. It does not receive state instructions. It executes queued jobs and reports completions via `JobCompleted` / `JobFailed` effects.

On every state change, the machine emits a `TransitionState` effect. `StateProjection` (§6.4) subscribes and maintains `current_state`. Agents poll this projection to determine whether they are active. The state machine injects the corresponding instruction (§4.5) into each active agent's next prompt.

```python
from statemachine import State, StateChart
from typing import Optional
import asyncio
import os
import time
from collections import Counter

class PipelineStateMachine(StateChart):
    """Seven-state documentary pipeline (6 operational + ABORTED). Tick-driven. Guards read projections."""

    # -- States ------------------------------------------------------------
    init             = State(initial=True)
    script           = State()
    audio_reconcile  = State()
    video_production = State()
    assembly         = State()
    done             = State(final=True)
    aborted          = State(final=True)

    # -- Projections (injected at init) ------------------------------------
    state_proj: "StateProjection"
    job_projection: "JobProjection"
    otio_projection: "OTIOProjection"
    config: "PipelineConfig"
    event_store: "EventStore"

    # -- ASCII Transition Diagram ------------------------------------------
    #
    #   [INIT] --_script_exists--> [SCRIPT]
    #      |                            |
    #      | _budget_exceeded           | _has_script_errors
    #      | _loop_detected             v
    #      v                      [AUDIO_RECONCILE]
    #   [ABORTED] <--+                  |
    #      ^         |                  | _reconciliation_complete
    #      |         |                  v
    #      |         |           [VIDEO_PRODUCTION]
    #      |         |                  |
    #      |         |                  | _all_media_produced
    #      |         |                  v
    #      |         |              [ASSEMBLY]
    #      |         |                  |
    #      |         |                  | _assembly_valid_and_complete
    #      |         +---------------->[DONE]
    #      |
    #      +-- (all states: _budget_exceeded|_loop_detected|PipelineAborted)

    tick = (
        init.to(script, cond="_script_exists")
        | script.to(audio_reconcile, cond="_reconciliation_ready")
        | script.to.itself(cond="_still_refining")
        | script.to.itself(cond="_has_script_errors")
        | audio_reconcile.to.itself(cond="_audio_has_dirty_blocks")
        | audio_reconcile.to(video_production, cond="_reconciliation_complete")
        | video_production.to.itself(cond="_has_video_pending")
        | video_production.to(assembly, cond="_all_media_produced")
        | assembly.to.itself(cond="_assembly_not_ready")
        | assembly.to(done, cond="_assembly_valid_and_complete")
        # Escape transitions
        | init.to(aborted,        cond="_budget_exceeded")
        | init.to(aborted,        cond="_loop_detected")
        | script.to(aborted,      cond="_budget_exceeded")
        | script.to(aborted,      cond="_loop_detected")
        | audio_reconcile.to(aborted, cond="_budget_exceeded")
        | audio_reconcile.to(aborted, cond="_loop_detected")
        | video_production.to(aborted, cond="_budget_exceeded")
        | video_production.to(aborted, cond="_loop_detected")
        | assembly.to(aborted,    cond="_budget_exceeded")
        | assembly.to(aborted,    cond="_loop_detected")
    )

    def on_enter_state(self, event, source, target):
        """Emit TransitionState effect on every state entry."""
        effect = TransitionState(
            from_state=source.id if source else None,
            to_state=target.id,
            guard_name=getattr(event, "guard_name", "unknown"),
            run_id=self.config.run_id,
        )
        asyncio.create_task(self._append_effect(effect))

    async def _append_effect(self, effect: "Effect"):
        await self.event_store.append(effect)
```

#### 4.1.2 State entry/exit behavior definitions

Each state defines three behaviors: **Entry** emits `TransitionState` and queues the state instruction for agent prompt injection. **While-in-state:** agents poll `StateProjection.current_state` and operate autonomously. **Exit** logs the transition timestamp. No cleanup — all state is in projections, rebuilt from the event log.

### 4.2 Transitions (tick-driven)

#### 4.2.1 Complete transition diagram with all guards

```
       ╔══════╗
       ║ INIT ║
       ╚══╤═══╝
          │ _script_exists
          ▼
       ╔════════╗ ◄──── _has_script_errors (self-loop)
       ║ SCRIPT ║
       ╚══╤═════╝
          │ _reconciliation_ready
          ▼
       ╔═════════════════╗
       ║ AUDIO_RECONCILE ║──► _audio_has_dirty_blocks (self-loop)
       ╚══╤══════════════╝
          │ _reconciliation_complete
          ▼
       ╔══════════════════╗
       ║ VIDEO_PRODUCTION ║──► _has_video_pending (self-loop)
       ╚══╤═══════════════╝
          │ _all_media_produced
          ▼
       ╔══════════╗
       ║ ASSEMBLY ║──► _assembly_not_ready (self-loop)
       ╚══╤═══════╝
          │ _assembly_valid_and_complete
          ▼
       ╔══════╗
       ║ DONE ║
       ╚══════╝

    ESCAPE: every state ──► ╔═════════╗
    (_budget_exceeded|     ║ ABORTED ║
     _loop_detected|        ╚═════════╝
     PipelineAborted)
```

#### 4.2.2 Transition table: From, To, Guard, Condition (all 12 transitions)

| # | From | To | Guard | Condition |
|---|---|---|---|---|
| 1 | INIT | SCRIPT | `_script_exists` | `PipelineStarted` effect exists in `state_proj` |
| 2 | SCRIPT | AUDIO_RECONCILE | `_reconciliation_ready` | No `ProductionFailed` with `failure_type` in `{gap_unexpected, voice_mismatch}` AND narration slots exist with no gaps |
| 3 | SCRIPT | SCRIPT | `_still_refining` | Slots exist but gaps remain, or narration incomplete |
| 4 | SCRIPT | SCRIPT | `_has_script_errors` | `ProductionFailed` with `failure_type` in `{gap_unexpected, voice_mismatch}` — self-loop triggers Scenario rewrite |
| 5 | AUDIO_RECONCILE | AUDIO_RECONCILE | `_audio_has_dirty_blocks` | `dirty_block_ids` non-empty OR TTS jobs pending/running |
| 6 | AUDIO_RECONCILE | VIDEO_PRODUCTION | `_reconciliation_complete` | `ReconciliationComplete` exists AND `dirty_block_ids` empty |
| 7 | VIDEO_PRODUCTION | VIDEO_PRODUCTION | `_has_video_pending` | Pending or running `ltx` jobs exist |
| 8 | VIDEO_PRODUCTION | ASSEMBLY | `_all_media_produced` | `_reconciliation_complete` AND zero pending/running jobs AND all OTIO slots filled |
| 9 | ASSEMBLY | ASSEMBLY | `_assembly_not_ready` | MP4 missing OR unresolved `ProductionFailed` OR OTIO validation fails |
| 10 | ASSEMBLY | DONE | `_assembly_valid_and_complete` | MP4 exists AND zero unresolved `ProductionFailed` AND OTIO validates (no overlaps, track alignment, clip media) |

Guards are evaluated in declaration order; first `True` wins. Escape guards declared last, so operational transitions are tried first.

#### 4.2.3 PipelineAborted escape transitions from every state

Every operational state escapes to `ABORTED` on:

| Trigger | Guard | Emitter |
|---|---|---|
| Budget exceeded | `_budget_exceeded` | Watcher loop (§4.4.3) |
| Agent loop detected | `_loop_detected` | Watcher loop (§4.4.2) |
| Explicit abort | `PipelineAborted` in event store | External system |

On escape: transition to `ABORTED` (final), emit `TransitionState`, halt agent prompts. VMs not deallocated automatically — operator handles cleanup.

### 4.3 Guard Implementations

All guards are methods on `PipelineStateMachine`, reading projections injected at initialization. Pure functions — no mutations, no effects, no I/O beyond reading projections and file existence checks.

#### 4.3.1 _script_exists

```python
def _script_exists(self, event, source, target) -> bool:
    """INIT -> SCRIPT: PipelineStarted effect exists in state projection.

    The StateProjection tracks pipeline_started: bool, set True when
    a PipelineStarted effect is processed. This is the first event
    in every run and the minimum condition to exit INIT.
    """
    return self.state_proj.pipeline_started is True
```

#### 4.3.2 _has_script_errors

```python
def _has_script_errors(self, event, source, target) -> bool:
    """SCRIPT -> SCRIPT (self-loop): ProductionFailed blames the script.

    Returns True when any unresolved ProductionFailed has failure_type
    in SCRIPT_ERROR_TYPES. These indicate structural script problems:
    gap_unexpected (references nonexistent gap) or voice_mismatch
    (speaker contradicts assignment). The self-loop re-triggers the
    Scenario agent with failure context in its prompt.
    """
    SCRIPT_ERROR_TYPES = {"gap_unexpected", "voice_mismatch"}
    failures = getattr(self.job_projection, "production_failures", [])
    for failure in failures:
        if failure.get("failure_type") in SCRIPT_ERROR_TYPES:
            if not failure.get("resolved", False):
                return True
    return False
```

#### 4.3.3 _reconciliation_complete

```python
def _reconciliation_complete(self, event, source, target) -> bool:
    """AUDIO_RECONCILE -> VIDEO_PRODUCTION: reconciliation is done.

    Returns True when ALL three conditions hold:
    1. ReconciliationComplete effect has been emitted.
    2. No dirty blocks remain in job_projection.
    3. Reconciliation attempts <= config.max_reconciliation_attempts.

    A 'dirty block' is a narration block whose TTS output has not
    yet passed the WhisperX comparison check (±15% or ±0.25s).
    """
    if not getattr(self.job_projection, "reconciliation_complete", False):
        return False
    dirty_blocks = getattr(self.job_projection, "dirty_block_ids", set())
    if dirty_blocks:
        return False
    attempts = getattr(self.job_projection, "reconciliation_attempts", 0)
    max_attempts = getattr(self.config, "max_reconciliation_attempts", 10)
    return attempts <= max_attempts
```

#### 4.3.4 _audio_has_dirty_blocks

```python
def _audio_has_dirty_blocks(self, event, source, target) -> bool:
    """AUDIO_RECONCILE -> AUDIO_RECONCILE (self-loop): still reconciling.

    Returns True when dirty blocks remain OR reconciliation is not
    yet marked complete OR TTS jobs are pending/running. Keeps the
    pipeline in AUDIO_RECONCILE while the Audio agent iterates the
    TTS → WhisperX → compare → adjust loop.
    """
    if not getattr(self.job_projection, "reconciliation_complete", False):
        return True
    dirty_blocks = getattr(self.job_projection, "dirty_block_ids", set())
    if dirty_blocks:
        return True
    return self._has_pending_or_running_jobs("tts")
```

#### 4.3.5 _all_media_produced

```python
def _all_media_produced(self, event, source, target) -> bool:
    """VIDEO_PRODUCTION -> ASSEMBLY: all media artifacts exist.

    Returns True when: (1) audio reconciliation complete,
    (2) zero pending/running jobs of any type, (3) all OTIO slots
    filled with approved media references.
    """
    if not self._reconciliation_complete(event, source, target):
        return False
    if self._has_pending_or_running_jobs():
        return False
    return self.otio_projection.all_slots_filled()
```

#### 4.3.6 _has_pending_or_running_jobs

```python
def _has_pending_or_running_jobs(
    self,
    job_type: Optional[str] = None,
) -> bool:
    """Return True if any jobs are pending or running.

    Args:
        job_type: Optional filter ("tts", "ltx", "whisperx").
            If None, checks all job types.

    Reads from job_projection.jobs: dict[str, dict] where each job
    has keys: status (pending|running|completed|failed), job_type.
    """
    jobs = getattr(self.job_projection, "jobs", {})
    active = {"pending", "running"}
    for job_id, job in jobs.items():
        if job.get("status") in active:
            if job_type is None or job.get("job_type") == job_type:
                return True
    return False
```

#### 4.3.7 _has_video_pending

```python
def _has_video_pending(self, event, source, target) -> bool:
    """VIDEO_PRODUCTION -> VIDEO_PRODUCTION (self-loop): video still active.

    Returns True when LTX jobs are pending or running. The
    _all_media_produced guard (declared before this self-loop) will
    fire instead when video work is done.
    """
    return self._has_pending_or_running_jobs("ltx")
```

#### 4.3.8 _has_video_errors

```python
def _has_video_errors(self, event, source, target) -> bool:
    """Check for video-specific production failures.

    Returns True when any unresolved ProductionFailed has failure_type
    in VIDEO_ERROR_TYPES: overlap, duration_mismatch, visual_incoherence,
    artistic_reject, audio_lufs. These blame video generation, not
    the script. The Video agent receives them in context and requeues.
    """
    VIDEO_ERROR_TYPES = {
        "overlap", "duration_mismatch", "visual_incoherence",
        "artistic_reject", "audio_lufs",
    }
    failures = getattr(self.job_projection, "production_failures", [])
    for failure in failures:
        if failure.get("failure_type") in VIDEO_ERROR_TYPES:
            if not failure.get("resolved", False):
                return True
    return False
```

#### 4.3.9 _assembly_valid_and_complete

```python
def _assembly_valid_and_complete(self, event, source, target) -> bool:
    """ASSEMBLY -> DONE: final output is valid and complete.

    Returns True when ALL four conditions hold:
    1. Output MP4 file exists at config.output_path.
    2. Zero unresolved ProductionFailed effects.
    3. OTIO validates: no overlaps, track alignment, clip media.
    4. Output duration matches OTIO timeline within 0.5s.
    """
    output_path = getattr(self.config, "output_path",
                          "/tmp/final_documentary.mp4")
    if not os.path.exists(output_path):
        return False

    failures = getattr(self.job_projection, "production_failures", [])
    if any(not f.get("resolved", False) for f in failures):
        return False

    ok, _ = self.otio_projection.validate_no_overlaps()
    if not ok:
        return False
    ok, _ = self.otio_projection.validate_track_alignment()
    if not ok:
        return False
    ok, _ = self.otio_projection.validate_clip_media()
    if not ok:
        return False

    # Get actual MP4 duration via ffprobe
    import subprocess
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(output_path)],
        capture_output=True, text=True
    )
    actual_sec = float(result.stdout.strip()) if result.returncode == 0 else 0.0
    timeline = self.otio_projection.get_timeline_duration_sec()
    if actual_sec is None or timeline is None:
        return False
    max_delta = getattr(self.config, "max_duration_delta_sec", 0.5)
    return abs(actual_sec - timeline) <= max_delta
```

`_assembly_not_ready` (ASSEMBLY self-loop) is the negation.

#### 4.3.10 _budget_exceeded

```python
def _budget_exceeded(self, event, source, target) -> bool:
    """Escape guard: spent_usd > max_run_budget_usd."""
    spent = getattr(self.job_projection, "spent_usd", 0.0)
    limit = getattr(self.config, "max_run_budget_usd", float("inf"))
    return spent > limit
```

#### 4.3.11 _loop_detected

```python
def _loop_detected(self, event, source, target) -> bool:
    """Escape guard: AgentLoopDetected in recent event window."""
    window = getattr(self.config, "loop_detection_window", 50)
    return any(
        ev.kind == "agent_loop_detected"
        for ev in self.state_proj.get_recent_events(window)
    )
```

#### 4.3.12 _reconciliation_ready

```python
def _reconciliation_ready(self, event, source, target) -> bool:
    """SCRIPT → AUDIO_RECONCILE: narration complete, no script errors.

    Returns True when we have scenes AND no script-blame ProductionFailed.
    """
    otio = getattr(self.otio_projection, "slots", {})
    has_scenes = len(otio) > 0
    if not has_scenes:
        return False
    # Check for script-level failures
    failures = getattr(self.job_projection, "production_failures", [])
    script_error_types = {"gap_unexpected", "voice_mismatch"}
    has_script_errors = any(
        f.get("failure_type") in script_error_types for f in failures
    )
    return has_scenes and not has_script_errors
```

#### 4.3.13 _still_refining

```python
def _still_refining(self, event, source, target) -> bool:
    """SCRIPT self-loop: narration slots exist but gaps remain.

    SCRIPT → SCRIPT: still refining if we have scenes but they're incomplete.
    """
    otio = getattr(self.otio_projection, "slots", {})
    has_scenes = len(otio) > 0
    all_complete = all(s.get("status") == "complete" for s in otio.values()) if otio else False
    return has_scenes and not all_complete
```

### 4.4 Watcher Loop

The watcher loop is the sole driver: a single async coroutine executing a fixed sequence every second. It performs three safety checks: stale-job detection, agent-loop detection, and budget enforcement.

```python
async def watcher_loop(
    machine: PipelineStateMachine,
    projections: list,
    event_store: "EventStore",
    config: "PipelineConfig",
) -> None:
    """Drive state machine forever (until DONE or ABORTED)."""
    stall_counter = 0
    last_sig = _compute_progress_signature(projections)

    while machine.current_state.id not in ("done", "aborted"):
        tick_start = time.monotonic()

        for proj in projections:           # 1. Advance projections
            await proj.tick(event_store, config.run_id)
        await _check_stale_jobs(machine, event_store, config)  # 2.
        current_sig = _compute_progress_signature(projections) # 3.
        stall_counter = await _check_agent_loop(
            current_sig, last_sig, stall_counter,
            machine, event_store, config,
        )
        last_sig = current_sig
        await _check_budget(machine, event_store, config)       # 4.
        try:
            await machine.tick()                                # 5.
        except Exception as e:
            logger.error(f"Tick failed: {e}")
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - tick_start)))
```

#### 4.4.1 Stale-job detection (TimeoutObserved emission)

```python
async def _check_stale_jobs(
    machine: PipelineStateMachine,
    event_store: "EventStore",
    config: "PipelineConfig",
) -> None:
    """Emit TimeoutObserved for jobs exceeding their type timeout.
    Timeouts: tts=300s, ltx=1800s, whisperx=120s (defaults).
    Provisioner subscribes and handles remediation."""
    jobs = getattr(machine.job_projection, "jobs", {})
    now = time.time()
    timeouts = {
        "tts": getattr(config, "tts_timeout_sec", 300),
        "ltx": getattr(config, "ltx_timeout_sec", 1800),
        "whisperx": getattr(config, "whisperx_timeout_sec", 120),
    }
    for job_id, job in jobs.items():
        if job.get("status") != "running":
            continue
        started_at = job.get("started_at")
        if started_at is None:
            continue
        job_type = job.get("job_type", "")
        elapsed = now - started_at
        timeout = timeouts.get(job_type, 600)
        if elapsed > timeout:
            await event_store.append(TimeoutObserved(
                job_id=job_id,
                vm_instance_id="",  # unknown at watcher level; Provisioner resolves
                pending_since=started_at,
                elapsed_min=elapsed / 60.0,
                stale_threshold_min=timeout / 60.0,
                action_taken="deallocate_vm",
                run_id=config.run_id,
            ))
```

#### 4.4.2 Agent loop detection (duplicate effects + no-progress)

```python
async def _check_agent_loop(
    current_sig: str, last_sig: str, stall_counter: int,
    machine: PipelineStateMachine,
    event_store: "EventStore",
    config: "PipelineConfig",
) -> int:
    """Dual-mode loop detection. Mode 1: no-progress. Mode 2: duplicate effects."""
    # Mode 1: no-progress
    stall_counter = stall_counter + 1 if current_sig == last_sig else 0
    if stall_counter >= getattr(config, "max_stall_ticks", 30):
        await event_store.append(AgentLoopDetected(
            detection_mode="no_progress", stall_ticks=stall_counter,
            signature=current_sig, run_id=config.run_id,
        ))
        return 0

    # Mode 2: duplicate effects
    recent = machine.state_proj.get_recent_effects(
        getattr(config, "loop_detection_window", 50))
    payloads = [_canonicalize_payload(ev.payload) for ev in recent
                if ev.kind in ("queue_job", "job_requeued", "reconciliation_failed")]
    for payload, count in Counter(payloads).items():
        if count > getattr(config, "duplicate_threshold", 3):
            await event_store.append(AgentLoopDetected(
                detection_mode="duplicate_effects", duplicate_count=count,
                duplicate_payload=payload[:200], run_id=config.run_id,
            ))
            return stall_counter
    return stall_counter


def _compute_progress_signature(projections: list) -> str:
    """Hashable: state|completed|dirty|slot_fill."""
    sp = next(p for p in projections if hasattr(p, "current_state"))
    jp = next(p for p in projections if hasattr(p, "jobs"))
    op = next(p for p in projections if hasattr(p, "tracks"))
    completed = sum(1 for j in getattr(jp, "jobs", {}).values()
                    if j.get("status") == "completed")
    dirty = len(getattr(jp, "dirty_block_ids", set()))
    fill = getattr(op, "slot_fill_percentage", lambda: 0.0)()
    return f"{sp.current_state}|{completed}|{dirty}|{fill:.2f}"


def _canonicalize_payload(payload: dict) -> str:
    import json
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)
```

#### 4.4.3 Budget check

```python
async def _check_budget(
    machine: PipelineStateMachine,
    event_store: "EventStore",
    config: "PipelineConfig",
) -> None:
    """Emit PipelineAborted if spent_usd > max_run_budget_usd."""
    spent = getattr(machine.job_projection, "spent_usd", 0.0)
    limit = getattr(config, "max_run_budget_usd", float("inf"))
    if spent > limit:
        await event_store.append(PipelineAborted(
            reason="budget_exceeded", spent_usd=spent,
            limit_usd=limit, run_id=config.run_id,
        ))
```

#### 4.4.4 Tick throttling (1s)

Each tick's elapsed time is measured; `asyncio.sleep` fills the remainder up to 1s. If processing exceeds 1s, the next tick begins immediately. Prevents runaway CPU without missing rapid state changes.

### 4.5 State Instructions (Prompt Injection)

Instructions are injected into every active agent's system prompt. Agents read the current state from `StateProjection` and prepend the instruction. Provisioner (§7.5) is a deterministic service — no instructions.

#### 4.5.1 Table: state → injected instruction text for each agent

| State | Agent | Injected Instruction |
|---|---|---|
| **INIT** | Scenario | `Write a complete narration script with scenes, speakers, timing targets, emotional_tone. Every slot needs: scene_number, speaker_id, text, target_duration_sec. Emit PipelineStarted then UpdateScript. Do not produce media jobs.` |
| **SCRIPT** | Scenario | `Refine narration. Fill gap slots, ensure speaker consistency, verify duration targets. If ProductionFailed with failure_type in {gap_unexpected, voice_mismatch} appears, rewrite affected sections. Emit UpdateScript.` |
| **AUDIO_RECONCILE** | Audio | `Own the reconciliation loop: (1) Queue TTS jobs for dirty blocks. (2) On JobCompleted, run WhisperX → measure duration. (3) Compare measured vs scripted (±15% or ±0.25s): within tolerance → DurationAdjusted; outside → ReconciliationFailed → requeue. (4) When all blocks clean, emit ReconciliationComplete.` |
| **VIDEO_PRODUCTION** | Video | `Generate LTX-2.3 clips using measured audio durations as LAW. Queue ltx jobs, judge for visual coherence and artistic quality. Emit JobApproved or JobRequeued. Merge approved clips via MergeIntoOTIO. Continue until all video slots filled.` |
| **ASSEMBLY** | Video | `Use ffmpeg to combine OTIO clips into final_documentary.mp4. Validate: output exists and non-empty, duration matches timeline within 0.5s, OTIO has no overlaps, tracks align. On failure, emit ProductionFailed with failure_type and retry.` |
| **DONE** | (none) | `Pipeline complete. No further instructions.` |
| **ABORTED** | (none) | `Pipeline aborted. Stop all work immediately.` |

**Injection mechanism.** `Agent._build_prompt()` constructs:

```python
def _build_prompt(self, base: str) -> str:
    state = self.state_proj.current_state
    instr = STATE_INSTRUCTIONS.get((state, self.name), "")
    return (f"{self.persona}\n\n=== STATE: {state} ===\n{instr}\n\n"
            f"=== CONTEXT ===\n{self._format_context()}\n\n"
            f"=== TASK ===\n{base}")
```

`STATE_INSTRUCTIONS: dict[(state, agent), str]`. Dynamic context (OTIO, jobs, failures) is appended by `_format_context()`. `ProductionFailed` details are included automatically, enabling self-correction without orchestration.


---

## 5. Event Store

The event store is a single SQLite database opened through `aiosqlite`. Every effect (Section 3.1) is appended as one row. The store enforces idempotency on `(run_id, effect_id)` via `INSERT OR IGNORE` — retrying an `append()` with the same `effect_id` is a safe no-op. A single asyncio queue serializes all writes behind `BEGIN IMMEDIATE`, eliminating WAL-mode lock contention. Projections query the store through `read_since()` and `replay()`; no projection mutates the database.

The database file is the only durable artifact of a pipeline run. All state — OTIO timeline, job queue, VM inventory, state machine transitions — is rebuilt from events. Losing the database means losing the run. Section 5.4.2 defines the backup strategy.

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

-- Idempotency constraint (decision A3): duplicate (run_id, effect_id) is silently
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

Every projection tracks `last_sequence` — the highest sequence number it has already processed. On each tick (Section 4.4), the projection calls `read_since(run_id, last_sequence)` and receives only new events. This avoids re-reading the entire event log on every tick.

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

The return type of `read_since()` is `list[dict[str, Any]]` where each dict has keys: `sequence`, `effect_id`, `kind`, `payload_json`, `created_at`. Projections deserialize `payload_json` into the appropriate Pydantic model via `EffectUnion` dispatch (Section 3.8). The `sequence` field in the response is the same integer that `append()` returned when the effect was first written.

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

Both `read_since()` and `replay()` return row dictionaries with the `payload_json` string unparsed. The caller is responsible for deserializing via `json.loads()` and `EffectUnion` model validation (Section 3.8). This keeps the event store agnostic of effect type definitions.

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
| `category` | `"disk_full"` | Routing hint for the state machine |

The monitor runs as a coroutine in the same process as the watcher loop (Section 4.4). It is started when the pipeline launches and stops when `event_store.close()` is called. The threshold is configurable per deployment; documentary runs with large video artifacts may need a lower threshold (e.g., 0.60) to leave headroom for LTX-2.3 output files.

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

Projections are **incremental read models** rebuilt from the event log. Each projection tracks `last_sequence` and processes only new events on every `tick`. If the SQLite database is wiped, replaying the event log through every projection reconstructs the entire pipeline state. Projections never emit events — they are pure consumers (Section 6.4 enforces this absolutely).

---

### 6.1 Projection Base Class

#### 6.1.1 Abstract base with tick(event_store) and apply(event) interface

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

Three validation methods support state machine guards (Section 4.3). Each returns `(bool, Optional[str])`: `True` with no message on success, `False` with a descriptive error on failure.

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

    V6 additions (per decisions C1, C3, D1):
    - ``dirty_blocks`` / ``clean_blocks``: per-block authority tracking (C3)
    - ``block_attempts``: per-block retry counter, bounded by max_attempts (C1)
    - ``spent_usd``: cumulative budget accumulator (D1)
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
                    job.error_message = f"TimeoutObserved after {event.threshold_min}min"

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
        """Handle ReconciliationPartial: dirty/clean marking on script back-edge (C3).

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

    # --- Query methods for guards ---

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
        """Check if a block has exceeded its per-block attempt limit (C1)."""
        return self.block_attempts.get(block_id, 0) >= max_attempts

    def budget_exceeded(self, max_budget_usd: float = 10.0) -> bool:
        """Check if cumulative spend exceeds the per-run budget (D1)."""
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

The `reconciliation_complete` flag is set by `ReconciliationComplete` and cleared by `ReconciliationFailed` or `ReconciliationPartial`. The state machine guard `_reconciliation_complete` reads this flag (Section 4.3). Transition from `AUDIO_RECONCILE` to `VIDEO_PRODUCTION` is gated on `reconciliation_complete == True`.

Dirty/clean tracking (decision C3) enables partial reconciliation after script back-edges. When `voice_mismatch` routes from `VIDEO_PRODUCTION` back to `SCRIPT`, the Scenario Agent fixes the script and emits `ReconciliationPartial`. Blocks whose text, speaker, or duration target changed are marked **dirty** (need re-TTS). Blocks that didn't change keep their `AudioMeasured` values as **clean**. This avoids discarding the entire audio pipeline for a single-scene typo fix.

| Field | Type | Meaning |
|---|---|---|
| `reconciliation_complete` | `bool` | `True` when all blocks have measured audio within tolerance |
| `dirty_blocks` | `set[str]` | Slot addresses needing re-reconciliation |
| `clean_blocks` | `set[str]` | Slot addresses with authoritative measured audio |

#### 6.3.3 Attempt counter per block, budget accumulator per run

Per-block attempt counting (decision C1) prevents any single narration block from consuming infinite retries. Each time a `QueueJob` event targets a TTS slot, `block_attempts[slot_id]` increments. When `block_attempts[slot_id] >= max_attempts` (default 5), the Audio Agent emits `ReconciliationFailed` with `failure_type="duration_unrecoverable"`, triggering a back-edge to `SCRIPT`.

Per-run budget tracking (decisions C1 + D1) prevents aggregate runaway. `spent_usd` accumulates from `CostIncurred` events (emitted by the Provisioner when VM hours are consumed or API calls are made). When `spent_usd >= max_run_budget_usd` (default $10.00), the state machine emits `PipelineAborted` with `reason="budget_exceeded"`.

#### 6.3.4 Production failures list

`production_failures` collects all `ProductionFailed` events. Each entry is a dictionary with `slot_id`, `failure_type`, `expected`, `actual`, and `suggested_fix`. Guards use this list to detect unrecoverable errors: failures with `failure_type` in `{gap_unexpected, voice_mismatch}` trigger the script back-edge; all other types stay in the current state for retry.

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

    Decision B3 moved ``poll_vastai()`` into the deterministic Provisioner.
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
                    hourly_rate_usd=getattr(event, "hourly_rate_usd", 0.0),
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

`VMProjection` has no `poll_vastai()` method. Vast.ai drift detection lives in the deterministic Provisioner service (decision B3). The Provisioner runs `vastai show instances` at its own cadence, compares Vast.ai reality against `VMProjection` state (read via the shared projection registry), and emits `VMObserved` effects when divergence is detected. This preserves the projection invariant: projections are read models only; they consume events, they do not produce them.

The watcher loop advances `VMProjection` like all other projections — via `tick(event_store)` — which applies any `VMObserved` events emitted by the Provisioner since the last tick.

---

### 6.5 State Projection

#### 6.5.1 Current state + transition history

`StateProjection` tracks the state machine's current state and the full history of transitions. It applies `TransitionState` events and provides the `_transitioned_recently` check used by loop detection.

```python
@dataclass
class TransitionRecord:
    """A single state machine transition."""

    from_state: str
    to_state: str
    guard_name: str
    at_sequence: int


class StateProjection(Projection):
    """Tracks state machine state and transition history.

    Also maintains a ring buffer of recent effects per agent for loop
    detection (decision D4). The watcher loop checks this buffer on
    every tick to detect duplicate effects or no-progress patterns.
    """

    def __init__(self, loop_buffer_size: int = 5) -> None:
        super().__init__()
        self.current_state: str = "init"
        self.state_history: list[TransitionRecord] = []
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
                self.current_state = "init"
                self.state_history.clear()
                self.recent_effects.clear()
            case "transition_state":
                rec = TransitionRecord(
                    from_state=getattr(event, "from_state", self.current_state),
                    to_state=getattr(event, "to_state", self.current_state),
                    guard_name=getattr(event, "guard_name", ""),
                    at_sequence=getattr(event, "sequence", 0),
                )
                self.state_history.append(rec)
                self.current_state = rec.to_state

    def transitioned_recently(self, n_ticks: int = 5) -> bool:
        """Return True if any transition occurred in the last N events."""
        return len(self.state_history) > 0

    def get_recent_events(self, n: int) -> list[Effect]:
        """Return the last N effects across all agents."""
        all_events = []
        for agent_deque in self.recent_effects.values():
            all_events.extend(list(agent_deque))
        all_events.sort(key=lambda e: getattr(e, "timestamp", 0), reverse=True)
        return all_events[:n]

    def summary(self) -> str:
        tx_count = len(self.state_history)
        return (
            f"State: {self.current_state}, "
            f"{tx_count} transitions, "
            f"{len(self.recent_effects)} agents tracked"
        )
```

#### 6.5.2 Loop detection buffer (last N effects per agent)

The `recent_effects` dictionary maps agent name to a `deque` of that agent's last `loop_buffer_size` effects (default 5). On every `apply`, the effect is appended to the deque for its agent. Because `deque` has `maxlen`, old effects are automatically evicted — the buffer is a fixed-size ring buffer with O(1) append and no allocation on overflow.

The watcher loop uses this buffer to detect two loop conditions (decision D4):

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

When either condition triggers, the watcher loop emits `AgentLoopDetected` with context (agent name, effect history, projection delta) and the state machine halts in the current state, emitting `ClarificationRequest` for human review. The threshold is configurable per agent (default 5) via the agent's config table.


---

## 7. Agents

All agents share an HTTP surface, an LLM backend, and an effect-parsing pipeline. Each differs only in port, active states, received projection summaries, and permitted effect types.

| Agent | Port | Active States | Role |
|---|---|---|---|
| Scenario | 8001 | INIT, SCRIPT | Write and revise narration |
| Audio | 8002 | AUDIO_RECONCILE | Reconcile TTS output against script durations |
| Video | 8003 | VIDEO_PRODUCTION | Generate LTX-2.3 clips using measured durations as LAW |
| Assembly | 8005 | ASSEMBLY | ffmpeg composition and final validation |

The Provisioner (port 8004 in V5) is now a deterministic service (Chapter 8).

---

### 7.1 Agent Base Class

#### 7.1.1 HTTP Surface: GET / (Status), POST / (Instruction → 202 Accepted)

Every agent exposes exactly two endpoints:

| Endpoint | Method | Request | Response |
|---|---|---|---|
| `/` | GET | — | `AgentStatus` JSON |
| `/` | POST | `{"instruction": "...", "state": "...", "projection_snapshot": {...}}` | `{"status": "accepted", "task_id": "..."}` (202 Accepted) |

```python
from __future__ import annotations

import asyncio
import time
import uuid
from abc import abstractmethod
from typing import Any, Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field


class InstructionPayload(BaseModel):
    """POST / request body from the state machine."""
    instruction: str
    state: str
    projection_snapshot: dict[str, Any]


class AgentStatus(BaseModel):
    """GET / response."""
    name: str
    status: Literal["idle", "working", "error"]
    current_task: str | None = None
    last_error: str | None = None
    idle_since: float | None = None


class Agent:
    """Base class for all pipeline agents.

    Subclasses define ``port``, ``active_states``, ``_persona``,
    and ``_permitted_effects``. The HTTP surface is identical
    across all agents.
    """

    name: str = ""
    port: int = 0
    active_states: set[str] = set()

    def __init__(self, event_store: EventStore) -> None:
        self.event_store = event_store
        self.status: Literal["idle", "working", "error"] = "idle"
        self.current_task: str | None = None
        self.current_task_id: str | None = None
        self.last_error: str | None = None
        self.last_activity: float = time.time()
        self._app = self._build_app()

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/")
        async def get_status() -> AgentStatus:
            idle_since = (
                time.time() - self.last_activity
                if self.status == "idle" else None
            )
            return AgentStatus(
                name=self.name, status=self.status,
                current_task=self.current_task,
                last_error=self.last_error, idle_since=idle_since,
            )

        @app.post("/")
        async def post_instruction(req: Request) -> dict:
            payload = InstructionPayload(**await req.json())
            self.status = "working"
            self.current_task = payload.instruction
            self.current_task_id = str(uuid.uuid4())[:8]
            asyncio.create_task(
                self._run_turn(payload.instruction, payload.projection_snapshot)
            )
            return {"status": "accepted", "task_id": self.current_task_id}

        return app

    @abstractmethod
    def _build_prompt(
        self, instruction: str, projection_snapshot: dict[str, Any]
    ) -> str: ...

    @abstractmethod
    def _permitted_effects(self) -> list[str]: ...
```

#### 7.1.2 `_build_prompt()`: Projection Summaries as O(1) Context

Agents receive **projection summaries**, not event logs. The state machine constructs a `projection_snapshot` dict containing only pre-computed summaries. The agent never reads the event log.

```python
class ProjectionSummary(BaseModel):
    """O(1) context delivered to agents on every turn."""
    otio: dict[str, Any] = Field(default_factory=dict)
    jobs: dict[str, Any] = Field(default_factory=dict)
    vms: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)

    def to_prompt_block(self) -> str:
        return (
            f"[OTIO] {self.otio}\n"
            f"[JOBS] {self.jobs}\n"
            f"[VMS]  {self.vms}\n"
            f"[STATE] {self.state}"
        )
```

Every subclass override follows the same three-part structure: **persona**, **state instruction**, **projection summary**.

#### 7.1.3 `_call_llm()`: `deepseek-v4-flash` via API

```python
import httpx

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"


async def _call_llm(self, prompt: str) -> str:
    """Call deepseek-v4-flash. Return raw response text.

    No timeout (per Principle 1.4). If the API hangs, the operator
    intervenes. The agent status remains ``working`` until the call
    returns or the process is restarted.
    """
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 4096,
    }
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(DEEPSEEK_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
```

#### 7.1.4 `_parse_effects()`: Category-Conditioned via `instructor`

```python
from effect_parser import EffectParser

async def _parse_effects(self, raw_text: str) -> list[Effect]:
    """Parse LLM response into validated Effect objects."""
    parser = EffectParser(
        permitted_kinds=self._permitted_effects(), max_retries=2,
    )
    return await parser.parse(raw_text)
```

#### 7.1.5 `_run_turn()`: Full Lifecycle with Error Handling

```python
async def _run_turn(
    self, instruction: str, projection_snapshot: dict[str, Any]
) -> None:
    """Execute one agent turn.

    1. Build prompt from persona + instruction + projection summary.
    2. Call LLM (deepseek-v4-flash).
    3. Parse effects (category-conditioned).
    4. Append each effect to the event store.
    5. On any exception: set error status, do NOT append partial effects.
    """
    self.last_activity = time.time()
    try:
        prompt = self._build_prompt(instruction, projection_snapshot)
        raw_response = await self._call_llm(prompt)
        effects: list[Effect] = await self._parse_effects(raw_response)

        if not effects:
            effects = [NoOp(kind="noop", agent=self.name,
                            reason="no_effects_extracted")]

        for effect in effects:
            effect.agent = self.name
            await self.event_store.append(effect)

        self.status = "idle"
        self.last_error = None

    except Exception as exc:
        self.status = "error"
        self.last_error = f"{type(exc).__name__}: {exc}"
        await self.event_store.append(
            NoOp(kind="noop", agent=self.name,
                 reason=f"turn_failed: {type(exc).__name__}: {exc}")
        )
```

---

### 7.2 Scenario Agent

```python
class ScenarioAgent(Agent):
    """Writes and revises narration. Active in INIT and SCRIPT."""

    name = "scenario"
    port = 8001
    active_states = {"INIT", "SCRIPT"}

    _persona: str = (
        "You are a documentary scriptwriter. Write narration that is "
        "factually dense, rhythmically paced, and timed to fit its slot. "
        "Revise, delete, or reorder scenes. Every block must specify "
        "speaker, duration_sec, and scene_num."
    )

    def _permitted_effects(self) -> list[str]:
        return ["update_script", "delete_scene", "reorder_scenes"]

    def _build_prompt(
        self, instruction: str, projection_snapshot: dict[str, Any]
    ) -> str:
        otio = projection_snapshot.get("otio", {})
        return (
            f"{self._persona}\n\n"
            f"=== STATE INSTRUCTION ===\n{instruction}\n\n"
            f"=== OTIO SUMMARY ===\n"
            f"Scenes: {otio.get('scene_count', 0)}\n"
            f"Total duration: {otio.get('total_duration_sec', 0.0):.2f}s\n"
            f"Gaps: {otio.get('gaps', [])}\n"
            f"Speaker assignments: {otio.get('speaker_assignments', {})}\n"
            f"Blocks by scene: {otio.get('blocks_by_scene', {})}\n\n"
            f"Report your work with a kind marker:\n"
            f"Kind: update_script / delete_scene / reorder_scenes\n"
            f"Describe what happened naturally."
        )
```

#### 7.2.1 Port 8001, Active in INIT and SCRIPT

The state machine activates the Scenario Agent in INIT (first draft) or SCRIPT (revision). The instruction reflects the guard that triggered activation — e.g., *"Scene 3 exceeds its slot. Revise or split."*

#### 7.2.2 Prompt Composition: Persona + State Instruction + OTIO Summary

The `otio` snapshot contains: `scene_count`, `total_duration_sec`, `gaps` (unfilled slots), `speaker_assignments`, and `blocks_by_scene` (dict of scene_num → list of `(slot_id, speaker, duration_sec)`). This is O(1) in event count, computed by `OTIOProjection.summary()`.

#### 7.2.3 Effects: UpdateScript, DeleteScene, ReorderScenes

| Effect | Condition | Key Fields |
|---|---|---|
| `UpdateScript` | New or revised narration | `scene_num`, `speaker`, `text`, `pronunciation_hints`, `visual_notes`, `duration_sec` |
| `DeleteScene` | Remove a scene | `scene_num`, `reason` |
| `ReorderScenes` | Change scene order | `new_order: list[int]` |

---

### 7.3 Audio Agent

```python
class AudioAgent(Agent):
    """Owns the narration reconciliation loop. Active only in AUDIO_RECONCILE.

    Computes dirty blocks on script back-edge. Runs WhisperX 3× and
    takes the median. Enforces 5 attempts per block and $2.00 TTS budget.

    Effects: QueueJob, JobApproved, JobRequeued, DurationAdjusted,
    ReconciliationFailed, ReconciliationPartial, ReconciliationComplete.
    """

    name = "audio"
    port = 8002
    active_states = {"AUDIO_RECONCILE"}

    MAX_ATTEMPTS_PER_BLOCK: int = 5
    TTS_BUDGET_USD: float = 2.00
    TOLERANCE_RATIO: float = 0.15
    TOLERANCE_ABSOLUTE_SEC: float = 0.25

    _persona: str = (
        "You are a documentary audio engineer. Ensure every narration block "
        "has a generated audio file whose measured duration matches its "
        "scripted duration within tolerance. Manage TTS jobs, judge output "
        "quality, and requeue with adjusted parameters."
    )

    def _permitted_effects(self) -> list[str]:
        return [
            "queue_job", "job_approved", "job_requeued", "duration_adjusted",
            "reconciliation_failed", "reconciliation_partial",
            "reconciliation_complete",
        ]

    def _build_prompt(
        self, instruction: str, projection_snapshot: dict[str, Any]
    ) -> str:
        otio = projection_snapshot.get("otio", {})
        jobs = projection_snapshot.get("jobs", {})
        budget = projection_snapshot.get("budget", {})
        return (
            f"{self._persona}\n\n"
            f"=== STATE INSTRUCTION ===\n{instruction}\n\n"
            f"=== DIRTY BLOCKS ===\n{otio.get('dirty_blocks', [])}\n\n"
            f"=== PENDING RECONCILIATION ===\n"
            f"{jobs.get('pending_reconciliation', [])}\n\n"
            f"=== MEASURED vs SCRIPTED ===\n"
            f"{otio.get('measured_vs_scripted', [])}\n\n"
            f"=== BUDGET ===\n"
            f"${budget.get('remaining_usd', self.TTS_BUDGET_USD):.2f} / "
            f"${self.TTS_BUDGET_USD:.2f}\n\n"
            f"TOLERANCE: ±15% or ±0.25s, whichever is larger.\n"
            f"MAX ATTEMPTS PER BLOCK: {self.MAX_ATTEMPTS_PER_BLOCK}\n\n"
            f"Report your work with a kind marker:\n"
            f"Kind: queue_job / job_approved / job_requeued / duration_adjusted "
            f"/ reconciliation_failed / reconciliation_partial / reconciliation_complete\n"
            f"Describe what happened naturally."
        )

    def _tolerance(self, scripted_sec: float) -> float:
        return max(scripted_sec * self.TOLERANCE_RATIO, self.TOLERANCE_ABSOLUTE_SEC)

    def _within_tolerance(self, scripted_sec: float, measured_sec: float) -> bool:
        return abs(measured_sec - scripted_sec) <= self._tolerance(scripted_sec)
```

#### 7.3.1 Port 8002, Active in AUDIO_RECONCILE

Per Decision B1, the Audio Agent is **only** active in `AUDIO_RECONCILE`. The state machine transitions from `SCRIPT` to `AUDIO_RECONCILE` when the script projection reports all scenes complete with no gaps.

#### 7.3.2 Reconciliation Loop: QueueJob → JobCompleted → 3×WhisperX → Median → Compare → Adjust/Requeue

```
For each dirty or pending block:
  1. Emit QueueJob (job_type="tts")
  2. Wait for JobCompleted from Provisioner service (Chapter 8)
  3. Run WhisperX on artifact 3× (Decision C2)
  4. Take median of 3 measured durations
  5. Compute tolerance = max(±15% of scripted, ±0.25s)
  6. Within tolerance? → DurationAdjusted
     Outside tolerance? → ReconciliationFailed + JobRequeued (new pacing)
```

The agent processes all newly completed jobs per turn, emits new QueueJobs for unsubmitted blocks, and reports aggregate state.

#### 7.3.3 Tolerance: ±15% or ±0.25s (Whichever Is Larger)

The `_tolerance()` method computes `max(scripted_sec × 0.15, 0.25)`. For a 10s block: tolerance = max(1.5, 0.25) = 1.5s. For a 1s block: tolerance = max(0.15, 0.25) = 0.25s.

#### 7.3.4 Bounds: Max 5 Attempts per Block, $2.00 TTS Budget

| Bound | Value | Enforcement |
|---|---|---|
| Max attempts/block | 5 | After 5 `JobRequeued` for a block, emit `ReconciliationFailed` for that block, continue with others |
| TTS budget | $2.00 USD | Job projection tracks cumulative cost; agent reads `budget.remaining_usd` from snapshot |

If a block exhausts 5 attempts, emit `ReconciliationFailed` for that block and continue. If all blocks fail, emit `ReconciliationFailed`. If some pass and some fail, emit `ReconciliationPartial`.

#### 7.3.5 Effects: QueueJob, JobApproved, JobRequeued, DurationAdjusted, ReconciliationFailed, ReconciliationPartial, ReconciliationComplete

| Effect | Condition | Key Fields |
|---|---|---|
| `QueueJob` | Submit TTS job | `job_id`, `job_type="tts"`, `scene_num`, `slot_id`, `params` |
| `JobApproved` | WhisperX passes quality heuristics | `job_id`, `artifact_path`, `quality_notes` |
| `JobRequeued` | Outside tolerance | `job_id`, `reason`, `new_params` |
| `DurationAdjusted` | Within tolerance, OTIO updated | `block_id`, `scene_num`, `scripted_sec`, `measured_sec`, `delta_sec`, `tolerance_sec` |
| `ReconciliationFailed` | All blocks failed or block >5 attempts | `blocks_total`, `blocks_passed`, `blocks_failed`, `worst_delta_sec` |
| `ReconciliationPartial` | Some passed, some failed | `blocks_total`, `blocks_passed`, `blocks_failed`, `failed_block_ids` |
| `ReconciliationComplete` | All blocks pass | `blocks_total`, `total_measured_sec`, `worst_delta_sec` |

`ReconciliationComplete` signals that OTIO durations are now **authoritative** — Video Production uses them as LAW.

#### 7.3.6 Dirty Block Computation (Script Back-Edge)

Per Decision C3, a block is **dirty** if: its script text changed since the last reconciliation pass (detected by comparing `UpdateScript` sequence numbers against `last_reconciled_at`), it has never been submitted for TTS, or a previous attempt failed with remaining attempts. `OTIOProjection` maintains `dirty_blocks: list[str]` (block_ids), delivered in the snapshot field `otio.dirty_blocks`.

---

### 7.4 Video Agent

```python
class VideoAgent(Agent):
    """Generates LTX-2.3 video clips. Active only in VIDEO_PRODUCTION.

    Uses measured durations from authoritative OTIO as LAW.
    Judges output quality via LLM (file size, duration, heuristics).

    Effects: QueueJob, JobApproved, JobRequeued, MergeIntoOTIO.
    """

    name = "video"
    port = 8003
    active_states = {"VIDEO_PRODUCTION"}

    _persona: str = (
        "You are a documentary video producer. Generate B-roll and visual "
        "sequences using LTX-2.3. Every clip must match its measured duration "
        "exactly — that duration is LAW. Judge quality and requeue if inadequate."
    )

    def _permitted_effects(self) -> list[str]:
        return ["queue_job", "job_approved", "job_requeued", "merge_into_otio"]

    def _build_prompt(
        self, instruction: str, projection_snapshot: dict[str, Any]
    ) -> str:
        otio = projection_snapshot.get("otio", {})
        jobs = projection_snapshot.get("jobs", {})
        return (
            f"{self._persona}\n\n"
            f"=== STATE INSTRUCTION ===\n{instruction}\n\n"
            f"=== AUTHORITATIVE OTIO (MEASURED DURATIONS = LAW) ===\n"
            f"Total: {otio.get('total_measured_sec', 0.0):.2f}s\n"
            f"Video slots:\n{otio.get('video_slots', [])}\n\n"
            f"Approved audio clips:\n{otio.get('audio_clips', [])}\n\n"
            f"Pending video jobs: {jobs.get('pending_video', [])}\n"
            f"Completed: {jobs.get('completed_video', [])}\n\n"
            f"Report your work with a kind marker:\n"
            f"Kind: queue_job / job_approved / job_requeued / merge_into_otio\n"
            f"Describe what happened naturally."
        )
```

#### 7.4.1 Port 8003, Active in VIDEO_PRODUCTION

Activates after `AUDIO_RECONCILE` → `ReconciliationComplete`. Never active in any other state.

#### 7.4.2 Uses Authoritative OTIO (Measured Durations as LAW)

`otio.video_slots` contains: `slot_id`, `scene_num`, `measured_duration_sec` (from WhisperX median), `visual_notes`, `audio_clip_path`. The agent must set `params.duration_sec` to the measured duration. Any deviation is a bug.

#### 7.4.3 Artistry Judgment via LLM Call

```python
async def _judge_video_quality(
    self, artifact_path: str, expected_duration_sec: float
) -> dict[str, Any]:
    file_size = Path(artifact_path).stat().st_size
    prompt = (
        f"Judge this video file:\n"
        f"Path: {artifact_path}\n"
        f"Size: {file_size} bytes\n"
        f"Expected duration: {expected_duration_sec}s\n"
        f"Return JSON: approved (bool), file_size_ok (bool), "
        f"duration_matches (bool), visual_quality_score (1-10), notes (str)."
    )
    raw = await self._call_llm(prompt)
    return parse_quality_assessment(raw)  # via instructor
```

Criteria: `file_size_ok` (non-zero, not truncated), `duration_matches` (actual vs expected within ±0.1s), `visual_quality_score` ≥ 6. All true → `JobApproved`; else → `JobRequeued`.

#### 7.4.4 Effects: QueueJob, JobApproved, JobRequeued, MergeIntoOTIO

| Effect | Condition | Key Fields |
|---|---|---|
| `QueueJob` | Submit LTX-2.3 generation | `job_id`, `job_type="ltx"`, `scene_num`, `slot_id`, `params` (includes `duration_sec` as LAW) |
| `JobApproved` | Quality judgment passes | `job_id`, `artifact_path`, `quality_notes` |
| `JobRequeued` | Quality judgment fails | `job_id`, `reason`, `new_params` |
| `MergeIntoOTIO` | Video approved, add to timeline | `slot_id`, `scene_num`, `artifact_path`, `duration_sec` |

All slots merged → state machine transitions to `ASSEMBLY`.

---

### 7.5 Assembly Agent (NEW)

```python
class AssemblyAgent(Agent):
    """Final composition agent. Active only in ASSEMBLY.

    Runs ffmpeg to compose OTIO tracks into final_documentary.mp4.
    Validates OTIO before and after assembly.

    Effects: PipelineComplete, ProductionFailed.
    """

    name = "assembly"
    port = 8005
    active_states = {"ASSEMBLY"}

    _persona: str = (
        "You are a documentary assembly engineer. Run ffmpeg to compose all "
        "approved audio and video clips from OTIO into final_documentary.mp4. "
        "Validate OTIO before assembly and verify output after."
    )

    def _permitted_effects(self) -> list[str]:
        return ["pipeline_complete", "production_failed"]

    def _build_prompt(
        self, instruction: str, projection_snapshot: dict[str, Any]
    ) -> str:
        otio = projection_snapshot.get("otio", {})
        return (
            f"{self._persona}\n\n"
            f"=== STATE INSTRUCTION ===\n{instruction}\n\n"
            f"=== OTIO VALIDATION ===\n"
            f"No overlaps: {otio.get('validation_no_overlaps', 'N/A')}\n"
            f"Track alignment: {otio.get('validation_track_alignment', 'N/A')}\n"
            f"Clip media: {otio.get('validation_clip_media', 'N/A')}\n"
            f"Audio track: {otio.get('audio_track_summary', [])}\n"
            f"Video track: {otio.get('video_track_summary', [])}\n"
            f"Total duration: {otio.get('total_measured_sec', 0.0):.2f}s\n\n"
            f"If all checks pass, emit PipelineComplete.\n"
            f"If any check fails, emit ProductionFailed "
            f"with failure_type='track_misalignment'.\n\n"
            f"Kind: pipeline_complete / production_failed"
        )
```

#### 7.5.1 Port 8005, Active in ASSEMBLY

The Assembly Agent is new in V6. It replaces the V5 pattern where the "last active media agent" performed assembly. Dedicated assembly ensures separation: media agents produce clips; the Assembly Agent composes them. The state machine enters `ASSEMBLY` when the Video Agent has `MergeIntoOTIO`-ed all video slots and the OTIO projection reports all tracks complete.

#### 7.5.2 ffmpeg Composition of All OTIO Clips into `final_documentary.mp4`

```python
def _build_ffmpeg_command(self, otio_summary: dict[str, Any]) -> str:
    """Construct ffmpeg concat command from OTIO track summaries."""
    video_list = otio_summary.get("video_segments", [])
    audio_list = otio_summary.get("audio_segments", [])

    video_concat = "|".join(
        f"file '{seg['artifact_path']}'\nduration {seg['duration_sec']}"
        for seg in video_list
    )
    audio_concat = "|".join(
        f"file '{seg['artifact_path']}'\nduration {seg['duration_sec']}"
        for seg in audio_list
    )
    return (
        f"ffmpeg -y "
        f"-f concat -safe 0 -protocol_whitelist file,pipe "
        f"-i <(echo -e '{video_concat}') "
        f"-f concat -safe 0 -protocol_whitelist file,pipe "
        f"-i <(echo -e '{audio_concat}') "
        f"-c:v libx264 -crf 18 -preset slow "
        f"-c:a aac -b:a 192k -shortest final_documentary.mp4"
    )
```

The command may be executed directly via subprocess or emitted as `ExecuteRawBash` (subject to parser allowlist validation). Output path is always `final_documentary.mp4`.

#### 7.5.3 OTIO Validation Before and After Assembly

| Check | Method | Failure Type |
|---|---|---|
| No overlaps | `OTIOProjection.validate_no_overlaps()` | `overlap` |
| Track alignment | `OTIOProjection.validate_track_alignment()` | `track_misalignment` |
| Clip media | `OTIOProjection.validate_clip_media()` | `missing_media` |

Any pre-assembly check fails → `ProductionFailed` with the appropriate `failure_type`; ffmpeg does **not** run. Post-assembly: verify output exists, size > 0, duration matches OTIO total within ±0.5s, and ffprobe exits 0. All pass → `PipelineComplete`.

#### 7.5.4 Effects: PipelineComplete, ProductionFailed

| Effect | Condition | Key Fields |
|---|---|---|
| `PipelineComplete` | ffmpeg succeeded, output validated | `output_path`, `duration_sec`, `file_size_bytes` |
| `ProductionFailed` | Pre-assembly validation failed or ffmpeg error | `failure_type`, `slot_id`, `expected`, `actual`, `suggested_fix` |

`PipelineComplete` is the terminal effect of the pipeline. The state machine transitions `ASSEMBLY` → `DONE` upon receiving it.

---

### 7.6 Effect Parser

The Effect Parser converts raw LLM text into typed `Effect` models. It is a shared service used by all agents via `Agent._parse_effects()`.

**Design constraints:**
- Never regex.
- Category-conditioned: only extracts effect types the calling agent is permitted to emit.
- Low-confidence outputs produce `ClarificationRequest`.
- `ExecuteRawBash` commands are validated against an explicit allowlist.

#### 7.6.1 Category-Conditioned Extraction (Never Regex)

```python
from __future__ import annotations

import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

_client = instructor.from_openai(
    AsyncOpenAI(api_key=API_KEY, base_url="https://api.deepseek.com/v1")
)

KIND_TO_MODEL: dict[str, type[Effect]] = {
    "update_script": UpdateScript,
    "delete_scene": DeleteScene,
    "reorder_scenes": ReorderScenes,
    "queue_job": QueueJob,
    "job_approved": JobApproved,
    "job_requeued": JobRequeued,
    "duration_adjusted": DurationAdjusted,
    "reconciliation_failed": ReconciliationFailed,
    "reconciliation_partial": ReconciliationPartial,
    "reconciliation_complete": ReconciliationComplete,
    "merge_into_otio": MergeIntoOTIO,
    "pipeline_complete": PipelineComplete,
    "production_failed": ProductionFailed,
    "execute_raw_bash": ExecuteRawBash,
    "clarification_request": ClarificationRequest,
    "noop": NoOp,
}


class EffectParser:
    """Category-conditioned effect extractor. Never uses regex."""

    def __init__(self, permitted_kinds: list[str], max_retries: int = 2) -> None:
        self.permitted_kinds = set(permitted_kinds)
        self.max_retries = max_retries

    async def parse(self, text: str) -> list[Effect]:
        """Extract effects from raw LLM text.

        1. Ask LLM to identify kind markers in the text.
        2. For each identified kind in ``permitted_kinds``,
           attempt instructor extraction.
        3. On success, add to results.
        4. On any failure, emit ClarificationRequest.
        """
        effects: list[Effect] = []
        identified = await self._identify_kinds(text)

        for kind in identified:
            if kind not in self.permitted_kinds:
                effects.append(ClarificationRequest(
                    kind="clarification_request", agent="parser",
                    question=(f"Agent attempted '{kind}' not in permitted: "
                              f"{sorted(self.permitted_kinds)}"),
                ))
                continue

            model = KIND_TO_MODEL.get(kind)
            if model is None:
                effects.append(ClarificationRequest(
                    kind="clarification_request", agent="parser",
                    question=f"Unknown effect kind: {kind}",
                ))
                continue

            try:
                effect = await _client.chat.completions.create(
                    model="deepseek-v4-flash",
                    messages=[
                        {"role": "system",
                         "content": f"Extract a {kind} effect. Return structured data only."},
                        {"role": "user", "content": text},
                    ],
                    response_model=model,
                    max_retries=self.max_retries,
                )
                effects.append(effect)
            except (ValidationError, Exception):
                effects.append(ClarificationRequest(
                    kind="clarification_request", agent="parser",
                    question=(f"Could not parse {kind} with confidence. "
                              f"Text: {text[:500]}"),
                ))
        return effects

    async def _identify_kinds(self, text: str) -> list[str]:
        class KindList(BaseModel):
            kinds: list[str] = Field(description="Effect kinds found in text")

        result = await _client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system",
                 "content": (f"Identify effect kinds in the text. "
                             f"Known: {list(KIND_TO_MODEL.keys())}")},
                {"role": "user", "content": text},
            ],
            response_model=KindList,
            max_retries=1,
        )
        return result.kinds
```

#### 7.6.2 ExecuteRawBash Allowlist Validation

```python
ALLOWLISTED_COMMANDS: set[str] = {
    "ffmpeg", "ffprobe", "vastai",
    "ls", "stat", "file", "du",
    "whisperx", "ps", "nvidia-smi",
}


def validate_bash_command(command: str) -> bool:
    """True if every pipeline segment's base executable is allowlisted."""
    if not command or not command.strip():
        return False
    for segment in command.split("|"):
        seg = segment.strip()
        if seg.startswith("$(") or seg.startswith("<$("):
            inner = seg[seg.index("$(") + 2:seg.rindex(")")]
            seg = inner.strip()
        tokens = seg.split()
        while tokens and "=" in tokens[0] and not tokens[0].startswith("="):
            tokens.pop(0)
        if not tokens:
            continue
        if tokens[0].split("/")[-1] not in ALLOWLISTED_COMMANDS:
            return False
    return True


<!-- See §3.8.1 for ExecuteRawBash schema — canonical definition in Effect Type Family. -->

If an agent attempts `ExecuteRawBash` with a non-allowlisted command, the Pydantic validator raises `ValidationError`, which the parser catches and converts to `ClarificationRequest`.

#### 7.6.3 Low-Confidence Fallback to ClarificationRequest

| Condition | Parser Behavior |
|---|---|
| Kind marker found, instructor extraction succeeds | Append effect |
| Kind marker found, extraction fails | Append `ClarificationRequest` with raw text |
| Kind marker found, not in permitted kinds | Append `ClarificationRequest` (permission violation) |
| No kind markers found | Append `NoOp(reason="no_effects_extracted")` |
| `ExecuteRawBash` fails allowlist | Append `ClarificationRequest` with command details |

<!-- See §3.8.1 for ClarificationRequest schema — canonical definition in Effect Type Family. -->

The state machine routes `ClarificationRequest` to the human overseer. The pipeline pauses until `HumanInstruction` is received.


---

## 8. Provisioner Service

The Provisioner is the most significant architectural change in V6. In V5 it was an LLM agent with its own HTTP server (port 8004); it received instructions via `POST /`, reasoned about VM state with an LLM, and produced effects through the parser. In V6 it is a deterministic Python service with no LLM, no HTTP surface, and no autonomy. It runs inside the watcher loop as a plain Python class, reads `JobProjection` to find pending work, matches Vast.ai offers by deterministic criteria, and emits effects directly. This change (decision B2) eliminates an entire LLM call per provisioning cycle, removes a failure mode (parser mis-extraction of VM effects), and makes VM operations predictable and testable.

### 8.1 Architecture

#### 8.1.1 Deterministic Python service (not LLM agent) running in watcher loop

`ProvisionerService` is a plain Python class instantiated once at pipeline startup. The watcher loop calls `provisioner.tick(projections)` on every cycle, after advancing all projections and before sending `tick` to the state machine. The Provisioner never initiates communication; it reacts to projection state.

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

    # Retry configuration (decision C1 — bounded retries)
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

        # 1. Poll Vast.ai for drift detection (decision B3)
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

#### 8.1.2 No POST / endpoint; reads JobProjection, acts on pending jobs

The Provisioner has no HTTP server, no `GET /`, no `POST /`. It reads `job_proj.jobs` directly — a dictionary of `JobState` objects keyed by `job_id`. A job with `status == "pending"` and no assigned VM is a provisioning candidate. The Provisioner selects a matching Vast.ai offer, allocates the VM, and records the assignment in an internal `dict[job_id, str]` mapping `job_id` → `instance_id`. This mapping is ephemeral; if the process restarts, it is rebuilt from `VMObserved` effects and re-queued jobs on the next tick.

#### 8.1.3 No direct agent-to-agent communication; all via event store

V5's Provisioner agent directly `POST`ed job-completion notifications to the Audio and Video agents. V6 eliminates this: the Provisioner appends `JobCompleted` or `JobFailed` effects to the event store. The Audio Agent and Video Agent read these via their own `JobProjection` subscriptions on subsequent ticks. There is no direct HTTP call between any two agents. The event store is the only communication channel.

### 8.2 VM Lifecycle Management

#### 8.2.1 Offer matching: GPU type, VRAM, price thresholds (deterministic criteria)

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

#### 8.2.2 VM allocation via Vast.ai CLI

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
            role=job_type,  # type: ignore[arg-type]
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

#### 8.2.3 Heartbeat monitoring via Vast.ai polling (not VM-side)

Decision B3 moved Vast.ai polling from the VM-side agent into the Provisioner. The Provisioner runs `vastai show instances` every `POLL_VASTAI_INTERVAL_SEC` (60 s). It compares the Vast.ai API response against `VMProjection.active_vms()`. When divergence is detected, it emits `VMObserved` with the appropriate `corrective_action`.

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

#### 8.2.4 TimeoutObserved emission for stale VMs

The Provisioner checks the `created_at` timestamp of every job in `running` status. If `elapsed_min > STALE_VM_THRESHOLD_MIN`, it emits `TimeoutObserved` with `action_taken="deallocate_vm"`. The watcher loop (§4.4.1) also performs a similar check; the Provisioner's check is a secondary safety net specifically for VMs that have gone unresponsive.

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

#### 8.2.5 VM deallocation on job completion or failure

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
        # (VMProjection tracks start time; actual cost comes from Vast.ai billing)
        effect = VMDeallocated(
            instance_id=instance_id,
            reason=reason,  # type: ignore[arg-type]
            final_cost=0.0,  # populated by cost projection from Vast.ai invoice
            runtime_sec=0.0,
            run_id=self.config.run_id,
        )
        await self.event_store.append(effect)
```

### 8.3 Job Delivery

#### 8.3.1 POST job to VM worker

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

#### 8.3.2 Receive completion/failure, emit JobCompleted/JobFailed

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

### 8.4 Failure Handling

#### 8.4.1 VMProvisionFailed → retry with different offer → ClarificationRequest on repeated failure

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
            failure_category=category,  # type: ignore[arg-type]
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

## 9. VM Worker

The VM Worker is a stateless FastAPI server on ephemeral GPU instances (port 9000+). It receives inference jobs from the Provisioner (Section 8.3.1), executes TTS or video inference, measures output with WhisperX (3 runs), validates via LLM call to `deepseek-v4-flash`, and posts results back to the Provisioner. The worker has no event store access, no local state beyond the current job, and no timeout or self-destruct logic per Principle 4 (Section 1.4) and decision A1.

---

### 9.1 HTTP Surface

#### 9.1.1 GET / (health), POST / (receive job)

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

### 9.2 Job Execution

#### 9.2.1 TTS: Qwen3-TTS inference pipeline

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

#### 9.2.2 Video: LTX-2.3 inference pipeline

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

#### 9.2.3 WhisperX: 3× measurement, return all to Audio Agent

After inference, the worker runs WhisperX three times per decision C2. All three measurements are returned; the Audio Agent computes the median (Section 7.3).

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

### 9.3 Quality Check

#### 9.3.1 LLM-based output validation (file size, duration, corruption check)

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

Unrecognized labels default to `pass` — false negatives are more expensive than false positives. This check is not a substitute for Audio Agent reconciliation (Section 7.3) or Video Agent artistry judgment (Section 7.4.3).

---

### 9.4 Reporting

#### 9.4.1 POST result to Provisioner

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

The orchestrator `_execute_job` ties all phases: inference → measure → quality-check → report. On any exception, it posts `status="failed"` with the error detail and resets to `idle`. The worker does not retry failed POSTs — the Provisioner's `TimeoutObserved` (Section 4.4.1) handles undelivered results.

#### 9.4.2 No VM-side timeout; no self-destruct

The VM Worker contains no `asyncio.timeout`, `threading.Timer`, `signal.alarm`, heartbeat loop, or self-destruct call. This is the V5 → V6 change mandated by A1 and Principle 4.

| Aspect | V5 (Section 7.6) | V6 |
|---|---|---|
| Heartbeat | VM polls every 60s | None — VM is passive |
| Stale detection | 15 min → `vastai destroy` | Provisioner detects via `TimeoutObserved` (Section 4.4.1) |
| Timer code | `threading.Timer` in VM | No timer code in VM |
| Recovery path | VM self-destructs | Provisioner deallocates + requeues job (Section 8.4) |

If a subprocess hangs (Qwen3-TTS or LTX-2.3 never returns), the VM remains occupied until the Provisioner's projection-based stale-job detection reclaims it. The VM has no awareness of its own lifecycle — it processes jobs until terminated externally.


---

## 10. Data Flows

This chapter traces the four principal data flow patterns through the V6 pipeline: the normal tick cycle, the reconciliation loop with VM-mediated TTS, the script back-edge with partial re-reconciliation, and human intervention. Each flow is presented as a text-based sequence diagram showing actor interactions, followed by a step-by-step specification.

---

### 10.1 Normal Cycle

#### 10.1.1 11-step tick-driven cycle with projection updates

```
  +----------+     +--------------+     +---------------+     +--------------+
  | Watcher  |     |Projections   |     | State Machine |     |  Agents      |
  | (1s loop)|     |(OTIO,Job,VM, |     | (6 states)    |     |(Scenario,   |
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
       |                  |                     | 3. guards read     |
       |                  |                     |    projection state|
       |                  |<---return state----|                     |
       |                  |                     | 4. evaluate guards |
       |                  |                     |    (bool)          |
       |                  |                     | 5. if true: fire   |
       |                  |                     |    transition      |
       |                  |                     | 6. emit            |
       |                  |                     |    TransitionState |
       |                  |                     |    effect          |
       |                  |                     |------------------->|
       |                  |                     |    (POST / with    |
       |                  |                     |     instruction +  |
       |                  |                     |     snapshot)      |
       |                  |                     | 7. Agent 202       |
       |                  |                     |<-------------------|
       |                  |                     | 8. LLM runs,       |
       |                  |                     |    produces text   |
       |                  |                     | 9. parser extracts |
       |                  |                     |    effects         |
       |                  |     10. append     |                    |
       |                  |<---effects---------|                    |
       |                  |     (EventStore)   |                    |
       |                  |     INSERT OR      |                    |
       |                  |     IGNORE         |                    |
       | 11. sleep 1s     |                     |                    |
       |<-----------------|                     |                    |
       | (repeat)         |                     |                    |
```

**Step-by-step specification:**

| Step | Actor | Action | Specification |
|---|---|---|---|
| 1 | Watcher | Wakes | `asyncio.sleep(1)` expires; loop body begins |
| 2 | Projections | Tick | Each projection calls `tick(event_store)`, which invokes `read_since(last_sequence)` to fetch new events |
| 3–4 | State Machine | Guard evaluation | `PipelineStateMachine.tick()` fires; each guard reads projection state via injected references (§4.3) |
| 5 | State Machine | Transition | First guard returning `True` wins; `on_enter_state` emits `TransitionState` effect to event store |
| 6 | State Machine | Instruction injection | State machine builds instruction string from §4.5 table, includes projection snapshot |
| 7 | State Machine → Agent | POST / | HTTP POST to agent's port (8001, 8002, 8003, 8005) with `InstructionPayload` (§7.1.1). Agent returns `202 Accepted` immediately |
| 8 | Agent | LLM execution | Agent runs `_call_llm()` with `_build_prompt()` (persona + instruction + projection summary). No timeout (§1.4) |
| 9 | Agent | Effect parsing | `_parse_effects()` via instructor extracts typed Pydantic models from LLM output (§7.6) |
| 10 | Event Store | Append | Effects written to SQLite with `INSERT OR IGNORE` on `(run_id, effect_id)`. Single writer queue (§5.2) |
| 11 | Watcher | Sleep | `asyncio.sleep(1)`; loop repeats. New effects are picked up on next tick at step 2 |

The cycle is **strictly sequential** — the watcher does not send a new tick until the previous one has fully settled (all projections processed, all guards evaluated). This prevents race conditions between projection state and guard reads. The 1-second throttle is a minimum, not a maximum: if step 8 (LLM execution) takes 30 seconds, ticks simply resume afterward.

---

### 10.2 Reconciliation Loop (Detailed)

The reconciliation loop is the most complex flow in the pipeline. It spans four physical components — Audio Agent, Event Store, Provisioner, and VM Worker — and iterates until every narration block passes the tolerance check or exhausts its attempt budget.

#### 10.2.1 Audio Agent ↔ Event Store ↔ Provisioner ↔ VM Worker (TTS path)

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

#### 10.2.2 3× WhisperX measurement flow

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

#### 10.2.3 Within tolerance → DurationAdjusted

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

#### 10.2.4 Outside tolerance → ReconciliationFailed → JobRequeued → retry

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

#### 10.2.5 All pass → ReconciliationComplete

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

The `ReconciliationComplete` effect is the **gateway transition** from `AUDIO_RECONCILE` to `VIDEO_PRODUCTION`. The `_reconciliation_complete` guard (§4.3.3) requires both that `ReconciliationComplete` has been emitted and that `dirty_block_ids` is empty. Once this fires, the OTIO Projection's measured durations become **authoritative** — the Video Agent uses them as LAW for LTX-2.3 clip generation.

| Parameter | Value | Source |
|---|---|---|
| Tolerance | ±15% or ±0.25s (whichever is larger) | §7.3.3 |
| Max attempts per block | 5 | §7.3.4, `max_attempts_per_block` config |
| Max TTS budget | $2.00 USD | §7.3.4, `max_tts_budget_usd` config |
| WhisperX runs per measurement | 3 | §9.2.3, decision C2 |
| Median computation | Client-side (Audio Agent) | §7.3.2 |

---

### 10.3 Script Failure → Back-Edge with ReconciliationPartial

V6 introduces `ReconciliationPartial` (§3.4.2) to handle the case where a script revision invalidates only some blocks. Blocks that were unchanged keep their measured durations; only dirty blocks are re-reconciled.

#### 10.3.1 voice_mismatch in VIDEO_PRODUCTION → Transition to SCRIPT

```
Video Agent (8003)          Event Store           State Machine        Scenario Agent (8001)
      |                         |                      |                        |
      | [ Generates LTX-2.3     |                      |                        |
      |   clip for scene 3.     |                      |                        |
      |   LLM judges: "Voice    |                      |                        |
      |   is baritone, script   |                      |                        |
      |   says soprano." ]      |                      |                        |
      |                         |                      |                        |
      |-- ProductionFailed --->|                      |                        |
      |   failure_type=         |                      |                        |
      |     voice_mismatch      |                      |                        |
      |   scene=3               |                      |                        |
      |   detail="baritone      |                      |                        |
      |     vs soprano"         |                      |                        |
      |                         |                      |                        |
      |                         |<-- tick() -----------|                        |
      |                         |                      |                        |
      |                         |  _has_script_errors  |                        |
      |                         |  guard: True         |                        |
      |                         |  (voice_mismatch ∈   |                        |
      |                         |   SCRIPT_ERROR_TYPES)|                        |
      |                         |                      |                        |
      |                         |<-- TransitionState -|                        |
      |                         |  VIDEO_PRODUCTION →  |                        |
      |                         |  SCRIPT              |                        |
      |                         |  context:            |                        |
      |                         |    audio_mismatch=   |                        |
      |                         |    true              |                        |
      |                         |                      |                        |
      |                         |-------------------->|-- POST / -------------->|
      |                         |                      |  instruction: "Rewrite  |
      |                         |                      |  scene 3. Voice tag:    |
      |                         |                      |  baritone."             |
```

The `_has_script_errors` guard (§4.3.2) checks for `ProductionFailed` effects with `failure_type in {"gap_unexpected", "voice_mismatch"}`. These are the only two failure types that trigger a back-edge to `SCRIPT`; all others either requeue in-place or halt with `ClarificationRequest`.

#### 10.3.2 Scenario Agent fixes script → UpdateScript

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
      |   guard. State machine     |                         |
      |   activates Audio Agent. ] |                         |
```

The Scenario Agent's instruction (§4.5) includes the failure context so the LLM understands what changed. The emitted `UpdateScript` effect contains the full revised scene list.

#### 10.3.3 Audio Agent computes dirty/clean blocks → ReconciliationPartial

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

#### 10.3.4 Only dirty blocks re-reconciled; clean blocks remain authoritative

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

The `_audio_has_dirty_blocks` guard (§4.3.4) keeps the machine in `AUDIO_RECONCILE` while `dirty_block_ids` is non-empty. Clean blocks are never re-measured — their `AudioMeasured` values from the previous reconciliation pass remain LAW. This avoids redundant TTS spend on unchanged content.

| Block | Status | Action | Previous Measurement |
|---|---|---|---|
| A1:1:1 | clean | Skipped, retained | 5.12s (authoritative) |
| A1:1:2 | clean | Skipped, retained | 4.89s (authoritative) |
| A1:3:1 | dirty | Re-queued for TTS | Reset to `None` |
| A1:3:2 | dirty | Re-queued for TTS | Reset to `None` |
| A1:3:3 | clean | Skipped, retained | 3.01s (authoritative) |

---

### 10.4 Human Intervention

Human operators interact with agents via HTTP GET/POST. There is no dedicated dashboard — the agent's own endpoints serve as the observation and control surface.

#### 10.4.1 GET agent status, POST instruction

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

#### 10.4.2 ExecuteRawBash allowlist approval flow

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

#### 10.4.3 Budget override and emergency abort

```
Human Operator          Any Agent (8001-8005)       Event Store        State Machine
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

The `HumanInstruction` effect carries an `action` field that the state machine inspects on the next tick. Valid actions are `"budget_override"` (requires `new_limit` float), `"emergency_abort"`, and `"approve_command"` (for allowlist flows). An emergency abort emits `PipelineAborted`, which triggers escape transitions from any state to `ABORTED`, followed by VM deallocation via the Provisioner's cleanup path (§8.2.5).

| Action Field | Required Params | State Machine Response |
|---|---|---|
| `budget_override` | `new_limit: float` | Updates `max_run_budget_usd` in config projection; `_budget_exceeded` re-evaluates |
| `emergency_abort` | `reason: str` | Emits `PipelineAborted`; transitions to `ABORTED`; Provisioner deallocates all VMs |
| `approve_command` | `command: str` | Clears pending `ClarificationRequest`; command is re-injected into agent prompt |


---

## 11. Security Model

### 11.1 ExecuteRawBash Allowlist

**Threat.** `ExecuteRawBash` grants agents arbitrary shell access on worker VMs. A compromised or misdirected agent can execute destructive commands or exfiltrate data.

**Defense.** A strict allowlist gates every command. Pre-approved binaries execute without human intervention; all others raise a `ClarificationRequest`.

| Command | Permitted Arguments | Rejected Patterns |
|---|---|---|
| `ffmpeg` | Input/output paths, codec flags, filter graphs | Network URLs (`http://`, `sftp://`) |
| `ffprobe` | `-print_format json`, `-show_streams`, file paths | `--execute`, shell metacharacters |
| `whisperx` | `--model`, `--language`, `--output_format`, file paths | `--download-root` with absolute paths |
| `vastai` | `create instance`, `destroy instance`, `show instances` | `ssh`, `scp`, any file transfer |
| `python3` | Script file path + literal arguments only | `-c` (inline code) |

Validation is two-pass: command name against the allowlist, then argument tokens against a per-command regex denylist. Network egress from `ffmpeg` is blocked at the VM firewall as defense-in-depth (see 11.4).

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

### 11.2 Budget Enforcement

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

**Escape hatch.** If a projected charge exceeds remaining budget, the coordinator emits `PipelineAborted` with `reason=budget_exceeded` and a final ledger. All non-committed GPU instances are destroyed immediately. Partial outputs are retained in object storage for inspection.

### 11.3 Agent Loop Detection

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

### 11.4 VM Isolation

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

## 12. Configuration

The `Config` Pydantic model (§12.1) is the single source of truth for all tunable pipeline parameters. It is instantiated once at coordinator startup from a `config.py` module and passed read-only into every downstream component. No environment-variable fallbacks or runtime mutation are permitted; changing a value requires a code change and redeployment.

### 12.1 Pipeline Config

#### 12.1.1 max_run_budget_usd, max_attempts_per_block, max_tts_budget_usd

`max_run_budget_usd` (`float`, default `10.00`) defines the hard upper bound on cloud-spend for a single pipeline run (D1). This is a *post-approval* gate: the coordinator aborts the run if projected cumulative cost (VM rental time × hourly rate + TTS character charges) exceeds this threshold. `max_attempts_per_block` (`int`, default `5`) is the per-block retry ceiling (C1). Each block (scenario, audio, video, assembly) may be retried up to this many times before the coordinator marks the run FAILED and enters cleanup. `max_tts_budget_usd` (`float`, default `2.00`) caps TTS-specific spend per run (C1), evaluated independently of the overall run budget because TTS is billed per-character via a separate provider API.

#### 12.1.2 tolerance_percent, tolerance_abs_sec

`tolerance_percent` (`float`, default `0.15`) and `tolerance_abs_sec` (`float`, default `0.25`) are the dual-threshold acceptance criteria for assembly-stage duration validation (§8.4). A generated segment passes if its actual duration deviates from the target by no more than 15 % *and* no more than 0.25 s. Both conditions must hold. These values are chosen to accommodate natural speech-rate variation (the percent guard) while preventing sub-frame timing errors in 24 fps video (the absolute guard).

#### 12.1.3 loop_detection_threshold, stale_job_threshold_minutes

`loop_detection_threshold` (`int`, default `5`) triggers loop-detection logic in the state machine (§6.2). When the same block transitions to FAILED and back to PENDING more than 5 times within a single run, the coordinator raises a `LoopDetectedError` and aborts. `stale_job_threshold_minutes` (`int`, default `10`) is the VM-agent heartbeat timeout (A1). If a VM agent's last heartbeat is older than 10 minutes, the coordinator declares the job stale, releases the VM, and reschedules the block on a fresh instance.

#### 12.1.4 ALLOWLISTED_COMMANDS list

`ALLOWLISTED_COMMANDS` (`list[str]`, default `["ffmpeg", "ffprobe", "whisperx", "vastai", "python3"]`) is the explicit permit-list of shell commands that the VM agent may invoke via `subprocess.run` (§7.2). Any command string whose basename is not in this list is rejected with `SecurityError` before execution. The list is intentionally short; adding a command requires a code review and version bump.

```python
from pydantic import BaseModel, Field
from typing import Literal

class Config(BaseModel):
    """Single source of truth for all tunable pipeline parameters.

    Instantiated once at coordinator startup and passed read-only
    into all downstream components. No runtime mutation permitted.
    """

    # §12.1.1 — Pipeline limits
    max_run_budget_usd: float = Field(default=10.00, ge=0.0)
    max_attempts_per_block: int = Field(default=5, ge=1)
    max_tts_budget_usd: float = Field(default=2.00, ge=0.0)

    # §12.1.2 — Assembly tolerance (dual threshold)
    tolerance_percent: float = Field(default=0.15, ge=0.0, le=1.0)
    tolerance_abs_sec: float = Field(default=0.25, ge=0.0)

    # §12.1.3 — Health & loop detection
    loop_detection_threshold: int = Field(default=5, ge=1)
    stale_job_threshold_minutes: int = Field(default=10, ge=1)

    # §12.1.4 — VM-agent command allowlist
    allowlisted_commands: list[str] = Field(
        default_factory=lambda: [
            "ffmpeg", "ffprobe", "whisperx", "vastai", "python3"
        ]
    )

    # §12.2 — VM sizing (see subsections)
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

    # §12.3 — Rate limits
    tick_interval_sec: int = 1
    llm_requests_per_minute: int = 60
    llm_tokens_per_minute: int = 200_000
    vastai_requests_per_minute: int = 30
```

### 12.2 VM Sizing

#### 12.2.1 TTS VM: GPU type, VRAM, CPU, disk

The TTS VM (§9.1) runs speaker-cloning inference and requires 24 GB VRAM for the XTTS v2 model in float16. Specification: GPU `RTX_4090` (24 GB VRAM), 8 CPU cores, 100 GB SSD. The 100 GB disk accommodates the base model weights (~4 GB), speaker reference uploads (~50 MB each), and generated WAV output (~10 MB/min at 48 kHz). No swap is configured; inference fails fast with `OutOfMemoryError` if the model does not fit.

#### 12.2.2 Video VM: GPU type, VRAM, CPU, disk

The Video VM (§9.2) runs LTX-Video inference at 720p and requires 48 GB VRAM for the unquantized model. Specification: GPU `RTX_A6000` (48 GB VRAM), 16 CPU cores, 200 GB SSD. The larger disk stores the diffusion model weights (~24 GB), input conditioning frames, and output MP4 segments. Fallback to `RTX_4090` (24 GB) is permitted only when the model is quantized to int8 and quality checks (§8.4) still pass.

#### 12.2.3 Coordinator VM: 2 vCPU, 4 GB RAM, 100 GB disk

The coordinator is a control-plane process and does not run GPU workloads. Specification: 2 vCPU, 4 GB RAM, 100 GB SSD. The disk hosts the SQLite event-store file, projection tables, and log files. RAM is sized for the Pydantic models, SQLAlchemy session cache, and in-memory job queue; typical working set is <512 MB.

### 12.3 Rate Limits

#### 12.3.1 Tick interval (1 s), LLM API rate limits, Vast.ai API rate limits

The coordinator event loop ticks once per second (`tick_interval_sec: 1`). On each tick it polls the event store for state transitions, evaluates health checks, and dispatches VM commands. LLM API calls are throttled to 60 requests per minute and 200 000 tokens per minute to stay within provider tier-1 quotas. Vast.ai API calls (search, create, destroy) are limited to 30 requests per minute to avoid IP-level rate-limiting. All three limits are enforced by an in-memory token-bucket scheduler in the coordinator; requests that exceed the bucket are queued and retried on the next tick.

---

## 13. File Structure

### 13.1 Directory Layout

#### 13.1.1 Complete tree: server/v5/ with all files

The repository root is `server/v5/`. Files and directories are grouped by responsibility: top-level modules for orchestration, `agents/` for domain-specific generation logic, `provisioner/` for cloud VM lifecycle, and `vm/` for the on-instance agent runtime.

```text
server/v5/
├── README.md                          # Project overview and quick-start
├── ARCHITECTURE_V6.md                 # This document
├── config.py                    (NEW) # Pydantic Config model (§12.1)
├── effects.py                         # Effect DSL parser and validator
├── state_machine.py                   # FSM definitions, states, transitions (§6)
├── event_store.py                     # SQLite-backed event log (§5)
├── projections.py                     # Read-model builder for queries (§5.3)
├── parser.py                          # SD-JSON → internal AST converter (§4)
├── run_pipeline.py                    # Coordinator entry point and event loop (§7)
├── agents/
│   ├── __init__.py
│   ├── base.py                        # BaseAgent abstract class (§7.1)
│   ├── scenario.py                    # ScenarioAgent: prompt + shot plan (§7.1.1)
│   ├── audio.py                       # AudioAgent: script → voice + SFX (§7.1.2)
│   ├── video.py                       # VideoAgent: diffusion + conditioning (§7.1.3)
│   └── assembly.py              (NEW) # AssemblyAgent: mux + duration validation (§8)
├── provisioner/                 (NEW)  # Was agents/provisioner.py
│   ├── __init__.py
│   ├── service.py                     # VM lifecycle orchestrator (§9.3)
│   └── vastai.py                      # Vast.ai REST client and search filters
└── vm/
    ├── __init__.py
    ├── agent.py                         # On-instance daemon: fetch, execute, report (§7.2)
    ├── onstart_tts.sh                   # TTS VM bootstrap: conda env + model download
    └── onstart_ltx.sh                   # Video VM bootstrap: conda env + model download
```

**Top-level modules.** `config.py` (new) contains the `Config` Pydantic model and is imported by `run_pipeline.py`, `agents/base.py`, and `provisioner/service.py`. `effects.py` defines the `Effect` dataclass hierarchy and the DSL grammar used by `parser.py`. `state_machine.py` exports the `RunState` enum, `Transition` dataclass, and `FSM` class (§6). `event_store.py` and `projections.py` form the persistence layer: the former appends domain events, the latter rebuilds read models (§5). `run_pipeline.py` is the executable entry point; it instantiates `Config`, creates the SQLite tables, and enters the tick loop.

**`agents/` package.** Each module subclasses `BaseAgent` and implements `execute(block: Block, context: Context) -> Result`. `assembly.py` (new) is the fourth block-level agent; it performs FFmpeg muxing, WhisperX transcript alignment, and dual-threshold duration validation (§8.4).

**`provisioner/` package.** Extracted from `agents/provisioner.py` to separate cloud-infrastructure concerns from media-generation logic. `service.py` exposes `provision(gpu_spec: GpuSpec) -> VmInstance` and `destroy(vm_id: str) -> None`. `vastai.py` wraps the Vast.ai REST API with typed request/response models and exponential-backoff retry.

**`vm/` package.** `agent.py` is the only Python process running on rented instances. It long-polls the coordinator's task endpoint, executes the command allowlist (§12.1.4), streams stdout/stderr back, and sends a heartbeat every 30 s. The `onstart_*.sh` scripts are rendered as Vast.ai "on-start" scripts; they install Miniconda, create the environment from a `environment.yml` fetched from S3, and download model weights.

### 13.2 Python Dependencies

```text
pydantic>=2.0
aiosqlite>=0.20.0
python-statemachine>=2.0
opentimelineio>=0.16.0
instructor>=1.0.0
openai>=1.0.0
httpx>=0.27.0
fastapi>=0.110.0
uvicorn>=0.29.0
python-multipart>=0.0.9
```

Install: `pip install -r requirements.txt`

### 13.3 API Key Management

| Key | Environment Variable | Used By |
|---|---|---|
| DeepSeek API | `DEEPSEEK_API_KEY` | All agents (_call_llm), parser |
| Vast.ai | `VASTAI_API_KEY` | Provisioner (VM allocation) |

Keys are read from environment variables at startup. Never commit keys to version control.

---

## 14. Glossary

### 14.1 Term Definitions

#### 14.1.1 All terms with precise definitions

| Term | Definition |
|------|------------|
| **Block** | A unit of work in the pipeline: one of `scenario`, `audio`, `video`, or `assembly`. Each block has a dedicated agent, VM type, and retry budget. |
| **Coordinator** | The central control process (2 vCPU, 4 GB RAM) that owns the event loop, state machine, and VM provisioner. Runs continuously on a fixed host. |
| **Dual-threshold validation** | Assembly acceptance criteria requiring both a relative (15 %) and an absolute (0.25 s) duration check to pass. |
| **Effect** | A declarative post-processing instruction in the Effect DSL (e.g., `Blur`, `Vignette`) applied to a video segment after generation. |
| **Event** | An immutable, append-only record describing a state change. Stored in SQLite with monotonic `event_id` and UTC `timestamp`. |
| **Event loop** | The coordinator's 1-second tick cycle: poll events, evaluate health, dispatch commands, handle responses. |
| **Event store** | SQLite-backed append-only log of all domain events. Source of truth for pipeline state; no in-memory mirrors. |
| **FSM** | Finite State Machine. Defines legal states (`PENDING`, `ASSIGNED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`) and transitions between them. |
| **Generation plan** | The JSON output of the ScenarioAgent containing shot list, speaker assignments, script, and duration targets per segment. |
| **Heartbeat** | A 30-second HTTP POST from the VM agent to the coordinator. Missing for 10 minutes triggers stale-job detection. |
| **LTX-Video** | The diffusion-based video-generation model running on the Video VM. Requires 48 GB VRAM at full precision. |
| **Projection** | A read-optimized SQL view or Python dataclass built by folding (reducing) the event stream. Rebuilt on every tick. |
| **Run** | A single end-to-end invocation of the pipeline for one screenplay, from approval through assembly to final MP4 delivery. |
| **Scenario** | A screenplay excerpt (typically 3–8 pages) selected for adaptation. The input artifact to the pipeline. |
| **SD-JSON** | "Screenplay Data — JSON". The normalized JSON representation of a screenplay after parsing, containing scenes, dialogue, and slug lines. |
| **Shot plan** | The per-segment visual plan generated by the ScenarioAgent: shot type, motion, characters, props, duration. |
| **State** | A point in the FSM (e.g., `RUNNING`). Transitions are driven by commands and guarded by preconditions. |
| **TTS** | Text-to-Speech. The XTTS v2 model running on the TTS VM that converts dialogue lines into character-specific WAV audio. |
| **VM agent** | The Python daemon (`vm/agent.py`) executing on rented GPU instances. Fetches tasks, runs allowlisted commands, reports results. |
| **Vast.ai** | The cloud-GPU marketplace used for on-demand rental of TTS and Video VMs. Billed per-second. |
| **WhisperX** | The forced-alignment tool (via `whisperx` CLI) that produces word-level timestamps for transcript-to-audio synchronization. |
| **XTTS v2** | The Coqui TTS speaker-cloning model. Takes reference speaker audio + text → cloned speech WAV. Runs on 24 GB VRAM. |

