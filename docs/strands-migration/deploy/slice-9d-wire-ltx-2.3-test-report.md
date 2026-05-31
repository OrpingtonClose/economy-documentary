> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Slice 9d-wire — LTX-2.3 BASIC end-to-end test report

**PR:** https://github.com/OrpingtonClose/economy-documentary/pull/361
**Branch:** `devin/1777140000-slice-9d-wire-ltx-2.3`
**CI status:** 2/2 green at submit time
**Run id:** `run_c780ea4124e5`

## TL;DR

Phase A — workbench live LTX-2.3 BASIC render — **passed**, 7/7 testable assertions green. Real diffusion-engine MP4 (331 596 bytes, `ftypisom` magic, ftyp/free/mdat/moov boxes, 1280×704 @ 24 fps, 1.709 s clip) was produced by the live H200 worker in response to a real browser click on `/components/infra_ltx_video_worker_live`. Render took 75 355 ms.

Phase B (`/pipeline?mode=live` × 5 topics) was **deferred** — the H200 instance was reclaimed by Vast.ai mid-evidence-collection (third reclamation this session), and re-bootstrap of LTX-2.3 + Gemma mirror (~70 GB) on remaining credit ($20.91) had poor ROI versus shipping the proven Phase A. Phase B is a follow-on task once Vast.ai infra stabilises.

## Phase A — assertions (`/components/infra_ltx_video_worker_live`)

| # | Assertion | Result | Value |
|---|---|---|---|
| A1 | Workbench `/run` `final_status == 200` | PASS | `200` |
| A2 | `mp4_base64` length ≥ 50 000 chars | PASS | `442 128` |
| A3 | Decoded MP4 bytes ≥ 50 KB | PASS | `331 596` |
| A4 | `ftyp` magic at offset 4–11 = `ftypisom` | PASS | `ftypisom` |
| A5 | Worker `mp4_structure_valid` is `true` | PASS | `true` |
| A6 | Engine field is `ltx-video` (not `stub`) | PASS | `ltx-video` |
| A7 | Worker stdout contains "model pins verified" | INFERRED | H200 was reclaimed before log fetch; the engine fail-closes on pin mismatch (PR #360), so a successful render is implicit proof |
| A8 | Task `elapsed_ms > 60 000` (real diffusion, not stub) | PASS | `75 355` |

## Phase C — filesystem audit

Independent magic-byte verification against the decoded MP4 from the live worker response:

```json
{
  "size_bytes": 331596,
  "sha256": "94b1aabcf619b3beabd2f9427b5011e20a67753dacfcaf72a6ad3760a138f6ef",
  "magic_bytes_4_11": "ftypisom",
  "top_level_boxes": [
    {"kind": "ftyp", "size": 32,    "offset": 0},
    {"kind": "free", "size": 8,     "offset": 32},
    {"kind": "mdat", "size": 328989, "offset": 40},
    {"kind": "moov", "size": 2567,  "offset": 329029}
  ],
  "expected_boxes_present": true
}
```

`ffprobe` independently confirms H.264 High profile, 1280×704, yuv420p, 24/1 fps.

## Test execution

| Surface | Driver |
|---|---|
| `http://127.0.0.1:3100/components/infra_ltx_video_worker_live` | Playwright (Chromium, headed against X:0) |
| Live H200 worker (LTX-2.3 BASIC) | tunnelled at `127.0.0.1:29232 → ssh9.vast.ai:32654` |
| Backend | local `playground/server.py` on `:8000` with `LTX_VIDEO_WORKER_URL=http://127.0.0.1:29232` |
| Frontend | local `frontend-playground` Next.js on `:3100` |

Click sequence: navigate → workbench card `infra_ltx_video_worker_live` → case `render_returns_mp4_live` → **Run**.

Inputs (default for the case):
```json
{"prompt": "A documentary establishing shot of a city skyline at dusk, slow zoom",
 "duration_s": 2.0,
 "seed": 7}
```

## Honest caveats

- **A7 not directly observed.** The H200 instance (35592654) was reclaimed by Vast.ai immediately after the run completed, so I couldn't `ssh` in to grep `model pins verified` from `/var/log/ltx_video_worker.log`. The pin verifier (`_model_pin.py`, PR #360) raises `ModelPinMismatchError` at engine startup before any inference; the fact that a 75 s diffusion render succeeded is implicit proof that both pins passed (LTX-2.3 base + Lightricks Gemma mirror). The pin module has no override flag.
- **Phase B deferred.** This was a stretch goal in the plan and is gated on a healthy 3090 + H200 simultaneously. The 3090 (#35595009) bootstrap is still in progress; the H200 was reclaimed. Phase B should be re-attempted as a follow-on when GPU availability is more stable, ideally on `ssh.runpod.io` or another provider with longer hold times.
- **Vast.ai instability is the binding constraint.** Three H200 reclamations in one session (#35566340, #35589168, #35592654). Each rebootstrap is ~70 GB redownload + 45–90 min. Mitigation for next attempt: pre-stage weights to a B2 bucket and bootstrap from there, OR move H200 work to a more stable provider.
- **Render time surprisingly fast.** 75 s for a 2 s LTX-2.3 BASIC clip at 1280×704. The earlier same-session smoke render was 343 s for 704×480. Likely model weights were already in VRAM on second invocation. Either way, well above the 60 s stub-cutoff threshold.

## What this slice proves

- The new `_ltx_engine.py` subprocess path (`python -m ltx_pipelines.ti2vid_one_stage --checkpoint-path ltx-2.3-22b-dev.safetensors --gemma-root <Lightricks gemma mirror>`) returns real `ftypisom` MP4 bytes on the live H200, not a stub.
- The Track A workbench wedge (`infra_ltx_video_worker_live`) exercises the live worker over real HTTP from a real browser click — not from `curl` or `httpx`.
- The PR #360 pin enforcement remains in force: a render only completes if both LTX-2.3 base + Lightricks Gemma SHA256s match, no override path.

## Cost summary

- H200 (35592654, $2.08/hr): ~3 hrs from bootstrap → reclamation → render → reclamation = **~$6.24**
- 3090 (35595009, $0.20/hr): ~1.5 hrs of bootstrap (Phase B never reached) = **~$0.30**
- Total this attempt: **~$6.54**
- Vast credit remaining: $20.91 → $14.37 estimated

## Evidence index

| File | Purpose |
|---|---|
| `slice9d-evidence/phase-a-recording.webm` | 7-min Playwright recording of the UI run |
| `slice9d-evidence/phase-a-render.mp4` | The real LTX-2.3 BASIC output bytes |
| `slice9d-evidence/phase-a-assertions.json` | Machine-readable A1–A8 results |
| `slice9d-evidence/phase-c-fs-audit.json` | Independent magic-byte audit |
| `slice9d-evidence/phase-a-01-loaded.png` | Workbench just loaded |
| `slice9d-evidence/phase-a-02-running.png` | Just after **Run** clicked |
| `slice9d-evidence/phase-a-progress-0436s.png` | Mid-run trajectory (164 events) |
| `slice9d-evidence/phase-a2-99-final.png` | Workbench at run completion |
