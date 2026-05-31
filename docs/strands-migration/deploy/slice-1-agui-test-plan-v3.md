> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Slice 1 AG-UI wire format — test plan v3 (live UI regression)

PR: https://github.com/OrpingtonClose/economy-documentary/pull/346
Branch: `devin/1776960759-agui-wire-format` @ `7f98e17`
Stack: local (staging CPU VM from PR #342 is gone; pure wire-format change has no worker dependency so localhost is a valid surface)
Backend: `http://localhost:18000` (no `GOOGLE_API_KEY` → OpenAI fallback; Gemini quota irrelevant)
Frontend: `http://localhost:3100`

## What this plan is adversarial about

Last session's browser test was thin: c04 success + c01 error banner. I never
observed the scenario agent's inner-tool loop or live narration render in the UI
during a real long run. That is the **UI regression surface most likely to
silently break** from a wire-format migration:

- The envelope is spread via `**agui_envelope(self.kind)` into `Event.to_dict()`.
  Order-of-keys in that spread determines whether `kind` / `summary` survive.
- The frontend `RunEvent` widening accepts optional AG-UI fields; any
  consumer that previously depended on "exactly these 5 keys" could behave
  differently.
- The SSE decoder is a plain `JSON.parse(ev.data) as RunEvent` — extra keys
  flow through transparently, *unless* the shape of a specific event changed.

Each test below is constructed so it **fails differently** for different bugs,
with assertions evaluated as **state predicates over the complete event list
at the observation cutoff** (not flow/timing predicates). Observation cutoff is
`closed == true` — wait till terminal, always, per user directive.

## Environments

- **Local backend**: `uvicorn server.playground:app --port 18000` (already
  verified running in smoke-test; 59-event c01 run observed ending in
  `run.error` due to a mid-stream fallback, which means the inner tool loop
  does fire on this box before the fallback bails).
- **Local frontend**: `npm run dev` on port 3100 (already verified).
- **Google quota** is permanent reality and irrelevant here — we route c01 at
  `openai/gpt-4o-mini` via fallback. What matters is the UI render, not which
  model answered.

## Test surfaces

### Surface A — curl schema re-check (sanity, 3 components, ~30s)

Already passed on staging @ 7f98e17 last session (77 envelopes, 9 assertions,
exit 0). Re-run locally pointed at `http://localhost:18000` to prove the same
envelope schema holds on this box. This is a **forward-compat contract** test —
it fails if the wire shape drifts.

Pass criteria (state predicates over final event list per run):
1. Every envelope has a `type` field.
2. `type` ∈ `{RUN_STARTED, RUN_FINISHED, RUN_ERROR, STEP_STARTED, STEP_FINISHED, TOOL_CALL_START, TOOL_CALL_END, TEXT_MESSAGE_CONTENT, CUSTOM}`.
3. `type` matches the `_KIND_TO_AGUI` mapping for each `kind`.
4. `step_name` present iff `type` ∈ `{STEP_STARTED, STEP_FINISHED}`.
5. `source` present iff `type == TEXT_MESSAGE_CONTENT`.
6. `name` present iff `type == CUSTOM`.
7. `cancelled` present iff `kind == run.cancelled`.
8. Legacy keys `{seq, ts, kind, summary, detail}` all present.
9. No unknown keys outside `{seq, ts, kind, summary, detail, type, step_name, source, name, cancelled}`.

Components:
- `c04 basic_3_scenes` (fast, deterministic)
- `c03 timing_passed_noop` (probes + deterministic)
- `c01 economics_basics` (long LLM + inner tools — may hit mid-stream fallback)

### Surface B — live UI regression (primary, c01, ~60-180s or early terminal)

**This is the test that was missing.** A real c01 run in the browser,
recorded end-to-end, with annotations at setup / test_start / assertion
points. Observation is wait-till-terminal; we assert over the event list
as hydrated into the UI at terminal.

Steps:
1. `record_start` (cursor visible), maximize Chrome.
2. Navigate to `http://localhost:3100/components/c01`.
3. Annotate `setup` — "Navigating to c01 economics_basics workbench".
4. Click Run. `record_annotate` `test_start` — "It should render live narration and inner tool events during a c01 scenario run".
5. Poll `/playground/runs/<id>` via DevTools console (or wait visually) until `closed == true`.
6. Snapshot the LiveStatusLine, RawEventLog (expanded), and terminal state.
7. `record_annotate` assertions per the predicates below.

**Pass criteria (state predicates over event list at `closed == true`):**

| # | Predicate | Evidence |
|---|---|---|
| B1 | Event list length ≥ 6 (any terminal run has at least dispatch + task + terminal) | RawEventLog summary "Raw event log (N)" with N ≥ 6 |
| B2 | At least one event has `kind == "narrate"` (proves narrator emitted during the run) | RawEventLog row with `kind = narrate` |
| B3 | At least one event has `kind == "tool.called"` AND at least one has `kind == "tool.returned"` — OR — terminal is `run.error` mid-stream (valid error path) | RawEventLog rows; or terminal banner red |
| B4 | Narration diversity: at least 3 distinct `narrate` summaries (proves novel-or-admit-repetition content, not just elapsed-counter churn) | Visible in RawEventLog summary column |
| B5 | LiveStatusLine text at terminal is ≠ `"undefined"`, ≠ `"[object Object]"`, ≠ empty — it's either the last narration or the terminal summary | Screenshot of status rail |
| B6 | LiveStatusLine `#<seq> · <kind>` badge renders the literal `kind` string (e.g. `task.start`, not `undefined`, not `STEP_STARTED`) | Right-side of status rail visible in screenshot |
| B7 | RawEventLog rows render `kind` verbatim in legacy form (`tool.called`, not `TOOL_CALL_START`) — proves UI still reads `kind`, not `type` | Screenshot of expanded event log |
| B8 | LiveStatusLine height is the fixed 44px rail — no layout jump when narration text length changes | Visible in recording (no vertical shunt of InterpretationCard) |
| B9 | Exactly one terminal event (`run.ok` or `run.error` or `run.cancelled`) in event list at `closed == true` | RawEventLog |
| B10 | If terminal is `run.ok`: InterpretationCard renders a paragraph + status chip. If `run.error`: RunResultPanel renders a red error banner with the error text. Either is a valid terminal render. | Screenshot of terminal state |

**Adversarial framing — what would break each predicate:**

- Envelope spread bug dropping `kind` → B6, B7 fail simultaneously (`kind` would render as `undefined`).
- Frontend consumer switched to reading `type` instead of `kind` → B7 fails (`TOOL_CALL_START` shown instead of `tool.called`).
- Narrator stopped emitting `narrate` events → B2, B4 fail.
- `Event.to_dict()` serialization dropped `summary` → B4, B5 fail (rows empty).
- SSE decoder breaking on extra keys → event count < curl-measured count → B1, B9 fail.
- Fixed-height rail regressed → B8 fails (visible layout shunt in recording).

### Surface C — c04 fast regression (secondary, ~2s)

Same browser, same recording, run c04 `basic_3_scenes` for a sub-second
terminal to prove the legacy fast-path UI is unchanged. Pass criteria:

- C1: `run.ok` lands, status rail goes green dot + terminal text.
- C2: InterpretationCard renders a paragraph with OK chip.
- C3: RawEventLog has 6 events with `kind` values in
  `{run.dispatched, probe.start/done, task.start/done, run.ok, interpret}`
  (exactly the pre-slice-1 shape).

## Non-goals

- Not asserting `type` values in the UI — the UI legitimately ignores `type`
  (it's additive-forward-compat). That's Surface A's job.
- Not asserting run duration. Wait till terminal, always.
- Not asserting Gemini path specifically. OpenAI fallback is fine.
- Not asserting c01 convergence to `run.ok`. A mid-stream fallback
  `run.error` is a valid terminal state that still exercises the UI error
  render path and still carries AG-UI envelopes on every intermediate event.

## Observation & reporting

- One recording (max Chrome, cursor visible) covering Surface B + Surface C.
- One PR comment on #346 with: recording link, state-predicate table with
  pass/fail per row, screenshot evidence for B5/B6/B7/B8, curl re-run
  summary (exit code + event counts).
- If any predicate fails: flag explicitly in comment with the observed vs
  expected value. No euphemisms.

## Three-criterion pass gate (unchanged from last session)

1. Curl harness exit 0 on all 3 components (Surface A).
2. All Surface B + Surface C state predicates pass at observation cutoff.
3. PR #346 CI stays green.

All three must hold or slice 1 does not merge.
