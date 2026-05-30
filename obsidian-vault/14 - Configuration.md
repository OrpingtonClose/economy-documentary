---
{
  "title": "Configuration",
  "section": "14",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[13 - Security Model|Security Model]] | [[00 - Index|Index]] | [[15 - File Structure|File Structure]] ->

# Configuration


The `Config` Pydantic model (§14.1) is the single source of truth for all tunable pipeline parameters. It is instantiated once at control plane startup from a `config.py` module and passed read-only into every downstream component. No environment-variable fallbacks or runtime mutation are permitted; changing a value requires a code change and redeployment.

### 14.1 Pipeline Config

#### 14.1.1 max_run_budget_usd, max_attempts_per_block, max_tts_budget_usd

`max_run_budget_usd` (`float`, default `10.00`) defines the hard upper bound on cloud-spend for a single pipeline run. This is a *post-approval* gate: the agent aborts the run if projected cumulative cost exceeds this threshold. `max_attempts_per_block` (`int`, default `5`) is the per-block retry ceiling. Each block may be retried up to this many times before the agent marks the run FAILED and enters cleanup. `max_tts_budget_usd` (`float`, default `2.00`) caps TTS-specific spend per run, evaluated independently of the overall run budget because TTS is billed per-character via a separate provider API.

#### 14.1.2 tolerance_percent, tolerance_abs_sec

`tolerance_percent` (`float`, default `0.15`) and `tolerance_abs_sec` (`float`, default `0.25`) are the dual-threshold acceptance criteria for assembly-stage duration validation. A generated segment passes if its actual duration deviates from the target by no more than 15 % *and* no more than 0.25 s. Both conditions must hold. These values are chosen to accommodate natural speech-rate variation (the percent guard) while preventing sub-frame timing errors in 24 fps video (the absolute guard).

#### 14.1.3 loop_detection_threshold

`loop_detection_threshold` (`int`, default `5`) triggers loop-detection logic in the agent. When the same block transitions to FAILED and back to PENDING more than 5 times within a single run, the agent raises a `LoopDetectedError` and aborts. There is no automatic stale-job detection; the operator monitors via `GET /` and intervenes manually.

#### 14.1.4 ALLOWLISTED_COMMANDS list

`ALLOWLISTED_COMMANDS` (`list[str]`, default `["ffmpeg", "ffprobe", "whisperx", "vastai", "python3"]`) is the explicit permit-list of shell commands that the VM agent may invoke via `subprocess.run`. Any command string whose basename is not in this list is rejected with `SecurityError` before execution. The list is intentionally short; adding a command requires a code review and version bump.

```python
from pydantic import BaseModel, Field
from typing import Literal

class Config(BaseModel):
    """Single source of truth for all tunable pipeline parameters.

    Instantiated once at control plane startup and passed read-only
    into all downstream components. No runtime mutation permitted.
    """

    # 14.1.1 — Pipeline limits
    max_run_budget_usd: float = Field(default=10.00, ge=0.0)
    max_attempts_per_block: int = Field(default=5, ge=1)
    max_tts_budget_usd: float = Field(default=2.00, ge=0.0)
    max_queue_wait_sec: float = Field(default=300.0, ge=0.0)
    # V7.1: max_queue_wait_sec guides agent judgment on stale job detection

    # 14.1.2 — Assembly tolerance (dual threshold)
    tolerance_percent: float = Field(default=0.15, ge=0.0, le=1.0)
    tolerance_abs_sec: float = Field(default=0.25, ge=0.0)

    # 14.1.3 — Health & loop detection
    loop_detection_threshold: int = Field(default=5, ge=1)

    # 14.1.4 — B2 storage
    # V7.1: B2 credentials removed. Artifacts stream back via HTTP.
    # If B2 is needed later, raw keys are injected via startup script
    # or file-injection at VM creation. No env vars.

    # 14.1.5 — VM-agent command allowlist
    allowlisted_commands: list[str] = Field(
        default_factory=lambda: [
            "ffmpeg", "ffprobe", "whisperx", "vastai", "python3"
        ]
    )

    # 14.1.6 — Event store
    log_dir: str = Field(default="/tmp/events", description="SQLite event store directory")
    # V7.1 fix: Added log_dir (used by EventStore §5.1.3)

    # 14.1.7 — Agent models
    agent_models: dict[str, str] = Field(
        default_factory=lambda: {
            "scenario": "openrouter:deepseek/deepseek-v4-flash",
            "audio": "openrouter:deepseek/deepseek-v4-flash",
            "video": "openrouter:deepseek/deepseek-v4-flash",
            "assembly": "openrouter:deepseek/deepseek-v4-flash",
            "provisioner": "openrouter:deepseek/deepseek-v4-flash",
        },
        description="Model name per agent role (used by create_pipeline_agent §8.2)",
    )
    # V7.1 fix: Added agent_models dict (was referenced but undefined)
    context_manager_max_tokens: int = Field(default=128_000, ge=1)
    compaction_model: str = Field(default="openrouter:deepseek/deepseek-v4-flash")
    # V7.1 fix: Added context manager and compaction model settings

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
    context_manager_max_tokens: int = 128_000
    compaction_threshold: float = 0.85

    # VMs
    vastai_api_key: str = ""
    vm_tts_image: str = "vastai/worker:tts"
    vm_ltx_image: str = "vastai/worker:ltx"

    # V7 additions
    max_tts_cost_hr: float = 0.80
    max_ltx_cost_hr: float = 1.20
    eventstore_uri: str = "esdb://localhost:2113?tls=false"
```

### 14.2 VM Sizing

#### 14.2.1 TTS VM: GPU type, VRAM, CPU, disk

The TTS VM (§11.1) runs speaker-cloning inference and requires 24 GB VRAM for the Qwen3-TTS model in float16. Specification: GPU `RTX_4090` (24 GB VRAM), 8 CPU cores, 100 GB SSD. The 100 GB disk accommodates the base model weights (~4 GB), speaker reference uploads (~50 MB each), and generated WAV output (~10 MB/min at 48 kHz). No swap is configured; inference fails fast with `OutOfMemoryError` if the model does not fit.

#### 14.2.2 Video VM: GPU type, VRAM, CPU, disk

The Video VM (§11.2) runs LTX-Video inference at 720p and requires 48 GB VRAM for the unquantized model. Specification: GPU `RTX_A6000` (48 GB VRAM), 16 CPU cores, 200 GB SSD. The larger disk stores the diffusion model weights (~24 GB), input conditioning frames, and output MP4 segments. Fallback to `RTX_4090` (24 GB) is permitted only when the model is quantized to int8 and quality checks (§11.3) still pass.

#### 14.2.3 Control Plane Host: 2 vCPU, 4 GB RAM, 100 GB disk

The control plane host runs the agent services and the SQLite event store. It does not run GPU workloads. Specification: 2 vCPU, 4 GB RAM, 100 GB SSD. The disk hosts projection state, log files, and agent code. RAM is sized for the Pydantic models and in-memory job queue; typical working set is <512 MB.

### 14.3 Rate Limits

#### 14.3.1 LLM and Vast.ai API rate limits

There is no central coordinator event loop in V7. Rate limits are enforced per-agent at the HTTP client level.

| Resource | Limit | Enforcement |
|---|---|---|
| LLM API requests | 60 per minute | Per-agent `AsyncLimiter` in the agent handler |
| LLM API tokens | 200 000 per minute | Per-agent token counter, resets every 60 s |
| Vast.ai API calls | **Max 3 concurrent** | Global semaphore across all Provisioner activations |

**Why max 3 concurrent Vast.ai calls:** The Vast.ai API has aggressive IP-level rate limiting. Exceeding 3 concurrent `search` / `create` / `destroy` calls triggers 429 responses. The Provisioner serializes VM operations through a `asyncio.Semaphore(3)`.

LLM rate limits are per-agent because each agent runs in a separate process and there is no shared scheduler.

---

