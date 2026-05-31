> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture V5 — Gap Audit

> Date: 2026-05-27
> Scope: Full document, all 1135 lines
> Method: Line-by-line read, cross-reference checks, consistency audit

---

## CRITICAL — System will not work as specified

### C1. Commands vs. Effects: Fundamental confusion in §3

**Problem:** §3 header says "Effect Type Family" and "Events (and their corresponding command counterparts)." But §3 Commands vs. Events says commands are **ephemeral** and **NOT stored**. Then every effect type in §3.1–3.8 is shown being appended to the event store (§5.2). `QueueJob` is described as produced by Audio/Video agents — but that's a command intent, not a validated fact.

**Contradiction:** If `QueueJob` enters the event log, then either (a) commands ARE stored, violating §3's command/event split, or (b) `QueueJob` is actually an event named `JobQueued` (past tense), but the schema uses imperative naming.

**Fix needed:** Either rename all stored types to past-tense (`JobQueued`, `BashExecuted`, `ScriptUpdated`) and keep imperative names for the pre-validation command schema, OR drop the command/event split and admit that the event log stores both intents and facts (with validation gating).

---

### C2. VM self-destruct timer violates "No timeouts" and "Agentic stale detection"

**Location:** §7.6: "15 min without heartbeat → self-destruct via `vastai destroy`"
**Also:** §1.4: "No `threading.Timer`, `signal.alarm`, or deadline parameters anywhere."
**Also:** Principle 10 (just added): "Stale-state detection is agentic... No VM-side timers."

**Problem:** The VM agent has a hardcoded 15-minute self-destruct. This is a timeout. It directly contradicts §1.4 and Principle 10.

**Fix needed:** Remove VM-side heartbeat and self-destruct. The Provisioner agent (bash-agentic) probes VMs via `ssh`/`nvidia-smi` and judges stalls. VMs are passive — they run jobs until terminated externally.

---

### C3. python-statemachine cannot do async callbacks

**Location:** §4.2 state machine code uses `on_enter_state` that calls `asyncio.create_task()`.

**Problem:** `python-statemachine` callbacks are **synchronous**. Calling `asyncio.create_task()` from a sync callback only works if an event loop is already running, but it's fragile and may raise `RuntimeError: no running event loop` depending on context. The state machine's `tick()` event is also sync — `await machine.tick()` in §4.4 implies `tick()` is async, which python-statemachine does not support natively.

**Fix needed:** Either (a) run the state machine in a dedicated thread with its own event loop, (b) use a different FSM library that supports async, or (c) make the state machine purely sync and have the watcher loop handle async I/O around it.

---

### C4. Projection `tick()` is synchronous but calls async event store

**Location:** §6.1–6.4: all projections define `tick()` as `def tick(self, event_store)` without `async`.

**Problem:** `event_store.read_since()` is async (§5.3). The watcher loop (§4.4) calls `proj.tick(event_store)` without await. This will return a coroutine object, not execute it.

**Fix needed:** All projection `tick()` methods must be `async def tick(self, event_store)` and called with `await`.

---

### C5. State machine code is missing the `aborted` state and escape transitions

**Location:** §4.2 python code:
```python
class PipelineStateMachine(StateChart):
    init = State(initial=True)
    script = State()
    audio_video = State()
    assembly = State()
    done = State(final=True)
```

**Problem:** `aborted` is not defined. Escape transitions (`_budget_exceeded`, `_loop_detected`, `PipelineAborted`) are not in the `tick` transition definition, even though §4.1 diagram and table show them.

**Fix needed:** Add `aborted = State(final=True)` and all escape transitions to the `tick` event.

---

### C6. Agent effects have no `run_id` field, but event store requires it

**Location:** §3 effect schemas (e.g., `UpdateScript`, `QueueJob`) do not show `run_id` fields.
**But:** §5.2 `_write_one` accesses `effect.run_id`.

**Problem:** Every effect must carry `run_id` to scope it to a pipeline run. The schemas are missing this field.

**Fix needed:** Add `run_id: str` to the base `Effect` class, or to every effect schema.

---

## MAJOR — Will cause incorrect behavior or data loss

### M1. `UpdateScript` schema cannot represent a complete script

**Location:** §3.1 `UpdateScript` has fields for ONE block (`scene_num`, `speaker`, `text`, `duration_sec`).

**Problem:** To write a script with 20 narration blocks, the agent must emit 20 separate `UpdateScript` effects. Each one independently mutates the OTIO projection. There's no atomic "replace entire script" operation. If the agent emits 10 blocks and then crashes, the script is half-written.

**Also:** `OTIOProjection._add_narration_slot` (§6.1) is not defined. We don't know if it replaces existing slots or appends.

**Fix needed:** Either (a) make `UpdateScript` carry a `blocks: list[Block]` field for atomic script updates, or (b) define how partial script updates are handled by the projection.

---

### M2. No `job_started` event — cannot track "running" jobs

**Location:** §3.2 Job Effects lists: `QueueJob`, `JobCompleted`, `JobFailed`, `JobRequeued`, `JobApproved`.

**Problem:** Jobs go from `pending` (on `QueueJob`) directly to `completed`/`failed`. There is no `JobStarted` event. This means:
- The job projection cannot distinguish "queued but not yet running" from "actively running on VM."
- Agentic stale detection (Principle 10) cannot tell if a job has been running for 20 minutes or sitting in queue for 20 minutes.
- The `_has_pending_or_running_jobs()` guard (§4.3) conflates both states.

**Fix needed:** Add `JobStarted` effect, emitted by the Provisioner when it successfully POSTs a job to a VM worker.

---

### M3. `AudioMeasured` producer is wrong

**Location:** §3.3 table says `AudioMeasured` is produced by **Provisioner**.
**But:** §7.3 says Audio Agent runs WhisperX and compares. §9.2 data flow shows Audio Agent emitting `AudioMeasured`.

**Problem:** The Provisioner delivers raw artifacts. WhisperX measurement is an audio-domain judgment that belongs to the Audio Agent. If the Provisioner runs WhisperX, it becomes a media-quality judge, violating the lackey principle.

**Fix needed:** `AudioMeasured` producer = **Audio Agent**.

---

### M4. Provisioner contradicts itself on direct agent POSTs

**Location:** §7.5: "The Provisioner does not POST directly to Audio/Video agents. Agents observe `JobCompleted` via the JobProjection on their next tick."
**But:** §9.2 data flow shows: `Audio Agent <-- POST / from Provisioner: "Job done, verify"`

**Problem:** Direct contradiction. Does the Provisioner notify agents directly, or not?

**Fix needed:** Remove the direct POST. The Provisioner appends `JobCompleted` to the event store. Agents observe via projections. The data flow diagram in §9.2 must be corrected.

---

### M5. Missing effect schemas for half the effect types

**Location:** §3.1–3.8

**Problem:** Only Script Effects, Job Effects, Reconciliation Effects, and ProductionFailed have full schemas. The following are listed in tables but never defined:
- `MergeIntoOTIO` (§3.5)
- `DeleteFromOTIO` (§3.5)
- `PipelineStarted` (§3.6)
- `TransitionState` (§3.6)
- `PipelineComplete` (§3.6)
- `PipelineAborted` (§3.6)
- `ExecuteRawBash` (§3.7)
- `HumanInstruction` (§3.7)
- `ClarificationRequest` (§3.7)
- `AgentLoopDetected` (§3.7)
- `NoOp` (§3.7)
- `VMAllocated`, `VMDeallocated`, `VMProvisionFailed`, `VMObserved` (§3.4)

**Fix needed:** Define schemas for all 28 effect types.

---

### M6. `ProductionFailed` has `suggested_fix: str` but routing table needs structured data

**Location:** §3.8 `ProductionFailed` has `suggested_fix: str` (free text).
**But:** §3.8 routing table says `gap_unexpected` → back to SCRIPT, `visual_incoherence` → stay in AUDIO_VIDEO, etc.

**Problem:** The state machine guard `_has_script_errors` must read `failure_type` to route. But `suggested_fix` is unstructured string. If the agent wants to suggest a retry count, new params, or a target scene, it has no structured field.

**Fix needed:** Make `suggested_fix` a structured object or add fields like `target_scene`, `new_params`, `retry_count`.

---

### M7. No max-attempt limit in reconciliation loop

**Location:** §7.3 reconciliation loop logic.

**Problem:** A narration block could fail tolerance check, get requeued, fail again, requeue again... forever. There's no `max_attempts_per_block` config or event. The agent could emit infinite `ReconciliationFailed` → `JobRequeued` → `QueueJob` cycles.

**Fix needed:** Add `max_attempts_per_block` to config. After N attempts, emit `ReconciliationFailed` with `failure_type="duration_unrecoverable"` and route to script back-edge.

---

### M8. No budget tracking events or projection fields

**Location:** §11 Hard Principles has no budget principle. §6.2 JobProjection has no `spent_usd`.

**Problem:** `_budget_exceeded` is referenced in the escape transitions but there's no mechanism to accumulate cost. VM allocation costs, LLM API costs, and TTS costs are never recorded.

**Fix needed:** Add `CostIncurred` effect (or have `VMAllocated`/`VMDeallocated` carry cost). Add `spent_usd` to JobProjection. Define budget config fields.

---

### M9. Event store has no deduplication / idempotency

**Location:** §5.2 `_write_one` does plain `INSERT`.

**Problem:** If an agent retries `append()` (network timeout, process restart), the same effect will be inserted twice. Projections that increment counters or append to lists will double-count.

**Fix needed:** Add `effect_id: UUID` to every effect. Use `INSERT OR IGNORE` with `UNIQUE(run_id, effect_id)` constraint.

---

### M10. `VMProjection.poll_vastai()` contradicts "agentic stale detection"

**Location:** §6.3: `VMProjection` has `poll_vastai()` method.
**But:** Principle 10 says "Stale-state detection is agentic... No VM-side timers, no projection-based dead thresholds."

**Problem:** A projection doing deterministic polling is exactly the "projection-based dead threshold" that Principle 10 forbids. VM health observation must be done by the Provisioner agent (bash-agentic), not by a projection.

**Fix needed:** Move Vast.ai polling out of `VMProjection`. The Provisioner agent probes VMs and emits `VMObserved` effects. `VMProjection` is a pure read model that applies them.

---

### M11. Watcher loop does not implement budget check or loop detection

**Location:** §4.4 watcher loop code.

**Problem:** The watcher loop is supposed to check `_budget_exceeded` and `_loop_detected` before sending tick (per escape transitions), but the code only advances projections and sends tick. No safety checks.

**Fix needed:** Add `await _check_budget(machine, event_store)` and `await _check_agent_loop(machine, event_store)` before `machine.tick()`.

---

## MODERATE — Will cause operational pain or confusion

### D1. `audio_generated` in wrong parser category

**Location:** §8.1 parser categories list `audio_generated` under "reconciliation".

**Problem:** `AudioGenerated` is produced by the **Provisioner** (per §3.3 table), not the Audio agent. Putting it in the "reconciliation" category means the Audio agent's parser might try to extract it, creating false positives.

**Fix:** Move `audio_generated` and `audio_measured` to a "provisioner" or "job" category. The Audio agent produces `DurationAdjusted`, `ReconciliationFailed`, `ReconciliationComplete`.

---

### D2. `DeleteScene` and `ReorderScenes` OTIO behavior undefined

**Location:** §6.1 `OTIOProjection.apply` references `_add_narration_slot`, `_merge_clip`, `_adjust_slot_duration` but not deletion or reordering.

**Problem:** We don't know how the projection handles scene deletion (remove all clips in scene? renumber subsequent scenes?) or reordering (mutate track order?).

**Fix:** Define `DeleteScene` and `ReorderScenes` handlers in OTIOProjection.

---

### D3. `DurationAdjusted` does not update job projection

**Location:** §6.2 JobProjection `apply` has no case for `duration_adjusted`.

**Problem:** When reconciliation passes, the job projection still thinks the TTS job is "completed" but doesn't know the block is "clean." There's no `dirty_blocks` / `clean_blocks` tracking.

**Fix:** Add dirty/clean block tracking to JobProjection, or at minimum add a `clean_block_ids` set.

---

### D4. Agent can emit effects for wrong state

**Location:** §7.1 Agent framework.

**Problem:** Agent receives instruction, returns 202, runs LLM in background. If the state machine transitions to a new state before the agent finishes, the agent may emit effects that are invalid for the new state (e.g., `QueueJob` emitted while in `ASSEMBLY`).

**Mitigation:** The parser validates against permitted kinds per agent, but the agent doesn't know the state changed. The event store will accept the effects regardless.

**Fix:** Agent should check `StateProjection.current_state` before appending effects, or effects should carry the state they were emitted in and be rejected if stale.

---

### D5. `script` self-loop guard is missing

**Location:** §4.2 transition table.

**Problem:** SCRIPT → SCRIPT transition has no guard name in the table (says "(default)"). But the diagram shows `script.to.itself()` with no condition. This means the state machine will ALWAYS self-loop in SCRIPT, never transitioning to AUDIO_VIDEO, unless `_audio_reconciled` happens to be checked first.

**Fix:** In python-statemachine, guards are evaluated in declaration order. If `script.to.itself()` is declared before `script.to(audio_video, cond=...)`, the self-loop will always fire first. The self-loop should have a condition like `_still_refining`.

---

### D6. `assembly` self-loop also missing guard

**Location:** §4.2 transition table: "MP4 missing or validation failed" — no named guard.

**Fix:** Add `_assembly_not_ready` guard.

---

### D7. File structure missing `assembly.py`

**Location:** §10 file structure shows agents: `scenario.py`, `audio.py`, `video.py`, `provisioner.py`. No `assembly.py`.

**But:** §4.1 says ASSEMBLY state active agent is "Assembly".

**Fix:** Add `agents/assembly.py` to file structure.

---

### D8. VM agent calls LLM for quality judgment

**Location:** §7.6: "Judges output via LLM call (file size, duration, quality)" and "Calls deepseek-v4-flash via API for troubleshooting."

**Problem:** GPU VMs are ephemeral and may have no API key access. LLM calls from VMs add cost and latency. Quality judgment should happen on the coordinator side (Audio/Video agents), not on the VM.

**Fix:** VM worker does inference and returns raw artifact. Quality judgment is done by media agents using projections.

---

### D9. `_reconciliation_complete()` guard referenced but not defined

**Location:** §4.3 `_audio_still_reconciling` calls `self._reconciliation_complete()`.

**Problem:** This guard is never defined in §4.3. We can infer it checks for `ReconciliationComplete` event existence, but the exact logic (does it check all blocks? just the event?) is missing.

**Fix:** Define `_reconciliation_complete` in §4.3.

---

### D10. `QueueJob` has no `block_id` but reconciliation effects need it

**Location:** §3.2 `QueueJob` has `job_id`, `job_type`, `scene_num`, `slot_id`, `params`.
**But:** `ReconciliationFailureDetail` (§3.3) has `block_id`. `AudioMeasured` has `block_id`. `DurationAdjusted` has `block_id`.

**Problem:** If `QueueJob` doesn't specify `block_id`, how do reconciliation effects correlate jobs with blocks? The `slot_id` might be the block address, but that's not documented.

**Fix:** Add `block_id` to `QueueJob`, or document that `slot_id` IS the block identifier.

---

### D11. `audio_video` state has two phases but no phase tracking

**Location:** §4.1 says AUDIO_VIDEO has Phase 1 (reconciliation) and Phase 2 (video production).

**Problem:** The state machine has no sub-state or phase variable. Guards must infer phase from projection state (`ReconciliationComplete` emitted? any pending TTS jobs?). This works but is implicit and fragile.

**Mitigation:** Document exactly how guards distinguish Phase 1 vs Phase 2. Currently `_audio_still_reconciling` checks `ReconciliationComplete` not yet emitted, and `_video_still_pending` presumably checks for pending LTX jobs. This is acceptable but should be explicit.

---

## MINOR — Editorial / clarity issues

- §2 topology diagram doesn't show ABORTED state.
- §3.3 `ReconciliationComplete` says "OTIO is now authoritative" but doesn't explain what "authoritative" means for downstream agents.
- §4.3 `_all_media_produced` is defined as `all_media_produced` (no leading underscore) but the transition table says `_all_media_produced`.
- §6.3 `VMProjection.poll_vastai()` is shown but not implemented.
- §7.6 "Monitors heartbeat every 60s" — heartbeat to whom? The event store? The Provisioner? Unclear.
- §8.1 `_extract_kind_markers` is referenced but not defined.
- §8.1 `KIND_TO_MODEL` and `instructor_client` are referenced but not defined.
- §9.2 data flow shows `AudioMeasured` emitted by Audio Agent but §3.3 says Provisioner.
- §9.3 back-edge flow says "Next tick: SCRIPT → AUDIO_VIDEO" but doesn't mention the guard condition.
- §11 Principle 7 updated to "bash-agentic lackey" but §7.5 title is still "Provisioner Agent" (not "Provisioner Agent (bash-agentic)")
- §12 Glossary: "Provisioner" definition updated, but "Agent" and "Worker" definitions don't capture the bash-agentic distinction.

---

## Summary by count

| Severity | Count |
|---|---|
| CRITICAL | 6 |
| MAJOR | 11 |
| MODERATE | 11 |
| MINOR | 12 |
| **Total** | **40 gaps** |

## Top 5 to fix before any code is written

1. **C1** — Resolve command/event split or admit the event log stores commands.
2. **C2** — Remove VM self-destruct timer; make Provisioner agentic.
3. **C5** — Add `aborted` state and escape transitions to state machine code.
4. **M5** — Define schemas for all 28 effect types (half are undefined).
5. **M2** — Add `JobStarted` effect so we can track running vs queued jobs.
