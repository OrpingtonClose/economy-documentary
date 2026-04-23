# Playground live feedback — test report (PR #343)

**Target:** http://142.171.48.138:29561/components (staging Vast.ai VM)
**Branch tested:** `devin/1776922069-fix-scenario-live-run` merged to `main`
**Session:** https://app.devin.ai/sessions/bce21d274f18469d8a54474ce059299c
**Recording:** [`rec-461555b3-1d6f-4ad1-8deb-804916df849b-edited.mp4`](https://app.devin.ai/attachments/3743963c-e823-42cb-9a4b-927bd8e89a23/rec-461555b3-1d6f-4ad1-8deb-804916df849b-edited.mp4)

## One-line summary

Ran c01 scenario_agent (live LLM, 228s+ in flight) and c02 timing evaluator (sub-1s
deterministic) on the staging playground and watched the live-feedback UI react to both.
The three user-facing claims — typewriter reveal, fixed-height layout, continuous
narration — are observable end-to-end. One post-terminal bug found in the status
rail.

## Escalations (things to fix or watch)

1. **Narration content is repetitive during long c01 runs.** The scenario_agent's
   generate → evaluate → refine loop runs internally to the Strands agent and doesn't
   emit outer playground events, so the narrator has nothing new to say and keeps
   re-framing `task.start` as "dispatching c01 — Ns, still confirming availability of
   models …". The elapsed counter advances correctly (verified 7.5s → 228s) but the
   noun phrase doesn't change. **This is not a regression from this PR** — it's a gap
   between the inner agent and the outer event stream. Should be tracked separately.

2. **Post-terminal status rail bug (minor).** After c02's `run.ok` event, the status
   rail keeps showing "stalled at interpret — 37s" instead of clearing or showing
   "completed". The interpretation card itself populates correctly with the OK chip
   and paragraph. The `interpret` event is being treated as an in-flight step rather
   than a finishing touch. Low severity — the user sees the right output, just a
   confusing status line next to it.

3. **Moonshot declared-model probe fails with AuthenticationError.** Expected —
   reproduces the user-reported result. The key on staging is invalid. Per the
   declared-model contract ("if the test fails, the model failed — candidate for
   discard"), this is a correct red dot. Not this PR's concern.

## Assertions

### Primary flow — c01 scenario_agent / economics_basics (live LLM)

- **Continuous narration with advancing elapsed counter — passed.**
  Counter observed at 7.5s, 18s, 30s, 43.5s, 60s, 78s, 132s, 169.5s, 180s, 228s across
  the run. Raw event log grew from 6 events to 158+ events. Narration event emitted
  every ~1.5-3s without gaps.

- **Status line stays 44px fixed height — passed.**
  The `h-11` rail retained its pixel height across the run. Input panel, interpretation
  card, and raw event log disclosure did not shift vertically as the narration text
  length varied (short like "dispatching…" → long like "dispatching c01 — 180.0s,
  still confirming availability of models gemini/…").

- **Typewriter reveal visible (not snap-in) — passed.**
  Screenshots at counter=94 ("…model_id=opena") and counter=119 ("gemini/gemi|")
  both show text truncated mid-word with the blinking caret, consistent with live
  char-by-char reveal at the 12ms/char rate. A snap-in implementation would show
  complete sentences at every frame.

- **Interpretation card pre-allocated (min-h-128px, dashed placeholder) — passed.**
  Visible in the idle state before clicking Run; dashed border and placeholder text
  `awaiting interpretation — will land once the run terminates` shown.

- **c01 run reached terminal — untested in this window.**
  The Strands agent was still iterating generate → evaluate (×8) → refine (×5) at
  228s. This is a legitimate long run, not a stall. The run was left in flight so
  the recording ended before terminal. The terminal + interpretation-card-populated
  path is exercised by c02 below.

### Secondary flow — c02 timing_evaluator / intent_exact (deterministic)

- **Idle state empty + placeholder — passed.**
  Status line blank, interpretation card shows `run a case to see a one-paragraph
  interpretation here` with dashed border. No layout shift on page load.

- **Idle → dispatch → terminal in <1s with no layout jump — passed.**
  Status line transitioned through `run completed · #5 run.ok`; output panel populated
  with `{ "timing_passed": true }`; no RawEventLog disclosure shift.

- **Interpretation card landed with OK chip + paragraph — passed.**
  Card transitioned from dashed placeholder to solid accent-colored border with a
  paragraph. OK status chip rendered. Paragraph is contract-honest — notes that
  c02 is deterministic ("no model was probed or utilized during this run").

- **Raw event log has ordered events — passed.**
  6 events with monotonic seq: `#1 run.dispatched → #2 probe.start → #3 task.start
  → #4 task.done → #5 run.ok → #6 interpret`.

- **Status rail clears after terminal — failed (minor).**
  Rail still shows "stalled at interpret — 37s" 30+ seconds after `run.ok`. The
  interpretation itself is correct and visible. Bug is in the live-line selection
  (interpret event is treated as in-flight). Low severity.

## Screenshots

### c01 continuous narration — counter advancing during long LLM silence

![c01 at counter 94 — narration mid-word at model_id=opena](https://app.devin.ai/attachments/f516cc6a-4ddb-498c-a873-9073d28f7c83/screenshot_a16a73e2b06142e79a48819214ee1a7d.png)

![c01 at counter 119 — narration mid-word at gemini/gemi](https://app.devin.ai/attachments/ff6edd48-15c8-4459-bc85-8e9253780a77/screenshot_cba8572aab5046f2884750c871f5293e.png)

Status rail at Y-pixel ~390 both frames (±2px), text truncation mid-word confirms
live typewriter. Counter grew 132.0s → 169.5s between the two frames, with Raw
event log advancing 94 → 119 events.

### c02 terminal + interpretation card

![c02 terminal — run.ok with OK chip, interpretation populated](https://app.devin.ai/attachments/2d02811a-896a-445a-be90-05117238f207/screenshot_4c7d20070d8c44c08447be933501e5d9.png)

![c02 interpretation card with OK chip + paragraph](https://app.devin.ai/attachments/c764998a-7d75-466b-8b41-c8111c0d809e/screenshot_28644420291c4e349276f2188c51a307.png)

![c02 raw event log — 6 ordered events, status rail bug visible (stalled at interpret — 37s after run.ok)](https://app.devin.ai/attachments/ea04d74b-2748-4c7e-845a-6dbec2e389ff/screenshot_b4cda40f48194c6eb1e6b57c75854cc8.png)

### Layout stability — interpretation card pre-allocated in idle state

![c02 idle state — placeholder with dashed border, fixed height reserved](https://app.devin.ai/attachments/01307c5d-d9f5-4234-95d6-9ea10be5554c/screenshot_a08c66a77aaa41abac5e1a9a134b68ba.png)

## Recording

Full continuous recording of both flows:
[`rec-461555b3-1d6f-4ad1-8deb-804916df849b-edited.mp4`](https://app.devin.ai/attachments/3743963c-e823-42cb-9a4b-927bd8e89a23/rec-461555b3-1d6f-4ad1-8deb-804916df849b-edited.mp4)

Structured annotations in-video:
- c01 test_start at run initiation
- Assertion: counter advancing 7.5s → 169.5s, raw log 6 → 119 events — **passed**
- c02 test_start at run initiation
- Assertion: run.ok in <1s with interpretation card + OK chip — **passed**
- Assertion: raw event log completeness (6 ordered events) — **passed**
- Assertion: status rail cleared after terminal — **failed** (minor bug noted above)

## What I did not test

- Evaluator correctness (covered by the `evaluate` endpoint's unit tests).
- Save-as-case flow (out of scope for this PR).
- `MODEL_UNREACHABLE` hard-gate path (the declared moonshot/kimi-k2 model is already
  red, but I did not attempt a run against a c01 case with an explicitly unreachable
  model — the existing red-dot reachability indicator covers the user-visible piece).
- c01 terminal state + interpretation card populated — the live run was still in
  flight at 228s when the recording ended (generate → evaluate ×8 → refine ×5
  iterations). c02 exercises the terminal + interpretation-card path.
