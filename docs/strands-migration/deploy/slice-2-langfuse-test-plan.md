# Slice 2 — Langfuse OTel exporter + "View Trace" button — test plan

**PR:** https://github.com/OrpingtonClose/economy-documentary/pull/347
**Branch:** `devin/1776971344-langfuse-otel-exporter` (head commit `b6123df`)
**Test surface:** local, real stack. No mocks.

- Playground backend: `127.0.0.1:8000` (uvicorn, `LANGFUSE_*` env vars set)
- Playground frontend: `127.0.0.1:3100` (Next.js dev, proxies `/playground/*` to backend)
- Langfuse v3 stack: `127.0.0.1:3000` (postgres + clickhouse + redis + minio + web + worker, upstream compose, pre-seeded via `LANGFUSE_INIT_*`)

---

## What changed (user-visible)

1. Every run card in the playground workbench now renders a `View Trace ↗` link in the live status rail **iff** the backend is configured with Langfuse credentials. Clicking the link opens the Langfuse trace page for that run in a new tab.
2. A new `GET /playground/config/langfuse` endpoint returns `{enabled, host}` so the frontend can gate UI on exporter status without ever seeing the credentials themselves.
3. Every run dispatched via `POST /playground/components/<id>/runs` now opens a root OTel span named `playground.run` with `playground.component_id` / `playground.case_name` / `playground.run_id` attributes. The OTLP/HTTP exporter pushes this span (and any child spans Strands' inner tool loop emits) to Langfuse.
4. Missing creds / missing SDK / malformed host are all graceful no-ops — the playground never refuses to boot because observability is off.

---

## Primary flow — adversarial E2E

**Goal:** prove the full chain works end-to-end — OTel span creation on the backend → export to Langfuse → trace URL propagated to the frontend → button renders with correct href → clicking it lands on a rendered trace tree that contains the expected metadata.

**Why this single flow:** if any link in the chain is broken (exporter doesn't install, trace id doesn't reach the frontend, button doesn't render, URL is wrong, span attributes missing, Langfuse doesn't receive the OTLP push), this flow fails visibly. A broken build would NOT look identical to a working one because the button would be absent, or the trace page would 404, or the span tree would be empty, or the attributes would be missing.

### Steps

1. Navigate to `http://127.0.0.1:3100/components/c04` in the browser.
   - **Pass:** The workbench renders with the c04 description, the model chip, the case dropdown pre-selected to `basic_3_scenes`, and a "Run" button.
   - **Fail:** Any of those controls missing or an error banner.

2. Click **Run**.
   - **Pass:** The live status rail starts ticking (narrator typing text), the "#N · kind" counter advances, and within ~3 seconds the rail shows a terminal dot (green for success).
   - **Fail:** The rail stays idle, or the run errors, or the dot turns red.

3. Observe the live status rail after the `run.ok` terminal event (within ~5 seconds of clicking Run).
   - **Pass:** A `View Trace ↗` chip renders at the right edge of the rail. Its `title` attribute contains a 32-character lowercase hex trace id. Its `href` has the form `http://localhost:3000/trace/<that same 32-hex id>`. The chip is a proper `<a target="_blank" rel="noreferrer noopener">`.
   - **Fail:** No chip, or the chip has an empty href, or the trace id is not 32-char hex, or the host portion is missing/wrong.
   - **Adversarial rationale:** if `useRunStream.ts` didn't hydrate `traceUrl`, if `/playground/runs/<id>` didn't include `trace_url`, if `ComponentWorkbench.tsx`'s conditional is wrong, or if `langfuse_trace_url()` returned `None`, the chip would simply not appear. A broken build looks visibly different.

4. Middle-click (or open in new tab) the `View Trace ↗` chip.
   - **Pass:** A new tab opens at `http://localhost:3000/trace/<trace_id>`. After Langfuse auth (already established in this session) the page shows a trace titled `playground.run` with a root span.
   - **Fail:** 404, empty trace page, or "Trace not found" in Langfuse.
   - **Adversarial rationale:** if the exporter failed to install or the OTLP push didn't land, Langfuse would not have this trace id at all.

5. Expand the `playground.run` root span in the Langfuse trace viewer (Metadata → Attributes).
   - **Pass:** The following attributes are present and exact:
     - `playground.component_id = c04`
     - `playground.case_name = basic_3_scenes`
     - `playground.run_id` = 16-char run id matching the one in the browser URL (`run_<12hex>`)
   - **Fail:** Any attribute missing, empty, or wrong value.
   - **Adversarial rationale:** `_start_run_root_span()` in `server/playground.py` is the only place that sets those attributes; if the span was opened but attributes weren't set, or if attributes used different keys, this step would fail.

6. In the Langfuse trace tree panel, count immediate children of the root span.
   - **Pass:** There is at least 1 child span (Strands' tool-dispatch spans from the `c04` task adapter). The child spans have non-empty names that are not `playground.run`.
   - **Inconclusive:** If c04 happens to complete entirely inside `_dispatch_run` without emitting child spans, accept just the root span. The primary claim of this PR is root-span metadata, not child-span count — but child spans if present are bonus evidence the Strands telemetry bridge works.

---

## Invariant flow — graceful no-op when creds are unset

**Goal:** prove the PR's "fail-closed on missing creds" claim — with `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` unset, the backend boots, `/config/langfuse` returns `enabled:false`, and the button does **not** render.

### Steps

1. Stop the playground backend.
2. Unset `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` (leave `LANGFUSE_HOST`).
3. Restart uvicorn; wait for `/health` to come back.
4. Hit `GET /playground/config/langfuse`.
   - **Pass:** Response is exactly `{"enabled": false, "host": null}` (the host is suppressed when disabled so the frontend never tries to concatenate against a real URL it can't authenticate).
   - **Fail:** `enabled:true`, or the response 500s.
5. Reload `/components/c04` in the browser, run `basic_3_scenes`.
   - **Pass:** Run completes with `run.ok`. `View Trace ↗` chip is **absent** from the rail. The right-hand `#N · kind` counter still advances normally.
   - **Fail:** Chip still visible, or the page errors, or the run silently doesn't start.
   - **Adversarial rationale:** the only mechanism that hides the chip is `stream.traceUrl !== null`. If `trace_url` still got set (e.g. because `langfuse_trace_url()` returned a URL even without creds), the chip would render with a broken link. This step distinguishes "correct fail-closed" from "leaky URL generation".
6. Restore env vars, restart backend. Hit `/config/langfuse` → must flip back to `{"enabled":true, "host":"http://localhost:3000"}`.

---

## Out of scope

- Long c01 runs — already covered end-to-end by the c04 flow (both components dispatch via the same `_dispatch_run` → `_start_run_root_span` code path). Repeating with c01 would add ~160s of recording without changing the pass/fail shape of any assertion above.
- Testing the bootstrap script end-to-end — verified separately by running upstream's compose directly (the script now delegates to upstream's compose, so we're testing the delegation path). A Vast.ai bring-up of the full script is a separate follow-up.
- Regression of AG-UI wire format — already proven in slice 1 (#346).

---

## Evidence to capture

- Recording: one continuous browser recording covering the primary flow end-to-end, with `computer(action="record_annotate")` marks at each numbered step above.
- Screenshots: (1) View Trace button visible on the rail, (2) Langfuse trace page showing root span + attributes expanded, (3) `/config/langfuse` returning `enabled:false` and the button absent after unsetting creds.
- Exit-code harness: `curl` invocations with `set -e` to verify the two `/config/langfuse` states before recording, captured as command output in the report.
