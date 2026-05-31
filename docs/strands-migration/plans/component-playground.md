> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Component Playground — Plan

A secondary AG-UI backend plus a standalone frontend that lets the user
poke each of the 15 atomic components in `server/strands_agents/`
directly. Every test case already written becomes a one-click button
that fills the input form. The user can edit that input or write a
fresh one and watch the component run against its declared model.

Not a replacement for `server/agui.py`. That file drives the real
documentary pipeline. This one is a workbench for inspecting the parts
in isolation.

---

## 1. What the user sees

- A standalone Next.js app at `frontend-playground/` served alongside
  the existing frontend.
- Sidebar lists the 15 components grouped by row of the test-case atlas:
  - Row 1: scenario, timing, refiner, audio, timing-loop
  - Row 2: content-analyst, visual-concepter, coherence, visual-loop, production-supervisor
  - Row 3: assembly, recovery, escalation, pipeline, approval-gates
- Each component card shows three first-class facts up front:
  1. **Declared model(s)** — which model the component is built around,
     and a reachability dot (green if the probe just succeeded, red if
     it didn't).
  2. **Test cases** — the existing `Case` list from
     `server/strands_agents/evals/experiments/*.py`, one button each.
  3. **Evaluator stack** — the evaluators the case is graded against,
     with hard-gate vs. soft-gate styling taken straight from
     `eval-framework/THRESHOLDS.md`.
- Clicking a case opens a three-pane view:
  - **Cases** — buttons, colored by role (pass-path / negative / edge).
    Hovering shows the input and expected trajectory. Clicking fills the
    editor and selects the model the case was designed against.
  - **Input** — a schema-driven form matching the component's signature.
    Free text where the component takes text, JSON editor where it takes
    structured state, sliders/toggles where it takes numbers/booleans.
    Model picker shows only the component's declared candidates.
  - **Output** — live streamed trace on the left (tool calls the
    component made, model messages, hook fires), structured output on
    the right. Evaluator chips below, pulled from the component's real
    evaluator stack.
- **Save as case** — once the user has an input + model + expected
  output they like, they can promote it to a committed case. The button
  produces a unified diff against
  `server/strands_agents/evals/experiments/<component>_user_cases.py`,
  shows it for preview, and on confirm opens a PR.

---

## 2. Models are first class

Models are not runtime toggles. They are part of each component's
definition, the same way its tools, hooks, and evaluators are.

- Each component declares its canonical model and, where relevant, a
  small set of candidate models (e.g. Gemini 3.1 vs. Gemma 4 uncensored
  for a judge). The declaration lives next to the component in
  `server/strands_agents/<component>.py`, following the pattern
  already established by
  `server/strands_agents/subagents/production.py`
  (`PRODUCTION_SUBAGENT_DEFAULT_MODEL`) and
  `server/strands_agents/subagents/visual.py`
  (`VISUAL_SUBAGENT_DEFAULT_MODEL`).
- The playground never swaps in a model the component didn't declare.
  The picker is not "pick any model" — it's "pick from the declared
  candidate set".
- **Unreachable model is an automatic failure.** Before a run starts,
  the playground sends a minimal probe to the declared model. If the
  probe fails (no key, endpoint down, 4xx/5xx, persistent rate limit),
  the run returns a `MODEL_UNREACHABLE` result that the evaluator stack
  records as a hard-gate fail. Not a skip, not a fallback to something
  else, not a 503 hidden from the case report. The same rule holds in
  CI.
- The component card shows model reachability as a green/red dot so the
  user can see at a glance which components are currently testable.
- The fake substrate in `server/strands_agents/sim/` (FakeLLM,
  FakeRenderer, FakeTTS, FakeB2, FakeClock, FakeInterrupt) exists for
  CI smoke tests of orchestration wiring — it is never substituted in
  for a real model on a run the user initiates.

---

## 3. Backend shape

New module: `server/playground.py` — a FastAPI `APIRouter(prefix="/playground")`,
mounted next to the existing `/agui` router in `server/server.py`.
Nothing in `server/agui.py` moves; the playground is additive.

### 3.1 Component catalog

`GET /playground/components` — returns the 15 components with:
- `id`, `title`, `kind` (`leaf` / `tool` / `loop` / `graph` / `gate`)
- `row` (atlas row index, 1-3)
- `declared_models`: `[{id, provider, role}]`
- `evaluators`: `[{name, threshold, hard_gate}]`

`GET /playground/components/{id}` — same payload but for one component.

The payload is derived by importing the existing registry module (new)
at `server/strands_agents/playground/registry.py`, which in turn
imports from each component and each experiment module. No duplication:
if a component's evaluator stack changes upstream, the catalog changes
with it.

### 3.2 Case catalog

`GET /playground/components/{id}/cases` — returns the component's cases,
each entry carrying:
- `name`, `session_id`
- `input` (serialized from `Case.input`)
- `expected_trajectory`, `expected_output`, `metadata`
- `role`: `pass` / `neg` / `edge` (derived from `name` prefix or
  metadata, matching the atlas chip colors)

Source of truth is the existing `*_cases()` factory functions in
`server/strands_agents/evals/experiments/`. The playground imports them
and serializes; there is no separate case store.

### 3.3 Input schema

`GET /playground/components/{id}/schema` — JSON schema describing the
component's atomic entry point.

- For `@tool`-decorated functions (timing, audio, assembly, refiner
  tools, etc.) the schema is derived from the function signature and
  Pydantic type hints via Strands' tool-spec introspection.
- For agents (`scenario_agent`, `content_analyst`, `visual_concepter`,
  `coherence_evaluator`, recovery, escalation, production supervisor)
  the schema is the agent's first-turn message shape plus the
  `invocation_state` fields it reads. Defined once in
  `server/strands_agents/playground/schemas.py` alongside the agent
  definitions.

### 3.4 Model-reachability probe

`GET /playground/components/{id}/models/health` — returns one entry per
declared model: `{model_id, ok: bool, latency_ms, checked_at, reason}`.
Cached for 60 seconds. The UI polls this lazily (on first load of a
component page).

### 3.5 Run endpoint

`POST /playground/components/{id}/run` — body is either `{case_name}`
or `{input, metadata, model_id}`. The server:

1. Probes the selected model. On failure, returns an SSE event
   `{"type": "model_unreachable", ...}` and closes the stream with an
   evaluator hard-fail record attached. Done.
2. Builds the component with the declared model + real tools/hooks.
   Fakes are only wired in for non-model dependencies that are unsafe
   or unreasonable to touch from the playground (GPU workers, B2
   writes, real TTS). Those fakes are declared per-component in
   `server/strands_agents/playground/substrate.py`.
3. Streams back an SSE channel of the agent's events plus the
   component's custom events. The shape matches the existing
   `/agui` stream so the frontend can reuse the event renderer.

### 3.6 Evaluator endpoint

`POST /playground/components/{id}/evaluate` — body is `{input, output,
model_id}`. Runs the component's real evaluator stack (the same
`scenario_evaluators()`, `visual_concepter_evaluators()`, …) and
returns `list[EvaluationOutput]`. Hard-gate vs. soft-gate comes from
the existing threshold dicts so the UI chip colors match the atlas.

### 3.7 Save-as-case endpoint

`POST /playground/components/{id}/cases` — accepts a user-authored case
(input + model_id + optional expected output), produces a unified diff
against `server/strands_agents/evals/experiments/<component>_user_cases.py`,
and returns it for preview. A second call with `{confirm: true}` writes
the file and opens a PR via the existing git integration. Nothing is
written without confirmation.

---

## 4. Frontend shape

Standalone Next.js app at `frontend-playground/`. Sharing the
design-tokens + icon set from the existing frontend via a small shared
package (or copied snapshot — TBD in PR 5).

- `app/layout.tsx` — sidebar + main pane.
- `app/page.tsx` — landing: atlas thumbnail + component grid.
- `app/c/[componentId]/page.tsx` — three-pane view.
- `app/c/[componentId]/cases/[caseName]/page.tsx` — deep-linked case.
- Case buttons colored green / red / amber by `role` (pass / neg /
  edge), matching the atlas.
- Input editor: schema-driven form on top of a JSON editor so users
  aren't forced to hand-write JSON.
- Output pane: existing-style SSE event renderer + evaluator chip row.

---

## 5. Work breakdown

Eight small PRs, each green on its own:

1. **Scaffold + catalog** — plan doc committed; `server/playground.py`
   with `GET /components`, `GET /components/{id}`,
   `GET /components/{id}/cases`, `GET /components/{id}/schema`; mount
   in `server/server.py`; no UI yet; a Strands `Experiment` over the
   catalog endpoints.
2. **Model registry + reachability probe** — `server/strands_agents/playground/registry.py`
   declares canonical + candidate models per component (following the
   subagent pattern). `GET /components/{id}/models/health`. Hard-fail
   behavior specified and evaluated.
3. **Run endpoint** — `POST /components/{id}/run` on declared models,
   streaming SSE, with the substrate policy from §3.5. Evaluated by a
   per-component smoke case.
4. **Evaluator endpoint** — `POST /components/{id}/evaluate` reusing
   each component's evaluator stack unchanged.
5. **Frontend scaffold** — `frontend-playground/` Next.js app with
   component list + case browser pane. Read-only against PRs 1-2.
6. **Frontend run + output** — connects the run endpoint, renders the
   SSE stream, shows evaluator chips.
7. **Frontend editable input + custom cases** — schema-driven form,
   "save as case" diff preview.
8. **PR integration for custom cases** — backend writes the diff and
   opens a PR via the existing git integration.

Each PR ends with a Strands `Experiment` that exercises the new
surface. No pytest.

---

## 6. Out of scope for now

- Running against real GPU workers from the playground. The existing
  production pipeline still owns that path.
- Multi-user / auth. Single-tenant local dev only.
- Persistent run history. Each run is ephemeral; if the user wants to
  keep it, they save it as a case.
- Replaying historical pipeline runs. That's what `/agui` already does.
