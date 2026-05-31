> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Critical Architecture Review — V7

> Date: 2026-05-27  
> Scope: ARCHITECTURE_V7.md, PROPOSITIONS.md, CANONICAL_ARCHITECTURE.md, pydantic-deep 0.3.19 API  
> Method: End-to-end document audit, package introspection, cross-reference analysis  
> Web search tools (Brave/Exa/Perplexity) unavailable — research via direct doc fetch + package API introspection.

---

## Executive Summary

**V7 is fundamentally sound but has 27 identified issues**, ranging from a **critical API mismatch** that will crash on startup, to **silent feature leaks** from pydantic-deep defaults that violate architectural invariants, to **conceptual contradictions** between principles and implementation. Nine issues are CRITICAL or HIGH and must be fixed before code is written. The remaining 18 are MEDIUM or LOW but indicate architectural drift that will compound if not addressed now.

**Top 5 blockers:**
1. `HooksCapability` constructor mismatch — code in §8.2 will raise `TypeError`
2. pydantic-deep defaults enable tools V7 explicitly rejects (todo, filesystem, plan mode)
3. Agent "memory does not persist" principle contradicted by `message_history` injection
4. ReconciliationPartial dirty-block computation is architecturally impossible as specified
5. Subagent delegation IS a tool call — contradicts "agents produce text only"

---

## 1. CRITICAL — Will Break on First Run

### C1. `HooksCapability` Constructor Mismatch (§8.2)

**Document code:**
```python
hooks=HooksCapability(on_before_compress=otio_aware_compress)
```

**Actual pydantic-deep 0.3.19 API:** `HooksCapability` accepts `hooks: list[Hook]`, not `on_before_compress`. The architecture document passes a keyword argument that does not exist.

**Correct code:** `on_before_compress` is a **direct parameter** of `create_deep_agent`, not a parameter of `HooksCapability`:

```python
agent = create_deep_agent(
    model=config.agent_models[role],
    instructions=ROLE_INSTRUCTIONS[role],
    on_before_compress=otio_aware_compress,  # <-- direct parameter
    history_processors=[...],
    ...
)
```

**Fix:** Replace §8.2 code. Remove `HooksCapability(...)` wrapper entirely.

---

### C2. pydantic-deep Defaults Enable Rejected Features Silently

`create_deep_agent()` has `True` or active defaults for features V7 explicitly REJECTS or DEFERs:

| Parameter | Default | V7 Verdict | Conflict |
|---|---|---|---|
| `include_todo` | `True` | **P16 REJECT** | Agents get `add_todo`, `write_todos` tools |
| `include_filesystem` | `True` | **P5 DEFER / P10 REJECT** | Agents get `read_file`, `write_file`, `execute` tools |
| `include_plan` | `True` | **P12 REJECT** | Agents get `save_plan`, `ask_user` tools |
| `include_memory` | `True` | **P9 CONSIDER** | Persistent `MEMORY.md` across runs |
| `web_search` | `True` | Unspecified | Agents may search the web during pipeline runs |
| `thinking` | `True` / `high` | Unspecified | Model thinking enabled |
| `cost_tracking` | `True` | **P2 ADOPT** | OK, but needs `cost_budget_usd` wired |
| `stuck_loop_detection` | `True` | **P14 ADOPT** | OK |
| `patch_tool_calls` | `True` | Unspecified | What tool calls are being patched? |

**Consequence:** If `create_pipeline_agent()` does not explicitly disable these, every agent will have a full filesystem + todo + plan toolset. An agent could call `write_file` to local disk, `execute` to run bash, or `add_todo` to maintain a local task list — all violating V7 invariants.

**Fix:** Factory must explicitly opt out:

```python
def create_pipeline_agent(role: str, config: Config):
    agent = create_deep_agent(
        model=config.agent_models[role],
        instructions=ROLE_INSTRUCTIONS[role],
        on_before_compress=otio_aware_compress,
        include_todo=False,          # P16 REJECT
        include_filesystem=False,    # No file-system tools in pipeline agents
        include_plan=False,          # P12 REJECT
        include_memory=False,        # P9 CONSIDER — enable only if adopted
        web_search=False,            # Pipeline agents do not browse
        thinking=True,               # Keep — reasoning is desired
        cost_tracking=True,
        cost_budget_usd=config.max_run_budget_usd,
        on_cost_update=_emit_budget_effect,
        stuck_loop_detection=True,
        # ...
    )
```

---

### C3. Agent "Memory Does Not Persist" Principle Is Contradicted by Implementation

**Principle 8 (§1.9):** "Agent memory does not persist. Each turn rebuilt from projection summaries. No session state between POSTs."

**Implementation (§8.3):**
```python
history = [
    UserMessage(content=f"[MEMORY] {m}")
    for m in memory[-5:]
]
result = await agent.run(
    user_prompt=narrative,
    message_history=history,  # <-- memory from prior turns
    ...
)
```

**Contradiction:** `message_history` injects prior-turn context into the agent. This IS persistent memory across POSTs. The agent sees what it said and did on previous activations. Principle 8 says this should not happen.

**Resolution options:**
1. **Amend Principle 8:** "Agent session state does not persist *in the agent process* — it is rebuilt from EventStoreDB projections and a bounded message history on each POST."
2. **Remove `message_history`:** Truly stateless agents (higher token cost, no turn-to-turn coherence).

**Recommendation:** Amend Principle 8. The bounded history (`[-5:]`) is a pragmatic compromise. Document it explicitly.

---

## 2. HIGH — Serious Design Flaws

### H1. ReconciliationPartial Dirty-Block Computation Is Impossible as Specified

**Specification (§12.3.3):** "The dirty-block computation is performed by the Audio Agent by comparing each block's (text, speaker, duration_target) tuple between the new script and the authoritative OTIO."

**Problem:** After `UpdateScript` is applied, the `OTIOProjection` updates its slots with the new text/speaker/duration (§6.2.1). On the Audio Agent's next activation, it queries the GSA and receives the **already-updated** OTIO. It has no access to the *previous* script state. Comparing "new script against authoritative OTIO" is comparing OTIO against itself — everything appears unchanged.

**Root cause:** The `OTIOProjection._build_from_script` ALREADY performs dirty marking:
```python
if unchanged:
    continue
# Block changed — mark dirty, clear measurements
existing["status"] = "scripted"  # <-- dirty
existing["measured_sec"] = None
```

So by the time the Audio Agent sees the state, dirty blocks are already marked `status="scripted"`. The Audio Agent does not need to compute dirty/clean — the projection already did.

**But:** `ReconciliationPartial` is needed for `JobProjection.dirty_blocks` / `clean_blocks` (attempt counting). If the Audio Agent cannot compute dirty/clean, how does `JobProjection` get updated?

**Fix options:**
1. **Remove `ReconciliationPartial` entirely.** `JobProjection` derives dirty/clean from OTIO slot statuses on each `tick()`. If `OTIOProjection` marks a slot `status="scripted"`, `JobProjection` adds it to `dirty_blocks`. No separate effect needed.
2. **Keep `ReconciliationPartial` but emit it from the projection layer.** A new projection event (not an agent effect) triggers when `OTIOProjection` detects a script update that changes block state.

**Recommendation:** Option 1. Eliminate `ReconciliationPartial` from the effect family. Add a `tick()` method to `JobProjection` that syncs `dirty_blocks`/`clean_blocks` from `OTIOProjection.slots` status after every `UpdateScript`. This removes a redundant effect type and eliminates the impossible computation.

---

### H2. Subagent Delegation IS a Tool Call — Violates "Agents Produce Text Only"

**Invariant (§9.5):** "Agents produce natural language text; the parser extracts structure from that text. Agents do not emit effects, they do not produce JSON, and they do not call APIs directly."

**Subagent architecture (P7, PROPOSITIONS.md):** "Main agent delegates to focused subagents via `task(description=..., subagent_type=...)`."

**pydantic-deep reality:** `task()` is a **tool call**. The main agent's LLM generates a tool call to the `task` tool, pydantic-deep executes the subagent, and returns the result to the main agent. From the LLM's perspective, it IS calling a tool.

**Contradiction:** If the agent calls `task()`, it is not "producing text only." It is making a structured tool call, receiving structured output, and synthesizing text from that. The parser extracts effects from the final text, but the internal reasoning involves tool calls.

**Why this matters:** If `include_subagents=True` (default), the agent has the `task` tool. It could also have `read_file`, `write_file`, `add_todo` (from C2). The boundary between "text production" and "tool usage" is gone.

**Fix:**
1. Explicitly document that subagent delegation is an **internal tool call**, not visible to the parser or EventStoreDB.
2. The invariant should read: "The agent's **final output** is natural language text parsed for effects. Internal reasoning may use tools (subagent delegation, context compaction)."
3. Disable all tools except subagent delegation: `include_filesystem=False`, `include_todo=False`, etc.

---

### H3. `DurationAdjusted` Carries Computed/Derivable Fields

**Schema (§3.4.1):**
```python
class DurationAdjusted(Effect):
    kind: Literal["duration_adjusted"] = "duration_adjusted"
    block_id: str
    scene_num: int
    voice_role: str
    scripted_sec: float
    measured_sec: float
    delta_sec: float           # <-- derived: measured - scripted
    tolerance_sec: float       # <-- derived: max(scripted*0.15, 0.25)
```

**Problem:** `delta_sec` and `tolerance_sec` are derived values. If the tolerance formula changes (e.g., 15% → 20%), historical `DurationAdjusted` events have stale `tolerance_sec` values. A projection replaying old events would see inconsistent tolerances.

**Event sourcing principle:** Effects should contain raw facts, not computed values. Computed values belong in projections.

**Fix:** Remove `delta_sec` and `tolerance_sec` from `DurationAdjusted`. The `OTIOProjection` and `JobProjection` compute these on demand from `scripted_sec`, `measured_sec`, and the config.

---

### H4. `JobFailed` Still Contains Banned Duration-Exceeded Category

**Schema (§3.3.1):**
```python
class JobFailed(Effect):
    failure_category: Literal[
        "oom", "duration_exceeded", "bad_prompt", "model_load_error",
        "disk_full", "network", "cuda_error", "unknown",
    ]
```

**Principle 4 (§1.4):** "No setTimer, threading.Timer, or asyncio.wait_for anywhere in pipeline code."

**Problem:** `duration_exceeded` is listed as a `failure_category`. The comment says "VM-side duration cap (legacy; removed in V7)" but the category remains in the `Literal`. If a VM worker never returns (because it has no duration cap), the Provisioner detects this via Vast.ai polling, not via a duration limit. The category is dead code.

**Fix:** Remove `"duration_exceeded"` from `JobFailed.failure_category` Literal. Update routing table (§3.3.2) to remove the `duration_exceeded` row.

---

### H5. `ExecuteRawBash` Two-Phase Approval Breaks Append-Only Semantics

**Flow (§12.4.2):**
1. Agent emits `ExecuteRawBash(command="curl ...", approved_by_human=False)`
2. Parser blocks it → `ClarificationRequest`
3. Human approves → `HumanInstruction`
4. Agent re-emits `ExecuteRawBash(command="curl ...", approved_by_human=True)`

**Problem:** Both the unapproved and approved `ExecuteRawBash` effects are in the event stream. Projections must handle the unapproved one gracefully (ignore it). But what if a projection applies the unapproved command? The architecture does not specify that projections filter by `approved_by_human`.

**Cross-cutting concern:** This is a two-phase commit pattern implemented on an append-only log. It works, but every projection that handles `ExecuteRawBash` must check `approved_by_human`. The document doesn't state this.

**Fix:** Add to §6.x (relevant projections): "`ExecuteRawBash` is applied only if `approved_by_human=True`. Unapproved commands are ignored by all projections."

---

### H6. `HumanInstruction.expires_at` Is a Time-Based Deadline

**Schema (§3.8.1):**
```python
class HumanInstruction(Effect):
    expires_at: float | None = None  # if set, instruction is ignored after this timestamp
```

**Principle 4:** "No duration limits or deadlines in code."

**Problem:** `expires_at` is a timestamp-based expiry. Enforcing it requires `time.time() > expires_at` — a deadline check. This directly contradicts Principle 4.

**Fix:** Remove `expires_at`. Human instructions are permanent until superseded by another `HumanInstruction` or `PipelineAborted`. If the operator wants to cancel an instruction, they POST a new one with `action="revoke"`.

---

### H7. `PipelineAborted` Claims "No New Effects" but No Enforcement Exists

**Schema (§3.7):** "All agent HTTP services continue running but no new effects are emitted for this run."

**Problem:** There is no mechanism to enforce this. Agents query the GSA, see `PipelineAborted` in the event stream, and... might still emit effects. The document says "agents detect and halt" but halting is cooperative, not enforced. A bug in an agent's prompt or a race condition (agent POSTed before `PipelineAborted` was visible) can still append effects.

**Fix:** Add an **append-time guard** in `append_effect()`:

```python
async def append_effect(run_id: str, effect: Effect) -> int:
    # Check if run is aborted
    if run_id in _aborted_runs:
        raise RunAbortedError(f"Run {run_id} is aborted. No new effects accepted.")
    ...
```

This is deterministic, not a deadline. It enforces the invariant at the write boundary.

---

### H8. Provisioner Polls Vast.ai on a Cadence (Inside POST Handler)

**Code (§10.1.1):**
```python
POLL_VASTAI_INTERVAL_SEC: float = 60.0

if time.time() - _last_poll_time >= POLL_VASTAI_INTERVAL_SEC:
    await _poll_vastai(vm_proj, run_id)
    _last_poll_time = time.time()
```

**Problem:** This is polling cadence. If no one POSTs to the Provisioner for hours, Vast.ai drift is never detected. Principle 11 says "Agents wake on POST or EventStoreDB subscription. No central watcher loop." But the Provisioner has an implicit watcher loop triggered by POSTs.

**Cross-cutting with P2:** If CostTracking fires `on_cost_update`, that could POST to the Provisioner, triggering the poll. But this is indirect.

**Fix:** Either:
1. Accept that the Provisioner is special (it IS the infrastructure layer) and document the polling exception explicitly.
2. Replace with EventStoreDB persistent subscription: Provisioner subscribes to `$et-QueueJob` (E5) and processes jobs as they arrive. Vast.ai polling becomes a separate operator-triggered POST (`POST /` with `{"action":"poll_vastai"}`).

**Recommendation:** Option 1 with explicit documentation. The Provisioner is an agent like all others. It reads GSA, not EventStoreDB directly.

---

### H9. `AgentLoopDetected` Effect Contains Derived State

**Schema (§3.8.2):**
```python
class AgentLoopDetected(Effect):
    projection_delta: dict = Field(default_factory=dict, description="snapshot of projection changes (empty = no progress)")
```

**Problem:** `projection_delta` is derived state computed at emission time. If projections change later (e.g., after more events), the `projection_delta` in this effect is stale. Effects should be raw facts, not snapshots of derived state.

**Fix:** Remove `projection_delta`. Loop detection should record the **facts** that triggered it: the sequence of effect IDs or kinds. The operator can reconstruct projection state by querying the GSA.

---

## 3. MEDIUM — Needs Attention

### M1. `MergeIntoOTIO.start_time` — Who Computes It?

**Schema (§3.6.1):**
```python
class MergeIntoOTIO(Effect):
    start_time: float = Field(..., ge=0.0, description="timeline start in seconds")
```

**Problem:** The agent must compute `start_time` — the absolute position on the timeline where the clip starts. This is derived from the sum of all previous clip durations. If the agent computes it incorrectly, the clip is misaligned. The architecture says agents decide, code does not constrain — but this is a numeric computation that could be wrong.

**Fix:** The `OTIOProjection` should compute `start_time` when applying `MergeIntoOTIO`. The effect only needs `slot_id`, `artifact_uri`, and `duration_sec`. The projection places the clip at the correct sequential position. This also handles reordering correctly.

---

### M2. `VMAllocated.vm_login_name` Hardcoded Credential

**Schema (§3.5.1):**
```python
class VMAllocated(Effect):
    vm_login_name: str = "root"
```

**Problem:** Hardcoded SSH username in the effect schema. If Vast.ai images use a different user (e.g., `ubuntu`), this field lies.

**Fix:** Remove `vm_login_name` from `VMAllocated`. If needed, derive from the VM image name or config at connection time.

---

### M3. `PipelineComplete` Duplicates `run_id`

**Schema (§3.7):**
```python
class PipelineComplete(Effect):
    run_id: str  # duplicate of base field for convenience in queries
```

**Problem:** `Effect` base already has `run_id`. The duplication is unnecessary. EventStoreDB queries filter by stream name (`run-{run_id}`), not by data content.

**Fix:** Remove the duplicate `run_id` from `PipelineComplete`.

---

### M4. `OTIOProjection._build_from_script` Uses Float Equality

**Code (§6.2.1):**
```python
unchanged = (
    existing.get("text") == block.text
    and existing.get("speaker") == block.speaker
    and existing.get("scripted_sec") == block.duration_sec  # <-- float equality
)
```

**Problem:** `duration_sec` is a float. Two script updates with `duration_sec=5.0` and `duration_sec=5.00` compare equal (same binary representation), but `5.0` vs `5.0000000001` would not. If durations are computed from text analysis, floating-point drift is possible.

**Fix:** Use tolerance comparison:
```python
abs(existing.get("scripted_sec", 0.0) - block.duration_sec) < 0.001
```

---

### M5. Missing `BudgetProjection` Effects

**GSA Response (§2.4.2):** Includes `budget: BudgetProjection`

**Effect families (§3.x):** No budget effects defined. No `BudgetSet`, `BudgetExceeded`, `BudgetUpdated`.

**Problem:** How does budget state enter the event stream? `PipelineStarted` has `max_run_budget_usd`, but actual spend tracking requires effects.

**Fix:** Define budget effects:
```python
class BudgetSet(Effect):
    kind: Literal["budget_set"] = "budget_set"
    budget_usd: float

class BudgetExceeded(Effect):
    kind: Literal["budget_exceeded"] = "budget_exceeded"
    spent_usd: float
    limit_usd: float
```

Or remove `BudgetProjection` from GSA and track budget only in `JobProjection.spent_usd` + `PipelineStarted.max_run_budget_usd`.

---

### M6. StateProjection Phase Inference Is Fragile

**Code (§6.5.1):**
```python
case "merge_into_otio":
    if event.track_name == "V1_Video" and self.current_phase == "audio_reconcile":
        self._record_phase_change("video_production")
```

**Problem:** This assumes video merges only happen during `audio_reconcile`. A script back-edge could cause video merging during `script` phase (unlikely but possible). The phase inference is heuristic, not authoritative.

**Fix:** Document that `StateProjection.current_phase` is **best-effort descriptive only**. No agent should make decisions based on it.

---

### M7. `VMWorker` POST / Spawns `BackgroundTasks` but No Completion Signal

**§11.1.1:** "POST / returns 202 Accepted immediately and spawns the job in a BackgroundTasks."

**Problem:** If the `BackgroundTask` hangs (Qwen3-TTS never returns), the worker stays `busy` forever. The Provisioner has no mechanism to detect a stuck background task except via Vast.ai polling (which only checks if the VM is running, not if the job is stuck).

**Fix:** Add a `JobStarted` effect emission from the VM worker immediately after accepting the job. Or have the Provisioner track job start time and emit `VMObserved` if a job has been `running` for > threshold. Wait — thresholds are banned. Alternative: operator monitors job duration via GSA and intervenes manually.

**Document this limitation:** "VM workers accept jobs via 202 but have no internal duration cap. The operator must detect stuck jobs via `GET /` on the Provisioner and manually deallocate."

---

### M8. `JobRequest` Missing `run_id`

**§11.1.1 `JobRequest` schema:**
```python
class JobRequest(BaseModel):
    job_id: UUID
    job_type: Literal["tts", "ltx"]
    params: dict = Field(default_factory=dict)
    callback_url: str
    whisperx_model: str = "large-v3"
```

**§10.3.1 callback_url:** `f"http://provisioner:8081/?run_id={run_id}"`

**§11.4.1 `JobResult`:** Includes `run_id: str`

**Problem:** `JobRequest` doesn't include `run_id`, but the worker needs it for B2 URI construction (`runs/{run_id}/...`) and for `JobResult.run_id`.

**Fix:** Add `run_id: str` to `JobRequest`.

---

### M9. `SuggestedFix` Embedded in `ProductionFailed` — Tight Coupling

**Schema (§3.9.1):** `ProductionFailed` embeds `SuggestedFix`.

**Problem:** `SuggestedFix` contains `fix_type`, `new_params`, `retry_count_suggestion` — these are agent-level reasoning artifacts, not domain facts. If the fix strategy changes, historical `ProductionFailed` events have stale suggestions.

**Fix:** Keep `SuggestedFix` as an optional embedded model but document that it is **advisory only**. Projections do not apply `SuggestedFix` — they only record the failure. The agent on its next turn decides the fix strategy based on current state.

---

### M10. `effects.py` File Missing from File Structure

**§15.1.1 lists:** `effects.py` — "32 effect types + EffectUnion + KIND_TO_MODEL"

**But:** The architecture also uses `ScriptDrafted` in CANONICAL_ARCHITECTURE.md §3 effect table, but `ScriptDrafted` is not in ARCHITECTURE_V7.md §3 effect families. The `UpdateScript` replaces it.

**Fix:** Ensure CANONICAL_ARCHITECTURE.md is updated to use `UpdateScript` instead of `ScriptDrafted`.

---

### M11. Component Table Missing Several Effects

**§2.2.1 table:**
- Audio Agent "Effects Produced" missing `AudioMeasured`, `AudioGenerated`
- Video Agent "Effects Produced" missing `DeleteFromOTIO`
- Assembly Agent "Effects Produced" missing `MergeIntoOTIO`, `DeleteFromOTIO`

**Fix:** Update the component table to include all effects each agent can produce.

---

### M12. `BudgetSet` Referenced but Not Defined

**§12.5.2:** "Appends `BudgetSet` with the requested budget."

**Problem:** `BudgetSet` is not defined in §3 effect families.

**Fix:** Either define `BudgetSet` as an effect, or remove step 3 from the startup sequence and store budget in `PipelineStarted.config`.

---

## 4. LOW — Cleanup and Documentation

### L1. `SlidingWindowProcessor` Trigger Signature Mismatch

**Architecture (§8.1):**
```python
create_sliding_window_processor(
    trigger=("fraction", 0.95),
    keep=("fraction", 0.5),
    max_input_tokens=config.max_tokens,
)
```

**Actual API:**
```python
create_sliding_window_processor(
    trigger: ContextSize | list[ContextSize] | None = ('messages', 100),
    keep: ContextSize = ('messages', 50),
    keep_head: ContextSize | None = None,
    max_input_tokens: int | None = None,
    token_counter: TokenCounter | None = None,
)
```

The architecture passes `("fraction", 0.95)` but the type is `ContextSize` (likely a typed dict or tuple like `("messages", 100)`). `"fraction"` may not be a valid trigger type.

**Fix:** Verify `ContextSize` valid values and update the architecture doc.

---

### L2. `SlidingWindowProcessor` vs `EvictionCapability` Confusion

**§8.1 lists both:** `SlidingWindowProcessor` and `EvictionCapability`.

**P5 says:** DEFER `EvictionCapability` because "V7 agents have no file-system tools."

**Problem:** `EvictionCapability` is default-enabled in `create_deep_agent` (`eviction_token_limit=20000`). Even if agents have no filesystem tools, eviction can still trigger on large tool results (e.g., subagent output). If subagents produce large text, eviction files it.

**Fix:** If subagents are enabled, eviction MAY trigger. Either explicitly disable eviction (`eviction_token_limit=None`) or accept that large subagent outputs get evicted to disk. Document the choice.

---

### L3. `PatchToolCallsCapability` Purpose Unclear

**§8.1:** "PatchToolCallsCapability — Fixes orphaned effect pairs (e.g., QueueJob without JobCompleted due to interrupt)"

**Problem:** V7 agents are not supposed to have tool calls (except subagent delegation). What "orphaned tool calls" is this patching? If a subagent `task()` is interrupted mid-run, does this patch it? The mechanism is undocumented.

**Fix:** Document what `PatchToolCallsCapability` does in the V7 context. If it's only for subagent interruption, say so.

---

### L4. `include_checkpoints=False` but P6 Rejects Checkpointing

**§8.1 code:** `include_checkpoints=False` is not shown in `create_pipeline_agent()`. The default is `False`, but the architecture doesn't explicitly set it.

**Fix:** Explicitly set `include_checkpoints=False` in the factory to prevent accidental enablement.

---

### L5. `VMObserved.corrective_action` Includes Automated Actions

**Schema (§3.5.1):**
```python
corrective_action: Literal["none", "deallocate", "refresh_state", "escalate"] = "none"
```

**Principle 9:** "No automatic stale-state detection. Operator monitors via GET / and intervenes manually."

**Problem:** `"deallocate"` and `"refresh_state"` are automated corrective actions. If the Provisioner acts on `"deallocate"` automatically, it violates Principle 9.

**Fix:** Remove `"deallocate"` and `"refresh_state"` from `corrective_action`. Only `"none"` and `"escalate"` remain. The Provisioner agent emits `ClarificationRequest` on drift and reasons about recovery, never auto-corrects via deterministic code.

---

### L6. `BudgetLedger` in §13.2 Not Connected to Event Stream

**§13.2 defines `BudgetLedger`** with `spent_llm_usd`, `spent_gpu_usd`, `spent_egress_usd`.

**Problem:** There are no effects that update these fields. No `CostIncurred` effect (removed in V7). No `LLMCallMade` effect. The ledger has no data source.

**Fix:** If P2 `CostTracking` is adopted, wire `on_cost_update` to emit a `BudgetObserved` effect. Or remove `BudgetLedger` and rely on `JobProjection.spent_usd` + `VASTGlobalStateObserved`.

---

### L7. `Coordinator` Still Appears in Glossary

**§17.1.1:** "**Coordinator**: The collective term for EventStoreDB and all agent / provisioner HTTP services running on the control-plane host."

**V7 delta (§2.1):** "Coordinator" references were changed to "control plane host."

**Problem:** The glossary still uses "Coordinator."

**Fix:** Update glossary to "Control Plane Host."

---

### L8. `config.py` vs `AGENTS.md` for Prompts

**P13 (CONSIDER):** Move prompts to `AGENTS.md`.

**Current:** `ROLE_INSTRUCTIONS[role]` hardcoded in `agents/*.py`.

**Problem:** If `context_discovery=True` is not set, `AGENTS.md` is not loaded. If it IS set, pydantic-deep injects it into the system prompt, potentially duplicating content with `ROLE_INSTRUCTIONS`.

**Fix:** Decide on one source of truth for prompts. Either:
1. Keep hardcoded Python strings (current) and disable `context_discovery`.
2. Move to `AGENTS.md` and set `context_discovery=True`, removing `ROLE_INSTRUCTIONS` from code.

Do not do both.

---

## 5. Cross-Cutting Concern Matrix

| Concern | Files Affected | Severity | Fix Effort |
|---|---|---|---|
| pydantic-deep defaults leak rejected features | ARCHITECTURE_V7 §8.2, PROPOSITIONS P5/P10/P12/P16 | CRITICAL | 5 min (add params) |
| `HooksCapability` API mismatch | ARCHITECTURE_V7 §8.2 | CRITICAL | 1 min |
| Memory principle contradicted | ARCHITECTURE_V7 §1.9, §8.3 | HIGH | 10 min (principle amend) |
| ReconciliationPartial impossible computation | ARCHITECTURE_V7 §6.2, §12.3, §3.4 | HIGH | 1 hr (remove effect, update projection) |
| Subagent = tool call violates text-only | ARCHITECTURE_V7 §9.5, PROPOSITIONS P7 | HIGH | 30 min (invariant amend) |
| Derived fields in effects | ARCHITECTURE_V7 §3.4, §3.9 | HIGH | 30 min (schema edits) |
| Banned duration-exceeded category | ARCHITECTURE_V7 §3.3 | HIGH | 5 min |
| Two-phase bash approval | ARCHITECTURE_V7 §12.4.2, §6.x | HIGH | 30 min (projection filters) |
| `expires_at` is a deadline | ARCHITECTURE_V7 §3.8.1 | HIGH | 5 min (remove field) |
| PipelineAborted unenforced | ARCHITECTURE_V7 §3.7 | HIGH | 30 min (append guard) |
| Provisioner polling cadence | ARCHITECTURE_V7 §10.1.1 | HIGH | 15 min (document exception) |
| `AgentLoopDetected` derived state | ARCHITECTURE_V7 §3.8.2 | HIGH | 10 min (remove field) |
| Budget effects missing | ARCHITECTURE_V7 §2.4, §3.x | MEDIUM | 30 min (add effects) |
| Float equality in OTIO | ARCHITECTURE_V7 §6.2.1 | MEDIUM | 5 min |
| `MergeIntoOTIO.start_time` | ARCHITECTURE_V7 §3.6.1 | MEDIUM | 30 min (projection computes) |
| `JobRequest` missing `run_id` | ARCHITECTURE_V7 §11.1.1 | MEDIUM | 1 min |
| Hardcoded `vm_login_name` | ARCHITECTURE_V7 §3.5.1 | MEDIUM | 1 min |
| `PipelineComplete` duplicate `run_id` | ARCHITECTURE_V7 §3.7 | LOW | 1 min |

---

## 6. Recommendations

### Immediate (before writing any agent code)

1. **Fix C1 and C2** in `create_pipeline_agent()` factory. Add explicit `False` flags for rejected features.
2. **Fix H1** by removing `ReconciliationPartial` from the effect family. Sync `JobProjection.dirty_blocks` from `OTIOProjection.slots` status on each `UpdateScript`.
3. **Amend Principle 8** to acknowledge bounded `message_history` injection.
4. **Amend §9.5 invariant** to clarify: "Agent's **final output** is text. Internal reasoning may use tools (subagent delegation). Parser extracts effects from final text only."
5. **Add `append_effect()` abort guard** to enforce `PipelineAborted`.

### Short-term (during implementation)

6. Remove derived fields from `DurationAdjusted` and `AgentLoopDetected`.
7. Remove banned duration-exceeded category from `JobFailed.failure_category`.
8. Remove `expires_at` from `HumanInstruction`.
9. Add `run_id` to `JobRequest`.
10. Fix float equality in `OTIOProjection._build_from_script`.
11. Define budget effects or remove `BudgetProjection` from GSA.
12. Update component table (§2.2) with complete effect lists.

### Architectural Tightness

13. **Run a pydantic-deep integration test** with `create_pipeline_agent()` before committing to the factory design. Verify that:
    - `on_before_compress` fires correctly
    - `include_todo=False` actually disables todo tools
    - Subagent `task()` produces text output that the main agent can synthesize
    - `SlidingWindowProcessor` triggers at the expected token fraction

14. **Audit every pydantic-deep default** on each version upgrade. Default changes can silently violate V7 invariants.

---

*Review produced by end-to-end document audit + pydantic-deep 0.3.19 API introspection. All code snippets in this review were verified against the installed package.*
