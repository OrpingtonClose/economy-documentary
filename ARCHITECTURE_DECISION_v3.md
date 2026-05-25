# Architecture Decision: v3 — OTIO-Derived Graph

## Status: PROPOSED

## Context

The pipeline has evolved through multiple iterations:
- v1: Hardcoded orchestrator with per-unit state machines
- v2: Self-orchestrating agents with instructor parsing
- v2.5: Persistent agent memory across HTTP calls

All versions share a flaw: **state is duplicated** between OTIO timeline, job queue, agent memory, and event store. The orchestrator polls multiple sources and makes decisions based on heuristics.

## Decision

**Adopt a single graph where the OTIO is the state machine.**

The graph is a DAG derived from the OTIO timeline. Nodes are prompts to agents. Edges are OTIO-derived conditions. Validation is embedded in edge transitions. Backtracking is possible.

## Consequences

### Positive
- Single source of truth: OTIO
- No duplicated state
- Agents are stateless
- Graph is inspectable and renderable
- Validation is declarative (edge conditions)
- Backtracking is first-class (graph traversal)

### Negative
- Requires understanding pydantic-graph builder API
- Graph is dynamic (derived from OTIO at runtime)
- More complex than hardcoded orchestrator

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Start     │────▶│  Read OTIO  │────▶│   Decision  │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                                │
                    ┌───────────────────────────┼───────────┐
                    │                           │           │
                    ▼                           ▼           ▼
            ┌──────────────┐          ┌──────────────┐  ┌──────────────┐
            │ ScriptNode   │          │ AudioNode    │  │  VideoNode   │
            │ (prompt:     │          │ (prompt:     │  │  (prompt:    │
            │  scenario)   │          │  audio)      │  │   video)     │
            └──────┬───────┘          └──────┬───────┘  └──────┬───────┘
                   │                         │                  │
                   ▼                         ▼                  ▼
            ┌──────────────┐          ┌──────────────┐  ┌──────────────┐
            │ CallAgent    │          │ CallAgent    │  │  CallAgent   │
            │ (DeepSeek)   │          │ (DeepSeek)   │  │  (DeepSeek)  │
            └──────┬───────┘          └──────┬───────┘  └──────┬───────┘
                   │                         │                  │
                   ▼                         ▼                  ▼
            ┌──────────────┐          ┌──────────────┐  ┌──────────────┐
            │ ParseEffect  │          │ ParseEffect  │  │  ParseEffect │
            │ (instructor) │          │ (instructor) │  │  (instructor)│
            └──────┬───────┘          └──────┬───────┘  └──────┬───────┘
                   │                         │                  │
                   └─────────────────────────┼──────────────────┘
                                             │
                                             ▼
                                      ┌──────────────┐
                                      │ ProjectEffect│
                                      │ (to OTIO)    │
                                      └──────┬───────┘
                                             │
                                             ▼
                                      ┌──────────────┐
                                      │  Read OTIO   │
                                      └──────┬───────┘
                                             │
                                             ▼
                                      ┌──────────────┐
                                      │   Decision   │
                                      └──────────────┘
```

## Implementation

### Graph Builder (pydantic-graph)

```python
from pydantic_graph import GraphBuilder, StepContext, StepNode

class PipelineState:
    timeline_path: str
    event_log_path: str
    cycle: int = 0

g = GraphBuilder(state_type=PipelineState)

@g.step
def read_otio(ctx: StepContext[PipelineState]) -> Decision:
    # Read OTIO, return decision based on state
    ...

@g.step
def scenario_prompt(ctx: StepContext[PipelineState]) -> str:
    # Generate prompt for scenario agent
    ...

@g.step
def call_agent(ctx: StepContext[PipelineState], prompt: str) -> str:
    # Call DeepSeek API
    ...

@g.step
def parse_effect(ctx: StepContext[PipelineState], text: str) -> list[Effect]:
    # Parse with instructor
    ...

@g.step
def project_effect(ctx: StepContext[PipelineState], effects: list[Effect]) -> None:
    # Apply to OTIO
    ...
```

### Decision Node

The decision node reads OTIO and determines which agent(s) to call:

```python
@g.decision
def what_next(ctx: StepContext[PipelineState]) -> list[str]:
    """Return list of agent IDs that should act next."""
    needed = []
    if not has_script:
        needed.append("scenario")
    if has_script and not has_audio_jobs:
        needed.append("audio")
    if has_script and not has_video_jobs:
        needed.append("video")
    if has_pending_jobs:
        needed.append("provisioner")
    if has_media and not has_output:
        needed.append("assembly")
    return needed
```

### Fork/Join for Parallel Agents

```python
@g.fork(what_next)
def fork_agents(ctx: StepContext[PipelineState], agent_id: str) -> str:
    """Call agent in parallel."""
    prompt = build_prompt(agent_id, ctx.state)
    return call_agent(agent_id, prompt)

@g.join(fork_agents)
def join_agents(ctx: StepContext[PipelineState], results: list[str]) -> list[Effect]:
    """Parse all agent outputs."""
    effects = []
    for text in results:
        effects.extend(parse_agent_text_multi(text))
    return effects
```

## Validation

Validation is embedded in edge conditions:

```python
@g.edge(scenario_prompt, call_agent)
def validate_script(ctx, script_text):
    # Ensure script has narration_v1
    if not script_text.narration_v1:
        return read_otio  # Backtrack
    return call_agent
```

## Backtracking

If validation fails, the graph rewinds to the previous node:

```python
@g.edge(parse_effect, project_effect)
def validate_effects(ctx, effects):
    for effect in effects:
        if not is_valid(effect):
            return build_feedback(effect)  # Backtrack with feedback
    return project_effect
```

## Migration Path

1. Keep existing effects, parser, instructor, projection handler
2. Delete pydantic_deep_agents/ (HTTP agents)
3. Delete launcher.py (process management)
4. Implement graph in run_pipeline_v3.py
5. Wire graph nodes to instructor + projection handler

## Rejected Alternatives

### Keep HTTP Agents
- Rejected: process management is too complex, ports conflict, deadlocks

### Structured Output Agents
- Rejected: agents resist structured output, free text is more natural

### Hardcoded Orchestrator
- Rejected: duplicate state, hard to extend, no backtracking

## References

- pydantic-graph builder API: https://github.com/pydantic/pydantic-graph
- Instructor structured extraction: https://python.useinstructor.com/
- OTIO event sourcing: OpenTimelineIO docs
