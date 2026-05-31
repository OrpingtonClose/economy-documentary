> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Problem Statement: Private Evolving Cognitive Maps for Autonomous Agents in Object-Mutating Event-Sourced Collaboration

## The Core Problem

How do autonomous agents maintain coherent, long-term orientation within a complex, evolving task space when:

1. **Objects mutate unpredictably** — scenes split into fragments, blocks merge, jobs fracture and recombine, and the ontology itself evolves during execution
2. **There is no central orchestrator** — agents are free peers collaborating through an append-only event log; no state machine, no workflow graph, no prescribed sequence
3. **Each agent has a different cognitive vocabulary** — the Audio agent thinks in "measurements and tolerances"; the Scenario agent thinks in "narrative arcs and emotional beats"; the Provisioner thinks in "VM offers and GPU utilization"
4. **Agents must remember and learn** — they need persistent working memory that grows smarter over time, not just a projection rebuilt from events each turn
5. **Guidance must be advisory, not enforced** — the agent can ignore its own past advice, override its map, invent new strategies

## What Fails

- **State machines / FSMs** — shatter when objects split; require perfect foresight of all transitions
- **Dependency graphs / DAGs** — assume stable object identities; break when nodes merge or split
- **Shared whiteboards / computed projections** — collapse different agent vocabularies into a lowest-common-denominator; destroy autonomy
- **Object-ID-based task lists** — become stale immediately when the object mutates
- **Fixed ontologies** — become prisons as the domain evolves

## What Is Needed

A **private, self-modifying, narrative cognitive substrate** per agent that:

- Stores **intent threads** (ongoing concerns, not tasks) anchored to **semantic descriptions** rather than object IDs
- Maintains **lineage awareness** — probabilistically rebinding to descendant objects after splits/merges
- Evolves its own **concept vocabulary** over time (self-authored schemas)
- Survives object mutation because it tracks **continuities of concern**, not state accuracy
- Is **advisory** — the agent deliberates over it, can ignore it, can rewrite it
- Persists across agent restarts (in the event log or private store)
- Scales to **long-running, multi-phase pipelines** (script → audio → video → assembly) where the agent's understanding deepens over time

## The Key Insight

The agent does not navigate tasks. The agent navigates **evolving continuities of concern** within a shared event stream, using a private associative memory field that reconsolidates and drifts like human episodic memory.

## Search Terms

- "BDI architecture for LLM agents" (Belief-Desire-Intention)
- "Cognitive architectures for language agents" (CoALA)
- "Semantic memory for autonomous agents"
- "Episodic memory in multi-agent systems"
- "Lineage-aware task tracking"
- "Narrative cognition in artificial agents"
- "Self-modifying agent ontologies"
- "Associative memory for dynamic environments"
- "Continuity of identity in agent systems"
- "Private belief bases for collaborative agents"
- "Event-sourced agent cognition"
- "Intention reconsideration in autonomous systems"
