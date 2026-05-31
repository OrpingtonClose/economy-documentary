> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Slice 1 — AG-UI wire format: test plan

**Target:** staging at http://142.171.48.138:29561 running commit
`7f98e17` (PR #346 head, branch `devin/1776960759-agui-wire-format`).

**PR:** https://github.com/OrpingtonClose/economy-documentary/pull/346

## What slice 1 claims, in one line

Every serialised playground event now carries two discriminators:
`kind` (legacy, untouched, load-bearing for every existing consumer)
and `type` (AG-UI, plus any AG-UI-specific fixed fields:
`step_name`, `source`, `name`, `cancelled`). Source of truth is
<ref_snippet file="/home/ubuntu/repos/economy-documentary/server/strands_agents/playground/agui.py" lines="61-77" />.
No emitter callsite changed. No UI consumer changed. Runtime
behaviour identical.

The test is the adversarial converse: **every envelope on the live
wire must carry `type`, the value must come from the mapping table,
extras must appear only where the table dictates, and the UI must
render identically**. Anything looser and the PR's zero-risk claim
is false.

## Testing contract (unchanged from PR #343)

> Wait till completion, always. Show progress via periodic narrator thoughts.

- Tests wait for a terminal event (`run.ok` / `run.error` /
  `run.cancelled`) — no time cutoff from the test side.
- Assertions are state predicates over the full event list at terminal.
- One safety timeout on the harness side (15 min).

## Deployment

Slice 1 is schema-only. Deploy is idempotent:

```bash
ssh -p 17600 root@ssh8.vast.ai \
    'export PLAYGROUND_GIT_BRANCH=devin/1776960759-agui-wire-format &&
     cd /workspace/economy-documentary &&
     git fetch --depth=1 origin "$PLAYGROUND_GIT_BRANCH" &&
     git reset --hard "origin/$PLAYGROUND_GIT_BRANCH" &&
     bash scripts/playground_staging_bootstrap.sh'
```

Pass gate: `curl -fsS http://142.171.48.138:29561/health` returns
`{"status":"ok"}` and `curl -fsS http://142.171.48.138:29561/playground/components`
lists 15 components.

Post-deploy sanity — fire one event and confirm `type` is on the wire
before running the full harness:

```bash
RUN_ID=$(curl -sS -X POST \
    http://142.171.48.138:29561/playground/components/c04/runs \
    -H 'content-type: application/json' \
    -d '{"case_name":"basic_3_scenes"}' \
    | jq -r '.run_id')
sleep 2
curl -fsS http://142.171.48.138:29561/playground/runs/$RUN_ID \
    | jq '.events[0] | {kind, type}'
# expected: {"kind": "run.dispatched", "type": "RUN_STARTED"}
```

If that response doesn't carry `type`, the deploy didn't take — stop
and redeploy before running anything else.

## Primary test surfaces

Five curl scenarios + one schema invariant + one browser regression.
Each is designed to **fail differently** if slice 1 is broken —
writing a single test that "passes if `type` is there" leaves too
much space for silent regressions.

### Curl 1 — c04 `basic_3_scenes` (deterministic, no probes, fast terminal)

**Why:** c04 has empty `declared_models` (deterministic tool). The
reachability gate is a no-op. Covers the smallest possible event
set that still exercises start/dispatch/terminal — useful as a
baseline and fast enough (<2s) to run many times.

**Expected event kinds (in order):**
1. `run.dispatched`
2. `task.start`
3. `task.done`
4. `run.ok`
5. `interpret` (optional; narrator LLM call)

**AG-UI assertions:**
- `run.dispatched` → `type == "RUN_STARTED"`. No extras.
- `task.start` → `type == "STEP_STARTED"`, `step_name == "task"`.
- `task.done` → `type == "STEP_FINISHED"`, `step_name == "task"`.
- `run.ok` → `type == "RUN_FINISHED"`. No `cancelled` field.
- `interpret` (if present) → `type == "TEXT_MESSAGE_CONTENT"`,
  `source == "interpreter"`.

**Fails if:** any envelope missing `type`; `step_name` missing on
either `task.*`; `source` missing on `interpret`; `cancelled` leaks
onto `run.ok`.

### Curl 2 — c03 `timing_passed_noop` (probes + deterministic refiner no-op)

**Why:** c03 declares gemini + openai. Reachability probe fires and
emits `probe.start` + `probe.done` events. Task body is a no-op
(refiner returns input unchanged), so the run terminates fast even
though there's LLM work in the probe. Exercises STEP_STARTED /
STEP_FINISHED with `step_name == "probe"` — distinct from the
`step_name == "task"` case.

**Expected event kinds (in order):**
1. `run.dispatched`
2. `probe.start`
3. `probe.done` × 2 (one per declared model)
4. `task.start`
5. possibly `tool.called` / `tool.returned` (if refiner logs)
6. `task.done`
7. `run.ok`
8. `interpret`

**AG-UI assertions (on top of Curl 1):**
- Every `probe.start` → `type == "STEP_STARTED"`, `step_name == "probe"`.
- Every `probe.done` → `type == "STEP_FINISHED"`, `step_name == "probe"`.
- No `probe.*` event carries `step_name == "task"` — proves the two
  step domains don't bleed.

**Fails if:** `probe.done`'s `step_name` accidentally inherits from
the prior `task.start` lookup; AG-UI table emits the same step_name
for two different step domains (regression of the table itself).

### Curl 3 — c01 `economics_basics` (long run with inner tool loop + narration)

**Why:** the *only* scenarios that emit `tool.called` / `tool.returned`
and a steady stream of `narrate` events are c01 / c03 / c05 style
long scenario-agent loops. This is where slice 1's claim about
`TOOL_CALL_START` / `TOOL_CALL_END` / `TEXT_MESSAGE_CONTENT` is
actually exercised on the wire.

Expected wall-clock: 30 s (happy, rating=GOOD first try) to 160 s
(3 refine iterations before hard cap). `SCENARIO_REFINE_CAP=3`
guarantees terminal within ~160 s.

**Expected event kinds at terminal (no fixed order, but all present):**
- `run.dispatched` × 1, `run.ok` × 1 (or `run.error` × 1)
- `probe.start` × 1, `probe.done` × N (N = len declared_models)
- `task.start` × 1, `task.done` × 1 (or neither if `run.error` hit first)
- `tool.called` ≥ 1 — at least one scenario tool fires
- `tool.returned` ≥ 1 — paired with a `tool.called`
- `narrate` ≥ 1 — narrator loop fires at least once
- `interpret` × 0 or 1

**AG-UI assertions:**
- Every `tool.called` → `type == "TOOL_CALL_START"`. No `step_name`,
  no `source`, no `name` extra (mapping table line 67 maps to
  bare `(TOOL_CALL_START, {})`).
- Every `tool.returned` → `type == "TOOL_CALL_END"`. Same — no extras.
- Every `narrate` → `type == "TEXT_MESSAGE_CONTENT"`,
  `source == "narrator"`.
- At least one `narrate.summary` matches `/no new signal, still on/`
  — proves the inner tool loop is still honest about repetition
  (PR #343 contract).
- At least one `narrate.summary` names a scenario tool
  (`generate_scenario` | `evaluate_scenario` | `refine_scenario` |
  `create_timeline`) — proves the narrator saw the inner-loop events
  and AG-UI translation didn't drop any inner-tool detail.

**Fails if:** any `tool.called` surfaces with `source` or `step_name`
(mapping bleed); `narrate` surfaces with `source == "interpreter"`
(namespace collision with `interpret`); `narrate` surfaces without
`source` at all.

### Curl 4 — RUN_ERROR on-wire (c01 with scrubbed credentials)

**Why:** `run.error → RUN_ERROR` is covered by unit tests but not
naturally reachable on a healthy staging VM — every component with
declared models also declares the healthy openai fallback, so the
`all-declared-models-unreachable` branch (`playground.py:918`)
never fires. To get RUN_ERROR on the wire we temporarily scrub
the credentials; the run then terminates in the reachability
gate without entering the task adapter, which is exactly the
MODEL_UNREACHABLE branch slice 1 must map to RUN_ERROR.

**Pre-flight:**

```bash
ssh -p 17600 root@ssh8.vast.ai \
    'cp /workspace/economy-documentary/server/.env \
        /workspace/economy-documentary/server/.env.preflight-backup'
ssh -p 17600 root@ssh8.vast.ai \
    'sed -i -E "s/^(GOOGLE_API_KEY|OPENAI_API_KEY|MOONSHOT_API_KEY|ANTHROPIC_API_KEY|KIMI_API_KEY)=.+$/\1=/" \
        /workspace/economy-documentary/server/.env'
ssh -p 17600 root@ssh8.vast.ai 'supervisorctl restart playground:playground-backend'
```

**Run:**

```bash
curl -sS -X POST \
    http://142.171.48.138:29561/playground/components/c01/runs \
    -H 'content-type: application/json' \
    -d '{"case_name":"economics_basics"}' | jq -r '.run_id'
# poll /runs/$RUN_ID until closed
```

**Post-flight (MUST run regardless of outcome):**

```bash
ssh -p 17600 root@ssh8.vast.ai \
    'mv /workspace/economy-documentary/server/.env.preflight-backup \
        /workspace/economy-documentary/server/.env'
ssh -p 17600 root@ssh8.vast.ai 'supervisorctl restart playground:playground-backend'
curl -fsS http://142.171.48.138:29561/playground/components/c01/models/health \
    | jq '.probes[] | select(.reachable == true) | .model_id' | head
# expected: at least one model reachable again.
```

**Expected event kinds:** `run.dispatched`, `probe.start`,
`probe.done` × 3 (all with `reachable == false`), `run.error`.

**AG-UI assertions:**
- `run.error` → `type == "RUN_ERROR"`.
- No `run.ok` or `run.cancelled` events present.
- `run.error.detail.status == "MODEL_UNREACHABLE"`.

**Fails if:** `run.error` maps to `RUN_FINISHED` (wrong mapping);
`run.error` carries `cancelled: true` (wrong mapping line copied
from `run.cancelled`); no `run.error` event fires (MODEL_UNREACHABLE
branch regression, out of slice 1 scope but would invalidate the
test).

### Curl 5 — CUSTOM fallback (unknown kind)

**Why:** <ref_snippet file="/home/ubuntu/repos/economy-documentary/server/strands_agents/playground/agui.py" lines="100-144" />
guarantees any unknown `kind` falls back to `CUSTOM` with `name` set.
On a healthy staging run, `task.pick_model` *may* or may not fire
(depends on the task adapter). This scenario confirms the fallback
works by emitting a synthetic unknown kind via a one-shot stream
write on staging — NOT by code-changing, but by hitting a debug
endpoint if one exists.

**Decision:** If no debug endpoint exists to inject a synthetic event,
this coverage is dropped to unit-test-only (already covered by
`test_unknown_kind_falls_back_to_custom` in
`test_playground_agui.py`). Unit test is exhaustive for the mapping
table; on-wire coverage here is a nice-to-have, not a blocker.

**Action:** check for an injection endpoint; if none, note as
"covered by unit test" in report.

### Schema 1 — terminal snapshot shape invariant

Applies to every curl run above. At terminal, walk every event in
`snapshot.events` and assert:

- `"kind" in event` (legacy, untouched).
- `"type" in event` (AG-UI, new).
- `event["type"] in AGUI_TYPES` (one of the 9 known types).
- If `kind in KNOWN_KINDS`, then `event["type"]` matches the mapping
  table (loaded from the deployed
  `server/strands_agents/playground/agui.py` via SSH `cat`, not
  hardcoded in the test — so a table edit on the deployed version
  automatically loosens the test).
- Extras rule: `step_name` only on `STEP_*` types; `source` only on
  `TEXT_MESSAGE_CONTENT`; `name` only on `CUSTOM`; `cancelled` only
  on `RUN_FINISHED` events whose `kind == "run.cancelled"`.
- `seq`, `ts`, `summary`, `detail` all still present (legacy consumer
  contract unchanged).
- No envelope has unknown top-level keys beyond
  `{seq, ts, kind, summary, detail, type, step_name, source, name, cancelled}`.

**Fails if:** any envelope is missing `type`; extras bleed across
types; legacy fields get dropped.

### Browser 1 — regression (UI renders identically)

**Why:** slice 1 claims zero UI behaviour change. The browser test
proves it — if anything in the UI renders differently, the claim is
false, even though unit tests pass.

**Recording required.** Open
http://142.171.48.138:29561/components, run c04
`basic_3_scenes` (fast), observe the live status rail +
interpretation card render. Run c01 `economics_basics` (long),
observe the inner-tool events surface in the raw log, the narration
keep ticking, and the stall rail suppress itself after `run.ok`.

**Assertions via annotation:**
- `setup`: Navigating to /components on staging, slice 1 commit
  7f98e17 deployed.
- `test_start`: "It should render the c04 run identically to
  pre-slice-1 (same status rail, same interpretation card)".
- `assertion`: "c04 run terminal in <2s, status rail chip green,
  interpretation card visible".
- `test_start`: "It should show inner-tool events + narration during
  a long c01 run, with stall rail suppressed after terminal
  (PR #343 regression)".
- `assertion`: "c01 run: tool.called/returned visible in raw log,
  narrator emitting fresh lines, rail stays green 15s after run.ok".

**Fails if:** any visible difference from the PR #343 recording
(https://app.devin.ai/attachments/58b5a2ca-6978-4c54-b18b-93b5220f6256/rec-43fc96a5-9941-4e0c-b6e2-0f785a8b0f84-edited.mp4).

## Harness

A single Python script drives all five curl scenarios + schema
invariant and exits non-zero on any predicate failure. Layout
mirrors `scripts/pr343_wait_till_terminal.py` so the pattern stays
consistent:

```
scripts/slice1_agui_wire_format_test.py
  --base-url http://142.171.48.138:29561
  --scenarios c04,c03,c01,c01_error
  --expected-types-file server/strands_agents/playground/agui.py
  --out-dir docs/strands-migration/deploy/slice-1-agui-wire-format-test-results/
```

Per-scenario output: terminal snapshot JSON + per-envelope
pass/fail table. Aggregate output: summary markdown that the PR
comment can link to.

## Out of scope

- Langfuse wiring (slice 2).
- VRAM pre-flight probe (slice 3).
- Qwen3-TTS / LTX / B2 (slices 4–6).
- Pipeline-as-component (slice 7+).
- c01 scenario-agent convergence quality (unchanged by slice 1).
- Narrator prompt quality (covered by PR #343 report).

## Pass gate for slice 1

All five curl scenarios + schema invariant + browser regression pass.
Every AG-UI type observed on-wire matches the mapping table.
Unit tests remain 19/19. CI stays green. No UI regression visible
in the recording.

If any single assertion fails, slice 1 does NOT merge — the PR goes
back for revision.
