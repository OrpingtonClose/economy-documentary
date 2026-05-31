> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture Assessment: Eliminate pydantic-deep Agents

## Correction

**You're right.** `pydantic-deep` agents with `include_memory=True` DO maintain persistent conversation history. Each agent remembers previous turns within its process. My previous assessment incorrectly stated this would be lost.

## What Persistent Memory Actually Does Here

Current flow with memory:
```
Cycle 1: Scenario agent → "Write script" → produces UpdateScript
          (agent remembers: "I wrote a script about rainbows")

Cycle 2: Audio agent → "Create audio jobs" → produces GenerateNarrationAudio
          (agent remembers: "I created 3 audio jobs")

Cycle 3: Provisioner agent → "Provision VMs" → produces ExecuteRawBash
          (agent remembers: "I provisioned 1 VM")
```

## But Does It Matter?

For **this pipeline**, each agent is called at most **once per stage**:
- Scenario: called once on cycle 0, then never again
- Audio: called once when script exists, then idle
- Video: called once when script exists, then idle
- Provisioner: called when jobs pending, then idle
- Assembly: called when media complete, then idle

The orchestrator already:
1. Passes full world state in every prompt
2. Only calls an agent when its stage is active
3. Expects the agent to produce effects or say NoOp

**Persistent memory provides no value** because:
- Each agent's "memory" is just "I did my job last time" — redundant with world state
- No agent needs to reference previous conversations to do its job
- The world state (OTIO metadata + queue summary) is the single source of truth

## What We Actually Lose

| Feature | Used by Pipeline? | Mitigation |
|---------|-------------------|------------|
| Persistent memory | No | Pass world state in prompt |
| Multi-turn reasoning within agent | No | Each agent acts once per stage |
| Agent "personality" consistency | Minimal | System prompt defines personality |

## What We Gain

| Feature | Benefit |
|---------|---------|
| No process management | No launch/wait/terminate/kill cycle |
| No port conflicts | No zombie processes holding ports |
| No HTTP overhead | Direct LLM call, no localhost round-trip |
| No deadlock risk | API call can be retried, hung process cannot |
| Lower memory | 1 process instead of 6 |
| Faster startup | No waiting for agents to bind ports |

## The Real Question

Is persistent agent memory worth 5 HTTP processes + process management + port management + deadlock recovery?

For this pipeline: **no**. The agents are stateless in practice because each acts once per stage with full context.

For a system where agents hold long conversations (e.g., chatbot, coding agent): **yes**. Memory is essential.

## Recommendation

**Proceed with simplification.** The persistent memory feature of pydantic-deep is real and works, but it's unnecessary overhead for a pipeline where each agent acts once per stage. The world state passed in the prompt provides all the context the agent needs.
