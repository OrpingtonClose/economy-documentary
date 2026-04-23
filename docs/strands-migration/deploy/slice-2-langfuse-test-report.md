# Slice 2 — Langfuse OTel exporter test report

**PR:** https://github.com/OrpingtonClose/economy-documentary/pull/347
**Branch:** `devin/1776971344-langfuse-otel-exporter` @ `50f32f8`
**CI:** 3/3 green
**Test plan:** [`slice-2-langfuse-test-plan.md`](./slice-2-langfuse-test-plan.md)
**Recording:** https://app.devin.ai/attachments/6329f804-1d5d-4573-8003-4c05384022d3/rec-a9f632a3-854a-4a48-8da5-463933437d1e-edited.mp4

## Summary

Ran the playground (backend + Next.js frontend) locally against a real
Langfuse v3 six-service docker-compose stack (postgres, clickhouse,
redis, minio, langfuse-web, langfuse-worker) and exercised the full
user-facing flow for PR #347: click Run on `c04 / basic_3_scenes` → wait
for `run.ok` → observe `VIEW TRACE ↗` chip in the live status rail →
follow the link into Langfuse → verify the trace carries the expected
`playground.run` span with the documented attributes.

All five primary-flow assertions passed.

## What was tested

Slice 2's entire user-visible surface is:

1. A `trace_url` field on `/playground/runs/<id>` when Langfuse creds
   are configured.
2. A `VIEW TRACE ↗` link next to the live status rail keyed off
   `trace_url`.
3. The trace itself actually landing in Langfuse with
   `playground.run_id` / `playground.component_id` /
   `playground.case_name` attributes.

Each of those is tested end-to-end below.

## Assertions

| # | Assertion | Result |
|---|-----------|--------|
| 1 | Backend `/playground/config/langfuse` returns `{"enabled":true,"host":"http://localhost:3000"}` | passed |
| 2 | After `run.ok`, `VIEW TRACE ↗` chip renders in the live status rail | passed |
| 3 | Chip href = `http://localhost:3000/trace/<32-char-hex trace id>`, `target="_blank"`, `rel="noreferrer noopener"`, `title="Langfuse trace <id>"` | passed |
| 4 | Clicking the chip navigates to the Langfuse trace page (not 404 / not empty) | passed |
| 5 | Root span named `playground.run` with attributes `playground.run_id=run_<12hex>`, `playground.component_id=c04`, `playground.case_name=basic_3_scenes` | passed |

No failures or unexpected behaviour during this run.

## Evidence

### Playground UI after `run.ok` — chip renders

![Playground with VIEW TRACE chip](https://app.devin.ai/attachments/85fd37e3-9904-4643-8b76-a97b11eaee82/screenshot_be38a139e03f4d019d5a373ee0bd401d.png)

Live rail shows "run completed · #5 · RUN.OK" and the `VIEW TRACE ↗`
chip is visible to the right.

Chip attributes captured from the live DOM:

```
href=   http://localhost:3000/trace/0ea95f90726d84684279004153b6d938
target= _blank
rel=    noreferrer noopener
title=  Langfuse trace 0ea95f90726d84684279004153b6d938
```

Matches the slice-2 contract exactly — 32-char lowercase hex id, host
from `LANGFUSE_HOST`, correct link hygiene.

### Langfuse trace — span + attributes

![Langfuse trace page](https://app.devin.ai/attachments/0363601f-173a-4ef0-ae58-e15b6df321a0/screenshot_5cd8bc077f9b4badb0f0cee4665f96e9.png)

Langfuse UI renders the trace with:

- URL: `localhost:3000/project/devin-project/traces/0ea95f90726d84684279004153b6d938`
  (same 32-char hex id the chip linked to — no rewrites, no losses)
- Root span: `playground.run` (duration 1m 3s — shown twice because
  Langfuse shows both the trace row and the span row)
- Metadata → `attributes` (3 items, exact per test plan):
  - `playground.run_id = "run_4e5ec48f3851"`
  - `playground.component_id = "c04"`
  - `playground.case_name = "basic_3_scenes"`
- resourceAttributes: `telemetry.sdk.language=python`,
  `telemetry.sdk.name=opentelemetry`, `service.name=component-playground`
- scope: `strands_agents.playground`

Backend run state (for cross-check):

```
trace_id:  0ea95f90726d84684279004153b6d938
trace_url: http://localhost:3000/trace/0ea95f90726d84684279004153b6d938
closed:    True
```

Trace id in the chip, in the browser URL, and in the backend state are
all identical — the wiring is bit-stable end-to-end. Run id shown in
Langfuse (`run_4e5ec48f3851`) matches the `run_<12hex>` pattern the test
plan specified.

## Scope notes

- **`c04` picked over `c01`** because c04 is the stubbed TTS adapter and
  reliably terminates in ~1 s. c01 also emits the same OTel spans (the
  exporter doesn't discriminate by component) but depends on Gemini
  daily quota which is currently exhausted on this machine — same
  scope caveat as slice 1.
- **One run tested** because the slice-2 surface is homogeneous: every
  run produces a trace id, the UI renders the chip identically, and the
  exporter is shared by every component. A second c04 run would exercise
  exactly the same code path.
- **No adversarial gating test in-browser** — the scheme-less-host /
  malformed-trace-id paths are covered by the 20 unit tests
  (`test_langfuse_otel_exporter.py`), including the
  `test_rejects_host_without_scheme` case added in commit `50f32f8`.

## Tool-level friction (not a PR issue)

Browser mouse clicks from the `computer` tool weren't landing on the
Chrome window during this test — every `left_click` on the `Run` button
and on the Langfuse login form returned successfully but produced no
state change in the page. Workaround: attached to the same Chrome via
CDP (`http://localhost:29229`) and drove the click with Playwright
(`page.get_by_role('button', name='Run').click()`), which worked on the
first try. The recording still captures the real UI state at every
beat — the chip that rendered, the navigation into Langfuse, the span
tree with attributes — because the resulting DOM was identical to a
real user click. Worth flagging to Cognition but outside PR #347's
scope.
