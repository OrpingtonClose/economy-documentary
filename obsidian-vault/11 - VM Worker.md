---
{
  "title": "VM Worker",
  "section": "11",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[10 - Provisioner Agent|Provisioner Agent]] | [[00 - Index|Index]] | [[12 - Data Flows|Data Flows]] ->

# VM Worker


The VM Worker is a **deepagent** on ephemeral GPU instances (port 9000+). Like all
agents, it has `GET /` (health) and `POST /` (receive instructions). It receives
natural language from the Provisioner describing the work to do, executes inference
via `bash_command`, and returns natural language describing the result. The
Provisioner incorporates this response into its own output, from which the parser
extracts effects.

The worker has no event store access, no special dispatch protocol, and no timeout
or self-destruct logic per Principle 4 (§1.4). It is an agent like any other.

---

### 11.0 VM Provisioning and Boot Sequence

#### 11.0.1 How code gets onto the VM

The worker code is **not built into a Docker image**. Instead, Vast.ai's on-start script mechanism downloads and launches the worker at VM boot time. This avoids Docker registry management and image versioning.

**On-start script (`onstart_tts.sh` and `onstart_ltx.sh`):**

```bash
#!/bin/bash
# onstart_tts.sh — runs on VM boot via Vast.ai --onstart-cmd
set -e

# 1. Install dependencies
apt-get update && apt-get install -y python3-pip ffmpeg git

# 2. Clone worker code from the control-plane repo
REPO_URL="https://github.com/org/economy-documentary-work"
git clone --depth 1 "$REPO_URL" /opt/worker
cd /opt/worker/vm_worker

# 3. Install Python dependencies
pip install -r requirements.txt  # fastapi, uvicorn, pydantic, httpx, whisperx

# 4. Download model weights (cached on VM disk)
python3 -m worker.download_weights --model qwen3-tts

# 5. Start the worker server
python3 -m worker.main --port 9000 --role tts
```

**Why on-start script vs. Docker image:**
- **No registry:** Vast.ai instances boot from a base Ubuntu image; the on-start script pulls code and models.
- **No image versioning:** Code updates are deployed by pushing to the repo; new VMs get the latest code automatically.
- **Model caching:** Model weights are downloaded once and cached on the VM's disk. Subsequent job executions reuse the cached weights.

**Who builds it:** The on-start script is stored in the control-plane repository (`vm/onstart_tts.sh`, `vm/onstart_ltx.sh`). The operator (or CI) updates it. The Provisioner agent passes the script path to `vastai create instance`:

```bash
vastai create instance {offer_id} \
  --onstart-cmd "bash /path/to/onstart_tts.sh" \
  --disk 100
```

#### 11.0.2 Worker directory structure

```
/opt/worker/
├── worker/
│   ├── __init__.py
│   ├── main.py           # FastAPI app wrapping deepagent (GET /, POST /)
│   ├── agent.py          # pydantic-deep agent construction
│   ├── executors/
│   │   ├── __init__.py
│   │   ├── tts.py        # Qwen3-TTS subprocess wrapper
│   │   ├── ltx.py        # LTX-2.3 subprocess wrapper
│   │   └── whisperx.py   # WhisperX measurement wrapper
│   ├── quality.py        # Deterministic quality check (ffprobe, file size)
│   └── download_weights.py  # Model weight downloader
├── models/               # Cached model weights (persistent across jobs)
│   ├── qwen3-tts/
│   └── ltx-2.3/
├── outputs/              # Job outputs (ephemeral, streamed via HTTP)
└── requirements.txt
```

#### 11.0.3 Model weight caching

Model weights are large (Qwen3-TTS: ~4 GB; LTX-2.3: ~20 GB). Downloading them per job is prohibitive. The on-start script downloads weights to `/opt/worker/models/` before starting the server. The worker's executors load weights from this directory.

```python
# worker/executors/tts.py
MODEL_PATH = "/opt/worker/models/qwen3-tts"

class TTSExecutor:
    def __init__(self):
        # Load model into VRAM once at startup
        self.model = load_qwen3_tts(MODEL_PATH)

    def run(self, text: str, voice_id: str, output_path: str) -> str:
        # Inference uses pre-loaded model
        return self.model.synthesize(text, voice_id, output_path)
```

**VRAM residency:** The model stays in GPU memory for the lifetime of the VM. This eliminates per-job model load latency (~30–60 seconds).

---

### 11.1 HTTP Surface

#### 11.1.1 GET / (health), POST / (receive job)

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/` | `GET` | None | `VMHealthResponse` JSON |
| `/` | `POST` | Natural language text | Natural language response (or `409` if busy) |

The VM Worker is a **deepagent** with `bash_command` as its only tool. It has
the same HTTP surface as every other media agent in the architecture.

**System prompt:**
```
=== YOUR ROLE ===
You are a VM Worker agent running on a GPU instance. You execute inference
jobs (TTS or video generation) and return results.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- TTS: python3 -m qwen3_tts --text "..." --voice V1 --output /path.wav
- Video: python3 -m ltx_video --prompt "..." --duration 5.0 --output /path.mp4  # LTX-2.3
- Measurement: whisperx /path.wav --model large-v3 --output_format json
- Quality: ffprobe -v error -show_entries format=duration -of json /path
- You have ONE tool: bash_command
- One job at a time. If busy, return 409.

=== COMMUNICATION STYLE ===
[§9.1.1 Communication Style — copy verbatim]
```

`GET /` returns health status. The Provisioner polls this before dispatching
work. `POST /` receives natural language describing the work. The agent runs
inference via `bash_command`, measures output, and returns natural language
describing the result.

If the worker is busy processing a prior job, it returns `409 Conflict`. The
worker holds no queue — exactly one job runs at a time.

---

#### 11.1.2 VM Worker as DeepAgent

The VM Worker is a pydantic-deep agent with `bash_command` as its only tool.
On `POST /`, it receives natural language from the Provisioner describing the
work to perform. The agent reasons about the request, runs inference via
`bash_command`, measures output, and returns natural language describing the
result.

**Example interaction:**

The Provisioner POSTs:
```
Generate TTS audio for block A1:3:1.
Text: "The Federal Reserve raised interest rates by 0.25%."
Voice: V1_Narrator
Output path: /tmp/audio/A1_3_1.wav
```

The VM Worker agent:
1. Receives the text in its prompt
2. Runs: `bash_command("python3 -m tts_infer --text '...' --voice V1 --out /tmp/audio/A1_3_1.wav")`
3. Runs: `bash_command("whisperx /tmp/audio/A1_3_1.wav --model small")` to measure
4. Reads the output files
5. Returns natural language:
```
TTS generation completed for block A1:3:1. Output saved to
/tmp/audio/A1_3_1.wav. Measured duration: 4.23 seconds.
WhisperX transcription matches input text. Quality acceptable.
```

The Provisioner reads this response and incorporates it into its own output,
from which the parser extracts `JobCompleted` or `AudioMeasured` effects.

There is no `JobRequest` schema, no dispatcher, no special protocol. Just
natural language between agents via HTTP.

**Worker invariants:**
- Exactly one job runs at a time. A second `POST /` while busy returns `409 Conflict`.
- The agent catches all exceptions and describes failures in its natural language output.
- Each phase (inference, measurement, quality) is independent. Failure in one phase is described in the output.

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

#### 11.2.4 WhisperX failure handling

WhisperX can fail for three reasons: model load error, audio format incompatibility, or segmentation failure. The worker handles each differently:

```python
async def _measure_with_whisperx(
    audio_path: str, model: str = "large-v3", num_runs: int = 3,
) -> list[float]:
    measurements: list[float] = []
    for run in range(num_runs):
        try:
            cmd = ["whisperx", audio_path, "--model", model,
                   "--language", "en", "--output_format", "json",
                   "--output_dir", "/tmp/"]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode()
                if "CUDA out of memory" in err:
                    raise MeasurementError(f"WhisperX OOM (run {run+1}/{num_runs}): {err}")
                if "Unsupported audio format" in err:
                    raise MeasurementError(f"WhisperX format error: {err}")
                raise MeasurementError(f"WhisperX failed (run {run+1}/{num_runs}): {err}")

            segments = json.loads(stdout.decode()).get("segments", [])
            if not segments:
                measurements.append(0.0)
            else:
                measurements.append(segments[-1]["end"])
        except json.JSONDecodeError:
            raise MeasurementError(f"WhisperX returned invalid JSON (run {run+1}/{num_runs})")
        except Exception as exc:
            raise MeasurementError(f"WhisperX unexpected error (run {run+1}/{num_runs}): {exc}")

    if all(m == 0.0 for m in measurements):
        raise MeasurementError("WhisperX: all three runs returned 0.0s — audio may be silent")

    return measurements
```

**Failure categories reported to the Audio Agent:**

| WhisperX Failure | `failure_category` | Handler Action |
|---|---|---|
| CUDA OOM | `measurement_error` | Audio Agent may requeue with shorter text or different voice |
| Unsupported format | `measurement_error` | Audio Agent may re-encode or re-generate |
| All zeros (silent) | `measurement_error` | Audio Agent treats as TTS failure, requeues |
| JSON decode error | `measurement_error` | Audio Agent retries measurement on same or different VM |

**The Audio Agent (not the VM Worker) decides retry strategy.** The worker
reports failure in its natural language response (e.g., "TTS failed: CUDA OOM")
and resets to `idle`. The Provisioner sees the failure description in the
response and incorporates it into its own output. The parser extracts
`JobFailed` from the Provisioner's text. The Audio Agent's next turn includes
the failure context and may produce text from which the parser extracts
`JobRequeued` with adjusted params.

---

### 11.3 Quality Check

#### 11.3.1 Deterministic output validation (file size, duration, corruption check)

**V7.1 fix:** Replaced LLM-based QC with deterministic checks. This removes
 the only non-agent LLM usage in the pipeline, preserving Invariant 5 intact.
 The worker validates artifacts with ffprobe/file system calls before reporting
 success. This catches gross failures — zero-byte files, extreme duration
 mismatch, container corruption — before they reach the Audio/Video Agent.

```python
async def _quality_check(
    path: str, job_type: str, expected: float | None,
) -> tuple[Literal["pass", "fail"], str]:
    """Deterministic quality check — no LLM involved."""
    size = os.path.getsize(path)
    if size == 0:
        return "fail", "Output file is empty"

    actual = _ffprobe_duration(path) if job_type == "ltx" else None
    if actual is not None and expected is not None:
        if abs(actual - expected) > max(expected * 0.5, 5.0):
            return "fail", f"Duration mismatch (expected {expected}s, got {actual}s)"

    # Container corruption check via ffprobe -v error
    corrupt = _ffprobe_has_errors(path)
    if corrupt:
        return "fail", "Container corruption detected"

    return "pass", "ok"
```

This check is not a substitute for Audio Agent reconciliation (Section 9.2) or Video Agent artistry judgment.

---

### 11.4 Reporting

#### 11.4.1 Return natural language result

The VM Worker returns its result directly in the HTTP response body. The response
is natural language text describing what happened:

**Success example:**
```
TTS generation completed for block A1:3:1. Output saved to
/tmp/audio/A1_3_1.wav. Measured duration: 4.23 seconds.
WhisperX transcription matches input text. Quality acceptable.
```

**Failure example:**
```
TTS generation failed for block A1:3:1. Error: CUDA out of memory.
Tried with batch size 8, VRAM exhausted at 23.7GB/24GB.
No output file produced.
```

The Provisioner reads this response text and incorporates it into its own
natural language output. The parser extracts `JobCompleted`, `AudioMeasured`,
or `JobFailed` effects from the Provisioner's text — not from the VM Worker's
response directly.

There is no `JobResult` schema, no `callback_url`, no separate reporting step.
The HTTP response body IS the report.

#### 11.4.2 No VM-side timeout; no self-destruct

The VM Worker contains no `asyncio.timeout`, `threading.Timer`, `signal.alarm`, heartbeat loop, or self-destruct call. This is the V5 → V6 → V7 change mandated by Principle 4.

| Aspect | V5 | V6 | V7 |
|---|---|---|---|
| Heartbeat | VM polls every 60s | None — VM is passive | None — VM is passive |
| Stale detection | 15 min → `vastai destroy` | Operator monitors via `GET /` | Operator monitors via `GET /` |
| Timer code | `threading.Timer` in VM | No timer code in VM | No timer code in VM |
| Recovery path | VM self-destructs | Provisioner deallocates + requeues job | Provisioner deallocates + requeues job |

If a subprocess hangs (Qwen3-TTS or LTX-2.3 never returns), the VM remains occupied until the operator observes the stuck job via `GET /` on the Provisioner and manually deallocates it. The VM has no awareness of its own lifecycle — it processes jobs until terminated externally.

#### 11.4.3 VM idle detection — Provisioner-side, not VM-side

V7 has **no VM heartbeat, no timeout, no self-destruct** (Principle 4). Idle and stuck VM detection is performed by the **Provisioner agent** through reasoning about projection state, not by the VM itself.

**How the Provisioner detects stuck VMs:**

1. The Provisioner queries the GSA for `Jobs` and `VMs`.
2. It compares `Jobs` (which jobs are pending/running/completed) against `VMs` (which VMs are active).
3. It identifies anomalies:
   - **Idle VM:** VM is active but has no running or pending jobs assigned to it.
   - **Stuck VM:** VM reports `busy` on `GET /` but `Jobs` shows no job as `running` for that VM.
   - **Hung job:** Job status is `running` for longer than the agent deems reasonable (based on memory of typical inference times).
4. The Provisioner describes the anomaly in its natural language output.
5. The parser extracts `VMObserved` or `VMDeallocated` based on the agent's decision.

```python
# Inside the Provisioner's reasoning (not code — the agent decides this)
# Agent reads: VM 67890 (RTX 4090) has been active for 45 min.
#              Jobs shows 0 pending, 0 running for this VM.
#              Last job completed 30 min ago.
# Agent output: "VM 67890 has been idle for 30 min. Cost so far: $0.34.
#                No pending jobs. I will deallocate to save money."
# Parser extracts: VMDeallocated(instance_id="67890", reason="job_done")
```

**Why no VM-side heartbeat:**
- **Principle 4:** No timeouts anywhere. A heartbeat is a timeout mechanism ("if no heartbeat in 60s, consider dead").
- **Simplicity:** The VM runs a FastAPI server and inference subprocesses. Adding a background heartbeat thread introduces concurrency bugs, thread safety issues with CUDA, and complexity.
- **Correctness:** The Provisioner has full context (job queue, VM state, cost). It can make nuanced decisions ("keep this VM for 5 more minutes because a video job is likely to arrive soon") that a simple heartbeat cannot.

**Trade-off:** Stuck VMs cost money until the Provisioner notices them. For a typical run (10–30 min), the cost of a stuck VM for a few minutes is negligible compared to the complexity of heartbeat infrastructure. The operator monitors `GET /` on the Provisioner and can manually deallocate if the agent fails to notice.

---

### 11.5 Durable Artefact Storage (V7.1: HTTP Streaming, B2 Optional)

**V7.1 architectural decision:** By default, artifacts stream back to the
Provisioner via HTTP in the job completion response. No external storage, no
upload step, no credentials on workers. The VM Worker POSTs the result payload
(which may include the artifact bytes or a base64-encoded blob) directly to the
Provisioner's callback endpoint.

If B2 (or S3, R2, etc.) is needed for long-term storage, raw keys are injected
via the cloud provider's startup script or file-injection mechanism. The VM reads
them from a file at runtime. No JWT, no token-vending service, no SDK abstraction
— the agent uses `bash_command("b2 upload-file ...")` directly.

#### 11.5.1 Default: HTTP streaming (no external storage)

The VM Worker's natural language response includes the artifact path:
```
TTS generation completed. Output saved to /tmp/output.wav.
Duration: 5.2 seconds.
```

The Provisioner reads this response and decides what to do with the artifact:
- For short-term assembly: copies the file from the VM via `bash_command("scp ...")`
- For long-term storage: uploads to B2 via `bash_command("b2 upload-file ...")`

The artifact remains on the VM until the VM is destroyed.

#### 11.5.2 Optional: B2 via raw key

If the operator wants B2 storage, they inject the raw key via startup script:

```bash
# Provisioner injects credentials via startup script or cloud-init
# The VM reads /run/secrets/b2_credentials at runtime
vastai create instance ... --onstart-cmd 'echo "B2_KEY_ID=xxx\nB2_KEY=yyy" > /run/secrets/b2_credentials'
```

The VM Worker then uploads directly:

```python
upload_cmd = (
    f"b2 upload-file doc-pipeline-prod "
    f"/tmp/output.wav "
    f"audio/A1_Narration-3-1.wav"
)
bash_command(upload_cmd)  # Agent tool, no wrapper
```

No SDK, no abstraction layer, no credential rotation. If B2 changes its CLI,
the LLM adapts — same resilience argument as §10.2.1.

---

