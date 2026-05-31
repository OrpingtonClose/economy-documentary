> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Slice 1 AG-UI Wire Format — Adversarial Test Plan v2

**PR:** https://github.com/OrpingtonClose/economy-documentary/pull/346
**Staging:** backend http://142.171.48.138:29561 · frontend http://142.171.48.138:29796
**Deployed commit:** 7f98e17 on branch `devin/1776922069-fix-scenario-live-run`
**CI:** 2/2 green, 0 PR comments

## What changed (user-visible)

Every serialised playground event envelope now carries an AG-UI `type`
field alongside the legacy `kind`. This is purely additive — zero UI
consumer changes, zero emitter changes. The frontend `RunEvent` type
widens to accept optional `type`, `step_name`, `source`, `name`,
`cancelled` fields. Runtime behaviour is identical.

## What could break (adversarial lens)

1. `type` missing on some envelopes (mapping function not called)
2. `type` wrong for a given `kind` (table error or fallback hit unexpectedly)
3. Extras leak across types (`step_name` on TOOL_CALL_START, `source` on STEP_STARTED)
4. Legacy fields dropped (envelope spread overwrites `kind`/`summary`/`detail`)
5. Unknown keys appear (agui_envelope returns unexpected fields)
6. `cancelled` flag leaks onto non-cancelled `run.ok` events
7. UI regression — frontend breaks because new keys in envelope confuse a consumer

## Testing approach

**Two complementary surfaces:**
- **Curl (programmatic):** Fire real component runs via the API, collect terminal snapshots, validate every envelope against the mapping table. This catches failures 1–6 with machine precision.
- **Browser (visual):** Run components through the UI, record the session, verify the UI renders identically to pre-slice-1. This catches failure 7.

Both are required because curl alone can't prove UI regression, and browser alone can't prove per-field schema correctness.

---

## Test 1: Curl — Full AG-UI schema validation across 3 component types

**Goal:** Exercise all 9 AG-UI types on the live wire and validate every envelope field.

**Steps:**

```bash
# Already verified working in prior session — rerun for fresh evidence
python3 scripts/slice1_agui_wire_format_test.py \
  --scenarios c04,c03,c01 \
  --base-url http://142.171.48.138:29561
```

This fires three runs covering all AG-UI types:

| Run | Component | Expected AG-UI types | Why this run |
|-----|-----------|---------------------|--------------|
| c04 `basic_3_scenes` | Audio Agent (deterministic, no probes) | RUN_STARTED, STEP_STARTED(task), STEP_FINISHED(task), RUN_FINISHED, TEXT_MESSAGE_CONTENT(interpreter) | Smallest envelope set — baseline |
| c03 `timing_passed_noop` | Scenario Refiner (probes + deterministic) | Same as c04 + STEP_STARTED(probe), STEP_FINISHED(probe) | Proves `step_name="probe"` vs `step_name="task"` don't bleed |
| c01 `economics_basics` | Scenario (long LLM + inner tools) | Same as c03 + TOOL_CALL_START, TOOL_CALL_END, TEXT_MESSAGE_CONTENT(narrator), RUN_ERROR | Covers tool events + narrator source attribution + error path |

**Pass/fail criteria (per envelope, machine-checked by harness):**

1. **`type` present:** `"type" in envelope` — FAIL if missing on any envelope
2. **`type` correct:** `envelope["type"] == MAPPING[envelope["kind"]]` — FAIL if mismatch
3. **`step_name` only on STEP_*:** `"step_name" in envelope` iff `type ∈ {STEP_STARTED, STEP_FINISHED}` — FAIL if `step_name` appears on TOOL_CALL_START or TEXT_MESSAGE_CONTENT
4. **`source` only on TEXT_MESSAGE_CONTENT:** `"source" in envelope` iff `type == TEXT_MESSAGE_CONTENT` — FAIL if `source` appears on STEP_STARTED or RUN_FINISHED
5. **`name` only on CUSTOM:** `"name" in envelope` iff `type == CUSTOM` — FAIL if `name` appears elsewhere
6. **`cancelled` only on cancelled runs:** `"cancelled" in envelope` iff `kind == "run.cancelled"` — FAIL if `cancelled` leaks onto `run.ok`
7. **Legacy fields intact:** `{seq, ts, kind, summary, detail} ⊂ envelope.keys()` — FAIL if any missing
8. **No unknown keys:** `envelope.keys() ⊆ {seq, ts, kind, summary, detail, type, step_name, source, name, cancelled}` — FAIL if extra keys
9. **`type` ∈ AGUI_TYPES:** value is one of the 9 known AG-UI types — FAIL if unknown type string

**Expected harness exit code:** 0 (all pass)

**What makes this adversarial:** The harness embeds its own copy of the mapping table (mirroring `agui.py`). If a deploy/code mismatch exists, the test catches it immediately. Each assertion is designed to fail differently for different bugs — assertion 3 catches extras bleed, assertion 6 catches cancelled flag leak, assertion 8 catches unknown keys.

---

## Test 2: Browser — UI regression (visual proof)

**Goal:** Prove the UI renders identically to pre-slice-1. Since the frontend reads `kind` (not `type`), any visual change would indicate the envelope spread broke something.

**Steps:**

1. Launch Chrome, navigate to `http://142.171.48.138:29796/components`
2. Start screen recording
3. **Annotate:** setup — "Staging at 142.171.48.138, slice 1 (commit 7f98e17) deployed"
4. Click into **c04** (Audio Agent) card
5. Select `basic_3_scenes` case
6. **Annotate:** test_start — "It should render c04 fast run identically to pre-slice-1"
7. Click **Run**
8. Wait for terminal (< 2s expected)
9. **Assert:** Status rail shows green chip with "OK" or similar terminal indicator
10. **Assert:** Interpretation card below output pane contains a paragraph (not empty, not "run a case to see...")
11. **Assert:** Raw event log shows events in order: run.dispatched → probe.start → task.start → task.done → run.ok → interpret
12. **Annotate:** assertion — pass/fail
13. Navigate back to `/components`, click into **c01** (Scenario)
14. Select `economics_basics` case
15. **Annotate:** test_start — "It should show inner-tool events + narration during c01 long run"
16. Click **Run**
17. Wait ~15-30s (or until run terminates — c01 may hit Gemini rate limit and error)
18. **Assert:** Status rail updates with live narration text (not frozen at "Click Run...")
19. **Assert:** Raw event log shows `tool.called` and `tool.returned` entries (inner Strands tools)
20. **Assert:** If run errors, error is displayed clearly (not silent failure)
21. **Annotate:** assertion — pass/fail
22. Stop recording

**Pass/fail criteria:**
- **c04 fast run:** Terminal in <5s, green status, interpretation card populated, event log complete — PASS
- **c01 long run:** Live narration visible, inner-tool events in log, error/success displayed clearly — PASS
- **Any visual breakage** (layout jumps, missing elements, JS errors in console) — FAIL

**What makes this adversarial:** If the `**agui_envelope(self.kind)` spread in `Event.to_dict()` accidentally overwrote `kind` or `summary` (e.g. if `agui_envelope` returned a dict with a `kind` key), the UI would break visibly because the narrator prompt and stall budget table both key on `kind`. The browser test catches this class of bug that curl schema validation alone cannot.

---

## Out of scope

- Langfuse wiring (slice 2)
- VRAM pre-flight probe (slice 3)
- Narrator prompt quality (covered by PR #343)
- c01 scenario-agent convergence (separate issue)
- CUSTOM type on-wire (no natural trigger; covered by unit test `test_unknown_kind_falls_back_to_custom`)

## Pass gate

All three criteria must hold:
1. Curl harness exit code 0 (all 9 assertions pass on all 3 runs)
2. Browser recording shows no visual regression
3. CI stays green (2/2)

If any single criterion fails, slice 1 does NOT merge.
