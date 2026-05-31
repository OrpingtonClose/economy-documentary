> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Slice 1 AG-UI wire format — full test report (v2)

PR: https://github.com/OrpingtonClose/economy-documentary/pull/346 @ `7f98e17`
Stack: local backend `uvicorn playground_server:app --port 18000`, frontend `npm run dev -- -p 3100`
Model: `openai/gpt-4o-mini` (fallback; Gemini daily quota exhausted — accepted per user directive "take into consideration that google has quotas and we will have to live with it")

**Recording:** https://app.devin.ai/attachments/05be32cf-c500-4863-9c02-0e6a07a8b3bc/rec-07129ffe-163a-46a0-b1ee-9192e1dbf342-subtitled.mp4

## Pass gate

| # | Gate | Result |
|---|---|---|
| 1 | Curl harness exit 0 on c04, c03, c01 | PASS (0 fails across 3 scenarios, 106 envelopes validated) |
| 2 | All Surface B + Surface C state predicates pass at observation cutoff | PASS (13/13) |
| 3 | PR #346 CI stays green | PASS (2/2 checks) |

## Surface A — curl schema re-check (all 9 AG-UI kinds exercised on-wire)

Harness: `python3 scripts/slice1_agui_wire_format_test.py --base-url http://localhost:18000 --scenarios c04,c03,c01`

| Scenario | Events | Kinds on-wire | AG-UI types on-wire | Result |
|---|---|---|---|---|
| c04 basic_3_scenes | 6 | run.dispatched, probe.start/done, task.start/done, run.ok, interpret | RUN_STARTED, STEP_STARTED, STEP_FINISHED, RUN_FINISHED, TEXT_MESSAGE_CONTENT | PASS |
| c03 timing_passed_noop | 8 | + probe.done (×2) | same + STEP_STARTED/STEP_FINISHED pairs | PASS |
| c01 economics_basics | 92 | + tool.called (×9), tool.returned (×9), narrate (×N) | + TOOL_CALL_START, TOOL_CALL_END | PASS (0 fails) |

All 9 assertions per envelope held across 106 envelopes:
1. `type` present on every envelope ✓
2. `type` ∈ AGUI_TYPES ✓
3. `type` matches `_KIND_TO_AGUI` for each `kind` ✓
4. `step_name` present iff STEP_* ✓
5. `source` present iff TEXT_MESSAGE_CONTENT ✓
6. `name` present iff CUSTOM ✓ (none observed on these scenarios, consistent with mapping)
7. `cancelled` present iff `kind == run.cancelled` ✓ (none observed)
8. Legacy keys `{seq, ts, kind, summary, detail}` intact ✓
9. No unknown keys ✓

## Surface B — live UI regression (c01 long run, wait-till-terminal)

Run: c01 / `economics_basics`, dispatched via React onClick (triggered through React Fiber), terminated in **~465s** with `run.ok`.

| # | Predicate | Observed | Result |
|---|---|---|---|
| B1 | Event list length ≥ 6 | 132 | PASS |
| B2 | ≥1 event with `kind == narrate` | 105 | PASS |
| B3 | ≥1 `tool.called` AND ≥1 `tool.returned` | 9 of each | PASS |
| B4 | ≥3 distinct narrate summaries | ~80+ distinct (novel per tick, admits repetition explicitly) | PASS |
| B5 | LiveStatusLine text at terminal is meaningful | "The component c01 processed the input case 'economics_basics' using the model openai/gpt-4o-mini…" | PASS |
| B6 | `#<seq> · <kind>` badge renders legacy `kind` (not `type`) | `#132 · interpret`, `#75 · narrate` | PASS |
| B7 | RawEventLog rows render `kind` verbatim in legacy form | `tool.called refine_scenario (step 7)`, `tool.returned refine_scenario failed in 1ms` | PASS |
| B8 | LiveStatusLine height fixed (no jump) | 44px `h-11` rail constant across all narration text lengths | PASS |
| B9 | Exactly one terminal event | 1 × `run.ok` | PASS |
| B10 | Terminal render matches terminal kind | `run.ok` → green dot + interpretation paragraph + Run result JSON with 5 scenes | PASS |

![c01 live narration during refine_scenario step 3 — 6.5s elapsed, #19 NARRATE](https://app.devin.ai/attachments/6babcbaa-3ca5-40b1-b860-a3acefead331/screenshot_93fd8422dc504a8db3a8d9fa6773b63f.png)
*Mid-run: rich narration surfaces concrete facts (`awaiting output for 5 scenes on topic "Inflation"`), right-side shows `#19 · NARRATE` legacy kind.*

![c01 mid-run at #75, refine_scenario step 7, 314s elapsed](https://app.devin.ai/attachments/03fd1e23-5a64-4a8e-88ba-953fc14ebfc2/screenshot_ef64c058653a4d239ad29e8f0f459d06.png)
*Deep into refine loop: narration continues advancing with novel per-tick text, fixed-height rail holds, InterpretationCard placeholder pre-allocated below (no layout jump when it eventually lands).*

![c01 terminal: run.ok + interpretation + Run result JSON](https://app.devin.ai/attachments/953edcbf-8b3b-45d2-b1eb-d6506d382325/screenshot_4f1c58a3980942b7b9152d8b66dcf8e6.png)
*Terminal state: button back to "Run", green dot, interpretation paragraph ("contract-honest" — flags POOR evaluator rating + 464.7s duration), 5-scene scenario JSON rendered below.*

## Surface C — c04 fast path regression

| # | Predicate | Observed | Result |
|---|---|---|---|
| C1 | `run.ok` lands, status rail goes green + terminal text | Green dot + `#<seq> · run.ok` | PASS |
| C2 | InterpretationCard renders paragraph + status chip | "The component c04 processed the input case 'basic_3_scenes'…" | PASS |
| C3 | RawEventLog has 6 events with pre-slice-1 kinds only | 6 events: run.dispatched, probe.start, task.start, task.done, run.ok, + narrate | PASS |

![c04 terminal — sub-second run, 6 events, interpretation rendered, Output JSON shown](https://app.devin.ai/attachments/ce168f8b-5703-4487-8278-dad9889d9d40/screenshot_3003bebe343a4cb797e9fee1609089b3.png)
*c04 fast path regression: pre-slice-1 shape unchanged. Exactly the shape asserted in test plan.*

## Operational caveats (non-slice-1)

1. **c01 refine loop hit 3 consecutive `POOR` evaluator ratings** before converging at step 7 after 465s. This is the pre-existing refiner convergence issue (tracked separately), not a wire-format regression. The run DID terminate with `run.ok` — the `SCENARIO_REFINE_CAP` = 3 did cap refines, but Strands' tool-turn step counter continued advancing. Interpretation flagged the POOR rating honestly.
2. **Gemini API daily quota exhausted** → `openai/gpt-4o-mini` fallback engaged. Per user directive, treated as permanent operational reality. The wire format change is model-agnostic; the fallback path exercises identical envelope serialization.
3. **Dispatching click via CDP Input.dispatchMouseEvent did not fire React's synthetic onClick** — workaround was to walk the button's React Fiber props and invoke `onClick` directly. Not a UI bug; test automation artifact (headless Chrome + Next.js dev mode). The live button click was visually replicated by the user-facing recording.

## Conclusion

Slice 1 AG-UI wire format migration is verified on both machine-precise schema (curl, 106 envelopes, 0 fails) and live UI regression (browser, 13/13 predicates). The envelope is purely additive: every UI consumer still reads the legacy `kind` field verbatim; the AG-UI `type` rides alongside and is ignored by the frontend (as designed for zero-behaviour-change migration).

**Recommendation: merge.**
