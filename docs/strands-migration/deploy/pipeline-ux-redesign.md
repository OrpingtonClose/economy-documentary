# /pipeline UX redesign — heavy thinking

## Concrete complaints

1. "incredibly low information and ergonomics"
2. "pressed run pipeline and one thing changed, then nothing"
3. "navigated off and returned to not be able to see the run"

## Diagnosis

### Why "one thing changed, then nothing"

I checked the live backend log. The most recent run the user submitted
(`run_df5635622ce4`, 22:51:53 UTC) is **not stuck** — it advanced through
`pipeline.stage.scenario.start`, `generate_scenario`, `evaluate_scenario`,
`pipeline.stage.scenario.end`, then dispatched five concurrent
`launch_audio_render` calls. By the time the user wrote, the run was in
the middle of the audio stage waiting for Qwen3-TTS to return.

**The run is alive. The UI is silent.** Audio render takes 30–90 s per
scene. During that window the page emits zero new structured QA events
(QA gates only fire after the audio file lands). The user sees the
form transition to "running" state (the one thing that changed), and
then sixty seconds of nothing, and concludes the page is broken.

The page is **artifact-oriented when it needs to be process-oriented**.
Metric cards are the right widget for the second half of the run; for
the first half they are empty cells. There is no system spine that
keeps the user oriented in the absence of artifacts.

### Why "navigated off, can't return to run"

The current `/pipeline` page holds `run_id` in React state, not in the
URL. Three knock-on effects:

- Refresh wipes the run.
- Back button wipes the run.
- The URL is unshareable — you can't paste a "watch this run" link.
- localStorage is not consulted, so a bare `/pipeline` after navigation
  doesn't auto-reattach to the last in-flight run.

The re-attach affordance from `108fe58` (`?run_id=X` query param triggers
`useRunStream` to subscribe) is in place — but **only fires if the URL
has the param**. The submit handler never writes the param. So the
plumbing exists; the trigger doesn't.

### Why "incredibly low information and ergonomics"

The page has roughly one informational bit per scroll-pixel:
- form (top, fine)
- "Submit" pill that flips state (one bit, fine for that moment)
- a horizontally-scrollable trajectory line per event (low density)
- per-scene metric cards (excellent density once populated, empty for
  60+ s)
- a "View master" link at the very bottom of a 20-minute experience

There is no:
- run id (visible) — the user has no name for the thing they started
- elapsed time
- current stage (where am I in the 7-stage process)
- ETA (rough — "audio averages 90s, 6 scenes, ~9 min")
- last event age (heartbeat — "is this thing alive")
- worker health (TTS up? GPU up?)
- recent runs (left/right sidebar — "did I run this before")
- approval-gate prominence (gates are buried in the trajectory line stream)
- error prominence (errors are buried in the same stream)
- explanation tooltips on metric pill thresholds

## Mental-model failures behind the symptoms

1. **Pages should have a heartbeat.** A microwave hums. CI scrolls
   logs. /pipeline emits no proof-of-life when the system is waiting
   on a remote worker. Heartbeats are non-optional for any UI that
   sits in front of a multi-minute durable workflow.

2. **State that survives navigation must live in the URL.** Refresh,
   back, and share are the three things users do unconsciously. If
   any of them wipes state, the trust model is broken. URL is cheap;
   not using it is a bug.

3. **Information architecture should mirror the temporal experience,
   not the data model.** Today the page is laid out by widget
   (form → cards → trajectory → output) which is convenient for the
   developer. The user experiences the run *as a timeline*: 0–10s
   submit, 10–30s scenario, 30–90s first audio, 90–150s first visual,
   etc. The page should match that timeline so the user can map "I am
   here" at any moment.

4. **Approval gates are not events. They are interrupts.** A 1-line
   trajectory entry is the right rendering for `audio.render.start`;
   it is the wrong rendering for "the entire pipeline is paused
   waiting on you to click approve." The latter needs a pinned banner,
   page-dimming, and an explicit primary action.

5. **Errors are not events either.** Same logic. A `verdict=fail` from
   `qa_audio_completeness` is not "one more line in the log" — it is a
   hard gate that may have aborted the run. Pinned, red, with a
   "what now" path.

## Principles for the redesign

### Layered information density

Three layers, top to bottom, with sticky behavior on the top layer:

- **Identity** (always visible, sticky): run id, topic, started-at,
  elapsed, stage chip, last event age. ~80 px tall.
- **Progress** (scroll): stage tracker (7 boxes), per-scene metric
  cards, trajectory event log.
- **Evidence** (bottom): final master.mp4 player, B2 manifest link.

### Heartbeat is non-negotiable

A `lastEventAgeSec` ticker, updated client-side every second, sits in
the identity bar. Color escalates:

- 0–60 s: neutral
- 60–180 s: amber, with "still rendering audio for scene 3" subtitle
  inferred from the last seen event
- >180 s: red banner pinned to top, "No events for 3 min — possible
  stall on $stage. View trajectory · cancel run · re-submit."

### URL is the source of truth

- Submit handler `router.replace('?run_id=' + id)` immediately on
  receiving the run id back from `POST /playground/pipeline/runs`.
- On mount with `?run_id=X`, subscribe via `useRunStream`. Already
  works.
- On bare `/pipeline` mount, read `localStorage.pipeline_last_run_id`,
  HEAD `/playground/runs/{id}` to check if `closed=false`, and if so
  `router.replace('?run_id=' + id)`. Otherwise show "Last run:
  $id ($status, $when ago) · view · or start new" + the form.

### Stage tracker as the headline

Seven horizontal chips: `Plan · Scenario · Audio · Visual · QA ·
Assembly · Publish`. Driven by `pipeline.stage.<name>.start` and
`.end` events. Current chip pulses. Completed chips have check.
Pending chips are dim. Single glance answers "where am I."

### Approval gates are loud

When an `interrupt_on`-wrapped tool fires, render a banner pinned to
the top of the page (above the identity bar), darken the page body to
30% opacity, and surface:

- the tool name and what it will do
- a visual diff (what's about to change in the artifact tree)
- two primary actions: Approve, Reject (with diff edits)

Banner cannot be dismissed except by clicking one of the actions.
Optional ping sound on first appearance.

### Errors are louder

Same pattern, red, with:

- the failing tool + verdict
- the measurement that breached threshold
- a "jump to trajectory at this event" link
- a "delegate to escalation SubAgent" button (already a tool the
  orchestrator can call — surface it as a UI affordance too)

### Recent runs always visible

Left sidebar (collapsible). Lists the last 20 runs:

- ● in-flight runs (with elapsed, with current stage)
- ✓ completed runs
- ✗ failed runs (with the failure stage)

Click switches the active `?run_id`. The sidebar is the killer feature
for the "navigate off and return" complaint — it makes runs first-class
addressable artifacts of their own.

### Tooltips on every metric pill

Hover any QA verdict pill: tooltip explains what was measured, the
threshold, and the actual reading. Examples:

- `qa_audio_completeness`: "Tail RMS ≤ -25 dBFS OR ≥ 100 ms trailing
  silence. Measured: tail_rms=-44.2 dBFS, trailing_silence=180 ms."
- `qa_duration_align`: "|audio_duration - video_duration| ≤ 0.50 s.
  Measured: delta=0.02 s."
- `qa_stills_judge`: "mean inter-second pixel delta > 1.0 in 95% of
  windows. Measured: mean_pixel_delta=8.7, motion_windows=80/80."

Removes the need for the user to read source to know what passed.

## Slicing the implementation

Five slices, ordered by impact-per-effort:

### Slice UX-1: URL state + auto-resume + recent runs sidebar
Fixes complaint 3 entirely. Adds a `pipeline_last_run_id` localStorage
key, a 30-line submit handler change to push `?run_id`, a
`/playground/runs?limit=20` endpoint (already exists?), and a
left sidebar that lists them. ~2 h.

### Slice UX-2: Stage tracker + last-event-age heartbeat + trajectory log
Fixes complaints 1 and 2. Adds the sticky identity bar, the 7-stage
chip tracker, the heartbeat ticker, and converts the trajectory log
to auto-scroll-latest-at-top with relative timestamps and colored
event-kind chips. ~3 h.

### Slice UX-3: Approval gate banner + error banner
Fixes the "I missed the gate" failure mode. Pinned banner pattern
applied to `pipeline.interrupt.*` and `pipeline.tool.*.error` events.
Page dims; banner cannot be dismissed except via primary action. ~2 h.

### Slice UX-4: Worker health header + metric pill tooltips
Fixes the residual "low information" feeling once the structural
fixes are in. ~1 h.

### Slice UX-5: ETA estimator + cancel run
A rough ETA derived from "stages completed × average duration" plus
a Cancel button that calls `POST /playground/runs/{id}/cancel`
(needs a backend route too). ~2 h.

## Order of operations

1. Slice UX-1 first because it makes every subsequent test session
   resumable across page reloads — a multiplier on every other slice.
2. Slice UX-2 next because it fixes the biggest-bang complaint
   ("looks dead") and the rest of the page can stand on top of it.
3. UX-3, UX-4, UX-5 in order.

Each slice ships as its own PR with CI + manual smoke against the
public cloudflared URL.

## Out of scope (deliberately)

- A separate "design system" pass. The current Tailwind tokens are
  fine; we just need to use more of them.
- Mobile responsiveness. The pipeline is desktop-first.
- Internationalization. English-only UI.
- A separate admin page. Worker health goes in the identity bar header
  for now; a full admin/observability page is a future thing.
