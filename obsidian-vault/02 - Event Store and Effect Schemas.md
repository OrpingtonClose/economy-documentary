---
{
  "title": "Event Store and Effect Schemas",
  "section": "2",
  "tags": [
    "architecture",
    "v7.1",
    "event-store",
    "schemas"
  ]
}
---

[[00 - Index|◀ Back to Index]]

# 🗄️ Event Store & Effect Schemas

This module defines the complete family of **Pydantic Effect Schemas** and the implementation details of the **SQLite WAL Event Store** used as the pipeline's sole source of truth.

---

## 1. Pydantic Effect Schemas

All mutations in the system are represented by typed Pydantic models derived from a common `Effect` base class.

### 1.1 Base Effect Model

Every event appended to the event store inherits from `Effect`. It contains metadata for validation, tracking, and idempotency.

```python
class Effect(BaseModel):
    """Base for all effect types. NEVER instantiated directly.

    Fields present on every effect emitted into the event store:
    - effect_id:     UUIDv7 generated client-side for idempotent retries
    - kind:          Literal discriminant string (overridden per subclass)
    - agent:         component that produced the effect (e.g. "scenario")
    - timestamp:     seconds since epoch at creation time
    """
    effect_id: UUID = Field(default_factory=uuid7)
    kind: str = "effect"  # overridden per subclass via Literal
    agent: str
    timestamp: float = Field(default_factory=time.time)
```

---

### 1.2 Script Effects

These effects dictate the logical structure and textual narration of the documentary.

```python
class ScriptBlock(BaseModel):
    """A single narration block within an UpdateScript."""
    scene_num: int = Field(..., ge=1, description="1-based scene index")
    block_id: str = Field(..., description="stable identifier for this narration block")
    speaker: str = Field(..., description="voice role: narrator, guest_a, etc.")
    text: str = Field(..., min_length=1, description="narration text")
    pronunciation_hints: list[str] = Field(default_factory=list)
    visual_notes: str = ""
    dopamine_hook: str = ""
    duration_sec: float = Field(..., gt=0.0, description="target duration in seconds")


class UpdateScript(Effect):
    """Write or revise one or more scene narration blocks."""
    kind: Literal["update_script"] = "update_script"
    blocks: list[ScriptBlock] = Field(..., min_length=1)


class DeleteScene(Effect):
    """Remove a scene and all its narration blocks from the timeline."""
    kind: Literal["delete_scene"] = "delete_scene"
    scene_num: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1)


class ReorderScenes(Effect):
    """Change scene order. new_order[i] is the scene_num that should occupy position i+1."""
    kind: Literal["reorder_scenes"] = "reorder_scenes"
    new_order: list[int] = Field(..., min_length=1)
```

---

### 1.3 Job Effects

These effects manage the job queue for media compilation (TTS and Video rendering).

```python
class QueueJob(Effect):
    """Demand creation of a media artifact by a VM worker."""
    kind: Literal["queue_job"] = "queue_job"
    job_id: str = Field(..., description="stable unique job identifier")
    job_type: Literal["tts", "ltx"]
    scene_num: int = Field(..., ge=1)
    block_id: str
    slot_id: str = Field(..., description="OTIO slot where the result belongs")
    params: dict = Field(default_factory=dict, description="type-specific generation params")


class JobStarted(Effect):
    """VM worker accepted the job. Job is now running."""
    kind: Literal["job_started"] = "job_started"
    job_id: str
    vm_instance_id: str
    started_at: float = Field(default_factory=time.time)


class JobCompleted(Effect):
    """VM worker finished successfully; artifact is ready for quality review."""
    kind: Literal["job_completed"] = "job_completed"
    job_id: str
    artifact_uri: str = Field(..., description="URI to generated file")
    duration_sec: float = Field(..., ge=0.0, description="actual media duration")
    vm_instance_id: str
    measurements: list[float] = Field(
        default_factory=list,
        description="WhisperX measurements from VM worker (3 runs)",
    )


class JobFailed(Effect):
    """VM worker failed."""
    kind: Literal["job_failed"] = "job_failed"
    job_id: str
    error_message: str
    failure_category: Literal[
        "oom",           # GPU out of memory
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
    """Artistry rejection: previous output did not meet quality bar."""
    kind: Literal["job_requeued"] = "job_requeued"
    job_id: str
    reason: str = Field(..., min_length=1, description="why the previous attempt was rejected")
    new_params: dict | None = None


class JobApproved(Effect):
    """Artistry approval: artifact passes quality review, ready for OTIO merge."""
    kind: Literal["job_approved"] = "job_approved"
    job_id: str
    artifact_uri: str
    quality_notes: str = ""
    reviewed_by: str = Field(default="agent", description="'agent' or human name")
```

---

### 1.4 Reconciliation Effects

These effects handle synchronization and duration validation of TTS tracks.

```python
class ReconciliationFailureDetail(BaseModel):
    """Per-block failure diagnostic embedded in ReconciliationFailed."""
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


class AudioGenerated(Effect):
    """TTS WAV produced by a VM worker. Artifact awaits WhisperX measurement."""
    kind: Literal["audio_generated"] = "audio_generated"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    artifact_uri: str


class AudioMeasured(Effect):
    """WhisperX measured the actual spoken duration of a generated WAV."""
    kind: Literal["audio_measured"] = "audio_measured"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    measured_sec: float = Field(..., description="median of measurements")
    measurements: list[float] = Field(
        default_factory=list,
        description="all three WhisperX measurements, unsorted",
    )
    whisperx_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DurationAdjusted(Effect):
    """Measured duration within tolerance; OTIO slot updated with authoritative value."""
    kind: Literal["duration_adjusted"] = "duration_adjusted"
    block_id: str
    slot_id: str = Field(..., description="Canonical slot address, e.g. 'A1:3:2'")
    scene_num: int
    voice_role: str
    scripted_sec: float
    measured_sec: float


class ReconciliationFailed(Effect):
    """One or more blocks failed the tolerance check. Retry or escalate."""
    kind: Literal["reconciliation_failed"] = "reconciliation_failed"
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    failures: list[ReconciliationFailureDetail] = Field(default_factory=list)
    worst_delta_sec: float
    suggested_adjustments: list[dict] = Field(default_factory=list)
    failure_type: Literal["duration_mismatch", "duration_unrecoverable"] = "duration_mismatch"


class ReconciliationComplete(Effect):
    """All narration blocks pass tolerance. OTIO is now authoritative."""
    kind: Literal["reconciliation_complete"] = "reconciliation_complete"
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    worst_delta_sec: float
    total_measured_sec: float
```

---

### 1.5 VM Effects

These effects monitor VM health and state.

```python
class VMAllocated(Effect):
    """GPU instance created and ready for job assignment."""
    kind: Literal["vm_allocated"] = "vm_allocated"
    instance_id: str = Field(..., description="Vast.ai instance ID")
    role: Literal["tts", "ltx"] = Field(..., description="worker role determines GPU type")
    offer_id: str = Field(..., description="Vast.ai offer ID that was accepted")
    worker_url: str = Field(..., description="full URL including port, e.g. http://1.2.3.4:9000")
    gpu_type: str = Field(..., description="GPU model, e.g. 'RTX 4090'")
    cost_per_hour: float = Field(..., gt=0.0)


class VMDeallocated(Effect):
    """GPU instance destroyed. Final cost is recorded for budget tracking."""
    kind: Literal["vm_deallocated"] = "vm_deallocated"
    instance_id: str
    reason: Literal[
        "job_done",       # worker finished all assigned jobs
        "cost_limit",     # exceeded per-VM cost threshold
        "stale",          # operator manually deallocated stuck VM
        "provision_failed",  # never reached healthy state
        "manual",         # human operator destroyed via instruction
    ]
    final_cost: float = Field(default=0.0, ge=0.0)
    runtime_sec: float = Field(default=0.0, ge=0.0)


class VMProvisionFailed(Effect):
    """Provisioner could not create a VM for a pending job."""
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
    """Provisioner detected drift between event-derived VM state and Vast.ai reality."""
    kind: Literal["vm_observed"] = "vm_observed"
    instance_id: str
    observed_status: Literal["running", "offline", "not_found", "unknown"] = Field(..., description="what Vast.ai API reports")
    expected_status: Literal["running", "offline", "not_found", "unknown"] = Field(..., description="what VMs believe")
    drift_description: str = Field(..., description="human-readable drift summary")
    corrective_action: Literal["none", "escalate"] = "none"
```

---

### 1.6 OTIO / Timeline Effects

These effects manipulate clip placement in the timeline stack.

```python
class MergeIntoOTIO(Effect):
    """Approved clip enters the OTIO timeline at the specified track and slot."""
    kind: Literal["merge_into_otio"] = "merge_into_otio"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str = Field(..., description="Canonical slot address, e.g. 'A1:3:2'")
    artifact_uri: str = Field(..., description="URI to the approved media file")
    track_name: Literal["A1_Narration", "V1_Video"] = Field(..., description="Target track")
    duration_sec: float = Field(..., gt=0.0)
    transition_type: Literal["cut", "dissolve", "none"] = "cut"
    transition_duration_sec: float = Field(default=0.0, ge=0.0)


class DeleteFromOTIO(Effect):
    """Remove a clip from the OTIO timeline."""
    kind: Literal["delete_from_otio"] = "delete_from_otio"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str
    track_name: Literal["A1_Narration", "V1_Video", "both"]
    reason: str = Field(..., min_length=1)
```

---

### 1.7 Pipeline & Budget Effects

These effects manage run lifecycles and cost-limit constraints.

```python
class PipelineStarted(Effect):
    """Signal that a new pipeline run has begun."""
    kind: Literal["pipeline_started"] = "pipeline_started"
    config: dict = Field(default_factory=dict, description="pipeline configuration snapshot")
    max_tts_budget_usd: float = Field(default=2.0, gt=0.0)
    max_attempts_per_block: int = Field(default=5, ge=1)
    max_run_budget_usd: float = Field(default=10.0, gt=0.0)
    output_path: str = Field(default="/tmp/final_documentary.mp4")


class PipelineComplete(Effect):
    """Assembly finished. Final MP4 validated and ready."""
    kind: Literal["pipeline_complete"] = "pipeline_complete"
    output_path: str
    duration_sec: float = Field(..., ge=0.0)
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    validation_passed: bool = True


class PipelineAborted(Effect):
    """Unrecoverable stop."""
    kind: Literal["pipeline_aborted"] = "pipeline_aborted"
    reason: Literal["budget_exceeded", "vm_unavailable", "human_request", "loop_detected", "unknown"]
    error_log: list[str] = Field(default_factory=list)
    spent_usd: float = Field(default=0.0, ge=0.0)


class VASTGlobalStateObserved(Effect):
    """Global Vast.ai account state observed by the Provisioner."""
    kind: Literal["vast_global_state_observed"] = "vast_global_state_observed"
    credit_balance_usd: float = Field(default=0.0, description="Current Vast.ai credit balance")
    active_instance_count: int = Field(default=0, ge=0)
    current_billing_rate_usd_hr: float = Field(default=0.0, ge=0.0)
    observed_at: float = Field(default_factory=time.time)


class BudgetSet(Effect):
    """Run budget established or updated."""
    kind: Literal["budget_set"] = "budget_set"
    budget_usd: float = Field(..., gt=0.0)
    reason: str = Field(default="run_start", description="run_start or operator_override")


class BudgetExceeded(Effect):
    """Cumulative spend exceeded the run budget."""
    kind: Literal["budget_exceeded"] = "budget_exceeded"
    spent_usd: float = Field(..., ge=0.0)
    limit_usd: float = Field(..., gt=0.0)
```

---

### 1.8 Human, Loop & Production Failures

These effects handle exceptions, manual redirection, and fallback loops.

```python
class HumanInstruction(Effect):
    """Human operator posted a directive to a specific agent."""
    kind: Literal["human_instruction"] = "human_instruction"
    target_agent: str = Field(..., description="target agent name or 'all'")
    instruction: str = Field(..., min_length=1)
    from_human: str = Field(..., description="human identifier")
    posted_at: float = Field(default_factory=time.time)
    priority: Literal["normal", "urgent", "blocking"] = "normal"
    action: Literal["budget_override", "emergency_abort", "approve_command", "revoke", "generic"] = "generic"
    action_params: dict = Field(default_factory=dict)


class ClarificationRequest(Effect):
    """Parser or agent needs human input to proceed."""
    kind: Literal["clarification_request"] = "clarification_request"
    target_agent: str = Field(default="human", description="target agent")
    parser_category: str = ""
    raw_text: str = ""
    failure_reason: str = Field(..., description="why clarification is needed")
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    suggested_resolution: str = ""
    question: str = ""
    referenced_text: str = ""


class AgentLoopDetected(Effect):
    """An agent detected it is stuck in a loop."""
    kind: Literal["agent_loop_detected"] = "agent_loop_detected"
    agent: str = Field(..., description="agent that is looping")
    loop_signature: str = Field(..., description="concatenated kind sequence")
    effect_sequence: list[str] = Field(default_factory=list, description="last N effect kinds")
    detection_mode: Literal["duplicate_effects", "alternating", "noop", "both"] = "both"
    detection_count: int = Field(..., ge=1, description="loop count")


class NoOp(Effect):
    """Informational effect carrying no state mutation."""
    kind: Literal["noop"] = "noop"
    reason: str = Field(default="no_effects_extracted")
    agent_context: str = ""


class SuggestedFix(BaseModel):
    """Structured fix proposal."""
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
    retry_count_suggestion: int = Field(default=3, ge=0, description="try N times")
    rationale: str = ""


class ProductionFailed(Effect):
    """Media production or assembly failure with structured suggested fix."""
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
    expected: str = ""
    actual: str = ""
    suggested_fix: SuggestedFix = Field(default_factory=SuggestedFix)
    vm_instance_id: str = ""
    attempt_number: int = Field(default=1, ge=1)


class MeasurementRequested(Effect):
    """Demand WhisperX measurement of generated audio clip."""
    kind: Literal["measurement_requested"] = "measurement_requested"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str


class VideoMeasured(Effect):
    """Ffprobe or similar measured the actual duration of generated video clip."""
    kind: Literal["video_measured"] = "video_measured"
    job_id: str
    block_id: str
    measured_sec: float
```

---

### 1.9 Discriminator Union

```python
EffectUnion = Annotated[
    Union[
        UpdateScript, DeleteScene, ReorderScenes,
        QueueJob, JobStarted, JobCompleted, JobFailed, JobRequeued, JobApproved,
        AudioGenerated, AudioMeasured, DurationAdjusted, ReconciliationFailed, ReconciliationComplete,
        VMAllocated, VMDeallocated, VMProvisionFailed, VMObserved,
        MergeIntoOTIO, DeleteFromOTIO,
        PipelineStarted, PipelineComplete, PipelineAborted, VASTGlobalStateObserved, BudgetSet, BudgetExceeded,
        HumanInstruction, ClarificationRequest, AgentLoopDetected, NoOp,
        ProductionFailed, MeasurementRequested, VideoMeasured
    ],
    Field(discriminator="kind"),
]
```

---

## 2. Event Store

The event store uses **SQLite** (single file, WAL mode) to guarantee cross-process atomicity, global sequence allocation, and idempotent writes via `UNIQUE(effect_id)` constraints.

### 2.1 Schema

The database contains a single primary table named `events`:

```sql
CREATE TABLE events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    effect_id      TEXT UNIQUE NOT NULL,   -- idempotency key (UUIDv7)
    kind           TEXT NOT NULL,           -- effect discriminant
    effect_json    TEXT NOT NULL,           -- Pydantic model_dump_json()
    otio_hash_before TEXT NOT NULL,         -- OTIO state hash at append time
    agent          TEXT NOT NULL,           -- agent that produced the event
    timestamp      REAL NOT NULL,           -- wall-clock epoch seconds
    appended_at    REAL DEFAULT (unixepoch())
);

CREATE INDEX idx_events_kind ON events(kind);
CREATE INDEX idx_events_agent ON events(agent);
```

### 2.2 EventStore Class

```python
class EventStore:
    """Append-only SQLite event store. Stored in a single events.db.

    Cross-process safe via SQLite WAL mode + BEGIN IMMEDIATE.
    """

    def __init__(self, log_dir: str) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.log_dir / "events.db"
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    effect_id TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL,
                    effect_json TEXT NOT NULL,
                    otio_hash_before TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    appended_at REAL DEFAULT (unixepoch())
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent)")
            conn.commit()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def append(self, effect: Effect, otio_hash_before: str) -> EventRecord:
        """Append an effect. Idempotent via UNIQUE(effect_id)."""
        effect_id = str(effect.effect_id)
        kind = effect.kind
        effect_json = effect.model_dump_json()
        agent = effect.agent
        ts_method = getattr(effect.timestamp, "timestamp", None)
        timestamp = ts_method() if ts_method else float(effect.timestamp)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT INTO events
                       (effect_id, kind, effect_json, otio_hash_before, agent, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (effect_id, kind, effect_json, otio_hash_before, agent, timestamp),
                )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return self._find_by_effect_id(effect_id)

            cur = conn.execute("SELECT seq FROM events WHERE effect_id = ?", (effect_id,))
            seq = cur.fetchone()[0]
            conn.execute("COMMIT")

        return EventRecord(
            seq=seq,
            effect=cast(EffectUnion, effect),
            otio_hash_before=otio_hash_before,
        )
```

---

## A. Appendix: EventStoreDB Migration Path

For large scale distributed deployments, the SQLite backend can be seamlessly swapped with **EventStoreDB** using client-side streams.

### A.1 Distributed ESDB Client Implementation

```python
from esdbclient import EventStoreDBClient, NewEvent, StreamState

client = EventStoreDBClient(uri="esdb://localhost:2113?tls=false")

async def append_effect_esdb(
    effect: Effect, otio_hash_before: str, causation_id: str = "", correlation_id: str = ""
) -> int:
    stream_name = "global-stream"
    event = NewEvent(
        type=effect.kind,
        data=effect.model_dump_json().encode(),
        metadata=json.dumps({
            "agent": effect.agent,
            "timestamp": effect.timestamp,
            "otio_hash_before": otio_hash_before,
            "causation_id": causation_id or str(effect.effect_id),
            "correlation_id": correlation_id or str(effect.effect_id),
        }).encode(),
        event_id=str(effect.effect_id),
    )
    recorded = await client.append_to_stream(
        stream_name=stream_name, events=[event], current_version=StreamState.ANY,
    )
    return recorded.next_expected_version
```