> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Next wave — plan (v2)

**Status:** revised after Orpington sign-off. v1 feedback folded in:
(1) pipeline is a playground component; (2) Wave 2 and Wave 3 merge
into one milestone with real compute from day 1 — no simulator path;
(3) observability baked in from the first PR, not deferred to a
stretch wave; (4) VRAM is a pre-flight probe, not a runtime surprise;
(5) AG-UI adopted as the formal event wire format instead of our
current ad-hoc names.

**Precondition unchanged.** Every existing component (c01–c15 specs
and task adapters, playground backend, frontend-playground, narrator
rail, inner-tool emitter, wait-till-terminal harness, refine cap)
stays **untouched as testable units.** New work composes them
through HTTP; new modules live in new files.

---

## 0. Core facts (enumerated, not assumed)

### What's testable in isolation today

- `c01..c15` declared in
  [`server/strands_agents/playground/registry.py`](../../../server/strands_agents/playground/registry.py)
  with declared model(s), canonical case list, evaluator stack.
- Uniform `POST /playground/components/<id>/runs` →
  event-ring-buffer → SSE → narrator pipeline works for every
  component. c02 (deterministic) in <1s; c01 (live LLM + refine cap
  of 3) in ~30–150s. Same rail, same interpretation card for both.
- Live reachability probe (`/playground/models/health`) — no env
  sniff, no fallback substitution during user-initiated runs.
- Save-as-case → PR flow is working.
- Inner-tool events (`PlaygroundToolEventEmitter`) pipe scenario-agent
  tool calls out to the playground rail.
- Wait-till-terminal harness (`scripts/pr343_wait_till_terminal.py`)
  asserts 8 state predicates over the full event list at terminal.

### What's stubbed vs wired

- `server/strands_agents/pipeline.py` exists but resolves missing
  leaves through
  [`_placeholders.py`](../../../server/strands_agents/_placeholders.py);
  no real end-to-end documentary run.
- c04 audio / c10 GPU dispatch / c11 assembly — module stubs only;
  no real TTS / GPU / B2.
- `interrupt_on` declared in pipeline.py; no operator UI.
- Event names in `playground/events.py` are ad-hoc (`run.dispatched`,
  `task.start`, `tool.called`, `narrate`, `interpret`, …) — not yet
  aligned with any external protocol.

### Hard invariants binding (from AGENTS.md, unchanged)

1. One TTS voice per VM.
2. All GPU workers healthy **and VRAM-sufficient** before assembly.
3. Fail closed on TTS / video render / **insufficient VRAM**.
4. QA immediately after each artifact.
5. Every artifact to B2 immediately.
6. Revision tags are sacred.
7. Approval gates are binding.

---

## 1. Principles

- **Composition, not rewrite.** No edits to `server/strands_agents/c0*`,
  no edits to `server/playground.py`, no edits to
  `frontend-playground/src/app/components/**`. New routes, new files.
- **AG-UI is the wire format.** From the first PR in this wave the
  event stream migrates from ad-hoc names to
  [AG-UI](https://docs.ag-ui.com/) typed events
  (`RUN_STARTED`, `STEP_STARTED`, `TOOL_CALL_START`, `TOOL_CALL_ARGS`,
  `TOOL_CALL_END`, `TEXT_MESSAGE_CONTENT`, `STATE_DELTA`,
  `RUN_FINISHED`). Existing ad-hoc events map 1:1.
- **Observability from day 1.** Langfuse is wired as the persistent
  OTel exporter alongside the AG-UI emitter. Every playground run
  (component or pipeline) lands as a trace. The playground card
  grows a "View Trace" button that opens
  `LANGFUSE_HOST/trace/<trace_id>`.
- **No simulators for compute.** TTS uses real Qwen3-TTS on a
  Vast.ai GPU VM. Video uses real LTX-Video 2.3 on a Vast.ai GPU
  VM. B2 storage uses real Backblaze.
- **Fail-closed pre-flight.** Before a pipeline run enters any stage
  that needs a worker, the playground probes (a) model reachability,
  (b) GPU worker health, (c) GPU VRAM ≥ component-declared minimum.
  Failure surfaces the exact shortfall, not a vague error.
- **Narrator stays load-bearing.** A real 5-min documentary takes
  20–40 min to render. The rail must never go silent for >3s. AG-UI
  migration adds `STATE_DELTA` events which give the narrator richer
  per-tick inputs.

---

## 2. Wave 2 — Real-compute pipeline as a playground component

**Framing:** the pipeline is one more component, running under
the same event contract, same narrator, same wait-till-terminal
harness — but with real TTS, real LTX video, real B2, real Langfuse
traces, and a VRAM pre-flight that fails loud. Ship on Vast.ai
GPU VMs provisioned on demand.

### 2.1 Scope — all stages use real compute

| Stage | Runs against | Notes |
|-------|--------------|-------|
| Scenario (c01) | Real LLM (OpenAI + refine cap of 3) | Already live. |
| Timing (c02/c05) | Real evaluator against real whisperX alignment from audio stage | Deterministic. |
| Audio (c04) | **Real Qwen3-TTS on Vast.ai GPU fleet** | One voice per VM; parallel per-scene. |
| Content analyst (c06) | Real LLM | Cheap. |
| Visual concepter (c07) | Real LLM | Cheap. |
| Coherence evaluator (c08) | Real LLM | Cheap. |
| Visual loop (c09) | Real orchestration; no GPU of its own | — |
| Production (c10) | **Real LTX-Video 2.3 on Vast.ai GPU fleet** | Per-VM VRAM probe; fail-loud on insufficient. |
| Assembly (c11) | Real OTIO build + real ffmpeg concat over real clips | Output is a real `.mp4` on B2. |
| Recovery / escalation (c12/c13) | Surface-only in Wave 2 | Full UX in Wave 3. |
| Approval gates (c15) | Auto-accepted in Wave 2 via `PIPELINE_AUTOACCEPT_GATES=1` | Binding in Wave 3. |

### 2.2 Backend — new modules, zero edits to existing ones

- `server/pipeline_playground.py` — `/playground/pipeline/runs` POST (start),
  `/playground/pipeline/runs/<id>` GET (snapshot),
  `/playground/pipeline/runs/<id>/stream` SSE. Reuses the existing
  `PlaygroundEventBus`.
- `server/pipeline_playground_adapter.py` — builds the orchestrator
  via `server.strands_agents.pipeline.build_orchestrator` (real
  leaves, no placeholders), runs it in an asyncio task, pipes every
  internal tool event through the existing `PlaygroundToolEventEmitter`,
  and maps deepagents stage transitions onto AG-UI `STEP_STARTED` /
  `STEP_FINISHED` with a `stage` attribute.
- `server/strands_agents/playground/events.py` — **mechanical
  migration** to AG-UI types. Old names (`run.dispatched`,
  `task.start`, `tool.called`, `narrate`, `interpret`) map onto
  (`RUN_STARTED`, `STEP_STARTED`, `TOOL_CALL_START`,
  `TEXT_MESSAGE_CONTENT [role=narrator]`,
  `TEXT_MESSAGE_CONTENT [role=interpreter]`). Backward-compatible
  field shape under the hood — no visible behaviour change for
  component runs; the wire format becomes AG-UI-spec-compliant.
- `server/worker_registry.py` — Redis-backed fleet registry.
  Workers self-register with
  `{worker_id, role, vram_gb, voice_id?, last_heartbeat}`. One TTS
  voice per VM is enforced here: `register_voice(worker_id, voice_id)`
  rejects if another worker holds that voice.
- `server/b2_checkpoint.py` — `checkpoint_artifact(run_id, artifact_id, local_path) -> b2_url`
  + `load_manifest(run_id) -> dict` + `resume(run_id) -> PipelineState`.
  Every `STEP_FINISHED` event carries `artifacts.*.b2_url`; absent
  → stage fails closed.
- `server/observability.py` — wires the Strands SDK OTel emitter to
  a Langfuse exporter. Three env vars: `LANGFUSE_HOST`,
  `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`. Each run's
  `trace_id` is written into the event stream so the "View Trace"
  button can link out.
- **VRAM probe** in `server/worker_registry.py`:
  `probe_vram(worker_id) -> {total_gb, free_gb, compute_capability}`
  via a new `/health/vram` endpoint on the worker. Pipeline refuses
  to enter Production stage if `min(worker.vram_gb for worker in pool) < 48`.
  Failure event:
  `STEP_FAILED { stage:"production", reason:"VRAM_INSUFFICIENT",
  detail:{worker_id, actual_gb, required_gb, model:"ltx-video-2.3"} }`.

### 2.3 GPU worker bootstrap (new)

Extend the existing
[`scripts/provision_playground_staging.py`](../../../scripts/provision_playground_staging.py)
with a `--role={tts,ltx}` switch. Each role gets its own bootstrap
script:

- `scripts/worker_bootstrap_tts.sh` — Python 3.12, preload Qwen3-TTS
  weights, expose `/tts/synthesise` + `/health` + `/health/vram`,
  register with the fleet registry on boot.
- `scripts/worker_bootstrap_ltx.sh` — Python 3.12 + CUDA, preload
  LTX-Video 2.3 weights, expose `/ltx/render` + `/health` +
  `/health/vram`, register on boot.

Both scripts are idempotent and run under supervisor.

### 2.4 Frontend — new routes, zero edits to existing ones

- `frontend-playground/src/app/pipeline/page.tsx` — simple form:
  topic (text), target_duration_sec (slider, 30–300), language
  (select), Run.
- `frontend-playground/src/app/pipeline/PipelineRunView.tsx` —
  three rails (status, output, interpretation) identical to the
  component workbench + a **stage ribbon**
  (`Scenario → Audio → Visual → Production → Assembly`) with the
  current stage highlighted and elapsed-in-stage.
- **View Trace button** on both the pipeline page and the existing
  component card (one small addition to the component card —
  additive, no regressions).
- **VRAM dot** beside the model-reachability dot on any card whose
  component touches a GPU worker. Red with exact shortfall on hover
  (`worker ltx-a: 40 GB available, 48 GB required for LTX-Video 2.3`).

### 2.5 Testing (harness preserved, new predicates added)

- `scripts/pipeline_wait_till_terminal.py` — copy of the PR #343
  harness, same 8 predicates + 4 new pipeline-specific:
  - `stage.sequence` — five `STEP_STARTED` events in the expected
    order.
  - `stage.duration` — no stage exceeds its declared budget.
  - `artifacts.manifest` — terminal state includes `otio_b2_url`,
    `final_mp4_b2_url`, per-scene `audio_b2_url` + `video_b2_url`.
  - `vram_probe.honoured` — if any worker reports insufficient VRAM,
    the pipeline emits `STEP_FAILED { reason:"VRAM_INSUFFICIENT" }`
    and does **not** call `/ltx/render`.
- `scripts/pipeline_vram_insufficient_test.py` — runs the harness
  on a deliberately under-provisioned GPU VM (e.g. a 24 GB RTX 4090);
  asserts fail-closed wording and that the render was never
  attempted.
- `docs/strands-migration/deploy/pipeline-wave-2-test-plan.md` —
  full test plan, same shape as the PR #343 plan.

### 2.6 Out of scope for Wave 2

- Approval-gate UI (Wave 3).
- Multi-run dashboard (Wave 3 stretch).
- Two-run-isolation on the worker pool (Wave 3).

### 2.7 Acceptance criteria

- [ ] `POST /playground/pipeline/runs` with `topic="how inflation works", target_duration_sec=60` drives every stage to terminal on real Vast.ai GPU VMs under ~15 min wall-clock.
- [ ] Terminal manifest includes B2 URLs for OTIO + final mp4 + every per-scene audio + video artifact.
- [ ] Langfuse shows the full nested trace (orchestrator → stage → SubAgent → leaf → LLM call).
- [ ] Playground card shows `trace_id` and "View Trace" links directly to the Langfuse trace.
- [ ] Running the pipeline against the **CPU staging VM** (no CUDA) fails pre-flight with `VRAM_INSUFFICIENT` and never attempts a render.
- [ ] Running the pipeline against a 24 GB GPU VM (insufficient for LTX-Video 2.3) fails pre-flight with `VRAM_INSUFFICIENT: worker X / 24 GB < 48 GB required`.
- [ ] `scripts/pipeline_wait_till_terminal.py` exits 0; all 12 predicates green.
- [ ] c01..c15 individual runs still pass their own harnesses (regression gate).
- [ ] Event wire format is AG-UI-spec-compliant (schema validated via
      the AG-UI reference JSON Schema).

### 2.8 Estimated effort

15–20 engineering days. Biggest unknowns:

- First LTX run on Vast.ai almost certainly hits model-weight
  caching / CUDA / torch-version friction. Budget 2–3 days for that.
- Qwen3-TTS voice-model provisioning: per-voice weight downloads
  are a known pain point. Budget 1–2 days.
- AG-UI wire migration is mechanical but needs tests for every
  event kind. Budget 2 days.
- Langfuse wiring is ~half a day once OTel exporter pattern is
  clear.

---

## 3. Wave 3 — Binding approval gates + escalation + multi-run isolation

**Framing:** Wave 2 auto-accepts every `interrupt_on`. Wave 3 makes
them binding, wires the escalation SubAgent, and isolates worker
pools per concurrent run.

### 3.1 Scope

- **AG-UI `STATE_DELTA` event kind used for interrupts.** Whenever
  the orchestrator reaches an `interrupt_on` tool, the adapter emits
  `STATE_DELTA { pending_interrupt: { gate_name, proposed_args,
  allow_accept, allow_edit, allow_respond } }`.
- **Operator surface** (frontend-playground): when
  `state.pending_interrupt` is set, a card expands under the output
  pane with:
  - Tool name + proposed args (JSON editor, pre-filled).
  - Three buttons: Accept / Edit & Accept / Reject.
  - Decisions POST to `/playground/pipeline/runs/<id>/resume`.
- **Escalation SubAgent as its own testable component (`c19`)** —
  callable directly from the workbench with a seeded failure
  envelope; returns `{decision, rationale, next_action}`. Pipeline
  adapter routes any twice-failed scene into it.
- **Worker pool isolation** — a second concurrent pipeline run gets
  its own worker sub-pool (picked via the registry's
  `reserve_workers(run_id, role, n) -> [worker_id, ...]`). No
  contention on `voice_id`.
- **Approval audit log** — `server/user_cases/<run_id>/approvals.jsonl`.
- **Multi-run dashboard** (stretch) — last N runs, success rate per
  stage, median duration per stage, cost per minute of final output.

### 3.2 Acceptance criteria

- [ ] Seeded-fail GPU scene triggers escalation; operator approves
      "skip" in the UI; pipeline completes; terminal manifest
      annotates the skipped scene.
- [ ] Operator edits a visual prompt at the visual-production gate;
      pipeline resumes with the edited prompt; final video reflects it.
- [ ] Two concurrent pipeline runs with different voices each get
      their own TTS worker; neither stalls.
- [ ] All Wave 2 harnesses still pass.

### 3.3 Estimated effort

6–9 engineering days.

---

## 4. Risks and open questions

1. **GPU budget.** A Vast.ai H100-class VM is ~$2/hr under load,
   ~$0.30/hr idle. Comfortable monthly budget: ~$150. Minimum
   viable: ~$50. Decision before Wave 2 starts.
2. **TTS credential path.** Kimi invalid. Decision:
   (a) Qwen3-TTS self-hosted on our own GPU VM (baseline plan —
       adds 1–2 days for voice-model provisioning);
   (b) OpenAI TTS (paid, simple, less authentic);
   (c) ElevenLabs (paid, best quality, new dependency).
3. **B2 credentials.** `B2_APPLICATION_KEY_ID` +
   `B2_APPLICATION_KEY` needed on staging + on every worker VM.
   Confirm availability.
4. **Langfuse hosting.** Free tier (cloud.langfuse.com) covers
   ~50 k events/mo — plenty for early iteration. Self-host option
   exists but adds a VM. Recommend cloud free tier for Wave 2,
   self-host if volume grows.
5. **c01 convergence bug.** Separate from this plan; refine cap
   guarantees termination but sub-par scenarios may land at cap.
   File as its own issue so it doesn't block Wave 2.
6. **AG-UI migration risk.** Mapping ad-hoc events → AG-UI kinds is
   mechanical but the frontend decoder has to be updated
   atomically. Acceptance criterion #8 (AG-UI spec validation)
   catches this.

---

## 5. Decisions needed to unblock Wave 2 coding

- [ ] GPU budget sign-off (≥ $50/mo minimum, ≥ $150/mo comfortable).
- [ ] TTS credential choice (Qwen3-TTS self-hosted is the default;
      confirm or pick alternative).
- [ ] Confirm B2 credentials are available (if not, Wave 2 adds
      a credential-provisioning step).
- [ ] Langfuse hosting: cloud free tier (default) or self-host?
