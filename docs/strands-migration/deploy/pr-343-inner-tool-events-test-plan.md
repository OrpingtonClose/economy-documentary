> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# PR #343 — Test Plan (inner tool events + stall rail fix)

**Target:** staging at http://142.171.48.138:29561 running commit `2eb360e` (or later).

## Testing contract

> Wait till completion, always. Show progress via periodic narrator thoughts during the wait.

Concretely:

- **Tests wait for a terminal event** (`run.ok` / `run.error` / `run.cancelled`) — no time cutoff from the test side. A run that never terminates is a **test failure**, attributed to the agent (not the harness).
- **Assertions are state predicates over the full event list** at terminal. Not time-indexed samples (`t+5s`, `t+20s`, …). What happened during the wait is the test evidence; when terminal lands is a property of the run, not the test.
- **During the wait, the narrator is the progress channel.** A run that is alive but quiet must produce fresh `narrate` events every ~1.5 s (from `narrator.py:_NARRATE_INTERVAL_SECONDS`). Silence during a live run is a separate, hard-gate failure.
- **One safety timeout** on the harness side (15 min) — only to distinguish "agent didn't terminate" from a hung test. Exceeding it fails the test with `run-did-not-terminate` as the explicit reason, not a pass.

## What changed in user-visible terms

1. **Narration during long c01 runs is no longer a frozen paraphrase.** The
   scenario agent's inner tool loop (`generate_scenario` →
   `evaluate_scenario` × N → `refine_scenario` × M → `create_timeline`) now
   emits `tool.called` / `tool.returned` events into the playground bus
   via a Strands `HookProvider`. The narrator's prompt is rewritten to
   surface every new fact and to explicitly admit repetition (`"no new
   signal, still on <step> — Ns"`) when it has nothing new.
2. **The stall rail no longer says `"stalled at interpret — Ns"` after a
   run succeeds.** `useRunStream.computeStall` suppresses the stall
   indicator the moment any `run.ok` / `run.error` / `run.cancelled`
   event lands, regardless of whether a later `interpret` event lands
   on top.

Source of truth:
- `server/strands_agents/playground/tool_event_hook.py` (new file; `PlaygroundToolEventEmitter`).
- `server/strands_agents/evals/experiments/scenario.py:426-440` (hook wiring).
- `server/strands_agents/playground/narrator.py:78-222` (`_RICH_DETAIL_KEYS`, `_context_header`, `_tail_to_prompt`, `_NARRATE_SYSTEM`).
- `frontend-playground/src/lib/useRunStream.ts:246-268` (`computeStall`).
- `frontend-playground/src/app/components/[id]/ComponentWorkbench.tsx:417-479` (`LiveStatusLine`).

## Out of scope

- Component grid, case browser, save-as-case, evaluator flow regressions. Unchanged.
- Typewriter animation + fixed-height rail — covered by PR #344's report.

## Primary flow

Run c01 / `economics_basics` against staging. Wait for a terminal event. Assert over the full event list.

### Step 1 — Kick the run

```
POST http://142.171.48.138:29561/playground/components/c01/runs
Content-Type: application/json

{"case_name": "economics_basics"}
```

Response body carries `run_id`, `events_url`, `state_url`. Capture `run_id`.

**Pass criteria:** HTTP 200, body has `run_id` (non-empty string), `state_url == /playground/runs/<run_id>`.

### Step 2 — Wait till terminal

Poll `GET /playground/runs/<run_id>` every 1 s until the snapshot has `closed == true` **or** the harness safety timeout (15 min) trips.

Between polls, the browser (or `curl -N /events`) must show the narrator emitting — at least one fresh `narrate` event every ≤ 3 s. This is the progress channel the UX contract requires.

**Pass criteria:**
- `closed == true` inside 15 min.
- `terminal.status` is present and is one of `OK`, `TASK_ERROR`, `MODEL_UNREACHABLE`, `CANCELLED`.
- The final event is one of `run.ok`, `run.error`, `run.cancelled`, possibly followed by exactly one `interpret`.

**Failure mode** — harness timeout without terminal. Fail the test with literal reason `run-did-not-terminate-within-15min`. Do NOT score this as a PR #343 regression: PR #343's claim is about what the UI does *during and after* a terminal, not about causing termination. Non-convergence is a c01 agent issue, tracked separately.

### Step 3 — Narration diversity across the full wait

Collect every `narrate` event in the snapshot. Let `N = len(narrate_events)`.

**Pass criteria (all must hold):**
- `N ≥ (wait_duration_seconds / 3.0)` — at least one narrate per 3 s. Silence breaks the progress-channel contract.
- `distinct(narrate.summary) ≥ 3` — at least three byte-distinct narration sentences across the wait. A narrator repeating the same sentence for the whole run fails this.
- `count(narrate.summary.contains("no new signal, still on")) ≥ 1` — at least one honest-repetition line. The narrator must admit repetition, not paraphrase emptily.
- `count(narrate.summary matches /(generate_scenario|evaluate_scenario|refine_scenario|create_timeline|tool\.called)/) ≥ 1` — at least one narration names a scenario tool OR cites `tool.called` directly. Proves the narrator saw the inner-loop events.
- `count(narrate.summary matches /(step=|elapsed_ms=|rating=|num_scenes=|num_issues=|returning|completed in)/) ≥ 1` — at least one narration surfaces a concrete detail from `_RICH_DETAIL_KEYS`.

**Broken-state discrimination:** Pre-fix, every `narrate.summary` was a paraphrase of `task.start` with only `model_id` and `elapsed` to work with. `distinct` would be ≤ 1 and none of the tool-name / detail-key predicates would match. Post-fix, all predicates hold within ~30 s of real scenario-agent work.

### Step 4 — Inner-loop tool trajectory

Collect every `tool.called` and `tool.returned` in the snapshot.

**Pass criteria:**
- `count(tool.called) ≥ 1` with `detail.tool` in the scenario-tool set.
- `count(tool.returned) ≥ 1` with `detail.elapsed_ms > 0`.
- For each `tool.returned`, there exists an earlier `tool.called` with the same `detail.tool_use_id`. (No orphan returns.)
- `tool.called` seq numbers advance monotonically (`detail.step` is `1, 2, 3, …` in order of emission).

**Broken-state discrimination:** Pre-fix, the event buffer had zero `tool.*` events.

### Step 5 — Stall-rail suppression after terminal (UI predicate)

After the snapshot carries a terminal event, render the status rail from the event list and assert over its class state. This is a pure predicate over the event list (no wall-clock wait needed): the rail's stall state is a deterministic function of the event list.

**Pass criteria:**
- For every suffix of the event list starting at the first terminal event, `computeStall(events, tick) == null`. (The `TERMINAL_KINDS` guard at `useRunStream.ts:255`.)
- Therefore: the rendered `wrapperClass` must be `border-pg-border bg-pg-surface text-pg-muted` (muted-grey, from `LiveStatusLine.wrapperClass:438-439`). It must NOT contain `border-pg-stall`.
- The rail text must not contain the substring `stalled at` at any point after the first terminal event.

**Broken-state discrimination:** Pre-fix, the rail text would read `"stalled at interpret — Ns"` (amber) because the `interpret` event after `run.ok` flipped the stall budget check. Post-fix, any terminal event anywhere in the event list suppresses the rail unconditionally.

### Step 6 — Post-run interpretation

**Pass criteria:**
- Exactly one `interpret` event in the terminal snapshot.
- `interpret.ts > run.*-terminal.ts` (interpreter runs strictly after terminal).
- `interpret.summary` non-empty.

## Programmatic harness

The above is implementable without a browser. See `scripts/pr343_wait_till_terminal.py` (added in this PR): POSTs the run, polls `/runs/<id>` every 1 s, bails if no narrate in the last 3 s, asserts all predicates above at terminal, prints pass/fail per predicate. Run against staging with:

```
python scripts/pr343_wait_till_terminal.py \
    --base-url http://142.171.48.138:29561 \
    --component c01 \
    --case economics_basics
```

Exits 0 on full pass, 1 on any predicate fail, 2 on harness timeout.

## Browser recording (optional, UX proof)

Primary evidence is the programmatic harness. For UX proof (novel narration is actually readable on the live rail, typewriter feels smooth, layout stable), additionally record the browser run:
- `setup`: "Navigating to c01 workbench on staging."
- `test_start`: "It should emit distinct narration throughout the wait and terminate."
- `assertion` (at terminal): quote the exact `distinct(narrate) = N` count and the three representative lines.
- `test_start`: "It should suppress the stall rail after terminal."
- `assertion` (15 s after terminal): quote the rail class + text verbatim.

## Failure reporting

Any predicate fail:
- Quote the exact failing narrate line or missing event.
- Dump `curl -s /playground/runs/<run_id>` for the full snapshot.
- Do not paraphrase.

Harness timeout (`run-did-not-terminate-within-15min`):
- This is NOT a PR #343 failure — it is a c01 convergence bug (separate issue).
- File under c01, not #343.
- The per-wait narrator-diversity predicates (Step 3) are still evaluable against the partial event list and must be reported alongside.
