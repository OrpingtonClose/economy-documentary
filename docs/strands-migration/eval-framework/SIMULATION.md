# SIMULATION — ToolSimulator and ActorSimulator configurations

Evals for components that talk to GPU workers, TTS workers, or a human
escalator use `strands_evals.simulation`. This document specifies the
canonical simulator configurations. Every experiment that hits an external
worker must reuse one of these.

File locations assume `server/strands_agents/evals/simulators/`.

---

## 1. GPU Worker Simulator

**Backs:** `dispatch_video_job`, `check_job_status`, `check_worker_health`.

**share_state_id:** `"video_pipeline"` — one `StateRegistry` instance is
shared across the three tools so a dispatched job can be queried later.

**initial_state_description:**

> GPU worker pool: 2 workers available, queue empty. Each worker has
> `ltx` capability loaded. Jobs take ~90 s on average, 10% fail
> transiently with "CUDA OOM" (retry succeeds), 5% fail persistently
> with "model checkpoint missing". Worker health endpoints return
> `{"status": "ok", "capabilities": ["ltx"], "gpu_mem_free_mb": 12000}`.

**Tool output schemas (pydantic):**

```python
class DispatchResponse(BaseModel):
    job_id: str
    worker_url: str
    queued_at: float

class JobStatus(BaseModel):
    job_id: str
    state: Literal["queued", "running", "succeeded", "failed"]
    progress: float = Field(ge=0.0, le=1.0)
    error: str | None = None
    artifact_url: str | None = None  # B2 URL when succeeded

class WorkerHealth(BaseModel):
    status: Literal["ok", "degraded", "down"]
    capabilities: list[str]
    gpu_mem_free_mb: int
    queue_depth: int
```

**Failure mode library (metadata on Cases):**

| Failure mode | Probability in sim | Expected supervisor action |
|--------------|-------------------|----------------------------|
| `transient_cuda_oom` | 10% | retry |
| `persistent_checkpoint_missing` | 5% | escalate to escalation_supervisor |
| `worker_disconnect_mid_job` | 2% | reassign to second worker |
| `artifact_upload_failure` | 3% | retry upload, not regen |
| `timeout_no_progress` | 1% | escalate |

---

## 2. TTS Worker Simulator

**Backs:** `generate_tts`, `align_whisperx`, `check_tts_health`.

**share_state_id:** `"audio_pipeline"`.

**initial_state_description:**

> Single TTS worker with XTTS-v2 loaded. Generates audio at roughly
> realtime (a 10 s scene takes ~10 s to synthesize). WhisperX alignment
> runs on the same VM and takes roughly 20% of audio duration. Voice
> identity is deterministic per `voice_id`. 1% of requests fail with
> "model reload in progress".

**Tool output schemas:**

```python
class TtsResponse(BaseModel):
    wav_path: str              # absolute path into the test's tmpdir
    duration_sec: float
    voice_id: str
    sample_rate: int = 24000

class WhisperXResponse(BaseModel):
    word_timestamps: list[dict]    # [{"word": "inflation", "start": 1.23, "end": 1.47}, ...]
    total_duration_sec: float
    language: str

class TtsHealth(BaseModel):
    status: Literal["ok", "model_loading", "down"]
    loaded_model: str
    voice_ids_available: list[str]
```

**Failure mode library:**

| Failure mode | Probability | Expected handler |
|--------------|------------|------------------|
| `model_reload_in_progress` | 1% | retry after 30 s |
| `whisperx_misalignment` | 2% | escalate to scenario refiner (re-time) |
| `voice_id_unknown` | — (injected) | hard fail, contract violation |

---

## 3. Escalation Actor Simulator

**Backs:** the user / human side of the escalation supervisor conversation.

**Config:** `ActorSimulator.from_case_for_user_simulator(case, max_turns=8)`.

**Why 8 turns:** empirical — the current production logs show escalations
resolve in ≤ 6 turns in 95% of cases; 8 gives headroom without letting the
conversation sprawl.

**Case catalogue:**

| Case name | Scenario | Expected supervisor outcome |
|-----------|----------|----------------------------|
| `transient_error_retry` | Worker returned "CUDA OOM" once | Instruct supervisor to retry, verify retry decision |
| `persistent_error_escalate` | Same worker fails 3x in a row with checkpoint error | Supervisor escalates to human, halts pipeline |
| `fixable_error_with_hint` | Scene 4 exceeded target duration | Supervisor invokes scenario_refiner, not abort |
| `catastrophic_error_abort` | Two GPU workers dead, no spares | Supervisor aborts, persists state for resume |
| `confusing_mixed_signal` | One worker degraded, another ok | Supervisor partitions workload onto healthy worker |
| `user_overrides_suggestion` | User rejects proposed fix | Supervisor accepts override, logs rationale |
| `user_requests_diagnostic` | User asks "show me the error trace" | Supervisor returns structured diagnostic |
| `unresponsive_user` | User doesn't reply for 2 turns | Supervisor persists and emits interrupt |

All eight are required for the `13-escalation-supervisor` experiment and
are evaluated with `EscalationDecisionEvaluator` +
`InteractionsEvaluator`.

---

## 4. Wiring a simulator into an experiment

```python
from strands_evals import Case, Experiment
from strands_evals.simulation import ToolSimulator

gpu_sim = ToolSimulator(
    tools=["dispatch_video_job", "check_job_status", "check_worker_health"],
    share_state_id="video_pipeline",
    initial_state_description=GPU_POOL_DESCRIPTION,
    tool_output_schemas={
        "dispatch_video_job": DispatchResponse,
        "check_job_status": JobStatus,
        "check_worker_health": WorkerHealth,
    },
)

async def task(case: Case) -> dict:
    # simulator-provided tools replace the real ones at eval time
    agent = build_production_supervisor(tool_overrides=gpu_sim.tools)
    result = await agent.invoke_async(case.input, invocation_state=case.metadata)
    return {
        "output": result.message,
        "trajectory": [t.name for t in result.tool_uses],
        "environment_state": gpu_sim.state.snapshot(),
    }

experiment = Experiment(cases=CASES, evaluators=[...])
reports = await experiment.run_evaluations_async(task)
```

The same pattern applies to `TTS_WORKER_SIMULATOR` for audio experiments
(component 04, 05).

---

## 5. What simulators do NOT cover

- **Real GPU latency distributions.** The sim gives us correctness, not
  performance. Latency benchmarks remain the job of the integration tests in
  `tests_integ/` and live canary runs.
- **Real model drift.** If the LLM's output shape changes (e.g. new
  Venice params), the simulator won't catch it. That's what the nightly
  integration job is for (see [`CI_PIPELINE.md`](./CI_PIPELINE.md)).
- **Thundering-herd / quota errors.** These surface only under
  concurrency, which simulators single-thread by default. Mark cases that
  exercise parallelism with `metadata={"parallel_workers": N}` and run them
  serially with contention injected via the sim's `state.inject_error` hook.
