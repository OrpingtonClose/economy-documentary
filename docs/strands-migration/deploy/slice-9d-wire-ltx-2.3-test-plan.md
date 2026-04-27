# Slice 9d-wire — LTX-2.3 BASIC end-to-end test plan

**PR:** https://github.com/OrpingtonClose/economy-documentary/pull/361
**Branch:** `devin/1777140000-slice-9d-wire-ltx-2.3`
**CI status at plan time:** 2/2 green (Strands unit tests + ADK eval harness)

## What changed in this slice (the thing under test)

1. `server/strands_agents/ltx_video_worker/_ltx_engine.py` — the LTX
   engine no longer calls `diffusers.LTXPipeline`. It now subprocesses
   Lightricks' own BASIC pipeline:
   `python -m ltx_pipelines.ti2vid_one_stage --checkpoint-path
   <ltx-2.3-22b-dev.safetensors> --gemma-root <Lightricks/gemma-3-12b
   mirror>`. Pin verification (PR #360, slice 9d-pin) runs at engine
   startup before the first inference; SHA256 mismatch on either the
   LTX-2.3 base checkpoint or the Lightricks Gemma mirror raises
   `ModelPinMismatchError` with no override path.
2. `server/strands_agents/evals/experiments/infra_ltx_video_worker_live.py`
   — new strands-evals experiment with case `render_returns_mp4_live`
   that POSTs to `$LTX_VIDEO_WORKER_URL` (live H200) instead of an
   in-process `TestClient(stub)`. This is the **Track A** UI wedge:
   it makes `/components/infra_ltx_video_worker_live` exercise the
   real BASIC engine on a real H200 from a real browser click.
3. `server/strands_agents/playground/pipeline_live_real_workers.py` +
   `pipeline_live_demo.py` — when `LTX_VIDEO_WORKER_URL` /
   `QWEN3_TTS_WORKER_URL` are set, the demo agent's
   `launch_visual_production` and `launch_audio_render` tools dispatch
   to the live workers and persist the decoded base64 bodies to
   `<run_dir>/artifacts/<scene_id>-<token>.{wav,mp4}`. This is the
   **Track B** UI wedge: it makes `/pipeline?mode=live` produce real
   bytes on disk per scene.

The user's standing rule applies — every test below initiates from
the browser UI, not from `curl` or SSH. Direct H200 smoke runs done
during bootstrap do not count as proof.

## Worker fleet

| Role | Vast instance | Tunnel | Up? |
| --- | --- | --- | --- |
| LTX-2.3 BASIC (H200) | 35592654 | 127.0.0.1:29232 → ssh9.vast.ai:32654 | yes |
| Qwen3-TTS (3090) | 35592656 | 127.0.0.1:29231 → ssh1.vast.ai:32656 | sshd back after reboot, **workers wiped**, needs re-bootstrap |

Phase A only requires the H200. Phase B requires both. The 3090
re-bootstrap will run in the background while Phase A executes; if
it is healthy by the end of Phase A we run Phase B, otherwise we
ship Phase A as the primary deliverable and call out Phase B as
deferred.

## Phase A — workbench live LTX-2.3 BASIC render (PRIMARY)

**UI surface:** `http://127.0.0.1:3100/components/infra_ltx_video_worker_live`
**Trigger:** select case `render_returns_mp4_live`, click **Run**.
**Expected wall-clock:** ~6–10 min (single 2 s render at 704×480 on
the live H200; same dial as the bootstrap smoke that produced 598 KB
of `ftypisom` MP4).

### Inputs

```json
{
  "prompt": "A documentary establishing shot of a city skyline at dusk, slow zoom",
  "duration_s": 2.0,
  "seed": 7
}
```

### Assertions

| # | Assertion | How verified |
| --- | --- | --- |
| A1 | Workbench Run-result panel shows HTTP 200 | UI panel + `assertions.json` |
| A2 | Response body contains `mp4_base64` ≥ 50 000 chars | UI panel + `assertions.json` |
| A3 | Decoded `mp4_base64` bytes are ≥ 50 KB | `fs-audit.json` |
| A4 | Decoded bytes have `ftypisom` magic at offset 4–11 | `fs-audit.json` |
| A5 | Workbench `structure_valid` field is `true` | UI panel + `assertions.json` |
| A6 | Engine field returned by worker is **not** `"stub"` | response body |
| A7 | Worker stdout (collected via SSH for evidence) contains `model pins verified` for both LTX-2.3 base + Gemma mirror | `worker.log` excerpt |
| A8 | Production-time elapsed_ms in worker response is > 60 000 (real diffusion render, not stub) | response body |

A6+A7+A8 are the three independent ways slice 9d-wire's *new* engine
gets distinguished from the slice 9b stub or any pre-9d-wire diffusers
fallback.

## Phase B — `/pipeline?mode=live` end-to-end multi-worker (STRETCH)

**Gating condition:** the 3090 background re-bootstrap reports
`/health/vram` 200 within Phase A's wall-clock window. If not, Phase B
ships as deferred and the PR comment says so.

**UI surface:** `http://127.0.0.1:3100/pipeline?mode=live`
**Topics (≥5):**

1. `topic="The Federal Reserve"` · 60 s · `en`
2. `topic="How container shipping works"` · 60 s · `en`
3. `topic="What is inflation?"` · 90 s · `en`
4. `topic="Cómo funciona el mercado de valores"` · 60 s · `es`
5. `topic="The history of the gold standard"` · 45 s · `en`

### Per-topic assertions

| # | Assertion | How verified |
| --- | --- | --- |
| B1 | Hidden meta `pipeline-mode-meta` reads `"live"` before submit | DOM check |
| B2 | All 5 stage cells reach `done` (scenario, audio, visual, production, assembly) | DOM check + `assertions.json` |
| B3 | Audio stage `elapsed_ms` > 5 000 (real Qwen3-TTS) | `assertions.json` |
| B4 | Production stage `elapsed_ms` > 30 000 (real LTX-2.3 BASIC, not stub) | `assertions.json` |
| B5 | Both approval gates fold `waiting → resumed: approve` | DOM check |
| B6 | Status pill ends `run.ok` (green) | DOM check |
| B7 | At least one new `*.wav` file in `/tmp/pipeline_live_<token>/artifacts/` with `RIFF…WAVE` magic | `fs-audit.json` |
| B8 | At least one new `*.mp4` file in `/tmp/pipeline_live_<token>/artifacts/` with `ftypisom` magic, size > 50 KB | `fs-audit.json` |
| B9 | Trajectory log final event is `run.ok` | DOM check |

`run_dir` for Phase B is
`tempfile.mkdtemp(prefix="pipeline_live_")` → `/tmp/pipeline_live_*/artifacts/`
per the `_dispatch_pipeline_run` handler in `server/playground.py`.
The audit script snapshots the directory pre-run (must be empty) and
post-run (must contain at least one new MP4 + at least one new WAV
written by the real workers).

## Phase C — filesystem audit (negative control)

For Phase A, the audit decodes `mp4_base64` from the response body
and verifies magic bytes. For Phase B, it walks every per-topic
`run_dir/artifacts/` and confirms each file's magic bytes match its
suffix. A successful Phase C with **zero new files** is a regression
— the workers are stubbed somewhere upstream and the test fails.

## Recording & evidence shape

- One Playwright recording per phase (`phase-a.webm`, `phase-b.webm`).
  Annotated with `record_annotate` `setup` + `test_start` + `assertion`
  events at every transition.
- Two screenshots per topic in Phase B (form filled / final state).
- `assertions.json` (machine-readable list of all A/B assertions with
  pass/fail + numeric values).
- `fs-audit.json` (decoded magic-byte verification for every artifact).
- One real MP4 per phase, attached to the PR comment so a reviewer
  can replay the bytes.
- Vast.ai cost summary (hours × rate, both VMs).

## What this plan does NOT cover

- Real-LLM brain — slice 9d-wire still uses the scripted-LLM brain
  introduced in 9a. Real-LLM is slice 9c (separate, token-spend
  gated).
- Quality tuning — LTX-2.3 BASIC at default dials is what the user
  approved here; quality-knob exploration is post-9d-wire.
- B2 publish — final upload still mocked. The artifact-on-disk audit
  is the proof of bytes; B2 round-trip is a tiny follow-up slice.

## Forbidden actions during testing

- No `curl` / SSH / `httpx` smoke runs against the workers used as
  primary proof. They may be used to *collect* worker logs after a
  UI run, but never as the test trigger.
- No diffusers fallback. If the H200 worker tries to use diffusers
  the pin verifier will raise `ModelPinMismatchError` and the run
  fails closed.
- No silent retry past the third attempt on any UI run. AGENTS.md
  retry policy applies even in the test harness.
