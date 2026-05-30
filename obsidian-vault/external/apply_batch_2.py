#!/usr/bin/env python3
"""Batch 2: Config, projections, handler code, missing helpers."""

from pathlib import Path

DOC = Path("/Users/orpington/Documents/economy-documentary-work/ARCHITECTURE_V7.1.md")
text = DOC.read_text()

# ===================================================================
# FIX: Projection.tick() at §6.1.1 - still uses old ESDB dict API
# ===================================================================
old = '''    async def tick(self, run_id: str) -> int:
        """Fetch events since ``last_sequence`` and apply each.

        Returns the number of events processed.
        """
        events = await read_since(run_id, self.last_sequence)
        processed = 0
        for event in events:
            self.apply(event)
            self.last_sequence = event["sequence"]
            processed += 1
        return processed'''
new = '''    async def tick(self, run_id: str, store: EventStore) -> int:
        """Fetch events since ``last_sequence`` and apply each.

        V7.1 fix: Uses JSONL EventStore API (store.read_since) instead of
        the V7 ESDB bare function. Returns the number of events processed.
        """
        records = store.read_since(run_id, self.last_sequence)
        processed = 0
        for record in records:
            self.apply(record.effect)
            self.last_sequence = record.seq
            processed += 1
        return processed'''
if old in text:
    text = text.replace(old, new)
    print("✅ Projection.tick() fixed for JSONL")
else:
    print("⚠️  Projection.tick() pattern not found")

# ===================================================================
# FIX: build_memory signature - add store parameter, fix read_agent_events
# ===================================================================
old = '''async def build_memory(
    run_id: str,
    agent: str,
    limit: int = 5,
) -> list[UserMessage]:
    """Fetch the last N effects emitted by this agent and format as memory."""
    events = await read_agent_events(run_id, agent, limit=limit)'''
new = '''async def build_memory(
    run_id: str,
    agent: str,
    store: EventStore,
    limit: int = 5,
) -> list[UserMessage]:
    """Fetch the last N effects emitted by this agent and format as memory.

    V7.1 fix: Added `store` parameter. Memory is rebuilt from JSONL, not ESDB.
    """
    events = read_agent_events(run_id, agent, store, limit=limit)'''
if old in text:
    text = text.replace(old, new)
    print("✅ build_memory signature fixed")
else:
    print("⚠️  build_memory pattern not found")

# ===================================================================
# FIX: read_agent_events - rewrite for JSONL
# ===================================================================
old = '''async def read_agent_events(run_id: str, agent: str, limit: int = 5) -> list[Effect]:
    """Read the last N effects emitted by a specific agent from the JSONL event store.

    Scans the event log and filters by agent name. O(N) where N is event count.
    For documentary runs (500-2000 events) this is acceptable.
    """
    # V7.1: scan all events, filter by agent, return last N
    records = await replay(run_id)  # or store.replay(run_id)
    agent_events = [r.effect for r in records if r.effect.agent == agent]
    return agent_events[-limit:]'''
# Actually let me search for what's currently there

DOC.write_text(text)
print("Batch 2 written")
