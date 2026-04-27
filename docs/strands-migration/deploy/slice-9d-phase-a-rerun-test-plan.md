# Slice 9d — Phase A re-run (LTX-2.3 BASIC live, A7 = hard PASS)

## Goal

Drive a single 2 s LTX-2.3 BASIC render from
`/components/infra_ltx_video_worker_live` and turn assertion **A7
(model-pin verification observed in worker log)** from INFERRED into a
hard **PASS** by grepping the live H200 worker log for the literal
string emitted by `_ltx_engine._verify_pins` via `logger.info`:

> `model pins verified | ltx=Lightricks/LTX-2.3@76730e6… gemma=Lightricks/gemma-3-12b-it-qat-q4_0-unquantized@d62fe4f…`

This is the only assertion that was inferred in the prior run. Every
other assertion (A1–A6, A8) was already PASS with bytes on disk; this
re-run preserves those and upgrades A7.

## What changed (PR #361)

- Worker engine subprocesses BASIC `python -m
  ltx_pipelines.ti2vid_one_stage` against
  `Lightricks/LTX-2.3` (`76730e6…`) + `Lightricks/gemma-3-12b-it-qat-q4_0-unquantized`
  (`d62fe4f…`)
- Both pins SHA256-verified at first render via `verify_pin()` —
  mismatch = `ModelPinMismatchError`, fail-closed, no override
- New strands-evals Experiment `infra_ltx_video_worker_live` registers
  `/components/infra_ltx_video_worker_live` UI surface; case
  `render_returns_mp4_live` POSTs to live H200 via `LTX_VIDEO_WORKER_URL`

## Where the log line comes from (code grounding)

`_ltx_engine._verify_pins` at
<ref_snippet file="/home/ubuntu/repos/economy-documentary/server/strands_agents/ltx_video_worker/_ltx_engine.py" lines="208-225" />
runs **before** every render — the GPU subprocess only fires after
`verify_pin()` succeeds for both pins. The literal log string is:

```
model pins verified | ltx=%s@%s gemma=%s@%s
```

interpolated via `logger.info` into stdout, captured by systemd to
`/var/log/ltx-video-worker/ltx-video-worker.log`.

## Adversarial framing

If any of the following were broken, the test would still produce the
SAME UI animation but a visibly different outcome:

| Broken thing | What changes |
|---|---|
| Worker not actually pin-checking | Log file would have **0** matches for "model pins verified" — A7 FAIL |
| Worker secretly serving stub | `engine` field == "stub" — A6 FAIL; render < 5 s — A8 FAIL; bytes < 50 KB — A3 FAIL |
| Worker checked pins but mismatched | Render would 500 with `ModelPinMismatchError` — A1 FAIL |
| Worker checked pins of WRONG models | Log would say `ltx=Lightricks/LTX-Video@…` (the old 2B) — A7 FAIL on string match |
| Backend talking to a different worker | H200 log would have 0 hits even if render succeeded — A7 FAIL |

## Primary flow — single test, eight assertions

### Steps (UI driven, Playwright on bundled Chromium)

1. Navigate browser to `http://127.0.0.1:3100/components/infra_ltx_video_worker_live`
2. Wait for the page to load — health panel resolves to `ltx-video-worker reachable`
3. Click case row labelled `render_returns_mp4_live`
4. Verify input editor shows the pre-filled JSON
   (`prompt`, `duration_s=2.0`, `seed=7`)
5. Click the **Run** button (the only `<button>` whose visible text is
   "Run", inside the InputEditor)
6. Wait up to ~120 s for the run to terminate. Live status line
   should show `RUNNING` then `OK`
7. Read the response panel for `mp4_base64` length and the JSON shape
   (`engine`, `elapsed_ms`, `mp4_structure_valid`)
8. As soon as the run terminates (synchronously, before any chance
   of H200 reclamation), SSH into the H200 over the existing tunnel
   and capture:
   ```
   ssh -i ~/.ssh/id_ed25519 -p 37352 -o StrictHostKeyChecking=no \
     root@ssh6.vast.ai \
     "grep -n 'model pins verified' /var/log/ltx-video-worker/ltx-video-worker.log"
   ```
   Save full match line to
   `/home/ubuntu/slice9d-evidence/h200-worker-log.txt`
9. Decode the base64 MP4 bytes and run the magic-byte audit
10. Stop recording, dump assertions.json

### Assertions (every one a hard PASS, no INFERRED)

| # | Assertion | Pass criteria | Why broken impl looks different |
|---|---|---|---|
| **A1** | HTTP run terminates `OK` | `runResult.status == "OK"` rendered in the result panel | If pin check failed → `ERROR` |
| **A2** | `mp4_base64` ≥ 50 000 chars | `len(response.mp4_base64) >= 50000` | Stub returns short payload |
| **A3** | Decoded MP4 ≥ 50 KB | `len(b64decode(mp4_base64)) >= 50_000` | Stub returns tiny mp4 |
| **A4** | `ftypisom` magic at offset 4–11 | `decoded[4:12] == b"ftypisom"` | Random bytes / stub mp4 lacks isom |
| **A5** | `mp4_structure_valid` true | response field == `true` | Backend probe would fail |
| **A6** | `engine` field == `"ltx-video"` (not stub) | string equality | In-process stub returns `"stub"` |
| **A7** | **Worker log contains literal "model pins verified" line** | `grep -c 'model pins verified'` ≥ 1 AND match contains both `ltx=Lightricks/LTX-2.3@76730e6` and `gemma=Lightricks/gemma-3-12b-it-qat-q4_0-unquantized@d62fe4f` | If pin path not running → 0 matches; if WRONG models pinned → string mismatch |
| **A8** | `elapsed_ms` > 60 000 | `response.elapsed_ms > 60_000` | Stub returns ~5 ms |

A7 is captured **synchronously after the run completes** (not after
instance reclamation) by SSH-grep over the active tunnel. The log file
is currently 9 lines (uvicorn startup only, 0 matches for the literal)
— one render flips that to ≥1 with the exact pin strings.

### Recording

Single Playwright recording covering steps 1–8. Annotations:
- `setup`: "navigating to /components/infra_ltx_video_worker_live"
- `test_start`: "It should drive a real LTX-2.3 BASIC render and verify pins in the worker log"
- `assertion`: one per A1–A8 with `test_result=passed`

### Artifacts

All written to `/home/ubuntu/slice9d-evidence/`:

- `phase-a-rerun-recording.webm` — Playwright capture
- `phase-a-rerun.mp4` — decoded LTX-2.3 BASIC bytes from this run
- `phase-a-rerun-assertions.json` — machine-readable A1–A8, **all PASS**, A7 includes the matched log line as its `evidence` field
- `h200-worker-log.txt` — raw grep output from H200

## Out of scope

- Phase B (`/pipeline?mode=live` × N topics) — deferred per prior session,
  not part of this re-run
- Optimization, prompt tuning, scaling — explicitly later slice
