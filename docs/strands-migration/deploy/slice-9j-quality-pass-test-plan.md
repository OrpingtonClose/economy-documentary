# Slice 9j-quality-pass — full-movie E2E test plan

## What changed

Slice 9j is the **culmination** of slices 9a–9i. The first attempt of this E2E
run surfaced a real bug — the visual approval gate resumption crashed with
`KeyError: 'decisions'` because the legacy `resume_command_from_decision`
emitted `Command(resume={"type": "approve"})`, but
`langchain.agents.middleware.HumanInTheLoopMiddleware` requires
`Command(resume={"decisions": [...]})` with one entry per parallel
`action_request`. Commit `3d94868` on PR #370 adds
`langchain_resume_command_from_decision` which:

1. Wraps the decision in `{"decisions": [...]}` so the middleware finds its
   required key.
2. Translates project vocab → langchain vocab (`accept`→`approve`,
   `edit`→`edit` with `edited_action.{name,args}`, `reject`/`respond`→`reject`
   with `message`).
3. Replicates the operator's decision N times for batched gates where one
   user click covers N parallel `action_requests` (e.g. 6 scenes dispatching
   `launch_visual_production` at once).

This re-run will prove the fix lands the visual + assembly gates without the
`KeyError`. No new feature code beyond that bugfix; this is still primarily
plumbing-validation across slices 9a–9i:

| PR  | Slice                  | Capability                                                    |
|-----|------------------------|---------------------------------------------------------------|
| 358 | 9a live-orch           | scripted-LLM orchestrator with placeholder dispatch           |
| 359 | 9b real weights        | LTX-Video worker `/video/render` + Qwen3-TTS `/tts/render`    |
| 360 | 9d-pin                 | SHA256 model-pin enforcement                                  |
| 361 | 9d-wire                | LTX-2.3 BASIC `ti2vid_one_stage` subprocess wire              |
| 362 | 9e                     | real-worker dispatch in `build_documentary_orchestrator`      |
| 363 | 9c                     | real narration text + visual prompt threaded into dispatch    |
| 364 | 9c-LLM-scenario        | real-LLM scenario generation                                  |
| 365 | 9c-LLM-visual          | real-LLM visual concept generation                            |
| 366 | 9f-multiscene-prod     | scripted-LLM dispatches over N scenes                         |
| 367 | 9f-timing-real         | real WhisperX alignment driving timing loop                   |
| 368 | 9g-assembly            | OTIO + ffmpeg concat → single `.mp4` master                   |
| 369 | 9h-b2-publish          | real B2 checkpoint sync + manifest                            |
| 370 | 9i                     | real HITL approval gates on `/pipeline?mode=live`             |

## Primary flow

1. Maximize Chrome window, navigate to `http://127.0.0.1:3100/pipeline?mode=live`.
2. Fill the form: topic="The Federal Reserve and Inflation", duration=300s,
   language="en". Set mode dropdown → `live`.
3. Submit. Observe SSE event stream populate the trajectory view.
4. **Scenario approval gate**: when status pill shows `interrupt`, click
   "Approve" on the scenario-review side panel.
5. **Visual approval gate**: same flow once visuals are generated.
6. **Assembly approval gate**: same flow before final ffmpeg.
7. Wait for status pill to show `run.ok` and stage cells to all reach `done`.
8. Verify the `assembly` stage emits a single playable `.mp4` URI under
   `run_dir/artifacts/`.

## Key assertions (must all hard-PASS, no inferreds)

| #  | Assertion                                                              |
|----|------------------------------------------------------------------------|
| J1 | Pipeline mode-meta reads `live` (not `simulator`)                      |
| J2 | All five stage cells (`scenario`, `visual`, `timing`, `production`, `assembly`) reach `done` |
| J3 | At least one `pipeline.tool.launch_audio_render.end` event has `detail.envelope.engine == "qwen3-tts-12hz-1.7b-customvoice"` and `wav_bytes_len ≥ 50000` |
| J4 | At least one `pipeline.tool.launch_visual_production.end` event has `detail.envelope.engine == "ltx-video"` and `mp4_bytes_len ≥ 50000` |
| J5 | Scenario approval gate fires + `resumed: accept` recorded               |
| J6 | Visual approval gate fires + `resumed: accept` recorded                 |
| J7 | Assembly approval gate fires + `resumed: accept` recorded               |
| J8 | Final `assembly.end` event references a single `.mp4` master file      |
| J9 | Final MP4 on disk has `ftypisom` magic + size > 100 KB                 |
| J10| Status pill shows `run.ok` at end                                      |
| J11| H200 worker log shows literal `model pins verified` substring + `/video/render` POSTs in the run window |
| J12| L40S worker log shows `/tts/render` POSTs in the run window            |

## Recording

Single Playwright annotated recording with `record_start` / `record_annotate` /
`record_stop` covering the full E2E run. Annotations:

- `setup` — opening the form
- `test_start` — "It should generate a real ~5-min documentary end-to-end via
   /pipeline?mode=live with real H200+L40S workers"
- `assertion`s — one per gate fired, one per stage cell completing, final
   `run.ok` + MP4 master verified

## Out of scope

- A 5-minute brief is the **target**; if the orchestrator settles on a shorter
  duration after the timing loop, that is a successful run not a test failure.
- Quality of the rendered video / narration content is not asserted beyond
  bytes + magic + structure. This is a **plumbing pass**, not an artistic pass.
- Devin Review CI on the PR remains 3/3 green from slice 9i.
