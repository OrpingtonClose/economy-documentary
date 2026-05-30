---
{
  "title": "Authoring Workflow for Quasi-Deterministic Agents",
  "section": "18",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[17 - pydantic Ecosystem Deep Audit V7.1 Addendum|pydantic Ecosystem Deep Audit (V7.1 Addendum)]] | [[00 - Index|Index]] | [[19 - Discarded Propositions and Rationale|Discarded Propositions and Rationale]] ->

# Authoring Workflow for Quasi-Deterministic Agents


This section documents how to create "quasi-deterministic" agents — agents that behave predictably enough for pipeline use without being rigid scripts.

### 18.1 The Problem

Fully agentic systems are too unpredictable for pipeline use (may loop, hallucinate effects, ignore rules). Fully scripted systems are too rigid (cannot handle edge cases). The pipeline needs a middle path: agents that follow an authored script but can adapt within bounded parameters.

### 18.2 Solution: Script-Guided Agents with Prompt-Enforced Structure

Instead of custom HTTP services that execute Python scripts, we use **real pydantic-deep agents** with:

1. **Script injection via handler** — The handler reads the authored YAML script via `bash_command("cat scripts/scene_3.yaml")` and injects it into the agent's prompt
2. **Prompt-enforced sequencing** — The script's task list is included in the prompt; the agent follows it in order. No todo tools.
3. **Strong system prompts** — RULES block with priority ordering and escape hatches
4. **Effect parser** — Only permitted effect kinds are extracted; others are rejected

**Hard principle:** `bash_command` is the agent's only tool. Scripts are read by the handler, not by the agent. The agent does not have a `read_script` tool.

### 18.3 Authoring Format (YAML)

```yaml
# scripts/documentary_scene_3.yaml
scene_num: 3
blocks:
  - block_id: "A1:3:1"
    role: scenario
    instructions: |
      Write narration for the Federal Reserve scene.
      Duration target: 45s. Speaker: V1_Narrator.
    permitted_effects: [update_script, noop, clarification_request]
    escape_conditions:
      - condition: "agent_loop_detected"
        action: "request_clarification"
      - condition: "budget_critical"
        action: "abort_pipeline"

  - block_id: "A1:3:2"
    role: audio
    instructions: |
      Reconcile narration duration to script target (45s ±15%).
      Max attempts: 5. Voice: V1_Narrator.
    permitted_effects:
      [queue_job, job_requeued, duration_adjusted,
       reconciliation_failed, reconciliation_complete, noop]
    prerequisites:
      - block_id: "A1:3:1"
        required_effect: "update_script"
```

### 18.4 Runtime Enforcement

```python
async def run_authorized_turn(agent, script_block, projections, store):
    """Run an agent turn constrained by an authored script block."""

    # 1. Verify prerequisites
    for prereq in script_block.prerequisites:
        if not has_effect(projections, prereq.block_id, prereq.required_effect):
            raise PrerequisitesNotMet(prereq)

    # 2. Inject script into system prompt
    instructions = f"""
{ROLE_INSTRUCTIONS[script_block.role]}

=== AUTHORIZED SCRIPT ===
{script_block.instructions}

=== PERMITTED EFFECTS ===
{', '.join(script_block.permitted_effects)}

=== ESCAPE CONDITIONS ===
{yaml.dump(script_block.escape_conditions)}
"""

    # 3. Run agent (it can only produce text from which the parser extracts permitted effects — parser enforces)
    # V7.1 fix: pydantic-ai.Agent.run() does not accept `instructions` param.
    # System prompts are set at agent construction. For per-turn override,
    # prepend the script block to the user prompt.
    result = await agent.run(
        user_prompt=instructions + "

" + build_narrative(projections, script_block.role),
        deps=PipelineDeps(gsa_url="http://gsa:8000", agent_role=script_block.role),
    )

    # 4. Parse and validate effects
    effects = await parse_agent_text_multi(script_block.role, result.output)
    # V7.1 fix: Filter to permitted kinds after extraction
    effects = [e for e in effects if e.kind in script_block.permitted_effects]

    # 5. Append to event store
    for effect in effects:
        store.append(run_id, effect, otio_hash_before=hash_otio(projections["otio"]))

    return effects
```

### 18.5 Why This Is "Quasi-Deterministic"

| Aspect | Scripted | Agentic | Quasi-Deterministic (this approach) |
|---|---|---|---|
| Next action | Fixed | Unpredictable | From authored script + current state |
| Error handling | Hardcoded | Hallucinated | Escape conditions in YAML |
| Effect types | Single | Any | Subset defined in YAML |
| LLM creativity | None | Unlimited | Bounded by script scope |
| Edge cases | Crash | Loop | Escape to `ClarificationRequest` |

The agent still uses an LLM for natural language reasoning, but its **action space** is constrained by the script. It cannot produce text from which effects outside the permitted set are extracted. It cannot proceed without prerequisites. It cannot ignore escape conditions because they are in the prompt.

### 18.6 Script Versioning

Scripts are versioned alongside the architecture:
- `scripts/v7.1/scene_template.yaml` — template for new scenes
- `scripts/v7.1/scene_001.yaml` — specific scene script
- Changes to script format require architecture version bump (V7.1 → V7.2)

---

