#!/usr/bin/env python3
"""Batch 3: Parser fixes, handler fixes, missing definitions."""

from pathlib import Path

DOC = Path("/Users/orpington/Documents/economy-documentary-work/ARCHITECTURE_V7.1.md")
text = DOC.read_text()

# 1. Fix build_clarification_request: effect_type -> kind
old = '''def build_clarification_request(effects: list[Effect]) -> str | None:
    if len(effects) == 1 and effects[0].effect_type == "NoOp":'''
new = '''def build_clarification_request(effects: list[Effect]) -> str | None:
    if len(effects) == 1 and effects[0].kind == "noop":'''
if old in text:
    text = text.replace(old, new)
    print("✅ build_clarification_request fixed (effect_type→kind)")
else:
    print("⚠️  build_clarification_request not found")

# 2. Fix run_agent_turn to use parse_agent_text_multi
old = '''async def run_agent_turn(agent, agent_situations, memory, projections, config):
    """Build prompt and run agent via pydantic-deep."""
    # Build situation narrative
    narrative = "\n\n".join(
        SITUATION_TEMPLATES[s.type].format(**s.facts)
        for s in agent_situations
    )

    # Memory from prior turns (last 5)
    history = [
        UserMessage(content=f"[MEMORY] {m}")
        for m in memory[-5:]
    ]

    # Run agent with deps carrying projections for compaction
    result = await agent.run(
        user_prompt=narrative,
        message_history=history,
        deps=PipelineDeps(
            projections=projections,
            agent_role=agent.role,
            max_tokens=config.max_tokens,
            compaction_model=config.compaction_model,
        ),
    )

    # Parse effects from result.output
    return parse_effects(result.output)'''
new = '''async def run_agent_turn(
    agent, agent_role: str, agent_situations, memory, projections, config
):
    """Build prompt and run agent via pydantic-deep.

    V7.1 fix: Added agent_role parameter (agents don\'t have .role attribute).
    Uses parse_agent_text_multi for semantic extraction.
    """
    # Build situation narrative
    narrative = "\n\n".join(
        SITUATION_TEMPLATES[s.type].format(**s.facts)
        for s in agent_situations
    )

    # Memory from prior turns (last 5) — memory is already list[UserMessage]
    history = memory[-5:]

    # Run agent with deps carrying projections for compaction
    result = await agent.run(
        user_prompt=narrative,
        message_history=history,
        deps=PipelineDeps(
            projections=projections,
            agent_role=agent_role,
            max_tokens=config.max_tokens,
            compaction_model=config.compaction_model,
        ),
    )

    # Parse effects from result.output
    return parse_agent_text_multi(agent_role, result.output)'''
if old in text:
    text = text.replace(old, new)
    print("✅ run_agent_turn fixed")
else:
    print("⚠️  run_agent_turn not found")

# 3. Fix handler call to run_agent_turn
old = '''            # 4. Run agent turn
            effects = await run_agent_turn(
                agent, situations, memory, projections, config
            )'''
new = '''            # 4. Run agent turn
            effects = await run_agent_turn(
                agent, AGENT_ROLE, situations, memory, projections, config
            )'''
if old in text:
    text = text.replace(old, new)
    print("✅ Handler run_agent_turn call fixed")
else:
    print("⚠️  Handler run_agent_turn call not found")

# 4. Add parse_agent_text_multi definition after the Handler Integration section
old = '''The parser is a **pure function**: `parse_agent_text_multi(agent_id: str, text: str) -> list[Effect]`. It has no side effects, no state, and no access to the event store. All append operations happen in the handler.

---

### 9.5.7 Complete Parse Flow (Decision Tree)'''
new = '''The parser is a **pure function**: `parse_agent_text_multi(agent_id: str, text: str) -> list[Effect]`.

#### 9.5.6.1 Parser implementation

```python
import instructor
from openai import AsyncOpenAI

# V7.1: instructor client wraps deepseek-v4-flash via OpenRouter
_client = instructor.from_openai(AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key="...",
))

async def parse_agent_text_multi(agent_id: str, text: str) -> list[Effect]:
    """Extract effects from agent natural-language output.

    V7.1 fix: Defined the parser function that was referenced but never shown.
    Uses instructor + deepseek-v4-flash with discriminated-union validation.
    Returns validated Effect subclasses ready for event store append.
    """
    try:
        result = await _client.chat.completions.create(
            model="openrouter:deepseek/deepseek-v4-flash",
            response_model=_MultiEffect,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
    except Exception as exc:
        # Total parse failure — return NoOp for observability
        return [NoOp(reason=f"Parser could not extract: {exc}")]

    # Map parser output (_EffectUnion with `kind` discriminant) to real Effect subclasses
    effects: list[Effect] = []
    for parsed in result.effects:
        model_class = KIND_TO_MODEL.get(parsed.kind)
        if model_class is None:
            continue
        # Merge agent attribution fields
        data = parsed.model_dump()
        data["agent"] = agent_id
        effects.append(model_class.model_validate(data))

    if not effects:
        return [NoOp(reason="No actionable effects found")]

    return effects
```

**Key points:**
- `_MultiEffect` uses `kind` as discriminant (unified with event store §3.1.1).
- Parser output is mapped to real `Effect` subclasses via `KIND_TO_MODEL`.
- `agent_id` is injected by the handler, not the parser.
- The function is async because instructor LLM calls are async.

---

### 9.5.7 Complete Parse Flow (Decision Tree)'''
if old in text:
    text = text.replace(old, new)
    print("✅ parse_agent_text_multi definition added")
else:
    print("⚠️  parse_agent_text_multi insertion point not found")

# 5. Fix _parse_payload justification in §5.1.4
old = '''#### 5.1.4 _parse_payload() — effect deserialization glue

`read_all()` and `read_since()` return `EventRecord` objects with typed `Effect` fields. For projections that need raw dict access:

```python
import json

def _parse_payload(kind: str, payload_json: str) -> Effect:
    """Deserialize a JSON payload into the correct Effect subclass.

    Raises ValueError for unknown kind strings.
    """
    model_class = KIND_TO_MODEL.get(kind)
    if model_class is None:
        raise ValueError(f"Unknown effect kind: {kind!r}")
    data = json.loads(payload_json)
    return model_class.model_validate(data)
```'''
new = '''#### 5.1.4 _parse_payload() — effect deserialization glue

**V7.1 note:** JSONL stores typed `EventRecord` objects where `record.effect` is
already a validated `Effect` subclass. `_parse_payload()` is provided for
scenarios where raw JSON bytes are read (e.g., backup files, ESDB migration).
It is not needed for normal projection replay from JSONL.

```python
import json

def _parse_payload(kind: str, payload_json: str) -> Effect:
    """Deserialize a JSON payload into the correct Effect subclass.

    Raises ValueError for unknown kind strings.
    """
    model_class = KIND_TO_MODEL.get(kind)
    if model_class is None:
        raise ValueError(f"Unknown effect kind: {kind!r}")
    data = json.loads(payload_json)
    return model_class.model_validate(data)
```'''
if old in text:
    text = text.replace(old, new)
    print("✅ _parse_payload justification added")
else:
    print("⚠️  _parse_payload section not found")

# 6. Fix EventStoreBackend Protocol async vs sync
old = '''class EventStoreBackend(Protocol):
    async def append(self, run_id: str, effect: Effect, otio_hash_before: str) -> Any: ...
    async def read_all(self, run_id: str) -> list[Any]: ...
    async def read_since(self, run_id: str, from_seq: int) -> list[Any]: ...'''
new = '''class EventStoreBackend(Protocol):
    """V7.1: Protocol for swappable event store backends.

    JSONL implementation uses sync I/O (fine for single-process).
    ESDB implementation would use async I/O.
    """
    def append(self, run_id: str, effect: Effect, otio_hash_before: str) -> Any: ...
    def read_all(self, run_id: str) -> list[Any]: ...
    def read_since(self, run_id: str, from_seq: int) -> list[Any]: ...'''
if old in text:
    text = text.replace(old, new)
    print("✅ EventStoreBackend Protocol fixed (sync)")
else:
    print("⚠️  EventStoreBackend Protocol not found")

DOC.write_text(text)
print("\nBatch 3 written")
