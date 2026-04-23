# Next wave — plan

**Status:** draft for sign-off.
**Precondition:** every existing component (c01–c15 specs + their
task adapters, the playground backend, the frontend-playground, the
live narrator rail, the inner-tool emitter, the wait-till-terminal
harness, and the refine cap) stays **untouched as testable units**.
Nothing in this plan requires modifying them; new work composes them
through the same HTTP surface the workbench already uses.

---

## 0. Core facts (enumerated, not assumed)

### What's testable in isolation today

- Every slot `c01..c15` is declared in
  [`server/strands_agents/playground/registry.py`](../../../server/strands_agents/playground/registry.py)
  with its declared model(s), canonical case list, and evaluator stack.
- The component workbench runs **all** of them through a uniform
  `POST /playground/components/<id>/runs` → event-ring-buffer → SSE →
  narrator pipeline. c02 (deterministic intent-exact) terminates in
  <1s; c01 (live scenario agent on OpenAI / Kimi / Gemini) terminates
  in ~30–150s with a hard refine cap of 3. Both are observed through
  the same rail + interpretation card.
- Reachability per declared model is a **live probe**, not an env
  sniff (`/playground/models/health`). Fail-closed: no fallback
  substitution during a user-initiated run.
- Save-as-case → PR flow is a working primitive: any run can be
  promoted to a committed case with one click.

### What's stubbed vs wired

- `server/strands_agents/pipeline.py` (component 14) exists but
  resolves missing leaves through
  [`_placeholders.py`](../../../server/strands_agents/_placeholders.py)
  — the orchestrator can be built but does not yet run a real
  documentary end-to-end.
- c04 audio render, c10 GPU dispatch, c11 assembly have module
  stubs; no real TTS/GPU workers are provisioned.
- `interrupt_on` (approval gates) is declared in pipeline.py but the
  playground frontend has no surface for accept/edit/reject.
- B2 checkpointing (hard invariant #6 in AGENTS.md) is not wired.
- Escalation SubAgent exists as a module but isn't reachable from a
  real run.

### Hard invariants still binding (unchanged)

From [`docs/strands-migration/AGENTS.md`](../AGENTS.md). These do **not**
get relaxed for any wave below:

1. One TTS voice per VM.
2. All GPU workers healthy before assembly.
3. Fail closed on TTS / video render.
4. QA immediately after each artifact.
5. Every artifact to B2 immediately.
6. Revision tags are sacred.
7. Approval gates are binding.

### What the user is now asking

Compose the tested units into a pipeline, without touching them.
Success looks like: one more button on the workbench, labelled
*"Run full pipeline"*, that takes a topic + duration + language and
drives the same observed-through-the-rail experience as a single
component — but for the whole documentary.

---

## 1. Principles for every wave below

- **Composition, not rewrite.** New work is a layer above the
  playground HTTP surface. No edits inside `server/strands_agents/c0*`
  modules, no edits inside `server/playground.py` handlers that already
  pass tests, no edits to the `frontend-playground/src/app/components/`
  workbench. New UI goes on a **new route**
  (`frontend-playground/src/app/pipeline/`); new backend goes in a
  **new module** (`server/pipeline_playground.py`) mounted under
  `/playground/pipeline/*`.
- **Same event contract.** The pipeline run emits the same event
  kinds as a component run (`run.dispatched`, `task.start`,
  `task.done`, `tool.called`, `tool.returned`, `narrate`, `run.ok`,
  `interpret`) plus two new ones: `stage.enter` / `stage.exit`. This
  means the existing narrator, stall rail, typewriter, and
  wait-till-terminal harness work unchanged.
- **Narrator becomes load-bearing.** A real 5-min documentary takes
  on the order of 20–40 minutes to render. Anxiety-reduction over
  that window is the entire reason the narrator exists. The event
  stream must be rich enough that the narrator never has to say
  *"still working"* twice in a row.
- **Fail-closed all the way down.** Any leaf's `MODEL_UNREACHABLE` /
  `WORKER_UNHEALTHY` / `MISSING_CREDENTIAL` aborts the pipeline with
  a real error event, not a silent degrade.
- **Every change ships through the playground.** No hidden CLI-only
  path. If you can't observe it through the workbench, it doesn't
  count as done.

---

## 2. Wave 2 — Composable orchestrator as a playground component (CPU-only)

**Framing:** treat the pipeline as *one more component*. Same run
endpoint shape, same event stream, same narrator, same harness.
Ship it on the existing CPU staging VM with simulated TTS + simulated
GPU. No real media yet. Goal is to prove the wiring works end-to-end
and that a 15-stage run is observable without anxiety.

### 2.1 Scope

| Stage | Runs against | Why this is safe for Wave 2 |
|-------|-------------|-----------------------------|
| Scenario (c01) | Real LLM (OpenAI + refine cap of 3) | Already live on staging. |
| Audio (c04) | `TTSToolSimulator` — returns fake WAV paths + alignment | Already exists in `strands_evals.simulation`. |
| Timing (c02/c05) | Real evaluator against simulated alignment | Pure deterministic. |
| Content analyst (c06) | Real LLM | Cheap. |
| Visual concepter (c07) | Real LLM | Cheap. |
| Coherence evaluator (c08) | Real LLM | Cheap. |
| Visual loop (c09) | Orchestration only (no GPU) | Simulated frames back. |
| Production (c10) | `GPUWorkerSimulator` | Returns fake .mp4 URLs. |
| Assembly (c11) | **Real OTIO build** + real ffmpeg concat over fake clips | Output is a real `.otio` file; the `.mp4` will be black-frames concat but that's fine for wiring validation. |
| Recovery / escalation (c12/c13) | Seeded-fail path via simulator knob | Covers the escalation branch. |
| Approval gates (c15) | **Pending until Wave 4** — for Wave 2 gates are bypassed via `PIPELINE_AUTOACCEPT_GATES=1` in staging | Explicitly flagged in the rail. |

### 2.2 Backend changes (all **new** files)

- `server/pipeline_playground.py` — mount
  `/playground/pipeline/runs` (POST — start), `/playground/pipeline/runs/<id>`
  (GET — snapshot), `/playground/pipeline/runs/<id>/stream` (SSE).
  Reuses the same `PlaygroundEventBus` the component runs use so
  narrator, rail, typewriter, and the wait-till-terminal harness all
  work unchanged.
- `server/pipeline_playground_adapter.py` — the one place where the
  pipeline graph is invoked. Imports
  `server.strands_agents.pipeline.build_orchestrator`, injects the
  simulators as `@tool` shims, runs it in an asyncio task, pipes all
  internal tool events through `PlaygroundToolEventEmitter` (already
  exists — just re-used), and maps `deepagents` stage transitions onto
  `stage.enter` / `stage.exit` events.
- Two new event kinds: `stage.enter { name, position:N/5 }` and
  `stage.exit { name, position:N/5, duration_ms, artifacts:{...} }`.
  Narrator prompt is updated to preferentially mention stage in
  progress.
- Reachability aggregation: pipeline run refuses to start unless
  every **declared model for every component it will touch** has a
  green probe from within the last 60s. Failure returns
  `MODEL_UNREACHABLE` with the exact declared-model id that failed,
  not a vague "some model".

### 2.3 Frontend changes (all **new** files)

- `frontend-playground/src/app/pipeline/page.tsx` — simple form:
  topic (text), target_duration_sec (slider), language (select), Run.
- `frontend-playground/src/app/pipeline/PipelineRunView.tsx` — same
  three rails as the component workbench (status line, output,
  interpretation) plus a **stage ribbon** above showing
  `Scenario | Audio | Visual | Production | Assembly` with the
  current one highlighted and elapsed-in-stage.
- Zero changes to `frontend-playground/src/app/components/[id]/**`.
  New routes, new components, new `usePipelineRunStream` hook that
  piggybacks on the existing SSE plumbing.

### 2.4 Testing (wait-till-terminal contract preserved)

- `scripts/pipeline_wait_till_terminal.py` — copy of the PR #343
  harness, same 8 predicates plus 3 new pipeline-specific ones:
  - `stage.sequence`: exactly 5 `stage.enter` events in order
    (scenario, audio, visual, production, assembly).
  - `stage.duration`: no stage exceeds its declared budget in
    `STATE_SCHEMA.md`.
  - `artifacts.manifest`: terminal state includes
    `{otio_path, manifest_json, final_mp4_path}` all non-null.
- `docs/strands-migration/deploy/pipeline-wave-2-test-plan.md` —
  companion to `pr-343-inner-tool-events-test-plan.md`. Same format.

### 2.5 Out of scope for Wave 2

- No real TTS / LTX / B2.
- No approval-gate UI.
- No multi-run dashboard.
- No Langfuse.

### 2.6 Acceptance criteria

- [ ] `POST /playground/pipeline/runs` with `topic="how inflation works", target_duration_sec=60` drives every stage to terminal on the existing CPU staging VM in under 10 minutes.
- [ ] `scripts/pipeline_wait_till_terminal.py` exits 0, all 11 predicates green.
- [ ] Browser walkthrough (test-mode recording) shows the stage ribbon
      advancing, the rail never silent for >3s, the interpretation card
      rendering at terminal, and zero layout jumps.
- [ ] Pulling `http://142.171.48.138:29561/components/c01` and running
      the c01 case `economics_basics` standalone still passes its own
      harness — i.e. Wave 2 did not regress any existing component.

### 2.7 Estimated effort

4–6 engineering days. Lion's share is the simulator-shim plumbing
(translating `@tool` calls into playground events). Simulators
themselves exist in `strands-evals` already.

---

## 3. Wave 3 — Real compute (TTS + GPU + B2 checkpoints)

**Framing:** Wave 2 proved the wiring. Wave 3 swaps the simulators
out for real Vast.ai GPU workers and real TTS, and wires B2
checkpoints so any run is resumable.

### 3.1 Scope

- **GPU worker fleet** — extend the existing Vast.ai provisioner
  (`scripts/provision_playground_staging.py`) with a `--role=gpu-tts` /
  `--role=gpu-ltx` switch. Each role gets its own bootstrap script
  that preloads the model weights (Qwen3-TTS for tts, LTX-Video 2.3
  for video). Worker registers itself in a new redis-backed
  registry (`server/worker_registry.py`). One TTS voice per VM —
  hard-enforced via registry: `register(worker_id, role, voice_id?)`.
- **TTS pool** — a small service that picks the worker for a given
  `voice_id`. Per AGENTS.md invariant #1, different voices go to
  different VMs. Parallel audio launches across scenes with the same
  voice queue onto the same worker; scenes with different voices
  fan out.
- **LTX pool** — similar, but scheduling is per-GPU-VRAM. Uses the
  existing `AsyncTaskPool` contract from `server/tools/task_pool.py`.
- **B2 checkpoint helper** — `server/b2_checkpoint.py`:
  - `checkpoint_artifact(run_id, artifact_id, local_path) -> b2_url`
  - `load_manifest(run_id) -> dict`
  - `resume(run_id) -> PipelineState`
  - Hard-gated: every stage's `stage.exit` event must include
    `artifacts.*.b2_url`. If it doesn't, the stage fails closed.
- **Health-aware dispatch** — pipeline run refuses to enter
  Production stage unless the LTX pool reports N healthy workers,
  with N = number of scenes. Matches AGENTS.md invariant #2.

### 3.2 New components on playground

- **c16 — `tts_worker_fleet`** — testable as its own component:
  "does the TTS pool return a healthy worker for voice_id=X within
  Ns?"
- **c17 — `gpu_worker_fleet`** — same for LTX workers.
- **c18 — `b2_checkpoint`** — round-trip test: upload a small file,
  read it back, compare hash.

Wave 3 adds **three new testable units** without modifying any
existing one.

### 3.3 Out of scope for Wave 3

- No multi-tenant run isolation. Two pipeline runs compete for the
  same worker pool — a known Wave 4 item.
- No approval gates (still auto-accepted).

### 3.4 Acceptance criteria

- [ ] A real 3-scene, 60s documentary renders end-to-end with real
      Qwen3-TTS audio and real LTX video on Vast.ai GPUs.
- [ ] Every artifact (per-scene audio, per-scene video, OTIO, final
      mp4) has a B2 URL in the terminal manifest.
- [ ] Killing the pipeline run at Production stage, then calling
      `POST /playground/pipeline/runs/<id>/resume` picks up from the
      last checkpointed artifact, skipping already-rendered scenes.
- [ ] c01..c15 individual runs still pass their own harnesses.
- [ ] Cost per "hello economy" 60s run is under $2 in GPU time.

### 3.5 Estimated effort

10–14 engineering days. The unknowns are real: first LTX run on
Vast.ai almost certainly hits a model-weight caching issue, first
TTS run almost certainly hits a voice-model-not-found issue. Budget
for debugging before budget for polish.

---

## 4. Wave 4 — Approval gates UI + escalation SubAgent wiring

**Framing:** Waves 2–3 auto-accept every `interrupt_on`. Wave 4 makes
them binding, with real operator UX.

### 4.1 Scope

- **New event kind:** `interrupt { gate_name, proposed_args, allow_accept, allow_edit, allow_respond }`.
  Emitted by the pipeline adapter whenever the orchestrator reaches
  an `interrupt_on` tool.
- **Operator surface** (frontend-playground): when the rail shows
  `interrupt`, a modal-less card expands under the output pane with:
  - The tool name + proposed args (JSON editor, pre-filled).
  - Three buttons: Accept / Edit & Accept / Reject.
  - Accept → `POST /playground/pipeline/runs/<id>/resume {decision: "accept"}`.
  - Edit & Accept → client sends edited args.
  - Reject → triggers re-plan (pipeline re-enters upstream stage).
- **Escalation SubAgent as its own testable component (c19)** —
  callable directly from the workbench with a seeded failure
  envelope; returns `{decision, rationale, next_action}`. Pipeline
  adapter routes any twice-failed scene into it.
- **Approval audit log** — each decision writes a row to
  `server/user_cases/<run_id>/approvals.jsonl`. Auditable after the
  run.

### 4.2 Acceptance criteria

- [ ] A seeded-fail GPU scene triggers the escalation path; operator
      approves "skip" in the UI; pipeline completes with one fewer
      scene; terminal manifest annotates the skipped scene.
- [ ] Operator can edit a visual prompt at the visual-production gate;
      pipeline resumes with the edited prompt; final video reflects
      the edit.
- [ ] Rejecting an assembly gate drops the run back to Production
      re-plan, not a hard failure.
- [ ] All Wave 2 + Wave 3 harnesses still pass.

### 4.3 Estimated effort

5–7 engineering days.

---

## 5. Wave 5 (stretch) — Observability + multi-run dashboard

- Langfuse tracing wired through `deepagents`' middleware hook.
- Per-run cost tracked per model (OpenAI tokens, Vast.ai GPU hours,
  B2 egress).
- `frontend-playground/src/app/dashboard/` — last N runs, success
  rate per stage, median duration per stage, cost per minute of
  final output.

Acceptance: click any run in the dashboard → full Langfuse trace
opens in a side panel with nested spans per component.

---

## 6. Risks and open questions (need sign-off before coding)

1. **GPU budget.** Wave 3 requires at least one persistent Vast.ai
   GPU VM (~$0.50–$1.00/hr idle, more under load). A 60s hello-world
   run costs on the order of $1–$2 in GPU time. Green-light for a
   monthly GPU budget of roughly $100 covers comfortable
   iteration; $30–$40 covers minimum viability.
2. **Real TTS credential.** Kimi is invalid. Options:
   (a) provision Qwen3-TTS on our own GPU VM (covered by Wave 3,
   but adds ~3 engineering days of model-loading plumbing);
   (b) use OpenAI TTS (paid, simple, less authentic voice);
   (c) use ElevenLabs (paid, best quality, new dependency).
   Need a decision before Wave 3 starts.
3. **B2 creds.** Are Backblaze credentials available on staging today?
   If not, Wave 3 adds a credential-provisioning step.
4. **c01 convergence bug** — separate from this plan; currently the
   refine cap ensures termination but the final scenario may be
   sub-par when cap is hit. Worth filing as its own issue so it
   doesn't block Wave 2.
5. **Two-pipeline-runs-at-once** — Wave 3 does not isolate worker
   pools per run. If two runs race onto the same TTS worker with
   different voices, the second one stalls. Documented; explicit
   Wave 4 work.
6. **Is "run the whole pipeline as a component" the right framing?**
   This plan assumes yes. Alternative framing: keep the pipeline as
   a distinct top-level surface with its own UI and its own event
   contract. The cost of the alternative is doubling narrator /
   rail / typewriter / harness plumbing. Recommend the
   composition framing.

---

## 7. What needs to be decided to unblock Wave 2

- [ ] Accept the "pipeline-as-new-playground-surface" framing
      (yes = Wave 2 starts; no = re-frame).
- [ ] Confirm simulated TTS + GPU in Wave 2 is acceptable as the
      first milestone (vs insisting on real workers up front).
- [ ] Sign off on the new URL namespace
      `/playground/pipeline/*` on the backend and
      `/pipeline` on the frontend.

Nothing else is needed to start Wave 2. Waves 3–5 get re-confirmed
closer to their start.
