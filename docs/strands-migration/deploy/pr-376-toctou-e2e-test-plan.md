# PR #376 — TOCTOU + stale-revision guard E2E test plan

> **Branch:** `devin/1777314438-store-toctou-staleguard` @ `a3da786`
> **CI:** 3/3 green (Strands unit, ADK eval, Devin Review)
> **Scope of change:** `server/strands_agents/b2_checkpoint/store.py` only (171 lines added, 34 removed). 365-line unit test file. No other production code touched.

## What changed (user-visible)

- `LiveB2CheckpointStore.upload()` now serializes concurrent callers with the same idempotency key via a per-idem `threading.Event` slot. Previously, the idempotency check ran under one lock scope, the B2 upload ran unlocked, and the manifest registration ran under a second lock scope — two concurrent uploads with the same `(run_id, kind, revision_tag, sha256)` could both upload to B2 and orphan one of the manifest rows.
- Stale-revision and duplicate-idempotency guards from the in-memory variant are now applied to the live B2 store byte-for-byte.
- Slot release is wrapped in `try/finally` so a post-upload exception cannot leak the in-flight slot.

## What this E2E test proves

The TOCTOU/race-condition fix lives in a code path that is exercised on **every** B2 publish during a documentary run. The fix is correct iff:

1. The happy-path B2 publishes still succeed (one upload per artifact, manifest registered, idempotent re-publishes return the same `artifact_id`).
2. The full documentary pipeline still produces a watchable master.mp4 with motion in every scene + complete narration tail.
3. All 4 QA gates (`qa_audio_completeness`, `qa_duration_align`, `qa_stills_judge`, `qa_video_artifact_probe`) fire per scene with measurements visible on `/pipeline`.

A broken TOCTOU fix would surface as either (a) backend stack traces during checkpoint, (b) duplicate `pipeline.b2.checkpoint` events with the same idempotency key, or (c) the run hanging on a deadlocked `Event.wait()`.

## Primary flow (the one E2E run)

1. Open `http://127.0.0.1:3100/pipeline` in a fresh chromium.
2. Form starts pre-filled at `topic="The Federal Reserve"`, `duration=60s`, `language=English`. Bump duration to `120s`, set topic to `"The Federal Reserve and Inflation"`. Click **Start pipeline run**.
3. Watch the stage ribbon advance: `scenario` → `audio` → `visual` → `production` → `assembly`.
4. As each scene completes audio render, the per-scene metric row should populate with audio duration + `qa_audio_completeness` verdict pill.
5. As each scene completes visual production, the same row should populate with video duration + `qa_duration_align` and `qa_stills_judge` verdict pills with measurements.
6. When `launch_visual_production` approval gate fires, click **Accept** in the gate banner.
7. When `launch_assembly` approval gate fires, click **Accept** in the gate banner.
8. Wait for `run_finished` event with status `run.ok` and the master.mp4 link.
9. Open the master.mp4 link, verify it plays with motion in every scene + audible narration through to the end (no abrupt cut, no frozen frames).

## Key assertions (each one would fail visibly if the fix were broken)

| # | Assertion | Where to verify |
|---|---|---|
| 1 | The form posts and a run id appears in the URL bar (`?run_id=run_*`) within 2s | URL bar |
| 2 | Per-scene metric table renders one row per distinct `envelope.scene_id` (count == declared scenes from scenario stage) | `/pipeline` metric table |
| 3 | Each scene row has both `audio_duration_s` and `video_duration_s` cells populated with non-zero numeric values | metric table cells |
| 4 | `qa_audio_completeness` pill carries `data-verdict="pass"` and shows `tail_rms_db ≤ -25 dBFS` and `trailing_silence_s` numeric | inline measurement chips |
| 5 | `qa_duration_align` pill carries `data-verdict="pass"` and shows `delta_s ≤ 0.50` | inline measurement chips |
| 6 | `qa_stills_judge` pill carries `data-verdict="pass"` and shows `mean_pixel_delta > 0` | inline measurement chips |
| 7 | `qa_video_artifact_probe` pill carries `data-verdict="pass"` | inline measurement chips |
| 8 | Both approval gate banners (`launch_visual_production`, `launch_assembly`) appear and the **Accept** button responds (banner clears within 2s of click) | gate banner |
| 9 | `pipeline.b2.checkpoint` events appear in the trajectory stream with no duplicates per `(scene_id, kind)` | trajectory stream |
| 10 | No `Traceback` / `Error` / `RuntimeError` text in the trajectory stream summary lines | trajectory stream |
| 11 | Master row at the bottom shows scene count == declared scenes, `data-verdict="pass"` | metric table master row |
| 12 | Final `run_finished` event surfaces an MP4 URL that returns `200 video/mp4` with `ftypisom` magic in the first 12 bytes | curl HEAD + xxd |
| 13 | Downloaded master.mp4 plays with motion in every 1-second window (mean inter-frame delta > 0 across the whole timeline; spot-checked via ffmpeg frame extract) | ffmpeg frame analysis |
| 14 | Downloaded master.mp4 has audible room-tone tail at the very end (no abrupt mid-syllable cut; trailing silence ≥ 100 ms via ffmpeg `silencedetect`) | ffmpeg silencedetect |

## Why this distinguishes working from broken

- Assertion 9 (`pipeline.b2.checkpoint` events with no duplicates per `(scene_id, kind)`) is the direct visible signature of the TOCTOU fix. A broken fix that double-uploads would emit two checkpoint events for the same scene+kind (one orphan).
- Assertion 10 (no Traceback) is the safety net for the new `try/finally` slot release. A regression that re-introduced the bug Devin Review caught (slot leaked on post-upload exception) would surface as `BaseException` propagating to the orchestrator.
- Assertions 4–7 prove the QA gates still fire with measurements visible — without these, "no test_mode" and "everything visible from the UI" are both violated.
- Assertions 13–14 prove the actual user-facing artifact is correct, which is the only metric that matters end-to-end. The 12 assertions above are necessary preconditions; these two are sufficient.

## Evidence to capture

- Screen recording covering form submit through master.mp4 playback.
- Screenshot of metric table at three points: after first scene completes audio (assertion 3,4), after first scene completes visual (assertions 5,6,7), at master row (assertion 11).
- Final master.mp4 attached to the test report.
- ffmpeg silencedetect / mean-frame-delta output for assertions 13, 14.

## Code references that informed this plan

- `frontend-playground/src/app/pipeline/PipelineOrchestrator.tsx:96-120` — form state + `?run_id=<id>` re-attach.
- `frontend-playground/src/app/pipeline/PipelineSceneMetrics.tsx` — metric table that lifts `data-verdict` and measurements from envelope payloads.
- `server/strands_agents/playground/pipeline_live_runner.py:227-310` — backend SSE wire whitelist (`_ENVELOPE_PASSTHROUGH_KEYS`) for measurements.
- `server/strands_agents/qa_gates.py:21-991` — the 4 QA gates the metric cards display.
- `server/strands_agents/b2_checkpoint/store.py:300-472` — the TOCTOU + stale-revision + try/finally fix under test.
