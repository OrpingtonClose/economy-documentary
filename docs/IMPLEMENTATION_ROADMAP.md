> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Documentary Pipeline — Full Implementation Roadmap

Tagged `architecture-sound` on commit 5ef7b11.

**Architectural invariants (non-negotiable):**
- Agents are stateless. No shared module variables. No env-var passing. No blackboard. The OTIO file on disk is the ONLY shared state.
- The agent that produces the media also controls the infrastructure.
- Agents use raw LLM reasoning with vast CLI output + Letta-based memory for provisioning decisions.
- Preference Ledger is deferred — hard problem, natural fit when escalation reaches the human, done last.

---

## Stage 1: Stateless OTIO File Protocol

**Purpose:** Fix agent communication. The foundation everything else depends on.

**Why it exists:** The pipeline breaks because agents can't find each other's data. Scenario writes to an in-memory OTIOStateManager; audio can't see it. The fix: the OTIO file on disk is the only shared state.

**New modules:**
- `tools/otio_file_ops.py` — `otio_read()`, `otio_write()`, `otio_read_modify_write()`, `OTIOFileLock`, `resolve_timeline_path()`
- `tools/otio_metadata.py` — `read_pipeline_metadata()`, `write_pipeline_metadata()`, `metadata_key_exists()`
- `tools/otio_lifecycle.py` — `get/set_otio_lifecycle_state()`, `begin/end_escalation()`, `guard_mutation()`

**Deletions:**
- `_otio_state_manager` module variable in `otio_agent.py`
- `set_otio_manager()` function
- `os.environ["_timeline_path"]` in `run_strands.py`
- `threading.Lock()` in `otio_tools.py` → replaced by `fcntl.flock()`

**Modifications:**
- All `_tool_*` functions in `otio_agent.py`, `audio_provisioner_agent.py`, `video_provisioner_agent.py` → use `resolve_timeline_path()` + `otio_file_ops`
- `run_strands.py` → writes `pipeline_manifest.json`, sets `PIPELINE_DIR`, passes `_timeline_path` via `invocation_state`
- `graph_pipeline.py` → adds OTIO as validation gate node
- `callbacks/otio_state.py` → file-based guard fallback

**Graph topology:**
```
scenario → otio_gate → audio → otio_gate → video → otio_gate
```

**Key invariants:**
- Every tool function takes `timeline_path` as parameter (resolved from manifest)
- Every mutation goes through `otio_read_modify_write()` with `fcntl.flock(LOCK_EX)`
- Every write is atomic (temp file + rename)
- No caching — every tool reads the file fresh
- `PIPELINE_DIR` is the only env var, set once before forking

**Test strategy:**
- Unit: atomic write, concurrent RMW, manifest discovery
- Integration: cross-process locking, full pipeline run
- Regression: ADK pipeline still works (deprecated path)

**Dependencies:** None. This is the foundation.

---

## Stage 2: Gates, Validation, and OTIO Lifecycle

**Purpose:** Enforce stage boundaries. The pipeline must not silently "complete" with missing output.

**Why it exists:** Currently the pipeline says "completed" when only scenario produced data. Gates prevent this — each stage must pass validation before the next stage runs.

**Gate specifications:**

| Gate | After stage | Checks |
|------|-------------|--------|
| Gate One | Scenario | `scenes` exists in OTIO metadata, valid structure, duration sums match target |
| Gate Two | Audio | Narration clips exist in A1_Narration track, alignment data present, timing within tolerance. **Draft→authoritative transition happens here.** |
| Gate Three | Video (visual planning) | `visual_concepts` exists for all scenes, LoRA consistency, style matches visual_style |
| Gate Four | Video (production) | Video clips exist in V1_Video track, no gaps, QA passed, duration conservation |

**OTIO lifecycle state machine:**
```
draft ──Gate Two passes──→ authoritative
authoritative ──escalation opens──→ draft (escape hatch)
draft ──escalation closes──→ authoritative (re-crystallize)
```

- `guard_mutation()`: in draft state, mutations always allowed. In authoritative state, mutations only allowed during escalation window.
- All clip writes after Gate Two go through `guard_mutation()`.
- History recorded in `timeline.metadata["documentary"]["state_history"]`.

**Gate node implementation:**
- OTIO gate is a Strands Agent node in the graph
- Reads the OTIO file, validates, writes results to metadata
- If validation fails: writes `pipeline_status.error` to OTIO, graph routes backward via conditional edge
- If validation passes: returns summary for next stage

**Modifications:**
- `graph_pipeline.py` — gate edges, backward edge conditions read from OTIO file
- `otio_lifecycle.py` — `set_otio_lifecycle_state()` called by Gate Two
- `run_strands.py` — gate node configuration

**Key invariants:**
- No stage runs without its gate passing
- Draft→authoritative is a one-way door (except via escalation)
- Gate failures are written to OTIO metadata, not agent state
- Backward edge conditions read from the OTIO file

**Test strategy:**
- Unit: each gate's validation logic
- Unit: lifecycle state transitions
- Integration: gate failure → backward edge → recovery
- Property: pipeline never "completes" with missing data

**Dependencies:** Stage 1 (file protocol)

---

## Stage 3: Recovery Ladders

**Purpose:** Handle failures intelligently. Different media types need different recovery strategies.

**Why it exists:** Currently, failures are silent or fatal. The old architecture specifies per-medium ladders with asymmetric retry budgets calibrated to cost and role.

**Audio ladder (PERMISSIVE — diagram 2):**
Reconciliation IS the mechanism that produces the authoritative OTIO. Wide budgets at low tiers.

| Tier | Name | Budget | Strategy |
|------|------|--------|----------|
| L0 | FIX | 8 attempts | Reseed TTS, rephrase shorter/longer, adjust inter-block silence |
| L1 | RETRY | 4 attempts | Audio-understanding consultation, multi-shot param perturbations |
| L2 | CREATIVE | 2 attempts | Alternative voices, alternative TTS providers |
| L3 | COLLABORATIVE | 1 attempt | Coordinate across agents, upstream artifacts may change |
| L4 | HUMAN | 1 decision | Dashboard gate |

Stylistic QA invariants (enforced at every tier):
- Uniform LUFS across narration
- Voice continuity between adjacent blocks
- Character voice consistency
- No clicks, no truncated plosives
Media immutability: regenerate, never edit.

**Video content ladder (STRICT — diagram 3):**
One attempt per tier. Immediate escalation. Video is expensive.

| Tier | Name | Budget | Strategy |
|------|------|--------|----------|
| L0 | FIX | 1 attempt | Domain-informed prompt rewrite |
| L1 | RETRY | 1 attempt | Different generation strategy or model variant |
| L2 | CREATIVE | 1 attempt | Alternative approach |
| L3 | COLLABORATIVE | 1 attempt | May reshape clip's plan (duration-preserving) |
| L4 | HUMAN | 1 decision | Dashboard gate |

**Infra ladder (PERMISSIVE — diagram 8):**
Separate from content. Classified by diagnostic agent. Own budget.

| Tier | Name | Budget | Strategy |
|------|------|--------|----------|
| L0 | FIX | 4 attempts | Retry on different healthy worker |
| L1 | RETRY | 2 attempts | Recycle suspect worker, redispatch |
| L2 | CREATIVE | 1 attempt | Scale fleet, hot-swap GPU tier, different region |
| L3 | COLLABORATIVE | 1 attempt | Coordinate with content ladder, down-spec params |
| L4 | HUMAN | 1 decision | Dashboard gate |

**Diagnostic classifier:**
New module: `server/diagnostic_classifier.py`
- Classifies failures as content, infra, or unclear
- Requires 2+ independent signals before condemning a worker
- Single bad clip doesn't condemn a worker; single good clip doesn't exonerate one

**Modifications:**
- `escalation_policy.py` — add `INFRA_LADDER_CONFIG`
- `audio_provisioner_agent.py` — embed ladder logic in instruction + tools
- `video_provisioner_agent.py` — embed ladder logic in instruction + tools
- `graph_pipeline.py` — backward edges manage escalation windows

**Key invariants:**
- Audio ladder is permissive because reconciliation produces the OTIO
- Video ladder is strict because generation is expensive
- Infra ladder is separate — infra failures don't consume content budget
- Diagnostic classifier prevents correlated-failure cascades
- All failures are written to OTIO metadata for recovery routing

**Dependencies:** Stage 1 (file protocol), Stage 2 (gates, lifecycle)

---

## Stage 4: Previews and Critique Substrate

**Purpose:** Feedback loops that make the pipeline intelligent. Agents and humans can see what the pipeline has produced so far.

**Why it exists:** "I don't like this movie" should become actionable before the whole film is generated. The critique substrate gives agents and the escalation supervisor structured feedback.

**Critique substrate (diagram 6):**
- Producers emit artifacts. Critics score them.
- `ArtifactCritiqueRecord` stored in critique store (disk + B2 mirror)
- Fire-and-forget writes — critique never blocks the pipeline
- Escalation supervisor reads the store via read-only tools
- Existing code: `critique/record.py`, `critique/store.py`, `critique/adapters.py` — implemented but not wired

**Preview assemblies (diagram 9):**
- Trigger points: pre-production (audio-only), scene complete, act complete, halfway milestone
- Honest placeholders: missing video = black card + text label, missing audio = silence + caption
- Two audiences: agents (coherence evaluator, scenario director) and humans (dashboard)
- Previews are QA artifacts, not deliverables — they don't advance the pipeline
- Existing code: `previews/builder.py`, `previews/consumers.py`, `previews/preview_triggers.py` — implemented but not wired

**Work packages:**
- A: Critique store wiring — connect existing critics to the store, fire-and-forget writers, read-only supervisor tools
- B: Preview pipeline — wire triggers into graph, connect findings to critique store
- C: Cross-cutting — supervisor reads critique + preview findings, dashboard integration

**Key invariants:**
- Critique writes never raise (fire-and-forget)
- Preview gates never advance the pipeline
- Critique store is append-only
- Escalation supervisor is read-only

**Dependencies:** Stage 1 (file protocol), Stage 2 (gates), Stage 3 (ladders)

---

## Stage 5: Assembly

**Purpose:** Walk the OTIO timeline and produce the final MP4.

**Why it exists:** The pipeline produces clips and narration. Assembly composites them into the deliverable. The assembler reads the OTIO file as law — it doesn't generate, it composites.

**What it does:**
- Reads the authoritative OTIO timeline
- Walks V1_Video, A1_Narration, A2_Music tracks
- ffmpeg-trims, muxes, and concatenates
- Gaps render as black frames
- Narration and music composited against video
- Final file uploads to B2

**Key invariants:**
- Assembly is deterministic — no LLM calls
- Assembly reads the OTIO file, never modifies it
- Assembly is the final stage — runs after Gate Four passes
- No trimming, stretching, or frozen frames (media immutability)

**Dependencies:** Stage 1 (file protocol), Stage 2 (gates, lifecycle)

---

## Stage 6: Dashboard

**Purpose:** OTIO-centered timeline view. Human intervention interface.

**Why it exists:** The old architecture (diagram 10) specifies the dashboard's primary surface is the OTIO timeline rendered visually. Everything else orbits it. The human needs to see the pipeline state to make good L4 decisions.

**What it does:**
- Renders OTIO timeline as visual surface (three tracks to scale: V1, A1, A2)
- Per-slot inline rendering: thumbnails, waveforms, placeholders with ETA
- Red = failed, amber = in progress, green = done
- Click a slot → opens detail panel (artifact history, QA verdicts, reasoning, current rung)
- Play from any point → preview from current state
- Slot-scoped directives — typing while a slot is selected scopes the directive
- Proactive L4: halt-anywhere button, directive input without halt

**Key invariants:**
- OTIO timeline is the centerpiece, not a peripheral panel
- Continuity: the human's understanding stays synchronized with pipeline state
- Slot selection is the scope hint for Preference Ledger directives
- Dashboard reads from OTIO file and critique store, never writes directly

**Dependencies:** Stage 1 (file protocol), Stage 4 (previews), Stage 2 (gates — gate events appear on dashboard)

---

## Stage 7: Preference Ledger

**Purpose:** Intent SSOT. Scoped preference records. Surgical re-manifestation.

**Why it exists:** The pipeline is a running manifestation of a scoped Preference Ledger, not a flat prompt. L4 human directives are parsed into scoped records. When the ledger changes, the consistency checker detects the new revision, the impact analyzer scope-matches, and invalidated artifacts are surgically re-manifested.

**Why it's last:** The Preference Ledger is a hard problem. It requires:
- A Preference Interpreter agent that parses free-form directives into scoped records
- A consistency checker that fires on every stage boundary and tool call
- An impact analyzer that walks the ledger diff and scope-matches
- A re-manifestation planner that produces a minimal DAG of regeneration tasks
- A plan validator that checks against all pipeline invariants
- Wide combinations of agents working in tandem

This is the most complex component. It naturally fits when the escalation ladder reaches the human — the human's directive becomes a ledger record, the pipeline re-manifests accordingly.

**Key invariants:**
- L0–L3 never write to the ledger. Only L4 does.
- The original prompt is parsed into R0 records at run start — no special-cased "prompt" artifact.
- Scope is hierarchical: global > stage > scene > block > element
- More specific scope wins; more recent within scope wins
- Hard polarities dominate soft; two hard records that contradict → re-escalate to human
- Re-manifestation is surgical — narrow scope invalidates narrow artifacts

**Dependencies:** All previous stages. The ledger is the capstone.

---

## Dependency Graph

```
Stage 1: File Protocol
  ├── Stage 2: Gates & Lifecycle
  │     ├── Stage 3: Recovery Ladders
  │     │     └── Stage 4: Previews & Critique
  │     └── Stage 5: Assembly
  ├── Stage 6: Dashboard
  │     └── Stage 7: Preference Ledger
  └── (all stages depend on Stage 1)
```

## Implementation Order

```
1 → 2 → 3 → 4 → 5 → 6 → 7
           ↘        ↗
            5 can start after 2
            6 can start after 1 (incremental)
            7 must be last
```

## What Already Exists (don't rebuild)

| Component | Location | Status |
|-----------|----------|--------|
| Critique store | `critique/record.py`, `store.py`, `adapters.py` | Implemented, not wired |
| Preview builder | `previews/builder.py`, `consumers.py`, `preview_triggers.py` | Implemented, not wired |
| Timeline guardian | `callbacks/timeline_guardian.py` | Implemented, needs merge into gate |
| Recovery agents | `recovery_agents.py` (RecoveryAgent, RecoveryDecision) | Implemented, extend for ladders |
| Worker provisioner | `worker_provisioner.py` | Implemented, CLI functions used by merged agents |
| Fleet coordinator | `fleet/coordinator.py` | Implemented, used by video agent |
| Approval gates | `callbacks/approval_gate.py` | Implemented, needs conversion to Strands hooks |
| OTIO manager | `strands_agents/otio_manager.py` | Implemented, marked for deprecation after Stage 1 |

## What Gets Deleted (across all stages)

| Component | Stage | Reason |
|-----------|-------|--------|
| `_otio_state_manager` | 1 | Replaced by file protocol |
| `set_otio_manager()` | 1 | Replaced by file protocol |
| `os.environ["_timeline_path"]` | 1 | Replaced by manifest |
| `threading.Lock()` in otio_tools | 1 | Replaced by fcntl.flock |
| `OTIOStateManager` (eventually) | 1→2 | Deprecated in 1, removed when ADK path gone |
| `StatePropagationHook` | 1 | Already deleted |
| 4 contract hooks | 1 | Already deleted |
| Blackboard state pattern | 1 | OUT — hard breach |
