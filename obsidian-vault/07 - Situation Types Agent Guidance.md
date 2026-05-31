---
{
  "title": "Situation Types (Agent Guidance)",
  "section": "7",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[06 - Projections|Projections]] | [[00 - Index|Index]] | [[08 - Agent Architecture pydantic-deep|Agent Architecture — pydantic-deep]] ->

# Situation Types (Agent Guidance)


These are the situation types an agent should look for when scanning the projection bundle received from the Global State Agent. They are not a Python class — they are guidance text embedded in the agent's system prompt. The agent scans the GSA response directly and decides which situations apply.

### 7.1 Situation Types

| Type | Trigger | Description |
|---|---|---|
| `fresh_dirty_block` | Block exists, dirty, attempts < max | New/requeued block needs work |
| `measurement_complete_pass` | Block measured, within tolerance | Block passed reconciliation |
| `measurement_complete_fail` | Block measured, outside tolerance | Block failed, needs retry |
| `block_at_max_attempts` | Block dirty, attempts == max | Exhausted, needs escalation |
| `vm_stale` | VM last_seen > threshold | VM not reporting, may be dead |
| `vm_provision_failed` | `VMProvisionFailed` exists | Could not create VM |
| `job_queued_long` | Job queued > threshold | Job waiting too long for VM |
| `reconciliation_complete_all` | All blocks pass | Audio pipeline done |

| `assembly_ready` | All video approved | Ready for final assembly |
| `pipeline_budget_warning` | Spent > 80% of limit | Warning level |
| `pipeline_budget_critical` | Spent > 95% of limit | Critical, may abort |
| `agent_loop_detected` | Duplicate effects or no progress | Agent stuck |
| `human_instruction_pending` | `HumanInstruction` unread | Human input waiting |
| `noop_all_clean` | Nothing dirty, nothing queued | Idle, waiting |

### 7.2 Narrative Template Format

```
=== SITE: {slot_id} ===
{text_snippet}
TARGET: {target_sec}s | MEASURED: {measured_sec}s | DELTA: {delta_sec}s
ATTEMPTS: {attempts}/{max_attempts} | VERDICT: {verdict}

WHAT'S HAPPENING:
{situation_narrative}
===
```

### 7.3 Rules for Narrative Generation

1. **Dirty blocks get full narrative** — all fields, history, guidance
2. **Clean blocks get one line** — slot_id, verdict, measured duration
3. **Failed blocks get extra context** — previous attempts, error history
4. **Max-attempt blocks get escalation options** — accept, human, abort
5. **VM issues get infrastructure narrative** — not artistic
6. **Budget issues get fiscal narrative** — spent, limit, remaining

---

### 7.4 Situation Narrative Builder (Complete Specification)

The narrative builder is the bridge between projection state [[06 - Projections|[[06 - Projections|§6]]]] and agent LLM prompts. It transforms raw projection data into natural-language narratives that agents consume as their user prompt. This section defines every function, template, and rule.

#### 7.4.1 derive_situations() — Projection → Situation Objects

```python
from dataclasses import dataclass
from typing import Literal


SituationType = Literal[
    "fresh_dirty_block", "measurement_complete_pass", "measurement_complete_fail",
    "block_at_max_attempts", "vm_stale", "vm_provision_failed", "job_queued_long",
    "reconciliation_complete_all",
    "assembly_ready", "pipeline_budget_warning", "pipeline_budget_critical",
    "agent_loop_detected", "human_instruction_pending", "noop_all_clean",
]


@dataclass
class Situation:
    """A single situation detected from projection state."""
    type: SituationType
    priority: int           # 1=highest (safety), 5=lowest (work)
    slot_id: str = ""       # which slot this situation refers to
    facts: dict = None      # template variables

    def __post_init__(self):
        if self.facts is None:
            self.facts = {}
```

```python
# V7.1 fix: Defined here -- was referenced but never shown.
SITUATION_TEMPLATES: dict[str, str] = {
    "fresh_dirty_block": (
        "=== SLOT: {slot_id} ===\n"
        "{text_snippet}\n"
        "TARGET: {scripted_sec}s | ATTEMPTS: {attempts}/{max_attempts}\n"
        "WHAT IS HAPPENING: This block needs audio generation and measurement.\n"
        "WHAT TO DO: Emit QueueJob(job_type=tts, ...) for this block."
    ),
    "measurement_complete_pass": (
        "=== SLOT: {slot_id} === MEASURED: {measured_sec}s | TARGET: {scripted_sec}s\n"
        "Within tolerance. Emit DurationAdjusted to update OTIO."
    ),
    "measurement_complete_fail": (
        "=== SLOT: {slot_id} === MEASURED: {measured_sec}s | TARGET: {scripted_sec}s\n"
        "Outside tolerance. Emit ReconciliationFailed or requeue."
    ),
    "block_at_max_attempts": (
        "=== SLOT: {slot_id} === ATTEMPTS EXHAUSTED: {attempts}/{max_attempts}\n"
        "Emit ReconciliationFailed with duration_unrecoverable."
    ),
    "vm_stale": (
        "VM {vm_id} last_seen={last_seen}s ago. Health check failed.\n"
        "Consider deallocating and re-provisioning."
    ),
    "vm_provision_failed": (
        "VM provision failed: {reason}. Budget impact: ${cost_usd:.2f}\n"
        "Emit VMProvisionFailed and consider alternative GPU."
    ),
    "job_queued_long": (
        "Job {job_id} queued for {wait_sec}s. VM count: {vm_count}.\n"
        "Provisioner should allocate VM or diagnose."
    ),
    "reconciliation_complete_all": (
        "All {total} blocks pass tolerance. Audio pipeline complete.\n"
        "POST wake to Video Agent."
    ),
    "assembly_ready": (
        "All video approved. {slot_count} slots ready for assembly.\n"
        "Emit PipelineComplete when final mux validated."
    ),
    "pipeline_budget_warning": (
        "Budget: ${spent:.2f} / ${cap:.2f} ({pct:.0%}). Approaching limit."
    ),
    "pipeline_budget_critical": (
        "Budget: ${spent:.2f} / ${cap:.2f} ({pct:.0%}). CRITICAL.\n"
        "Emit PipelineAborted or request budget_override."
    ),
    "agent_loop_detected": (
        "LOOP DETECTED: {agent} emitted same effect {count} times.\n"
        "Last effects: {effect_kinds}. Emit ClarificationRequest."
    ),
    "human_instruction_pending": (
        "HUMAN INSTRUCTION for {target_agent}: {instruction_text}\n"
        "Priority: {priority}. Action: {action}."
    ),
    "noop_all_clean": (
        "No active situations. Pipeline state is clean.\n"
        "Emit NoOp and await next wake."
    ),
}

ROLE_INSTRUCTIONS: dict[str, str] = {
    "scenario": (
        "You are the Scenario agent. You write and revise narration scripts.\n"
        "Every block must specify speaker, duration_sec, and scene_num."
    ),
    "audio": (
        "You are the Audio agent. Own narration reconciliation:\n"
        "(1) Queue TTS jobs for scripted blocks. (2) On JobCompleted, measure.\n"
        "(3) Compare measured vs scripted (+-15% or +-0.25s).\n"
        "Within -> DurationAdjusted; outside -> ReconciliationFailed -> requeue.\n"
        "Max 5 attempts per block, $2 TTS budget."
    ),
    "video": (
        "You are the Video agent. Own video generation:\n"
        "Queue LTX jobs for approved audio blocks. Judge quality.\n"
        "Approve -> JobApproved; reject -> JobRequeued."
    ),
    "assembly": (
        "You are the Assembly agent. Final mux and validation.\n"
        "Merge approved clips. Validate dual-threshold tolerance.\n"
        "Emit PipelineComplete on success."
    ),
    "provisioner": (
        "You are the Provisioner. Most intelligence-requiring component.\n"
        "Provision VMs, dispatch jobs, learn from failures.\n"
        "Use bash_command for Vast.ai. Use remember/recall_memory for learning."
    ),
}


def derive_situations(
    projections: GlobalStateResponse,
    role: Literal["scenario", "audio", "video", "assembly", "provisioner"],
    config: Config,
) -> list[Situation]:
    """Scan projections and return all active situations for this agent role.

    Situations are ordered by priority (lowest number first).
    The agent's RULES block (§4.1) tells it which situation to act on.
    """
    situations: list[Situation] = []
    otio = projections.otio
    jobs = projections.jobs
    vms = projections.vms
    state = projections.state
    budget = projections.budget

    # --- Safety (priority 1) ---
    if budget.exceeded:
        situations.append(Situation(
            type="pipeline_budget_critical",
            priority=1,
            facts={"spent_usd": budget.spent_usd, "cap_usd": budget.budget_cap_usd},
        ))
    elif budget.remaining_usd < budget.budget_cap_usd * 0.05:
        situations.append(Situation(
            type="pipeline_budget_warning",
            priority=1,
            facts={"spent_usd": budget.spent_usd, "cap_usd": budget.budget_cap_usd,
                   "remaining_usd": budget.remaining_usd},
        ))

    # --- Blocked / infrastructure (priority 2) ---
    if role == "provisioner":
        for vm in vms.vms.values():
            if vm.status == "active" and vm.observed_status == "not_found":
                situations.append(Situation(
                    type="vm_stale",
                    priority=2,
                    facts={"instance_id": vm.instance_id, "role": vm.role},
                ))
        for job in jobs.jobs.values():
            if job.status == "pending" and job.created_at > 0:
                queued_sec = time.time() - job.created_at
                if queued_sec > config.max_queue_wait_sec:
                    situations.append(Situation(
                        type="job_queued_long",
                        priority=2,
                        facts={"job_id": job.job_id, "queued_sec": int(queued_sec),
                               "slot_id": job.slot_id},
                    ))

    # --- Work (priority 3–5) ---
    if role == "scenario":
        unfilled = [addr for addr, slot in otio.slots.items()
                    if slot.status == "scripted"]
        if unfilled:
            situations.append(Situation(
                type="fresh_dirty_block",
                priority=3,
                facts={"count": len(unfilled), "slots": unfilled[:5]},
            ))

    if role == "audio":
        dirty = [addr for addr, slot in otio.slots.items()
                 if slot.status == "scripted"]  # V7.1: "scripted" is the dirty state
        for addr in dirty:
            slot = otio.slots[addr]
            attempts = jobs.block_attempts.get(addr, 0)
            if attempts >= config.max_attempts_per_block:
                situations.append(Situation(
                    type="block_at_max_attempts",
                    priority=2,
                    slot_id=addr,
                    facts={"slot_id": addr, "attempts": attempts,
                           "max_attempts": config.max_attempts_per_block,
                           "scripted_sec": slot.scripted_sec,
                           "measured_sec": slot.measured_sec},
                ))
            else:
                situations.append(Situation(
                    type="fresh_dirty_block",
                    priority=3,
                    slot_id=addr,
                    facts={"slot_id": addr, "attempts": attempts,
                           "max_attempts": config.max_attempts_per_block,
                           "scripted_sec": slot.scripted_sec,
                           "text_snippet": slot.text[:200]},
                ))

        measured = [addr for addr, slot in otio.slots.items()
                    if slot.status == "measured"]
        for addr in measured:
            slot = otio.slots[addr]
            delta = abs((slot.measured_sec or 0) - slot.scripted_sec)
            tolerance = max(slot.scripted_sec * 0.15, 0.25)
            if delta <= tolerance:
                situations.append(Situation(
                    type="measurement_complete_pass",
                    priority=4,
                    slot_id=addr,
                    facts={"slot_id": addr, "measured_sec": slot.measured_sec,
                           "scripted_sec": slot.scripted_sec, "delta_sec": delta},
                ))
            else:
                situations.append(Situation(
                    type="measurement_complete_fail",
                    priority=3,
                    slot_id=addr,
                    facts={"slot_id": addr, "measured_sec": slot.measured_sec,
                           "scripted_sec": slot.scripted_sec, "delta_sec": delta,
                           "tolerance_sec": tolerance},
                ))

    if role == "video":
        pending_ltx = [j for j in jobs.jobs.values()
                       if j.job_type == "ltx" and j.status in ("pending", "running")]
        if pending_ltx:
            situations.append(Situation(
                type="fresh_dirty_block",
                priority=3,
                facts={"count": len(pending_ltx), "jobs": [j.job_id for j in pending_ltx]},
            ))

    if role == "assembly":
        if otio.dirty_slots == 0 and otio.delivered_slots == otio.total_slots:
            situations.append(Situation(
                type="assembly_ready",
                priority=3,
                facts={"total_slots": otio.total_slots,
                       "duration_sec": otio.duration_sec},
            ))

    # --- No-op (priority 5) ---
    if not situations:
        situations.append(Situation(
            type="noop_all_clean",
            priority=5,
            facts={"current_phase": state.current_phase},
        ))

    situations.sort(key=lambda s: s.priority)
    return situations
```

#### 7.4.2 SITUATION_TEMPLATES — Exact Output Strings

Each `Situation` is rendered through a template. The output is **natural language**, not structured data.

```python
SITUATION_TEMPLATES: dict[SituationType, str] = {
    # Safety
    "pipeline_budget_critical": (
        "🚨 BUDGET CRITICAL: Spent ${spent_usd:.2f} / ${cap_usd:.2f}. "
        "The pipeline has exceeded its budget cap. "
        "You MUST emit PipelineAborted immediately. No other work."
    ),
    "pipeline_budget_warning": (
        "⚠️ BUDGET WARNING: Spent ${spent_usd:.2f} / ${cap_usd:.2f} "
        "(remaining: ${remaining_usd:.2f}). Approaching limit. "
        "Consider cost-saving measures."
    ),

    # Blocked / infrastructure
    "vm_stale": (
        "VM {instance_id} ({role}) is stale — Vast.ai reports it gone "
        "but events say active. The Provisioner should investigate."
    ),
    "job_queued_long": (
        "Job {job_id} (slot {slot_id}) has been queued for {queued_sec}s. "
        "No VM picked it up. The Provisioner may need to allocate more VMs."
    ),

    # Work — dirty blocks
    "fresh_dirty_block": (
        "=== SITE: {slot_id} ===\n"
        "{text_snippet}\n"
        "TARGET: {scripted_sec}s | ATTEMPTS: {attempts}/{max_attempts}\n"
        "WHAT'S HAPPENING: This block needs audio generation and measurement.\n"
        "WHAT TO DO: Emit QueueJob(job_type='tts', ...) for this block."
    ),

    # Work — measurement results
    "measurement_complete_pass": (
        "=== SITE: {slot_id} ===\n"
        "TARGET: {scripted_sec}s | MEASURED: {measured_sec}s | DELTA: {delta_sec}s\n"
        "VERDICT: PASS (within tolerance)\n"
        "WHAT TO DO: Emit DurationAdjusted for this block."
    ),
    "measurement_complete_fail": (
        "=== SITE: {slot_id} ===\n"
        "TARGET: {scripted_sec}s | MEASURED: {measured_sec}s | DELTA: {delta_sec}s\n"
        "TOLERANCE: {tolerance_sec}s\n"
        "VERDICT: FAIL (outside tolerance)\n"
        "WHAT'S HAPPENING: The measured audio is too different from the script target.\n"
        "WHAT TO DO: Emit JobRequeued with adjusted TTS params (speed, voice, or text)."
    ),

    # Escalation
    "block_at_max_attempts": (
        "=== SITE: {slot_id} ===\n"
        "TARGET: {scripted_sec}s | MEASURED: {measured_sec}s\n"
        "ATTEMPTS: {attempts}/{max_attempts} — MAXED OUT\n"
        "WHAT'S HAPPENING: This block has failed reconciliation {attempts} times.\n"
        "WHAT TO DO: Escalate. Options:\n"
        "  1. Accept the mismatch (emit DurationAdjusted with note)\n"
        "  2. Request human guidance (emit ClarificationRequest)\n"
        "  3. Abort the pipeline (emit PipelineAborted)"
    ),

    # Assembly
    "assembly_ready": (
        "All {total_slots} slots are filled with approved media. "
        "Timeline duration: {duration_sec}s.\n"
        "WHAT TO DO: Run ffmpeg to assemble final_documentary.mp4, then emit PipelineComplete."
    ),

    # No-op
    "noop_all_clean": (
        "Nothing requires action. Current phase: {current_phase}. "
        "Emit NoOp with a brief status note."
    ),
}
```

#### 7.4.3 build_narrative() — Situations → User Prompt

```python
def build_narrative(
    situations: list[Situation],
    role: str,
    projections: GlobalStateResponse,
) -> str:
    """Render all situations into the agent's user prompt."""
    parts: list[str] = []

    # Header: what phase and what the agent's job is
    parts.append(f"=== PIPELINE PHASE: {projections.state.current_phase} ===")
    parts.append(f"=== YOUR ROLE: {role.upper()} AGENT ===\n")

    # Situation narratives (already sorted by priority)
    for s in situations:
        template = SITUATION_TEMPLATES[s.type]
        rendered = template.format(**s.facts)
        parts.append(rendered)
        parts.append("")  # blank line between situations

    # Footer: global context (always included)
    parts.append("=== GLOBAL CONTEXT ===")
    parts.append(f"Budget: {projections.budget.summary()}")
    parts.append(f"VMs: {projections.vms.active_count} active")
    parts.append(f"Jobs: {len(projections.jobs.jobs)} total")
    parts.append(f"Latest event sequence: {projections.latest_sequence}")

    return "\n".join(parts)
```

#### 7.4.4 Historical Context Injection (Last 5 Turns)

The agent's `message_history` carries the last 5 turns as memory. These are **not** part of the user prompt — they are passed to `agent.run()` as `message_history` so the LLM sees them as prior conversation turns.

```python
def read_agent_events(
    run_id: str,
    agent: str,
    store: EventStore,
    limit: int = 5,
) -> list[Effect]:
    """Read the last N effects emitted by a specific agent from the JSONL event store.

    V7.1 fix: Uses store.replay() which returns EventRecord objects with typed
    .effect fields. No _parse_payload needed — JSONL stores typed effects.
    O(N) scan; acceptable for documentary runs (500-2000 events).
    """
    records = store.replay(run_id)
    agent_events = [r.effect for r in records if r.effect.agent == agent]
    return agent_events[-limit:]


async def build_memory(
    run_id: str,
    agent: str,
    limit: int = 5,
) -> list[UserMessage]:
    """Fetch the last N effects emitted by this agent and format as memory."""
    events = await read_agent_events(run_id, agent, limit=limit)

    memory: list[UserMessage] = []
    for evt in events:
        ts = datetime.fromtimestamp(evt.timestamp).strftime("%H:%M:%S")
        kind = evt.kind
        payload = evt.model_dump_json(exclude={"effect_id", "timestamp", "agent", "kind"})
        memory.append(UserMessage(
            content=f"[MEMORY {ts}] You emitted {kind}: {payload}"
        ))

    return memory
```

**What memory contains:** Only effects previously emitted by THIS agent. Effects from other agents are invisible (the agent reads them via GSA projections on each turn, not via message history). This prevents stale state — the agent always sees current projections, not stale history.

**Why 5 turns:** Empirically, 5 turns captures the agent's recent reasoning context without overwhelming the token budget. Each memory entry is ~100–300 tokens; 5 entries = ~1K tokens, leaving room for the narrative (~2–5K tokens) and system prompt (~1K tokens).

#### 7.4.5 Memory Persistence and Agent Restart

**Does an agent restart lose its memory? No.** Memory is rebuilt from the JSONL event store on every turn. It is not stored in the agent process. When an agent restarts:

1. The new process receives a `POST /` with `run_id`.
2. The handler calls `build_memory(run_id, agent, limit=5)`.
3. `build_memory` queries the JSONL event store for the last 5 effects emitted by this agent.
4. It reconstructs the exact same memory that the previous process would have built.

**Does behavior change post-restart? No.** The reconstruction is deterministic: same event stream → same 5 most recent effects → same memory messages → same LLM context. The agent's behavior is fully determined by:
- The event stream (durable in JSONL files)
- The GSA projections (rebuilt from the event stream)
- The narrative builder (deterministic function of projections)
- The memory builder (deterministic function of the event stream)
- The system prompt (static per agent role)

**What IS lost on restart:** The pydantic-deep internal `message_history` (the raw LLM request/response pairs from prior turns) is lost. But this is irrelevant — it is not used for decision-making. The agent's "memory" is the last 5 effects from the JSONL event store, not the internal LLM conversation history. pydantic-deep's context compaction operates on the current turn only; prior turns' raw responses are not needed because the agent's decisions are already captured as effects in the event stream.

**Why not persist memory in the agent process:** Principle 8 ("Agent memory does not persist in process") keeps agents stateless. An agent process can be killed and restarted without losing context. This simplifies deployment, scaling, and recovery.

#### 7.4.6 "What Happened" vs "What Should Happen Next"

The narrative template intentionally separates these:

| Section | Content | Source |
|---|---|---|
| **"WHAT'S HAPPENING"** | Factual state from projections | `otio.slots`, `jobs.jobs`, `vms.vms` |
| **"WHAT TO DO"** | Suggested action based on agent's RULES block | Template text + situation type |

**"WHAT'S HAPPENING" is authoritative.** It describes the current state: slot A1:3:2 has measured_sec=4.1s and scripted_sec=3.5s, delta exceeds tolerance. This comes from projections.

**"WHAT TO DO" is a hint, not a command.** It suggests the agent emit `JobRequeued` with adjusted params, but the agent may choose differently (e.g., emit `ClarificationRequest` if it disagrees with the measurement). The agent's RULES block (§4.1) has the final say.

This separation prevents the narrative from becoming a deterministic instruction. The agent is free to ignore "WHAT TO DO" if its reasoning leads elsewhere — but it must still respect safety rules (budget critical, loop detected).

#### 7.4.7 Subagent Narrative Subsetting

When the main agent delegates to a subagent via `task()`, the subagent receives a **chiseled subset** of the narrative, not the full prompt. The main agent constructs a focused task description.

```python
# Inside the main agent's tool call
async def task_script_drafter(slot_id: str, text: str, target_sec: float) -> str:
    """Delegate script drafting to the script-drafter subagent."""
    subagent = get_subagent("script-drafter")

    # Subagent gets ONLY the slot it needs, not the full pipeline state
    focused_narrative = (
        f"Draft narration for slot {slot_id}.\n"
        f"Topic context: {text[:500]}\n"
        f"Target duration: {target_sec}s (~{int(target_sec * 2.5)} words)\n"
        f"Output three versions (V1, V2, V3) and visual notes."
    )

    result = await subagent.run(user_prompt=focused_narrative)
    return result.output
```

**Subagent receives:**
- The task-specific context (one slot, one topic)
- No global budget / VM / job state
- No memory from prior turns (subagents are stateless)
- A shorter system prompt without the full RULES block

**Why subset:** Subagents are specialists (script drafter, voice tagger, audio measurer). Giving them the full pipeline state would confuse them with irrelevant data. The main agent acts as the orchestrator — it reads the full narrative, decides which subagent to call, and constructs a focused task.

**Subagent system prompts are role-specific and shorter:**

```
=== YOUR ROLE ===
You are the script-drafter subagent. You write narration text only.
You do NOT emit effects. You do NOT reason about pipeline state.
You receive a topic and duration, and you output V1/V2/V3 narration.

=== OUTPUT FORMAT ===
V1: [primary narration]
V2: [alternate narration]
V3: [third take]
Visual Notes: [shot descriptions]
Duration Estimate: [seconds]
```

Subagents output text that the main agent incorporates into its own reasoning. The main agent then emits the actual effects (`UpdateScript`, `QueueJob`, etc.) based on the subagent's output plus the full pipeline context.

---

