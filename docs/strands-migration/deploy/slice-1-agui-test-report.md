> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Slice 1 AG-UI Wire Format — Test Report

**PR:** https://github.com/OrpingtonClose/economy-documentary/pull/346
**Deployed commit:** 7f98e17 on branch `devin/1776922069-fix-scenario-live-run`
**Staging:** backend http://142.171.48.138:29561 · frontend http://142.171.48.138:29796
**Recording:** https://app.devin.ai/attachments/5155f0c2-394c-4282-b859-abc653ba4e6d/rec-086b2d65-ba91-4a03-9eea-d6f353bbbeea-subtitled.mp4

## One-line summary

Tested PR #346 AG-UI wire format migration via curl harness against 3 component types
(77 events, 0 schema failures) + browser regression (c04 success path + c01 error path
on staging frontend). All pass criteria satisfied; no UI regression from envelope additions.

## Escalations

**None.** All criteria pass. One observation worth noting (not a slice 1 regression): c01
`economics_basics` on staging currently errors at `MidStreamFallbackError` because the
Gemini daily quota is exhausted. This prevented the browser test from observing the
scenario agent's inner-tool loop in the UI, but the curl harness already proved
`TOOL_CALL_START`/`TOOL_CALL_END`/`TEXT_MESSAGE_CONTENT(narrator)` envelopes ship correctly
on the wire (62 events on c01 in the curl pass). The Gemini quota is an operational
concern, not a slice 1 bug.

## Test assertions

### Curl harness — `scripts/slice1_agui_wire_format_test.py`

Fired 3 runs covering all 9 AG-UI types on the live wire. Harness embeds its own copy
of the mapping table so any deploy/code mismatch would fail immediately.

| Run | Events | Result |
|-----|--------|--------|
| c04 `basic_3_scenes` (fast, deterministic) | 6 | **passed** |
| c03 `timing_passed_noop` (probes + task) | 9 | **passed** |
| c01 `economics_basics` (long LLM + inner tools) | 62 | **passed** |

Per-envelope assertions (all 77 envelopes across 3 runs):

- **`type` present on every envelope** — passed
- **`type` matches mapping table for each `kind`** — passed
- **`step_name` only on `STEP_STARTED`/`STEP_FINISHED`** — passed (no leakage onto TOOL_CALL_* or TEXT_MESSAGE_CONTENT)
- **`source` only on `TEXT_MESSAGE_CONTENT`** — passed
- **`name` only on `CUSTOM`** — passed
- **`cancelled` only on `run.cancelled`** — passed (does not leak onto `run.ok`)
- **Legacy fields `{seq, ts, kind, summary, detail}` intact** — passed
- **No unknown keys** — passed
- **`type` ∈ `AGUI_TYPES`** — passed

**Harness exit code: 0**

### Browser regression — staging frontend

- **c04 fast run renders identically to pre-slice-1** — passed. Green status dot,
  interpretation card populated with paragraph (not placeholder), Run result shows 3
  scenes, Raw event log shows 6 events in order. No layout jumps, no console errors.
- **c01 error path renders correctly** — passed. Red "Run failed" banner displays
  `litellm.MidStreamFallbackError` with the upstream error class visible. Status rail
  holds the error text without layout shift. Raw event log shows 9 events ending in
  `run.error`. (c01 did not reach inner tool loop because Gemini daily quota was
  exhausted; curl test already proved tool/narrate envelopes on wire.)
- **No unknown-key console warnings** — passed (dev tools inspected during recording).

### Schema invariants proved by curl harness

The harness's assertion 3 (extras don't bleed) and assertion 8 (no unknown keys) are
the adversarial ones — they would fire differently for different classes of bug. Both
passed across 77 envelopes.

## Evidence

### c04 success path

![c04 complete — green status, 3-scene run result, Raw event log (6)](https://app.devin.ai/attachments/309d11b6-013f-4c71-bb72-1ca26a34faa0/screenshot_75891e22eb184ff8aa495aa152f1e704.png)

### c01 error path (Gemini quota mid-stream)

![c01 errored — red "Run failed" banner with MidStreamFallbackError class visible](https://app.devin.ai/attachments/bf613844-5bb3-4704-9fab-f98e2134097a/screenshot_fd8193d45d1e4f51bd9240f56186f5be.png)

### Full browser session recording

https://app.devin.ai/attachments/5155f0c2-394c-4282-b859-abc653ba4e6d/rec-086b2d65-ba91-4a03-9eea-d6f353bbbeea-subtitled.mp4

## Pass gate

All three criteria required by the test plan:

1. **Curl harness exit code 0** — satisfied (0 failures across 77 envelopes)
2. **Browser recording shows no visual regression** — satisfied (c04 success path
   and c01 error path both render correctly)
3. **CI stays green** — satisfied (2/2 green at write time)

Slice 1 is safe to merge on the wire-format dimension. The envelope additions are
purely additive and no consumer breaks.
