# Slice 9d — Phase B test plan: `/pipeline?mode=live` × N topics on real workers

**PR**: [#361](https://github.com/OrpingtonClose/economy-documentary/pull/361)
**Phase**: B (multi-topic end-to-end on real H200 + 3090, after Phase A's
single-render workbench proof)
**Mode**: scripted-LLM brain (real-LLM is slice 9c, deferred), real
Qwen3-TTS dispatch on RTX 3090, real LTX-2.3 BASIC dispatch on H200,
`/pipeline?mode=live` UI, Playwright over CDP

---

## What changed (in user-visible terms)

Slice 9d-wire (commits `630d0aa` → `5e8f2c2` on PR #361) does two things:

1. **Engine swap**: H200 worker no longer runs the old 2B `Lightricks/LTX-Video`
   diffusers pipeline. It now subprocesses Lightricks/LTX-2's official BASIC
   pipeline `python -m ltx_pipelines.ti2vid_one_stage` against the
   `Lightricks/LTX-2.3` 22B-dev checkpoint with the public-mirror
   `Lightricks/gemma-3-12b-it-qat-q4_0-unquantized` text encoder. Both pins
   are SHA256-verified at engine startup (PR #360); mismatch → `ModelPinMismatchError`.

2. **Real-worker wiring on `/pipeline?mode=live`**: previously this surface
   ran the scripted-LLM agent against placeholder tools (no real bytes).
   The new module
   <ref_file file="/home/ubuntu/repos/economy-documentary/server/strands_agents/playground/pipeline_live_real_workers.py" />
   replaces `launch_audio_render` and `launch_visual_production` with
   real-HTTP-dispatchers when `QWEN3_TTS_WORKER_URL` /
   `LTX_VIDEO_WORKER_URL` env vars are set. The dispatchers POST to
   `/tts/render` and `/video/render`, decode the base64 payloads, persist
   them under `run_dir/artifacts/`, and return tool envelopes that include
   `engine`, `mp4_bytes_len`, `wav_bytes_len`, `mp4_path`, `wav_path` so
   the trajectory contains in-stream proof of real bytes.

Phase A (workbench, single render) already proved the LTX-2.3 BASIC engine
works end-to-end with all 8 assertions hard-PASS, including the literal
`model pins verified` log line. Phase B is the **multi-topic** proof on
the **`/pipeline?mode=live`** surface — i.e. the surface a real user
clicks, with the orchestrator brain in the loop and approval gates firing.

---

## What I will test (the primary flow)

The same end-to-end flow, repeated against **3 distinct topics** with
varying duration/language inputs. Each topic is one `/pipeline?mode=live`
run dispatched from the browser:

1. "The Federal Reserve" / 60s / en
2. "Cryptocurrency Mining" / 75s / en
3. "Climate Tipping Points" / 90s / en

For each topic the test:
- Fills the `/pipeline` form via Playwright (real DOM, real React state,
  real form submission)
- Records a single Playwright session (one webm video) covering all 3 topics
- Captures the trajectory event stream from the page
- Greps both worker logs (H200 + 3090) for the literal evidence lines
  emitted only on real dispatch

Three topics is the right minimum: 1 proves "it works at all", 2 proves
"it isn't a fluke", 3 proves "different topics with different durations
and prompts all flow through". More than 3 is regression theatre.

---

## Key assertions (per topic — all 9 must PASS)

For each of the 3 topic runs, these are the assertions. **Every one is
adversarial — if real-worker dispatch is broken, that assertion would
visibly fail.**

### B1. `pipeline-mode-meta` data cell shows literal `live`
- **Expected**: `<dd data-testid="pipeline-mode-meta">live</dd>` text content `== "live"`
- **Why adversarial**: if the UI fell back to `simulator`, this would say `simulator`
- **Source**: <ref_snippet file="/home/ubuntu/repos/economy-documentary/frontend-playground/src/app/pipeline/PipelineOrchestrator.tsx" lines="324-328" />

### B2. Status pill flips to `run.ok`
- **Expected**: `[data-testid="pipeline-status-pill"]` text content `== "run.ok"` within 5 minutes of submit
- **Why adversarial**: if the orchestrator crashed or timed out, this would say `run.error`
  or stay at `running` forever
- **Source**: <ref_snippet file="/home/ubuntu/repos/economy-documentary/frontend-playground/src/app/pipeline/PipelineOrchestrator.tsx" lines="466-474" />

### B3. All 5 stage cells reach `done`
- **Expected**: each cell in `[data-testid="pipeline-stage-ribbon"]` (scenario, audio,
  visual, production, assembly) shows status text `done`, no `pending`, no `running`,
  no `error`
- **Why adversarial**: if production stage failed (real LTX dispatch broken), production
  cell would show `error`; rest would never reach `done`

### B4. Both approval gates resumed (`launch_visual_production` + `launch_assembly`)
- **Expected**: `[data-testid="pipeline-approvals"]` contains exactly 2 `<li>` rows;
  both show `resumed: approve`; neither shows `waiting…`
- **Why adversarial**: if the interrupt/resume plumbing broke, the run would hang
  with one gate stuck on `waiting…` and the run would never reach `run.ok`

### B5. Trajectory event for `tools.launch_audio_render.complete` carries real-worker fields
- **Expected**: an event in `[data-testid="pipeline-trajectory"]` whose detail payload
  contains `"engine": "qwen3-tts"`, `"wav_bytes_len" >= 50000`, `"status_code": 200`
- **Why adversarial**: if dispatcher fell through to placeholder, `engine` would be
  `null` / missing and `wav_bytes_len` would be `0`. The placeholder envelope shape
  has neither field.
- **Source**: <ref_snippet file="/home/ubuntu/repos/economy-documentary/server/strands_agents/playground/pipeline_live_real_workers.py" lines="150-159" />

### B6. Trajectory event for `tools.launch_visual_production.complete` carries real-worker fields
- **Expected**: an event in the trajectory whose detail payload contains
  `"engine": "ltx-video"`, `"mp4_bytes_len" >= 50000`, `"status_code": 200`,
  `"prompt"` non-empty
- **Why adversarial**: if dispatcher fell through to placeholder, `engine` would be
  missing and `mp4_bytes_len` would be `0`. The placeholder envelope shape has neither.
- **Source**: <ref_snippet file="/home/ubuntu/repos/economy-documentary/server/strands_agents/playground/pipeline_live_real_workers.py" lines="243-253" />

### B7. H200 worker log grows during the run, contains `/video/render` entry timestamped within the run window
- **Expected**: `tail -100 /var/log/ltx-video-worker/ltx-video-worker.log` on H200
  shows at least one POST `/video/render` log line whose timestamp is within
  `[run_start, run_end]` (UTC)
- **Why adversarial**: if dispatcher were a no-op, the H200 access log would show no
  `/video/render` entry for this topic's window. Negative control = the run window
  must intersect a real `/video/render` log line.

### B8. H200 worker log contains `model pins verified` matching PR #360 pinned values
- **Expected**: `grep "model pins verified" /var/log/ltx-video-worker/ltx-video-worker.log`
  finds at least one line containing both
  `ltx_model_id=<Lightricks/LTX-2.3>` and
  `ltx_revision=<76730e634e70a28f4e8d51f5e29c08e40e2d8e74>` and
  `gemma_model_id=<Lightricks/gemma-3-12b-it-qat-q4_0-unquantized>` and
  `gemma_revision=<d62fe4f1995ade703b49a0f3c0d0f161237ef437>`
- **Why adversarial**: if the H200 was running the old 2B engine or pin enforcement
  were silently disabled, this literal line wouldn't be present. The only way this
  line is emitted is `_ltx_engine._verify_pins()` succeeding against the SHA256s in
  PR #360. (This is the same proof Phase A captured — confirming it's still
  valid for each Phase B topic run, since the engine boots once and serves all
  3 topics.)
- **Source**: <ref_snippet file="/home/ubuntu/repos/economy-documentary/server/strands_agents/ltx_video_worker/_ltx_engine.py" lines="208-225" />

### B9. 3090 worker log grows during the run, contains `/tts/render` entry timestamped within the run window
- **Expected**: `tail -100 /var/log/qwen3-tts-worker/qwen3-tts-worker.log` on 3090
  shows at least one POST `/tts/render` log line whose timestamp is within
  `[run_start, run_end]` (UTC)
- **Why adversarial**: same as B7 but for the audio path

---

## Deliberate omissions (and why)

- **Post-run filesystem audit (WAV/MP4 on disk by ls)**: <ref_snippet file="/home/ubuntu/repos/economy-documentary/server/playground.py" lines="1711-1712" />
  wipes `run_dir` with `shutil.rmtree(run_dir, ignore_errors=True)` immediately after
  the terminal envelope closes. This means the WAV/MP4 the dispatcher just persisted
  *cannot* be `ls`-ed after the run. Proof has to come from the trajectory tool
  envelopes (B5, B6) which capture `wav_bytes_len`/`mp4_bytes_len` and `engine` *before*
  the rmtree. Slice 9b's filesystem-audit proof was timing-fortuitous; here it would be
  unreliable. I will instead sample one mid-run snapshot (during the approval-gate
  pause window) of `/tmp/pipeline_live_*/artifacts/` to demonstrate the artifacts
  were on disk at least transiently.
- **Multi-scene per topic**: scripted brain emits one scene per run (one
  `launch_audio_render` + one `launch_visual_production`). Multi-scene parallelism is
  a separate orchestrator capability (slice 9c+) and not what 9d-wire changed.
- **Final B2 URL panel value**: still a `b2://…` stub (slice 9b note); not in 9d-wire's
  scope.

---

## Pass/fail summary

For Phase B to pass overall, **all 3 topics × 9 assertions = 27 must hard-PASS**.
Any single INFERRED or FAILED is a Phase B failure and gets reported as such.

If a topic fails (e.g. H200 reclaimed mid-run, common Vast.ai friction), I
re-provision and re-run only the failed topic; I don't re-run topics that
already passed. Worker boot once verifies pins once, so B8 is captured once
per worker session and inherited by all topics that share the same boot.

---

## Evidence package shape

Single PR comment on #361 containing:

- This test plan (linked, not inlined)
- Playwright recording (one webm covering all 3 topics)
- 3 screenshots (one final state per topic)
- `phase-b-assertions.json` — machine-readable, 27 entries, each with
  `topic`, `assertion_id`, `expected`, `observed`, `result` (`PASS` /
  `FAIL` / `INFERRED`)
- Worker log excerpts (H200 + 3090) showing the literal lines for B7/B8/B9
- Cost summary (Vast.ai instance hours used)
- Link to this Devin session
