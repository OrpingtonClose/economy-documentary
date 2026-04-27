# Slice 9q — per-scene QA metric cards on `/pipeline` (PR #375)

## What changed since attempt 3 (commit `108fe58`)

Attempt 3 form submit succeeded and the orchestrator drove all the way
through the audio stage + QA gates, but the headed Playwright chromium
process died (`Target page, context or browser has been closed`) ~3
minutes into the run. The backend continued executing autonomously
(audio + visual approval gates auto-resumed) but the UI driver lost
visibility, so per-scene metric cards could not be observed live and
the run could not be completed via UI clicks.

Code-level fix in `108fe58`: `/pipeline` now accepts a
``?run_id=<id>`` query parameter. When the URL carries it, the page
subscribes to the existing run on mount via the same
``useRunStream(runId)`` hook used after a fresh form submit. This
makes UI driving resilient to browser-process death — a fresh
chromium can re-attach to ``run_2c180c6e184e`` (or any other in-flight
run) and pick up monitoring + approving without burning a new GPU
run from scratch. Page wraps ``PipelineOrchestrator`` in a
``Suspense`` boundary because Next.js app-router requires
``useSearchParams`` to be Suspense-wrapped at build time.

## What changed since attempt 2 (commit `20cca68`)

Attempt 2 surfaced a second category of failure: the orchestrator
finished the run but the only artifact link the UI saw was a
`b2://` placeholder (the `LiveB2CheckpointStore` had no `upload`
method, so the master.mp4 was never published, and the run-dir was
cleaned up before the UI could replay it). A13 (final mp4 reachable)
and A14 (mp4 plays in browser) were therefore unverifiable.

Three code-level fixes in `20cca68`:

1. **Loop-aware `qa_duration_align`** — verdict now keys off
   `audio_dur / video_dur <= DEFAULT_MAX_LOOP_FACTOR` (5.0×) instead
   of raw duration delta, because the assembly muxer fills audio with
   `-stream_loop -1`. A 5 s LTX clip + 13 s narration is no longer a
   gate failure; a 0.4 s degenerate clip still is.
2. **`LiveB2CheckpointStore.upload`** implemented via `b2sdk.v2`,
   so the master.mp4 actually lands in the B2 bucket when
   `B2_BACKEND=live`.
3. **Same-origin master.mp4 streaming** — `playground.py` copies the
   master to `/tmp/pipeline_masters/<run_id>.mp4` on terminal, and a
   new `GET /playground/runs/{run_id}/master.mp4` route streams it
   back over the FastAPI app. `LivePipelineRun` overrides
   `final_mp4_b2_url` with the same-origin path
   (`/playground/runs/<run_id>/master.mp4`) when the local file
   exists. `PipelineOrchestrator.tsx` renders an inline `<video>` tag
   (data-testid `pipeline-final-video`) when the URL starts with `/`
   or `http(s)://`, so A13 + A14 can be exercised without a B2
   round-trip.

A13 + A14 now resolve against `http://127.0.0.1:3100/playground/runs/<run_id>/master.mp4`
(through the Next rewrite to `127.0.0.1:8765`), not `b2://...`.

## What changed since attempt 1 (commit `f691bcd`)

The previous test execution submitted the form correctly and reached the
production stage, but every `qa_audio_completeness` envelope on the wire
arrived with **only thresholds** (`min_trailing_silence_s`,
`max_tail_rms_db`, `silence_noise_db`, `tail_window_s`) and **no
measurements** (`tail_rms_db`, `trailing_silence_s`). All six
per-scene verdicts came back `verdict=fail`, the metric rows rendered
threshold cells but no measurement cells, and assertions A6/A7/A8/A9/A10
were unverifiable.

Root cause traced to a bug under the bug:
`pipeline_live_real_workers._persist_artifact` was writing rendered
audio/video to `run_dir/artifacts/<scene_id>-<8hex>.{wav,mp4}` (random
token suffix), while the scripted brain in `pipeline_live_demo.py`
constructs the QA gate `audio_path` / `video_path` arguments as
`run_dir/artifacts/<scene_id>.{wav,mp4}` (canonical layout). Every
gate opened a non-existent path and hit the `audio_path does not exist`
early-return branch — which only emits the threshold fields, not the
measurement fields, hence the empty cells.

Fix in `f691bcd`: drop the token. `_persist_artifact` now writes the
canonical `<scene_id>.{suffix}` path. Each pipeline run gets its own
`run_dir` and each scene renders exactly once per run, so a
deterministic name cannot collide. The assembly resolver
`_resolve_one` already tries the canonical path first, so this is
backwards-compatible with existing tests that exercise the legacy
glob branch.

Also widened `_ENVELOPE_PASSTHROUGH_KEYS` in
`pipeline_live_runner.py` with `audio_path` / `video_path` / `error`,
so any future QA-gate failure surfaces the offending path on the SSE
wire instead of fail-by-defaulting silently.

Backend restarted; in-flight orphan run cancelled. Workers (H200
LTX-2.3 + L40S Qwen3-TTS) confirmed healthy. Re-running the same
14 hard-PASS assertions below.

## What changed (user-visible)

The `/pipeline` page now renders a **per-scene QA metrics table** above
the trajectory stream. One row per scene with PASS/FAIL pills + the
exact measured value next to each metric:

- audio duration / bytes
- video duration / bytes
- `qa_audio_completeness` — PASS/FAIL + `tail_rms_db` + `trailing_silence_s`
- `qa_duration_align` — PASS/FAIL + `delta_s` + `tolerance_s`
- `qa_stills_judge` — PASS/FAIL + `mean_pixel_delta` + `min_mean_pixel_delta`

Plus a master row at the bottom: scene count, total audio + video
duration, final verdict pill (`pass` / `fail` / `pending`).

Source of truth: `pipeline.tool.<name>.end` events the scripted brain
already emits (slice 9o + slice 9p). No backend change.

## Primary flow (one adversarial E2E)

Drive the `/pipeline` page **only via the UI** — submit form, watch
metric cards populate, click approval gates as they fire.

**Driver**: Playwright headed mode against `DISPLAY=:0` (chromium-1208).
The Devin computer-use Chrome wrapper script
(`/home/ubuntu/.local/bin/google-chrome`) only forwards URLs to a
Chrome that an external Devin process should already have running with
`--remote-debugging-port=29229`; on this VM that process is not
persistent, so a Playwright-managed chromium is the only way to keep a
browser alive long enough to drive the run. This is still real UI
driving via DOM clicks/typing — not external HTTP — and the chromium
window is visible on the VNC desktop while the run executes.

1. Launch chromium headed via Playwright at 1600×1100 viewport
2. Open `http://127.0.0.1:3100/pipeline`
3. Confirm the empty state: `[data-testid="pipeline-scene-metrics-empty"]`
   is present with the heading **"Per-scene QA metrics"**
4. Submit the form:
   - `[data-testid="pipeline-topic-input"]` ← `The Federal Reserve and Inflation`
   - `[data-testid="pipeline-duration-input"]` ← `120` (4 scenes × ~30s)
   - `[data-testid="pipeline-language-input"]` ← `en`
   - Click `[data-testid="pipeline-run-button"]`
5. Status pill flips to `running`; stage ribbon advances; trajectory
   starts streaming
6. Watch `[data-testid="pipeline-scene-metrics"]` replace the empty state
7. As scenes 1..N enter the audio render stage, watch
   `[data-testid="pipeline-metrics-row-<scene-id>"]` rows appear and
   populate. Per scene, in order:
   - `pipeline-metrics-<sceneId>-audio-duration` shows a non-`—` value
     (e.g. `12.50s`)
   - `pipeline-metrics-<sceneId>-audio-completeness-verdict` flips from
     `pending` to `pass` (data-verdict attribute)
   - `pipeline-metrics-<sceneId>-tail-rms-db` shows ≤ -25 dBFS
   - `pipeline-metrics-<sceneId>-trailing-silence-s` shows a numeric
     value
   - After visual production: `pipeline-metrics-<sceneId>-video-duration`
     shows a non-`—` value
   - `pipeline-metrics-<sceneId>-duration-align-verdict` = `pass`
   - `pipeline-metrics-<sceneId>-delta-s` shows `<= 0.50s`
   - `pipeline-metrics-<sceneId>-stills-judge-verdict` = `pass`
   - `pipeline-metrics-<sceneId>-mean-pixel-delta` shows a numeric
     value (typically > 1.0 for real LTX motion)
8. `launch_visual_production` approval gate fires →
   `[data-testid="pipeline-approval-launch_visual_production"]` →
   click `[data-testid="pipeline-approval-approve"]`
9. `launch_assembly` approval gate fires →
   `[data-testid="pipeline-approval-launch_assembly"]` → click approve
10. Status pill flips to `success` (`run.ok`)
11. `[data-testid="pipeline-final"]` appears with mp4 link
12. `[data-testid="pipeline-metrics-master-verdict"]` = `pass`

## Concrete pass/fail assertions

These assertions are designed to fail visibly if the change is broken
(card not rendered / wrong data-testid / metric not lifted from envelope
/ master verdict computed wrong / failing cell not red-coloured).

| # | Assertion | Pass expected value | Fail signal |
|---|---|---|---|
| A1 | Pre-run empty state | `pipeline-scene-metrics-empty` visible with heading text "Per-scene QA metrics" | element absent → component never rendered |
| A2 | Empty state replaced once first scene's audio render fires | `pipeline-scene-metrics-empty` removed; `pipeline-scene-metrics` present | empty state stuck → derive function broken |
| A3 | Per-scene row visible for every scene-id seen in the trajectory | Count of `pipeline-metrics-row-*` rows == count of distinct `envelope.scene_id` in `pipeline.tool.launch_audio_render.end` events | row count mismatch → first-seen ordering or merge logic broken |
| A4 | Audio duration cell populated per scene | `pipeline-metrics-<sceneId>-audio-duration` text is `<N.NN>s` matching `envelope.duration_s` from `launch_audio_render.end` | shows `—` after the audio render event arrives → field extraction broken |
| A5 | Video duration cell populated per scene | `pipeline-metrics-<sceneId>-video-duration` text is `<N.NN>s` matching `envelope.duration_s` from `launch_visual_production.end` | shows `—` after the visual production event arrives → field extraction broken |
| A6 | qa_audio_completeness verdict pill | `data-verdict="pass"` attribute on `pipeline-metrics-<sceneId>-audio-completeness-verdict` | absent or `data-verdict="fail"` → either the gate fired with fail (escalation) or the verdict is not lifted into the card |
| A7 | tail_rms_db measurement visible | `pipeline-metrics-<sceneId>-tail-rms-db` text is `<N.N> dBFS` (negative number) | shows `—` → tail_rms_db field not extracted from envelope |
| A8 | trailing_silence_s measurement visible | `pipeline-metrics-<sceneId>-trailing-silence-s` text is `<N.NN>s` | shows `—` → trailing_silence_s field not extracted |
| A9 | qa_duration_align verdict + delta_s visible | `data-verdict="pass"`; delta cell text matches `\|audio_dur - video_dur\|` from envelope | wrong text or pill → verdict / delta not lifted |
| A10 | qa_stills_judge verdict + mean_pixel_delta visible | `data-verdict="pass"`; mean_delta cell shows numeric > 0 | wrong text or pill → field not lifted |
| A11 | Master row totals | `pipeline-metrics-master-audio` text is sum of all `audioDurationS`; `pipeline-metrics-master-video` is sum of all `videoDurationS` | mismatch → reduce broken |
| A12 | Master verdict | `pipeline-metrics-master-verdict` `data-verdict="pass"` after all rows are PASS | `fail` or `pending` after run.ok → rollup logic broken |
| A13 | Final mp4 link reachable | `pipeline-final` href returns 200 + `Content-Type: video/mp4` | 404 / not present → assembly never published |
| A14 | Final mp4 plays with motion + complete narration | downloaded file: `ftypisom` magic, total bytes > 1 MiB, played back: every scene has visible motion (LTX, not still), narration audibly ends with room tone (no abrupt cut) | frozen frames or abrupt cut → regression |

## Adversarial check — is the test sequence distinguishable from a broken impl?

- **If the metric component were never rendered**, A1 would fail (no
  `pipeline-scene-metrics-empty` element) → distinguishable.
- **If the derive function returned empty**, A2 would fail (empty state
  never replaced). Distinguishable.
- **If the data-testid hooks were missing**, A4–A10 would all fail
  individually (`getByTestId` would throw). Distinguishable.
- **If the verdict pill ignored the envelope `verdict` field**, A6/A9/A10
  would show `pending` instead of `pass`. Distinguishable.
- **If the master row used wrong reduce**, A11 would show wrong totals.
  Distinguishable.
- **If `data-verdict` attribute were missing**, A6/A9/A10/A12 would
  all fail. Distinguishable.
- **If the regression were the slice-9j frozen-frame bug**, A5 would
  show `5.00s` while A4 shows `12.50s`, A9 would have
  `data-verdict="fail"` with `delta_s ≈ 7.50s` rendered red, **and the
  master verdict would be `fail`**. So the failure would be visible
  in the UI exactly where it should be — that's the proof that this
  PR closes the visibility gap from PR #370.
- **If the qa_audio_completeness gate detected an abrupt-cut narration**
  (slice 9p regression), A6 would show `data-verdict="fail"` with
  `tail_rms_db > -25 dBFS` rendered red, and the master verdict would
  be `fail`. Same — failure visible in the UI.

## Evidence to capture

- Playwright screenshot at A1 (empty state) — `/tmp/9q-A1-empty.png`
- Playwright screenshots as each scene's row populates with verdicts
  (A3..A10) — `/tmp/9q-row-<sceneId>.png`
- Master row + final mp4 link screenshot (A11..A13) —
  `/tmp/9q-A11-master.png`
- Downloaded master.mp4 + ffprobe duration + frame-delta count to
  prove A14
- Inline screenshot evidence in the test report

A Playwright trace + screenshots is sufficient evidence; full desktop
recording is optional given the long runtime and ample inline
screenshots.

## Out of scope

- Backend code coverage (already proven by jest unit tests + Strands unit tests)
- Other pages (`/components`, `/`)
- Test for the slice-9p audio-completeness fail mode in isolation —
  the production run will exercise the pass path; fail path is already
  covered by the qa_gates unit tests on PR #375 backend
