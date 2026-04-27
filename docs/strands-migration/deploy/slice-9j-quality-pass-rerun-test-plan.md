# Slice 9j-quality-pass — re-run test plan (real assembly + real B2 wired)

## What changed since the previous attempt

The previous slice 9j run completed `run.ok` end-to-end (11/12 hard-PASS) but
**J9** was not a hard-PASS because `launch_assembly` and `launch_b2_sync`
elapsed_ms was 1ms each — confirming `pipeline_live_demo._demo_tools()` was
binding the `_placeholders.launch_assembly` / `_placeholders.launch_b2_sync`
echoes, not the real ffmpeg + B2 implementations shipped in slices 9g/9h.

The real overlays are env-gated:

* `_real_assembly_tools.build_real_assembly_tools(enabled=…)` → empty dict
  unless `ENABLE_REAL_ASSEMBLY=1`.
* `_real_b2_tools.build_real_b2_tools(enabled=…)` → empty dict unless
  `ENABLE_REAL_B2=1`. Backend selected by `B2_BACKEND`; defaults to
  `memory`. For real on-disk + on-bucket evidence I'm setting
  `B2_BACKEND=live` so the master MP4 also lands in B2.

The backend was restarted with all four env vars set:

| env var                | value                                              |
|------------------------|----------------------------------------------------|
| `ENABLE_REAL_ASSEMBLY` | `1`                                                |
| `ENABLE_REAL_B2`       | `1`                                                |
| `B2_BACKEND`           | `live`                                             |
| `KEEP_RUN_DIR`         | `1` (skip `shutil.rmtree(run_dir)` so master.mp4 survives) |
| `ENABLE_PIPELINE_HITL` | `1` (gates queue-backed; resumed by approve_loop.py) |
| `QWEN3_TTS_WORKER_URL` | `http://75.19.25.4:9689`                           |
| `LTX_VIDEO_WORKER_URL` | `http://154.57.34.67:10058`                        |
| `B2_KEY_ID`            | (set)                                              |
| `B2_APPLICATION_KEY`   | (set)                                              |

`playground.py` was patched in-place (one-line env gate around the
`shutil.rmtree(run_dir)` call) so the per-scene MP4s + master.mp4 +
manifest.json survive past the run for evidence collection. The patch is
runtime-only (not committed) and the previous behaviour returns when
`KEEP_RUN_DIR` is unset.

## Primary flow

1. Maximize Chrome window, navigate to `http://127.0.0.1:3100/pipeline?mode=live`.
2. Fill the form: topic="The Federal Reserve and Inflation", duration=300s,
   language="en". Confirm mode dropdown → `live`.
3. Click submit. Observe SSE event stream populate the trajectory view.
4. **Visual approval gate** auto-approved by `approve_loop.py` (already
   running, pid 392510): polls `/playground/approval/pending` every 1–2s,
   POSTs `{"decision": "accept"}` on every gate fire.
5. **Assembly approval gate** auto-approved by the same loop.
6. Wait for status pill `run.ok` and stage cells all = `done`.
7. After `run.ok`, locate `run_dir` (logged by playground when
   `KEEP_RUN_DIR=1`), and inspect:
   - `run_dir/artifacts/master.mp4` (real ffmpeg concat output)
   - `run_dir/artifacts/scene_0XX.{wav,mp4}` (per-scene workers' real bytes)
   - `run_dir/manifest.json` (B2 sync manifest)

## Key assertions (all must hard-PASS, no inferreds)

| #   | Assertion                                                                                      | Expected |
|-----|------------------------------------------------------------------------------------------------|----------|
| J1  | Pipeline mode-meta (`data-testid="pipeline-mode-meta"`) reads `live` (not `simulator`)         | exact string `live` |
| J2  | All five stage cells reach `done`                                                              | scenario, audio, visual, production, assembly all `done` |
| J3  | ≥1 `pipeline.tool.launch_audio_render.end` event has `detail.envelope.engine == "qwen3-tts-12hz-1.7b-customvoice"` and `wav_bytes_len ≥ 50000` | 6 scenes, all match |
| J4  | ≥1 `pipeline.tool.launch_visual_production.end` event has `detail.envelope.engine == "ltx-video"` and `mp4_bytes_len ≥ 50000` | 6 scenes, all match |
| J5  | Visual approval gate (`launch_visual_production`) fires (`pipeline.approval.waiting`) + resumed (`pipeline.approval.resumed` with `decision == "accept"`) | exact match |
| J6  | Assembly approval gate (`launch_assembly`) fires + resumed accept                              | exact match |
| J7  | `launch_assembly.end` event elapsed_ms > 100 (real ffmpeg, not 1ms placeholder)                 | > 100 |
| J8  | `launch_assembly.end` event references a single `.mp4` master file                             | single string ending in `.mp4` |
| J9  | Final master MP4 on disk: `ftypisom` magic at offset 4–11 AND size > 100 KB                    | both must hold |
| J10 | Status pill shows `run.ok` at end                                                              | exact string `run.ok` |
| J11 | H200 worker log contains literal `model pins verified` substring AND ≥1 `/video/render` POST timestamped within run window | both must hold |
| J12 | L40S worker log contains ≥1 `/tts/render` POST timestamped within run window                    | true |

**Note on J5/J6/J7 numbering:** the original test plan listed J5 as
"scenario gate" but the live-mode design auto-approves scenario (no
`Interrupt` raised). Renumbering: previous J6→J5 (visual gate), previous
J7→J6 (assembly gate), and inserting a new J7 (assembly elapsed_ms > 100)
to prove the real ffmpeg ran (the previous J7 didn't catch the placeholder
because elapsed_ms wasn't checked). Total still 12 hard-PASS asserts.

## Recording

Single annotated recording:

- `setup` — opening the form
- `test_start` — "It should generate a real ~5-min documentary end-to-end via /pipeline?mode=live with real ffmpeg-assembled master MP4 + B2 sync"
- `assertion`s — one per gate fired, one per stage cell completing, final `run.ok` + master MP4 magic+size verified

## Adversarial check — would this look identical if broken?

* **If `_real_assembly_tools` weren't wired**: `launch_assembly.end` would
  return in 1ms (placeholder echo). J7 would fail.
* **If `_real_b2_tools` weren't wired**: `launch_b2_sync.end` would return
  in 1ms. J9 would still depend on whether assembly wrote to disk
  regardless of B2 sync.
* **If `KEEP_RUN_DIR=1` patch didn't apply**: `run_dir` would be
  `rmtree`d at end of run, master.mp4 wouldn't exist for J9.
* **If the visual gate weren't actually pausing**: SSE wouldn't show
  `pipeline.approval.waiting`. J5 would fail.

## Out of scope

* Five-minute brief is the **target**; if the timing loop settles on a
  shorter duration that's not a test failure.
* Quality of rendered video / narration content not asserted beyond bytes
  + magic + structure. This is a plumbing pass, not artistic.
