# Documentary Pipeline Architecture Invariants

These rules are **non-negotiable**. Every agent, session, and operator working
on this pipeline MUST enforce them. Violations waste hours of GPU time and
produce unusable output.

---

## 1. One Model Per VM — Never Share, Never Swap

Each GPU worker VM runs **exactly one model**. Models are loaded at boot and
stay loaded for the lifetime of the VM.

| VM role | Model | Env var |
|---------|-------|---------|
| TTS worker | Qwen3-TTS | `TTS_WORKER_URL` |
| Video worker(s) | LTX-2.3 (bf16, no quantization) | `VIDEO_WORKER_URLS` / `GPU_WORKER_URL` |

- **Never swap models** on a running worker (e.g. unload LTX to load TTS).
- **Never share models** on the same VM (e.g. `--mode both`).
- If you need a new model, **provision a new VM**.

## 2. All Workers Must Be Healthy Before Pipeline Starts

The pipeline runner (`run_pipeline.py`) performs **pre-flight checks** before
any work begins:

- TTS worker reachable + `tts_loaded: true`
- At least one video worker reachable + `ltx_loaded: true`

If any check fails, the pipeline **exits immediately** with a clear error.
It does NOT fall back to synthetic/placeholder media.

## 3. Never Silently Degrade — Fail Hard, Report Loud

If a required service fails mid-run:

- **TTS failure** → pipeline STOPS (raises `RuntimeError`). All video timing
  depends on real narration durations — synthetic audio makes every downstream
  artifact unusable.
- **Video worker failure** → retry on other workers. If all workers fail,
  pipeline STOPS.
- **B2 upload failure** → log warning but continue (non-critical).

Silent fallbacks (e.g. generating silent WAV when TTS is down) are **forbidden**.

## 4. Pipeline Stage Dependencies

```
Scenario (LLM) → Audio/TTS (Qwen3-TTS) → Visual Direction (LLM)
                                            → Video Generation (LTX-2.3)
                                              → Assembly (ffmpeg)
```

- Video timing comes from **real TTS durations** — never estimated.
- Visual direction uses TTS timing to create semantic phrase boundaries.
- Video generation uses visual direction for prompts and TTS timing for clip durations.
- **You cannot skip or fake an upstream stage** — every downstream stage depends on real upstream artifacts.

## 5. QA Immediately After Each Media Artifact

Quality checks happen **right after** each piece of media is created, not
batched at the end:

- TTS clip → validate non-silent, correct duration
- Video clip → brightness/contrast check + Qwen-Omni visual QA
- Blatant AI wonkiness (distorted shapes, morphing, cartoon when photorealistic
  was requested) → reject and regenerate (max 2-3 retries, then escalate)

## 6. Every Artifact Goes to B2 Immediately

Every artifact is uploaded to B2 **as soon as it is created**:

- TTS clips (`.wav` + `.txt` sidecar)
- Video clips (`.mp4` + `_status.json`)
- Pipeline state (`pipeline_state.json`)
- QA results, visual concepts, timelines — everything

This enables content-addressable resume: match by stored topic in
`pipeline_state.json`, not by run ID.

## 7. Resource Allocation

- **Be generous with RAM and disk** — they are cheap.
- **Only VRAM is precious** — plan model loading around available VRAM.
- **bf16 only** — no FP8, no quantization, no offloading tricks.
- Use full-precision weights from HuggingFace, never corrupted/partial downloads.

## 8. Problems Must Be Reported Immediately

Any failure, crash, hang, or degradation must be reported to the operator
**immediately** — not discovered hours later. This includes:

- Worker health check failures
- TTS/video generation errors
- Pipeline stage timeouts (expected vs actual, 2x threshold = alert)
- Model loading failures
- B2 upload failures (non-critical but logged)

## 9. VM Lifecycle

- Provision VMs **before** starting the pipeline (pre-flight enforces this).
- **Terminate VMs** when the pipeline run is complete to stop billing.
- Each project gets its own B2 bucket for artifact storage.
