#!/usr/bin/env python3
"""Batch 4: Add missing helper definitions, fix remaining handler code."""

from pathlib import Path

DOC = Path("/Users/orpington/Documents/economy-documentary-work/ARCHITECTURE_V7.1.md")
text = DOC.read_text()

# 1. Add helper functions before §9 Agents section
old = '''---

## 9. Agents — Per-Agent Implementations'''
new = '''---

### 8.6 Handler Helpers (V7.1 fix — defined here, referenced in §9)

These helper functions are used by all agent handlers. They were referenced
but never defined in V7.

```python
def hash_otio(otio_projection) -> str:
    """Compute a deterministic hash of OTIO state for EventRecord.

    Used to detect concurrent modifications: if two handlers append
    with different otio_hash_before values, the second may be stale.
    """
    import hashlib, json
    # Serialize slot states in deterministic order
    slots = sorted(otio_projection.slots.items()) if otio_projection else []
    payload = json.dumps(slots, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def rebuild_projections(run_id: str, store: EventStore) -> dict[str, Any]:
    """Replay all events for a run and rebuild all five projections.

    Called by the agent handler on every turn to get fresh state.
    """
    projections = {
        "otio": OTIOProjection(),
        "jobs": JobProjection(),
        "vms": VMProjection(),
        "state": StateProjection(),
        "budget": BudgetProjection(),
    }
    for record in store.replay(run_id):
        for proj in projections.values():
            proj.apply(record.effect)
    return projections


async def notify_downstream(effects: list[Effect], run_id: str) -> None:
    """POST wake notifications to downstream agents based on extracted effects.

    V7.1: Simple dispatcher — no retry logic (operator monitors via GSA).
    Agents that miss a wake will be re-awoken on the next state change.
    """
    import httpx
    wake_map = {
        "update_script": "http://localhost:8002",      # → Audio Agent
        "reconciliation_complete": "http://localhost:8003",  # → Video Agent
        "job_approved": "http://localhost:8003",       # → Video Agent
        "pipeline_started": "http://localhost:8002",   # → Audio Agent
    }
    targets = set()
    for effect in effects:
        if effect.kind in wake_map:
            targets.add(wake_map[effect.kind])
    for url in targets:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    url,
                    json={"run_id": run_id, "notification_type": "wake"},
                    headers={"X-Run-ID": run_id},
                )
        except Exception:
            pass  # No retry — next state change will re-trigger
```

---

## 9. Agents — Per-Agent Implementations'''
if old in text:
    text = text.replace(old, new)
    print("✅ Helper functions added (hash_otio, rebuild_projections, notify_downstream)")
else:
    print("⚠️  Helper insertion point not found")

# 2. Fix §5.6.1 handler code to match corrected handler
old = '''async def handle(payload: AgentPayload) -> AgentResponse:
    """Agent handler with per-run_id serialization."""
    lock = _run_locks.setdefault(payload.run_id, Lock())

    # Wait for any prior handler for this run to complete
    async with lock:
        # 1. Query GSA for fresh state (always current, see §5.6.3)
        projections = await query_gsa(payload.run_id)

        # 2. Build memory from EventStoreDB (rebuilt every turn, survives restart)
        memory = await build_memory(payload.run_id, AGENT_ROLE, store, limit=5)

        # 3. Build narrative and run agent
        situations = derive_situations(projections, AGENT_ROLE)
        effects = await run_agent_turn(agent, situations, memory, projections, config)

        # 3. Append effects (serialized with lock held)
        for effect in effects:
            store.append(payload.run_id, effect, otio_hash_before="...")

        # 4. Notify downstream
        await notify_downstream(effects, payload.run_id)

        return AgentResponse(status="ok", effects_extracted=[e.kind for e in effects])'''
new = '''async def handle(payload: AgentPayload, store: EventStore, config: Config) -> AgentResponse:
    """Agent handler with per-run_id serialization."""
    lock = _run_locks.setdefault(payload.run_id, Lock())

    async with lock:
        # 1. Replay projections from JSONL
        projections = rebuild_projections(payload.run_id, store)

        # 2. Build memory from JSONL
        memory = await build_memory(payload.run_id, AGENT_ROLE, store, limit=5)

        # 3. Build narrative and run agent
        situations = derive_situations(projections, AGENT_ROLE, config)
        effects = await run_agent_turn(agent, AGENT_ROLE, situations, memory, projections, config)

        # 4. Append effects
        otio_hash = hash_otio(projections.get("otio"))
        for effect in effects:
            store.append(payload.run_id, effect, otio_hash_before=otio_hash)

        # 5. Notify downstream
        await notify_downstream(effects, payload.run_id)

        return AgentResponse(status="ok", effects_extracted=[e.kind for e in effects])'''
if old in text:
    text = text.replace(old, new)
    print("✅ §5.6.1 handler code fixed")
else:
    print("⚠️  §5.6.1 handler code not found")

# 3. Fix the run_agent_turn call in §5.6.1 too (it might be the same snippet)

# 4. Fix derive_situations signature in §7.4.1 to show it accepts config
old = '''async def derive_situations(projections: ProjectionBundle, agent_role: str) -> list[Situation]:'''
new = '''async def derive_situations(
    projections: ProjectionBundle, agent_role: str, config: Config
) -> list[Situation]:
    """Build situation list for an agent from current projections.

    V7.1 fix: Added `config` parameter (was referenced but not in signature).
    """'''
if old in text:
    text = text.replace(old, new)
    print("✅ derive_situations signature fixed")
else:
    print("⚠️  derive_situations signature not found")

# 5. Fix the authoring workflow §18.4 — instructions param issue
old = '''    # 3. Run agent (it can only emit permitted effects — parser enforces)
    result = await agent.run(
        user_prompt=build_narrative(projections, script_block.role),
        instructions=instructions,  # overrides default for this turn
        deps=PipelineDeps(projections=projections, agent_role=script_block.role),
    )'''
new = '''    # 3. Run agent (it can only emit permitted effects — parser enforces)
    # V7.1 fix: pydantic-ai.Agent.run() does not accept `instructions` param.
    # System prompts are set at agent construction. For per-turn override,
    # prepend the script block to the user prompt.
    result = await agent.run(
        user_prompt=instructions + "\n\n" + build_narrative(projections, script_block.role),
        deps=PipelineDeps(projections=projections, agent_role=script_block.role),
    )'''
if old in text:
    text = text.replace(old, new)
    print("✅ Authoring workflow instructions param fixed")
else:
    print("⚠️  Authoring workflow instructions param not found")

DOC.write_text(text)
print("\nBatch 4 written")
