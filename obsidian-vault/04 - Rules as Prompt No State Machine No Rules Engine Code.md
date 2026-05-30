---
{
  "title": "Rules as Prompt (No State Machine, No Rules Engine Code)",
  "section": "4",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[03 - Effect Type Family Complete Schemas|Effect Type Family — Complete Schemas]] | [[00 - Index|Index]] | [[05 - Event Store|Event Store]] ->

# Rules as Prompt (No State Machine, No Rules Engine Code)


There is no state machine and no `RulesEngine` Python class. Prioritization, filtering, and response selection happen **inside the agent via prompt instruction**. The agent scans projections and decides what to do. Rules live in the agent's system prompt, not in code.

This follows the principle: *whenever something can be done via prompt, do so — cut code complexity.*

### 4.1 Agent System Prompt: Embedded Rules

Each agent's `instructions` (system prompt) includes a **RULES block** that tells it how to prioritize situations:

```
=== YOUR ROLE ===
You are the {role} agent. You produce effects. You decide what to do.

=== RULES ===
1. Prioritize safety situations (budget critical, loop detected) above all else.
2. Prioritize blocked situations (stale VM, job queued long) next.
3. Prioritize work situations (dirty block, measurement needed) last.
4. If multiple work situations, pick the one with the lowest slot_id.
5. If no situations apply, the parser extracts NoOp with reason.
6. The parser never extracts effects outside the permitted kinds: {permitted_effects}.

=== CURRENT SITUATIONS ===
{situation_narratives}

=== YOUR MEMORY ===
{memory}

=== AVAILABLE EFFECTS ===
{effect_schema}
```

### 4.2 Emergent Pipeline Phases

| Phase | Emergent Condition | Active Agents |
|---|---|---|
| **INIT** | No `PipelineStarted` effect | None |
| **SCRIPT** | `PipelineStarted` exists, OTIO (OpenTimelineIO; see §6.1.3) has unfilled slots | Scenario |
| **AUDIO_RECONCILE** | OTIO has dirty audio blocks | Audio |
| **VIDEO_PRODUCTION** | All audio clean, video slots unfilled | Video |
| **ASSEMBLY** | All slots filled, final MP4 missing | Assembly |
| **DONE** | Final MP4 exists and validates | None |
| **ABORTED** | `PipelineAborted` emitted | None |

These are not states. They are descriptive labels for human observation. No code enforces transitions — they emerge from what agents do.

### 4.3 Rules Block (Agent System Prompt Text)

Rules live in the agent's system prompt. They are not code. Each agent receives the same RULES block; only the `PERMITTED EFFECTS` section differs by role.

```
=== RULES ===
1. If agent_loop_detected -> parser extracts ClarificationRequest and stop.
2. If pipeline_budget_critical -> parser extracts PipelineAborted and stop.
3. If block_at_max_attempts -> handle escalation (accept, human, abort).
4. If measurement_complete_fail -> requeue with adjusted params.
5. If fresh_dirty_block -> do the work (queue job, measure, judge).
6. If vm_stale -> note it (Provisioner agent reasons about VM cleanup).
7. If noop_all_clean -> the parser extracts NoOp, nothing to do.

Pick the highest-priority rule that applies. Only one action per turn.
```

### 4.4 Maintainer Pattern (External Client Intervention)

**V7.1 architectural decision:** There is no "Maintainer Agent" as an internal
service. Emergency intervention is performed by an **external client** — whether
that is the operator typing `curl` commands, a GUI tool, or a separate
orchestrator process. The core agents (ports 8001–8005, 8081) expose their
`POST /` endpoints to the outside world. When a run is stuck, the external
client POSTs `HumanInstruction` effects directly to the stuck agent's open port.

**When to intervene:**
- Agent loop detected and human cannot determine cause from event stream
- OTIO has accumulated invalid edits that agents keep building on
- A block has exceeded max attempts and escalation requires human judgment
- Pipeline is in an emergent state that no active agent recognizes

**How it works:**
1. External client queries the GSA via `GET /` to see current state.
2. Client constructs a `HumanInstruction` with the target agent, directive, and context.
3. Client POSTs directly to the target agent's `POST /` endpoint (e.g., port 8002 for Audio Agent).
4. The target agent processes the instruction on its next turn; the parser extracts effects.
5. Client monitors GSA state for resolution.

**Why no internal Maintainer Agent:**
- No service discovery needed — agents have fixed ports
- No second coordinator — the operator IS the coordinator during emergencies
- No maintainer bugs, no resource consumption, no false positives
- The pipeline doesn't need to know the maintainer exists

---

