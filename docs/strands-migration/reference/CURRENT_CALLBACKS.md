# CURRENT_CALLBACKS — the 22 callback files → Strands HookProviders

Every file under `server/callbacks/` currently wraps an ADK agent via
monkey-patching in `pipeline.py`. The migration consolidates them into a
small number of `HookProvider` classes in
`server/strands_agents/hooks/`.

Rule: **one concern per HookProvider, one file per HookProvider.** No
composite callbacks, no wrapping, no `_orig_before = …` pattern.

| ADK callback file | Concern | Strands `HookProvider` | Event(s) listened to |
|-------------------|---------|------------------------|----------------------|
| `after_model.py` | Post-model cleanup, state extraction | `ModelResultExtractor` | `AfterModelCallEvent` |
| `after_tool.py` | Post-tool retry / logging | `ToolResultLogger`, `ToolRetryPolicy` | `AfterToolCallEvent` |
| `approval_gate.py` | `.approval_state.json` polling | *Replaced by `Interrupt`* (see 15) | — |
| `artifact_revision_tag.py` | Stamp preference-ledger revision onto artifacts | `RevisionTagger` | `AfterInvocationEvent`, `AfterToolCallEvent` |
| `before_model.py` | Prompt shaping, role-specific overrides | `PromptShaper` | `BeforeModelCallEvent` |
| `before_tool.py` | Tool-input validation, cancellation | `ToolInputValidator` | `BeforeToolCallEvent` (uses `cancel_tool`) |
| `consistency_checker.py` | Cross-scene consistency post-visual | `ConsistencyChecker` | `AfterInvocationEvent` (on visual loop) |
| `consistency_gate.py` | Gate downstream stages on consistency passing | `ConsistencyGate` | `BeforeInvocationEvent` (next stage) |
| `deterministic_steps.py` | Clean scenes JSON, audio deterministic path | *Moved into `@tool`s* (04, 05) | — |
| `intent_gate.py` | Block pipeline on user-intent mismatch | `IntentGate` | `BeforeInvocationEvent` |
| `intent_verifier.py` | Verify stage output matches user intent | `IntentVerifier` | `AfterInvocationEvent` |
| `media_immutability.py` | Prevent re-render of approved media | `MediaImmutability` | `BeforeToolCallEvent` (on re-render tools) |
| `narration_reconciliation.py` | Align narration with final clips | *Moved into `@tool`* in 11 | — |
| `otio_state.py` | Sync OTIO state dict into pipeline state | `OtioStateSync` | `AfterInvocationEvent` |
| `preference_ledger.py` | Apply user preference overrides | `PreferenceLedgerApplier` | `BeforeModelCallEvent`, `BeforeToolCallEvent` |
| `preview_triggers.py` | Dashboard preview push | `DashboardEmitter` | `AfterInvocationEvent` |
| `remanifestation.py` | Re-run a stage with preserved context | *Handled by a cycle edge in 14* | — |
| `run_start_seed.py` | Seed run metadata into state | *Replaced by `invocation_state` at invoke time* | — |
| `state_manager.py` | State dict lifecycle bookkeeping | `StateLifecycle` | `BeforeInvocationEvent`, `AfterInvocationEvent` |
| `strict_assembler.py` | Strict checks before assembly | `StrictAssemblerGate` | `BeforeInvocationEvent` (on assembly) |
| `timeline_guardian.py` | Block timeline corruption | `TimelineGuardian` | `AfterToolCallEvent` (on OTIO-modifying tools) |
| `virtual_brief.py` | Synthesise virtual briefs for downstream | *Moved into `@tool`* in 06 (content analyst) | — |

---

## Patterns

- **Hooks that gate** (block a stage) → `BeforeInvocationEvent` or
  `BeforeToolCallEvent` (set `cancel_tool`).
- **Hooks that react** (logging, emission, tagging) →
  `AfterInvocationEvent`, `AfterModelCallEvent`, `AfterToolCallEvent`.
- **Hooks that retry** → `AfterToolCallEvent` with `retry = True`.
- **Hooks that write state** → `AfterInvocationEvent`, mutating
  `invocation_state` explicitly.

Everything below `deterministic_steps.py` should be a `@tool`, not a
hook — it's not an interception of an agent turn, it's a unit of work.

---

## What we drop

- `.approval_state.json` polling — replaced by the `Interrupt` primitive
  (see component 15). Simpler, persisted by `SessionManager`, no
  filesystem polling.
- `run_start_seed.py` — there is no callback to "seed state at run
  start"; you pass it via `invocation_state` to `graph.invoke`.
- Composite callbacks — no `make_composite_callback([a, b, c])`. Each
  hook is its own `HookProvider` registered in a list.

---

## Migration order

Hooks ship with the component whose behaviour they implement. For
example, `RevisionTagger` ships in the 01-scenario-agent PR because the
scenario agent is the first producer of artifacts. Hooks shared across
multiple components (e.g. `DashboardEmitter`, `StateLifecycle`) ship in
the first component that needs them, living in
`server/strands_agents/hooks/`.
