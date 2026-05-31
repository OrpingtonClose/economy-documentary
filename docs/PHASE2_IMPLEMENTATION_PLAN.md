> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Phase 2 Implementation Plan — Gates, Validation, and Recovery Ladders

This document specifies the implementation of Phase 2: the **gates, validation, and recovery ladders** that sit between pipeline stages. It builds on Phase 1's stateless OTIO file protocol and codifies the architecture described in `docs/ARCHITECTURE_DIAGRAMS.md` (diagrams 1–10) into concrete files, functions, integration points, and test strategies.

---

## Table of Contents

1. [Gate Validation Logic](#1-gate-validation-logic)
2. [OTIO Lifecycle State Machine](#2-otio-lifecycle-state-machine)
3. [Audio Recovery Ladder (Permissive)](#3-audio-recovery-ladder-permissive)
4. [Video Recovery Ladder (Strict One-Shot)](#4-video-recovery-ladder-strict-one-shot)
5. [Infra Escalation Ladder](#5-infra-escalation-ladder)
6. [Graph Topology with Backward Edges](#6-graph-topology-with-backward-edges)
7. [Implementation Map](#7-implementation-map)
8. [Test Strategy](#8-test-strategy)

---

## 1. Gate Validation Logic

### 1.1 Four Gates

The pipeline has four stage-boundary gates, each checking specific invariants before allowing the pipeline to advance. Gates are implemented as `after_agent_callback` functions wired into the Strands Graph nodes.

| Gate | Location | Checks | Failure Action |
|------|----------|--------|----------------|
| **Gate One** (scenario) | After Scenario Agent | Track structure (V1_Video, A1_Narration, A2_Music) exists; scenes JSON is valid and non-empty; style_lock is set | Enter recovery ladder (otio policy) |
| **Gate Two** (narration + authoritative OTIO) | After Audio Agent | Every narration clip has valid WAV + source_range > 0; WhisperX alignment present; stylistic QA passed; narration reconciliation passed; timeline crystallises to authoritative | Enter audio recovery ladder; block crystallisation on violation |
| **Gate Three** (visual plan) | After Visual Direction Agent | Every video gap has prompt + lora_id metadata; style_lock positive_fragment applied | Enter recovery ladder (otio policy) |
| **Gate Four** (clips) | After Production Agent | Every video clip has MP4 + source_range > 0; source_range <= available_range; all placeholder gaps replaced; audio-video timing consistent per scene | Enter video recovery ladder; extension-clip escalation for shortfalls |

### 1.2 Gate Implementation

**Current state**: `server/callbacks/timeline_guardian.py` already implements all four validators as `_validate_scenario`, `_validate_audio`, `_validate_visual_direction`, `_validate_production`, plus `_validate_assembly` for the final gate. The `timeline_guardian_callback` dispatches on `state["pipeline_phase"]`.

**Phase 2 changes**:

1. **Wire gate outcomes into the recovery ladder**. Currently the guardian raises `RuntimeError` on failure. Phase 2 changes this to call `escalate_pipeline_error()` with the correct `agent_policy_type` based on the failing phase:
   - `scenario` → `agent_policy_type="otio"`
   - `audio` → `agent_policy_type="audio"`
   - `visual_direction` → `agent_policy_type="otio"`
   - `production` → `agent_policy_type="video"`

2. **Add crystallisation guard to Gate Two**. The `authoritative_transition_callback` in `server/callbacks/otio_state.py` already checks `_stylistic_qa_passed` and `_narration_reconciliation_passed` before crystallising. Phase 2 ensures these keys are written by the audio agent's callback chain (not just by the guardian).

3. **Add per-moment validation hooks**. The validators in `server/tools/otio_moments.py` (`validate_audio_duration_vs_scene_target`, `validate_video_duration_vs_audio`) run immediately after each artifact is persisted. Phase 2 wires their failure outputs into the appropriate recovery ladder *before* the stage completes, catching timing drift early.

### 1.3 Gate Flow Diagram

```
Stage completes
  → Timeline Guardian callback runs
    → Phase-specific validator checks OTIO
      → PASS: clear state["otio_violation"], continue
      → FAIL: set state["otio_violation"], call escalate_pipeline_error()
        → Recovery ladder runs (L0–L3)
          → Resolved: re-run validator
          → Unresolved: L4 human gate
```

---

## 2. OTIO Lifecycle State Machine

### 2.1 States and Transitions

The OTIO timeline carries a formal `state` field: `draft` → `authoritative`.

```
                    ┌──────────────────────────────────────────┐
                    │                                          │
    [Stage One]     │  [REPLACE/EXTEND escalation]             │
    create_timeline │  reset_to_draft()                        │
          │        │        │                                  │
          ▼        │        ▼                                  │
      ┌────────┐   │   ┌────────┐     end_of_audio     ┌──────────────┐
      │ DRAFT  │───┴──▶│ DRAFT  │─────────────────────▶│ AUTHORITATIVE│
      └────────┘       └────────┘                       └──────────────┘
                                                          │ ▲
                                                          │ │ (idempotent)
                                                          ▼ │
                                                    [already authoritative]
```

**Key rules**:
- Draft is born at timeline creation (Stage One).
- Authoritative crystallises at end of audio (Stage Two) — ONLY when both timing reconciliation AND stylistic QA pass.
- Once authoritative, downstream stages (visual direction, production, assembly) may NOT mutate the authoritative baseline.
- The ONLY way to revert to draft is via `reset_to_draft()` called during a REPLACE/EXTEND escalation.
- The `authoritative_transition_callback` is idempotent — calling it twice is a no-op.

### 2.2 Mutation Guard

The `guard_authoritative_mutation()` function in `server/callbacks/otio_state.py` enforces the mutation guard:

- **Raises `OtioStateViolation`** if any function attempts to mutate authoritative OTIO without an open escalation.
- **Escalation escape hatch**: `begin_escalation(state, escalation_type="REPLACE"|"EXTEND", ...)` opens a window that permits mutation. Must be paired with `end_escalation()`.
- **Persistent on disk**: The state is stamped onto the OTIO file's root metadata so it survives process restarts.

### 2.3 Phase 2 Changes

The state machine is already implemented. Phase 2 adds:

1. **Enforcement in all mutating OTIO tools**. Every function in `server/tools/otio_tools.py` that modifies the timeline (e.g., `add_clip`, `clear_narration_track`) must call `guard_authoritative_mutation()` at the top. Currently some tools do this; Phase 2 audits and completes coverage.

2. **Escalation-aware backward edges**. When the graph routes a failure back to the audio stage (backward edge), it must call `begin_escalation(state, escalation_type="REPLACE", ...)` before re-running audio, and `end_escalation(state)` after audio completes. The `authoritative_transition_callback` already closes the escalation window on success.

3. **Dashboard event on crystallisation**. The `set_otio_state()` function already emits an AG-UI event when the timeline crystallises (`emit_otio_authoritative`). Phase 2 ensures this is wired into the SSE stream so the dashboard drops its reconciliation overlay.

---

## 3. Audio Recovery Ladder (Permissive)

### 3.1 Design Rationale

Audio reconciliation IS the mechanism by which the authoritative OTIO is born (diagram 2). Abandoning L0 early would starve the timeline of the narration it needs. Therefore the audio ladder is **permissive** at low tiers — wide retry budgets, many attempts per block.

### 3.2 Budget Table

| Tier | Label | Attempts | Strategy |
|------|-------|----------|----------|
| L0 FIX | WIDE | 8 | Domain specialist rewrites narration text; reseed TTS; alternate reference sample; rephrase shorter/longer; insert/remove breath phrase; split/merge blocks; adjust inter-block silence |
| L1 RETRY | GENEROUS | 5 | Audio-understanding consultation; multi-shot across audio models; diagnose what changed vs target; bounded parameter perturbations |
| L2 CREATIVE | NARROW_MULTI | 3 | Bounded exploration of alternative voices, TTS providers within speaker-role frame; external MCPs/LLMs as consultants |
| L3 COLLABORATIVE | BOUNDED | 2 | Frame becomes negotiable; Scenario, Visual, Audio, Timeline Guardian may alter upstream artifacts; no model swaps |
| L4 HUMAN | SINGLE | 1 | Dashboard gate; Preference Interpreter parses directive into scoped records |

### 3.3 Canonical Configuration

Defined in `server/escalation_policy.py` as `AUDIO_LADDER_CONFIG`:

```python
AUDIO_LADDER_CONFIG = LadderBudgetConfig(
    ladder_id="audio_content",
    medium="audio",
    discipline=LadderDiscipline.PERMISSIVE,
    budgets={
        RecoveryLevel.FIX: RecoveryBudget.WIDE,        # 8 attempts
        RecoveryLevel.RETRY: RecoveryBudget.GENEROUS,   # 5 attempts
        RecoveryLevel.CREATIVE: RecoveryBudget.NARROW_MULTI,  # 3 attempts
        RecoveryLevel.COLLABORATIVE: RecoveryBudget.BOUNDED,   # 2 attempts
        RecoveryLevel.HUMAN: RecoveryBudget.SINGLE,     # 1 attempt
    },
)
```

### 3.4 Agent Assignments

| Tier | Agent | File | Tools |
|------|-------|------|-------|
| L0 | `TTSUnitAgent` | `server/recovery_agents.py` | `analyse_timing`, `rewrite_narration`, `get_actual_durations`, `get_provision_trace`, `suggest_provisioning_strategy`, `escalate_to_scenario`, `escalate_to_human` |
| L1 | (none — TTSUnitAgent handles L0 only; L1+ fall through to legacy) | | |
| L2 | (none — legacy creative amendments) | | |
| L3 | (none — legacy collaborative) | | |
| L4 | Human gate via AG-UI | | |

**Note**: The current `AUDIO_UNIT_AGENTS` dict maps `{0: TTSUnitAgent()}`. Levels 1–3 are not populated, so the agent ladder falls through to the supervisor consultation path. Phase 2 should populate L1–L3 with the appropriate agents from `recovery_agents.py`:

```python
AUDIO_UNIT_AGENTS = {
    0: TTSUnitAgent(),
    1: RetryAgent(),
    2: CreativeAgent(),
    3: CollaborativeAgent(),
}
```

### 3.5 Stylistic QA Invariants

Every narration block must pass stylistic QA before exiting L0, regardless of timing:

- **Uniform LUFS** across all narration blocks
- **Voice continuity**: no jarring register shifts between adjacent blocks of the same speaker role
- **Character voice consistency**: same speaker role → same voice identity across the whole film
- **Peak-limiter compliance**: no clicks, no truncated plosives, no hiss-floor discontinuities

A block that passes timing but fails stylistic QA does NOT exit L0 — it re-enters the ladder with the invariant violation as the failure signal.

A Preference Ledger record with scope covering a block can deliberately override a stylistic invariant for that block only.

### 3.6 Media Immutability Invariant

No trimming, no stretching, no frozen frames. Retries are always **REGENERATE**, never edit.

---

## 4. Video Recovery Ladder (Strict One-Shot)

### 4.1 Design Rationale

Video generation is expensive (LTX-2.3 on high-VRAM GPUs, multi-minute per clip). Permissive retries at any tier would blow cost budgets without improving quality signal — marginal win-rate per additional retry at the same tier drops quickly. Therefore the video content ladder is **strict one-shot per tier**: each tier gets exactly one attempt; failure escalates immediately.

### 4.2 Budget Table

| Tier | Label | Attempts | Strategy |
|------|-------|----------|----------|
| L0 FIX | SINGLE | 1 | Domain-informed prompt rewrite based on QA rejection feedback |
| L1 RETRY | SINGLE | 1 | Different generation strategy or model variant |
| L2 CREATIVE | SINGLE | 1 | Alternative approach (different LoRA, different prompt structure) |
| L3 COLLABORATIVE | SINGLE | 1 | Coordinated attempt that may reshape clip's plan (duration-preserving, OTIO is law) |
| L4 HUMAN | SINGLE | 1 | Dashboard gate |

### 4.3 Canonical Configuration

Defined in `server/escalation_policy.py` as `VIDEO_LADDER_CONFIG`:

```python
VIDEO_LADDER_CONFIG = LadderBudgetConfig(
    ladder_id="video_content",
    medium="video",
    discipline=LadderDiscipline.STRICT_ONE_SHOT,
    budgets={
        RecoveryLevel.FIX: RecoveryBudget.SINGLE,
        RecoveryLevel.RETRY: RecoveryBudget.SINGLE,
        RecoveryLevel.CREATIVE: RecoveryBudget.SINGLE,
        RecoveryLevel.COLLABORATIVE: RecoveryBudget.SINGLE,
        RecoveryLevel.HUMAN: RecoveryBudget.SINGLE,
    },
)
```

### 4.4 Runtime Enforcement

The `RecoveryPolicy._is_strict_one_shot()` method returns `True` for video policies. The execution engine in `_execute_with_agents()` and `_run_agent_ladder()` asserts:

```python
if strict and budget != 1:
    raise RuntimeError(
        f"ARCH-D2 violation: strict one-shot ladder returned "
        f"budget={budget} at tier L{level}..."
    )
```

This means a second failure at the same tier is **forbidden** — the ladder escalates immediately to the next tier, giving the next-higher-authority agent a genuinely different strategy space.

### 4.5 Agent Assignments

| Tier | Agent | File | Tools |
|------|-------|------|-------|
| L0 | `VideoUnitAgent` | `server/recovery_agents.py` | `rewrite_visual_prompt`, `check_lora_capabilities`, `get_provision_trace`, `suggest_provisioning_strategy`, `escalate_to_scenario`, `escalate_to_human` |
| L1 | `RetryAgent` | `server/recovery_agents.py` | `check_service_health`, `analyse_error_pattern`, provisioner tools |
| L2 | `CreativeAgent` | `server/recovery_agents.py` | `suggest_alternative`, `list_available_models`, provisioner tools |
| L3 | `CollaborativeAgent` | `server/recovery_agents.py` | `request_from_agent`, `get_pipeline_state_summary` |
| L4 | Human gate via AG-UI | | |

**Current state**: `VIDEO_UNIT_AGENTS = {0: VideoUnitAgent()}`. Phase 2 should populate:

```python
VIDEO_UNIT_AGENTS = {
    0: VideoUnitAgent(),
    1: RetryAgent(),
    2: CreativeAgent(),
    3: CollaborativeAgent(),
}
```

### 4.6 Content vs Infra Classification

Every clip failure is classified by the **diagnostic classifier** (diagram 8) before any content budget is charged:

- **Content fail** → enters the video content ladder (this section)
- **Infra fail** → enters the infra ladder (section 5) — own budget
- **Unclear** → short diagnostic run until reclassified

An OOM on worker A that succeeds on worker B is NOT a content L0 failure — it's an infra failure.

### 4.7 Extension Clip Escalation

When `validate_video_duration_vs_audio()` detects a video < audio shortfall, `fire_extension_escalation()` is called. This routes to the production supervisor, which picks from the action menu:

- `generate_extension_clip` — generate additional video to fill the gap
- `accept_shortfall` — accept the degraded output
- `abort` — stop the pipeline

Extension clips are constrained by the LTX 10-second cap (`LTX_CAP_SEC = 10.0`).

---

## 5. Infra Escalation Ladder

### 5.1 Design Rationale

Infrastructure failure is an escalation axis in its own right, orthogonal to content failure. Worker health is inferred from infra observation, never from job outcomes — a single failed clip does not condemn a worker, and a single good clip does not exonerate one.

### 5.2 Failure Classes

| Class | Signature | Examples |
|-------|-----------|----------|
| Worker death | Preemption, OOM, driver reset | `nvidia-smi` shows 0MB; process gone |
| Cold-start failure | Image pull, weight load | Docker pull timeout; model download 404 |
| Network partition | Provider outage, storage unreachable | Vast.ai API 503; B2 upload timeout |
| VRAM exhaustion | Model swap OOM | CUDA OOM on model load; fragmentation |
| Thermal throttle | GPU temp > 90°C | `nvidia-smi` shows thermal throttle active |
| Auth revocation | API key revoked, billing guard | 401 from Vast.ai; credits exhausted |

### 5.3 Budget Table

| Tier | Strategy | Budget |
|------|----------|--------|
| L0 FIX | Retry same job on a different healthy worker | Per-fleet (not per-clip) |
| L1 RETRY | Recycle suspect worker; redispatch in parallel | Per-worker |
| L2 CREATIVE | Scale fleet; hot-swap GPU tier (within VRAM floor); change region/provider | Per-role |
| L3 COLLABORATIVE | Coordinate with content ladder; down-spec params; negotiate with budget guard | Per-run |
| L4 HUMAN | Same L4 gate as content L4 | Single decision |

### 5.4 Implementation

The infra ladder is NOT yet implemented as a separate `LadderBudgetConfig`. Phase 2 adds:

1. **`INFRA_LADDER_CONFIG`** in `server/escalation_policy.py`:

```python
INFRA_LADDER_CONFIG = LadderBudgetConfig(
    ladder_id="infra",
    medium="infra",
    discipline=LadderDiscipline.PERMISSIVE,  # infra is permissive — cheap to retry on different workers
    budgets={
        RecoveryLevel.FIX: RecoveryBudget.GENEROUS,    # 5 attempts — try different workers
        RecoveryLevel.RETRY: RecoveryBudget.NARROW_MULTI,  # 3 — recycle workers
        RecoveryLevel.CREATIVE: RecoveryBudget.BOUNDED,    # 2 — scale fleet
        RecoveryLevel.COLLABORATIVE: RecoveryBudget.BOUNDED, # 2 — coordinate with content
        RecoveryLevel.HUMAN: RecoveryBudget.SINGLE,         # 1
    },
)
```

2. **Diagnostic classifier** in a new file `server/diagnostic_classifier.py`:

```python
def classify_failure(
    error: Exception,
    job_result: Optional[dict] = None,
    infra_signals: Optional[dict] = None,
) -> str:
    """Classify a clip failure as 'content', 'infra', or 'unclear'.

    Requires 2 independent signals before condemning a worker:
    - Job failure AND infra_agent CUDA error
    - Prevents correlated-failure cascades where one bad prompt
      takes down the whole fleet.
    """
```

3. **Infra-specific agents** in `server/recovery_agents.py`:

```python
class InfraFixAgent(RecoveryAgent):
    """L0: Retry on a different healthy worker in the fleet."""

class InfraRetryAgent(RecoveryAgent):
    """L1: Recycle suspect worker; redispatch in parallel."""

class InfraCreativeAgent(RecoveryAgent):
    """L2: Scale fleet; hot-swap GPU tier; change region/provider."""
```

4. **Single-failure-doesn't-condemn invariant**: The coordinator requires multiple INDEPENDENT signals (job failure AND infra_agent CUDA error) before marking a worker bad.

### 5.5 Worker Health Inference

Workers are provisioned lazily during CPU-bound script phases. Health is monitored by the `infra_agent` daemon:

- Reads GPU telemetry (nvidia-smi)
- Checks process health
- Checks network status
- Reports to fleet coordinator for health-aware dispatch

---

## 6. Graph Topology with Backward Edges

### 6.1 Current Graph

The Strands Graph in `server/strands_agents/graph_pipeline.py` has:

**Forward edges** (deterministic):
```
Scenario → Audio → Video
```

**Backward edges** (conditional recovery):
```
Audio → Scenario  (when _needs_scenario_retry)
Video → Audio     (when _needs_audio_retry)
```

### 6.2 Phase 2 Backward Edge Extensions

The architecture diagrams specify universal back-edges: any stage may drive re-manifestation of any earlier stage. Phase 2 extends the backward edges to cover all recovery paths:

| Backward Edge | Condition | Escalation Type | OTIO State Change |
|---------------|-----------|-----------------|-------------------|
| Audio → Scenario | Timing reconciliation fails after L3 | REPLACE | `reset_to_draft()`, `begin_escalation("REPLACE")` |
| Video → Audio | Video alignment off after L3 | EXTEND | `begin_escalation("EXTEND")` |
| Video → Scenario | Visual plan fundamentally broken after L3 | REPLACE | `reset_to_draft()`, `begin_escalation("REPLACE")` |

### 6.3 Recovery Shell

The `RecoveryShell` class wraps the graph invocation:

1. Catches `RuntimeError` from fail-fast Graph
2. Classifies failure via `_classify_failure()` — extracts the failed node name
3. Writes recovery context (`_recovery_target`, `_recovery_reason`) to state
4. Re-invokes the Graph so backward edges can route to the right recovery node

**Phase 2 changes**:

1. **Add more backward edge conditions**:

```python
def _needs_scenario_retry(state) -> bool:
    """Backward edge: audio/video → scenario when upstream fix needed."""
    return state.get("_recovery_target") == SCENARIO

def _needs_audio_retry(state) -> bool:
    """Backward edge: video → audio when alignment is off."""
    return state.get("_recovery_target") == AUDIO

def _needs_video_retry(state) -> bool:
    """Backward edge: assembly → video when clips are missing."""
    return state.get("_recovery_target") == VIDEO
```

2. **Add OTIO state transitions to backward edges**. When a backward edge routes back to audio (or scenario), the graph must:
   - Call `begin_escalation(state, escalation_type="REPLACE"|"EXTEND", reason=..., opened_by="recovery_shell")`
   - Re-run the target stage
   - The `authoritative_transition_callback` closes the escalation window on success

3. **Integrate with the Preference Ledger**. When L4 (human) writes a directive, the Preference Interpreter parses it into scoped records, the consistency checker detects the new revision, and the impact analyzer may invalidate artifacts belonging to any earlier stage, triggering surgical re-manifestation.

### 6.4 Graph Execution Flow with Recovery

```
RecoveryShell.run(task)
  for attempt in range(max_retries + 1):
    try:
      result = await graph.invoke_async(task)
      return result
    except RuntimeError as exc:
      failed_node = _classify_failure(exc)
      state["_recovery_target"] = failed_node
      state["_recovery_reason"] = str(exc)
      # If routing back to audio/scenario, open escalation window
      if failed_node in (SCENARIO, AUDIO):
        begin_escalation(state, escalation_type="REPLACE",
                         reason=str(exc), opened_by="recovery_shell")
```

---

## 7. Implementation Map

### 7.1 Files to Modify

| File | Changes | Priority |
|------|---------|----------|
| `server/escalation_policy.py` | Add `INFRA_LADDER_CONFIG`; register in `LADDER_CONFIGS` | P0 |
| `server/recovery_agents.py` | Populate `AUDIO_UNIT_AGENTS` and `VIDEO_UNIT_AGENTS` with L1–L3 agents; add `InfraFixAgent`, `InfraRetryAgent`, `InfraCreativeAgent`; add `INFRA_AGENTS` dict | P0 |
| `server/recovery.py` | Add `_make_infra_agent_policy()` factory; register in `_run_agent_ladder()` policy factories | P0 |
| `server/callbacks/timeline_guardian.py` | Wire gate failures into `escalate_pipeline_error()` with correct `agent_policy_type` per phase | P0 |
| `server/strands_agents/graph_pipeline.py` | Add `Video → Scenario` backward edge; add OTIO state transitions to backward edge handlers; extend `RecoveryShell` to call `begin_escalation`/`end_escalation` | P1 |
| `server/callbacks/otio_state.py` | Audit all mutating OTIO tools for `guard_authoritative_mutation()` coverage | P1 |
| `server/tools/otio_tools.py` | Add `guard_authoritative_mutation()` calls to all mutating functions | P1 |
| `server/tools/otio_moments.py` | Wire per-moment validator failures into the appropriate recovery ladder | P1 |

### 7.2 New Files to Create

| File | Purpose | Priority |
|------|---------|----------|
| `server/diagnostic_classifier.py` | Classify clip failures as content/infra/unclear; requires 2 independent signals before condemning a worker | P0 |
| `server/infra_agent.py` | Daemon that reads GPU telemetry, process health, network status; reports to fleet coordinator | P1 |
| `server/tests/test_diagnostic_classifier.py` | Unit tests for the classifier | P0 |
| `server/tests/test_escalation_policy.py` | Extend with infra ladder config tests | P0 |
| `server/tests/test_graph_backward_edges.py` | Integration tests for backward edge routing | P1 |
| `server/tests/test_mutation_guard_coverage.py` | Audit test: every mutating OTIO tool calls the guard | P1 |

### 7.3 Integration Points

| Integration Point | From | To | Mechanism |
|-------------------|------|----|-----------|
| Gate failure → Recovery ladder | `timeline_guardian_callback` | `escalate_pipeline_error()` | Phase-specific `agent_policy_type` |
| Per-moment failure → Recovery | `otio_moments.validate_*` | `escalate_pipeline_error()` | `agent_policy_type="audio"` or `"video"` |
| Recovery resolved → Re-validate | `execute_with_recovery` | `timeline_guardian_callback` | Re-run validator after fix |
| L4 human → Preference Ledger | AG-UI directive | `PreferenceInterpreter` → `append records` | Scoped records, consistency checker |
| Backward edge → OTIO state | `RecoveryShell` | `begin_escalation()` / `end_escalation()` | State transitions on graph re-entry |
| Infra classification → Ladder routing | `diagnostic_classifier.classify_failure()` | Content ladder OR infra ladder | Return value determines `agent_policy_type` |
| WhisperX oracle → Projection alarm | `WhisperXOracle.check_projection()` | `fire_reflection_event()` → `supervisor_escalate()` | Running projection vs target |

### 7.4 Function-Level Changes

#### `server/callbacks/timeline_guardian.py`

```python
# Current:
if error:
    raise RuntimeError(error_msg)

# Phase 2:
if error:
    error_msg = f"OTIO VIOLATION [{phase}]: {error}"
    state["otio_violation"] = error_msg
    from recovery import escalate_pipeline_error
    _POLICY_MAP = {
        "scenario": "otio",
        "audio": "audio",
        "visual_direction": "otio",
        "production": "video",
        "assembly": "video",
    }
    response = escalate_pipeline_error(
        operation_name=f"timeline_guardian_{phase}",
        error_msg=error_msg,
        severity="critical",
        default_action="abort",
        diagnosis_hint=f"OTIO timeline validation failed at {phase} phase: {error}",
        agent_policy_type=_POLICY_MAP.get(phase, "generic"),
    )
    if response.get("action") != "skip":
        raise RuntimeError(error_msg)
```

#### `server/recovery_agents.py`

```python
# Current:
AUDIO_UNIT_AGENTS = {0: TTSUnitAgent()}
VIDEO_UNIT_AGENTS = {0: VideoUnitAgent()}

# Phase 2:
AUDIO_UNIT_AGENTS = {
    0: TTSUnitAgent(),
    1: RetryAgent(),
    2: CreativeAgent(),
    3: CollaborativeAgent(),
}

VIDEO_UNIT_AGENTS = {
    0: VideoUnitAgent(),
    1: RetryAgent(),
    2: CreativeAgent(),
    3: CollaborativeAgent(),
}

INFRA_AGENTS = {
    0: InfraFixAgent(),
    1: InfraRetryAgent(),
    2: InfraCreativeAgent(),
    3: CollaborativeAgent(),
}
```

#### `server/recovery.py`

```python
# Add to policy factories:
def _make_infra_agent_policy() -> RecoveryPolicy:
    """Infra operations: L0 retries on different workers."""
    from recovery_agents import INFRA_AGENTS
    from escalation_policy import INFRA_LADDER_CONFIG
    return RecoveryPolicy(
        agents=INFRA_AGENTS,
        ladder_config=INFRA_LADDER_CONFIG,
        level_budget_labels=dict(INFRA_LADDER_CONFIG.budgets),
        retry_backoff_base=5.0,
        escalate_to_human=True,
    )

# Add to _run_agent_ladder policy_factories:
policy_factories = {
    "audio": _make_audio_agent_policy,
    "video": _make_video_agent_policy,
    "production": _make_production_agent_policy,
    "otio": _make_otio_agent_policy,
    "generic": _make_generic_agent_policy,
    "infra": _make_infra_agent_policy,  # NEW
}
```

#### `server/strands_agents/graph_pipeline.py`

```python
# Add Video → Scenario backward edge:
backward_edges = {
    GraphEdge(
        from_node=nodes[AUDIO],
        to_node=nodes[SCENARIO],
        condition=_needs_scenario_retry,
    ),
    GraphEdge(
        from_node=nodes[VIDEO],
        to_node=nodes[AUDIO],
        condition=_needs_audio_retry,
    ),
    # NEW: Video → Scenario when visual plan is fundamentally broken
    GraphEdge(
        from_node=nodes[VIDEO],
        to_node=nodes[SCENARIO],
        condition=_needs_scenario_from_video_retry,
    ),
}

# Extend RecoveryShell to manage OTIO state transitions:
class RecoveryShell:
    async def run(self, task: str) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                result = await self.graph.invoke_async(task)
                # Close any open escalation on success
                # (authoritative_transition_callback does this, but
                # we also do it here as a safety net)
                return result
            except RuntimeError as exc:
                failed_node = self._classify_failure(exc)
                # Open escalation window for backward-edge targets
                if failed_node in (SCENARIO, AUDIO):
                    from callbacks.otio_state import begin_escalation
                    begin_escalation(
                        self._state,
                        escalation_type="REPLACE",
                        reason=str(exc),
                        opened_by=f"recovery_shell_attempt_{attempt+1}",
                    )
                state_overrides["_recovery_target"] = failed_node
                state_overrides["_recovery_reason"] = reason
```

---

## 8. Test Strategy

### 8.1 Unit Tests

| Test File | What It Tests | Key Assertions |
|-----------|---------------|----------------|
| `test_escalation_policy.py` | `LadderBudgetConfig` validation | STRICT_ONE_SHOT rejects non-SINGLE budgets; PERMISSIVE accepts WIDE/GENEROUS; monotone non-increasing enforcement; coverage check for all 5 tiers |
| `test_diagnostic_classifier.py` | `classify_failure()` | Content errors → "content"; infra errors → "infra"; mixed signals → "unclear"; requires 2 independent signals for worker condemnation |
| `test_recovery_agents.py` | Agent decision parsing | Valid JSON → correct RecoveryDecision; invalid JSON → supervisor fallback; tool execution; budget enforcement |
| `test_otio_state.py` | State machine transitions | draft→authoritative only when both QA gates pass; authoritative→draft only via reset_to_draft; mutation guard raises OtioStateViolation; escalation window bypasses guard |

### 8.2 Integration Tests

| Test File | What It Tests | Key Assertions |
|-----------|---------------|----------------|
| `test_graph_backward_edges.py` | Backward edge routing | Audio failure routes to scenario; video failure routes to audio; escalation window opens/closes correctly; OTIO state transitions on re-entry |
| `test_gate_to_ladder.py` | Gate failure → recovery ladder | Each gate failure enters the correct ladder (audio/video/otio); ladder resolution re-validates; L4 human gate works |
| `test_per_moment_validation.py` | Per-moment hooks | Audio duration drift detected immediately; video shortfall fires extension escalation; WhisperX oracle projection alarm fires at 80% threshold |

### 8.3 Property-Based Tests

| Property | How to Test |
|----------|-------------|
| Strict one-shot invariant | For any video policy, `get_level_budget(level) == 1` for all levels |
| Monotone budget | For any ladder, `budget[L_n] >= budget[L_n+1]` for all n |
| Mutation guard coverage | Every mutating OTIO tool function calls `guard_authoritative_mutation()` |
| Single-failure-doesn't-condemn | Worker is only marked bad after 2+ independent failure signals |
| Media immutability | No test path produces trimmed/stretched/frozen-frame media |

### 8.4 End-to-End Test Scenarios

| Scenario | Steps | Expected Outcome |
|----------|-------|-----------------|
| Audio timing drift | Generate narration that runs 20% over target → L0 rewrites → re-TTS → WhisperX measures → passes | Timeline crystallises to authoritative |
| Video QA rejection | Generate clip that fails Bearnaise → L0 rewrites prompt → regenerate → passes | Clip uploaded to B2, OTIO updated |
| Video < audio shortfall | Generate 5s video for 8s narration → extension escalation → supervisor picks `generate_extension_clip` → 3s extension generated → total 8s | Video covers narration |
| Worker death during production | Worker preemption mid-render → diagnostic classifier → infra L0 → retry on different worker | Job completes on alternate worker |
| All L0–L3 exhausted | Force repeated failures → supervisor consultation → L4 human gate | Human sees full diagnostic chain; can approve/skip/abort |
| Backward edge to scenario | Audio L3 collaborative agent requests scenario rewrite → backward edge fires → scenario re-runs → new scenes → audio re-runs | Pipeline completes with revised scenario |

### 8.5 Regression Tests (PAG Run Lessons)

| Bug | Root Cause | Phase 2 Prevention |
|-----|-----------|-------------------|
| #61: Ladder passes buck (round-robin) | Recovery agent returned "escalate" on LLM failure | `_supervisor_fallback_decision()` — never returns "escalate" |
| #73: Zero LLM reasoning | Agent parse failure silently escalated | Supervisor consultation before L4 |
| #76/#77: Inter-agent communication via supervisor | Recovery agents couldn't coordinate | `CollaborativeAgent` with `request_from_agent` tool |
| #82: WhisperX duration oracle | TTS self-report was inaccurate | `WhisperXOracle` — ground truth from WhisperX |
| #84: Per-scene OTIO compliance | Issues discovered only at final assembly | `validate_scene_assembly()` runs after each scene |
| #85: Extension clip escalation | Video < audio discovered too late | `validate_video_duration_vs_audio()` runs after each clip |
| #102: Supervisor invariant | No guarantee of LLM call per escalation | `_consult_supervisor()` always makes at least one LLM call |
| #147: Draft → Authoritative formal state | No lifecycle state on OTIO | `otio_state.py` with `draft`/`authoritative` states |

---

## Appendix A: Recovery Decision Flow

```
Operation fails
  │
  ├─ Is it non-retryable? ──yes──▶ Skip to L4
  │
  ├─ Is it classified as infra? ──yes──▶ Enter infra ladder
  │
  └─ Enter content ladder (audio or video)
       │
       ▼
  L0 FIX (domain specialist)
    │ fix ──▶ apply state_patches, re-run ──▶ success? ──▶ done
    │                                              fail? ──▶ next attempt (if budget)
    │ retry ──▶ re-run as-is ──▶ success? ──▶ done
    │                                   fail? ──▶ next attempt
    │ skip ──▶ accept failure, continue pipeline
    │ abort ──▶ stop pipeline
    │ escalate ──▶ next tier
    │
    ▼ (budget exhausted or escalate)
  L1 RETRY (intelligent retry)
    │ ... same decision shape ...
    ▼
  L2 CREATIVE (alternative strategy)
    │ ... same decision shape ...
    ▼
  L3 COLLABORATIVE (inter-agent coordination)
    │ ... same decision shape ...
    ▼
  L4 HUMAN (AG-UI gate)
    │ approve ──▶ continue
    │ halt ──▶ abort
    │ directive ──▶ Preference Interpreter ──▶ Preference Ledger ──▶ re-manifestation
```

## Appendix B: OTIO State Transition Matrix

| Current State | Event | New State | Guard |
|---------------|-------|-----------|-------|
| (none) | `create_timeline` | `draft` | — |
| `draft` | End of audio + QA pass | `authoritative` | `_stylistic_qa_passed is True` AND `_narration_reconciliation_passed is True` AND `otio_violation is None` |
| `authoritative` | REPLACE/EXTEND escalation | `draft` | `reset_to_draft()` with required reason |
| `authoritative` | Idempotent transition call | `authoritative` | No-op |
| `authoritative` | Mutation attempt (no escalation) | (raises `OtioStateViolation`) | `guard_authoritative_mutation()` |
| `authoritative` | Mutation attempt (escalation open) | `authoritative` | Mutation permitted; escalation window open |

## Appendix C: Budget Summary Table

| Ladder | L0 | L1 | L2 | L3 | L4 | Discipline |
|--------|----|----|----|----|----|----|
| Audio content | WIDE (8) | GENEROUS (5) | NARROW_MULTI (3) | BOUNDED (2) | SINGLE (1) | PERMISSIVE |
| Video content | SINGLE (1) | SINGLE (1) | SINGLE (1) | SINGLE (1) | SINGLE (1) | STRICT_ONE_SHOT |
| Infra | GENEROUS (5) | NARROW_MULTI (3) | BOUNDED (2) | BOUNDED (2) | SINGLE (1) | PERMISSIVE |
