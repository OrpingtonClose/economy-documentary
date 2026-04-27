# Real-overlay-default-on E2E test plan

> **Local change** (not yet on PR): remove `ENABLE_REAL_ASSEMBLY` / `ENABLE_REAL_B2` env opt-in for the production path. `build_documentary_orchestrator` and the live demo brain now always pass `enable_real_assembly=True` + `enable_real_b2=True` to `build_real_worker_tools`. `_build_store` auto-selects `LiveB2CheckpointStore` when `B2_KEY_ID` + `B2_APPLICATION_KEY` are present in the env (no `B2_BACKEND=live` opt-in needed).

## What changed (user-visible)

The previous E2E (`run_554466b0c839`) completed all 18 QA pills + master verdict PASS, but `launch_assembly` and `launch_b2_sync` ran as 1ms placeholders — the final master row pointed at a `b2://...` placeholder URL with no real artifact. Single-production-path mandate from PR #373 says there is no opt-in for real workers; if credentials are wired the production code runs them.

After the change, the orchestrator always invokes `_real_assembly_tools.make_real_assembly_tool` (real `ffmpeg concat`) and `_real_b2_tools.make_real_b2_sync_tool` (real `LiveB2CheckpointStore.upload`) whenever `build_documentary_orchestrator` runs. The TOCTOU fix on `LiveB2CheckpointStore.upload` is therefore actually exercised, and the master.mp4 served from `/playground/runs/<run_id>/master.mp4` is a real artifact.

## What this E2E test proves

A run completing without the change would silently green-light placeholders again — same 18 PASS pills, same `b2://` placeholder URL, no real bytes assembled. A run with the change must produce a real assembled MP4 served over HTTP that plays end-to-end.

A broken change would surface as either:
- `launch_assembly` still completes in <100ms and the final URL is `b2://...` not `/playground/runs/<id>/master.mp4` (overlay still off)
- `launch_assembly` runs but throws (real ffmpeg path broken)
- `launch_b2_sync` raises a `b2sdk` auth/upload error (Live store path broken)
- Backend stack trace at the LiveB2 checkpoint call site (TOCTOU regression)
- Duplicate `pipeline.b2.checkpoint` events per `(scene_id, kind)` in the trajectory (TOCTOU)

## Primary flow (one E2E run via UI only)

1. Open `http://127.0.0.1:3100/pipeline` in a fresh chromium window. Maximize.
2. Form starts pre-filled at `topic="The Federal Reserve"`, `duration=60`, `language=English`. Set topic to `"The Federal Reserve and Inflation"` and duration to `120`. Click **Start pipeline run**.
3. Watch stage ribbon advance: `scenario` → `audio` → `visual` → `production` → `assembly` → `publish`.
4. As each scene completes audio render, the per-scene row populates with `audio_duration_s` + `qa_audio_completeness` PASS pill + measurements (`tail_rms_db`, `trailing_silence_s`).
5. As each scene completes visual production, same row populates with `video_duration_s` + `qa_duration_align` and `qa_stills_judge` PASS pills + measurements (`delta_s`, `mean_pixel_delta`).
6. When `launch_visual_production` approval gate fires, click **Accept** in the gate banner.
7. When `launch_assembly` approval gate fires, click **Accept** in the gate banner.
8. Wait for `run_finished` event with status `run.ok`.
9. Click the master.mp4 link surfaced in the master row and confirm browser plays it with audio + motion.

## Key assertions (each one fails visibly if change is broken)

| # | Assertion | Where to verify | Expected concrete value |
|---|---|---|---|
| 1 | Form POST succeeds and `?run_id=run_*` appears in URL within 2s | URL bar | URL contains `?run_id=run_` |
| 2 | Per-scene metric rows populate (≥4 distinct `scene_id` rows) | metric table | rows >= 4 with non-empty `audio_duration_s` AND `video_duration_s` |
| 3 | All 4 QA gate pills PASS per scene | inline pills | every cell has `data-verdict="pass"` |
| 4 | `qa_audio_completeness` shows `tail_rms_db ≤ -25 dBFS` AND `trailing_silence_s` numeric | inline measurement chips | values present, not "—" |
| 5 | `qa_duration_align` shows `delta_s ≤ 0.50` | inline measurement chips | values present, ≤ 0.50 |
| 6 | `qa_stills_judge` shows `mean_pixel_delta > 0` | inline measurement chips | numeric > 0 |
| 7 | Both approval gate banners fire and Accept button responds within 2s | gate banner | banner clears |
| 8 | **`launch_assembly` is the real overlay** — trajectory shows `engine="ffmpeg"` AND elapsed_ms > 100 (real ffmpeg run) | trajectory event detail | NOT "Assembly launch placeholder" envelope, NOT 1ms |
| 9 | **`launch_b2_sync` is the real overlay** — trajectory shows real `pipeline.b2.checkpoint` events with `manifest_id` per artifact | trajectory event detail | NOT placeholder envelope; real upload events |
| 10 | **NO duplicate `pipeline.b2.checkpoint` events per `(scene_id, kind)`** (TOCTOU signature) | trajectory grep | each `(scene_id,kind)` pair appears at most once |
| 11 | No `Traceback` / `RuntimeError` / `b2sdk` error in trajectory summary lines | trajectory stream | zero matches |
| 12 | **Master row final URL is `/playground/runs/<run_id>/master.mp4`** (HTTP, not `b2://`) | master row link | `href` starts with `/playground/runs/` |
| 13 | `HEAD` on the master URL returns `200` + `Content-Type: video/mp4` + `Content-Length > 500000` (real assembled MP4) | curl HEAD | non-trivial size |
| 14 | First 12 bytes of MP4 contain `ftypisom` (or `ftypiso5` / `ftypmp42`) | curl + xxd | `ftyp` magic in box header |
| 15 | Downloaded MP4 plays with motion in every 1-second window (mean inter-frame delta > 0 across timeline) | ffmpeg frame extract | zero frozen 1s windows |
| 16 | MP4 has audible room-tone tail at end ≥ 100 ms via `ffmpeg silencedetect` | ffmpeg silencedetect | trailing silence ≥ 0.1s, no abrupt cut |

## Negative controls

- Assertion 8: a broken (overlay-still-off) run would have `launch_assembly` complete in 1-3ms with `Assembly launch placeholder` text. Real run is expected to be ~hundreds of ms because ffmpeg concat actually runs.
- Assertion 12: a broken run would surface `b2://documentary/.../r0001.mp4` as the URL. Real run surfaces `/playground/runs/<id>/master.mp4`.
- Assertion 13: a broken run would 404 or return 0 bytes (placeholder).
- Assertion 15-16: a broken run (ffmpeg failure or wrong codec) would either not play, or have abrupt silence.

## Scope of evidence

One screen recording covering form submit through the master.mp4 playback in browser. Trajectory grep + ffmpeg outputs as text evidence in the report.
