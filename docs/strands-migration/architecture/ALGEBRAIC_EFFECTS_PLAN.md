> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture: Algebraic Effects + OTIO-Centric Pipeline

## Core Insight

> **OTIO is the state. Effects are the only legal way to touch it.**

Without this, agents run amok. With it, the pipeline becomes a controlled, auditable, iterative system where every mutation to the timeline is typed, handled, and observed.

The architecture is a synthesis of:
- **Algebraic Effect Systems** — typed, declared mutations; handlers decide execution semantics
- **Dependency Solvers** — dynamic computation of which effects are enabled based on current state
- **Intent Parsing** — LLM emits raw text; local parser extracts typed effects (not direct tool calling)
- **Event Sourcing** — append-only log of (Intent, Handler, Result, Observation) tuples

---

## Why Direct Tool Calling Fails Here

| Aspect | Direct Tool/Function Calling | Intent Parsing (ADT) |
|--------|------------------------------|----------------------|
| LLM output | Structured JSON / function call | Free natural language + reasoning |
| Burden on LLM | High — must obey schema while reasoning | Low — just think naturally |
| Cross-VM comms | Must serialize JSON on wire | Pure raw text only |
| Inter-agent chat | Rigid, schema-bound | Rich, natural, iterative |
| Error recovery | Bad JSON = hard failure | Parse fails → retry with better context |
| Audit trail | Just the JSON call | Full reasoning trace + typed intent |
| Escape hatch (bash) | Hard to mix cleanly | `ExecuteRawBash` is just another ADT variant |

**Division of labor:** LLM reasons → parser extracts → graph validates → handler executes → observation feeds back.

---

## The Three Layers

```
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 1: AGENTS (Creative, Natural Language)                          │
│                                                                         │
│  Agents on separate VMs communicate via raw text only.                  │
│  No JSON on the wire. No structured output forced on the LLM.           │
│                                                                         │
│  Agent A (scenario): "The opening scene needs a slower voiceover        │
│  to match the visuals. Please shift the audio timing by 200ms."        │
│                                                                         │
│  Agent B (audio): receives raw text → parses locally → extracts intent  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │ raw text
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 2: PARSER + GRAPH (Validation, Satisfiability)                  │
│                                                                         │
│  Local parser (instructor + Pydantic) turns raw text → typed Effect.   │
│  DependencyEngine checks: is this effect currently enabled?             │
│  Graph defines: what does this effect require / enable?                 │
│                                                                         │
│  If enabled → pass to handler.                                          │
│  If disabled → return Observation("prerequisite not met: X")            │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │ Effect ADT
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 3: HANDLERS + OTIO (Execution, Mutation, Observation)           │
│                                                                         │
│  Handler receives typed Effect → mutates OTIO / filesystem / VMs.      │
│  Handler ALWAYS returns Observation (success, summary, diff).          │
│  Observation sent back to agent as raw text.                           │
│                                                                         │
│  OTIO file is the single source of truth for "state of the movie".     │
│  Event store logs every (Intent, Handler, Result, Observation).        │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## OTIO as Central State

**The realization:** Once OTIO is accepted as the single source of truth, algebraic effect types become *required*, not optional. Without them, agents directly mutate the timeline with raw bash or ad-hoc code, and the system collapses into chaos.

**OTIO's role:**
- Contains the timeline, clips, markers, metadata
- Is the only mutable state that survives across VM boundaries
- Is read by agents to understand current state
- Is written ONLY by effect handlers (never by agents directly)

**Effects mutate OTIO:**
```python
class MergeIntoOTIO(BaseEffect):
    effect_type: Literal["merge_into_otio"] = "merge_into_otio"
    segment_id: str
    start_frame: int

# Handler is the ONLY code that touches OTIO
class OTIOHandler:
    async def handle(self, effect: MergeIntoOTIO) -> Observation:
        timeline = otio.adapters.read_from_file(self.otio_path)
        timeline.tracks[0].append(effect.to_clip())
        otio.adapters.write_to_file(timeline, self.otio_path)
        return Observation(
            success=True,
            summary="segment merged",
            otio_diff=f"+clip {effect.segment_id} at frame {effect.start_frame}"
        )
```

---

## The Effect ADT

```python
from pydantic import BaseModel, Field
from typing import Literal, Union

class BaseEffect(BaseModel):
    """Every effect declares its stage and what it affects."""
    effect_type: str
    stage: Literal["scenario", "audio", "video", "otio", "any"]
    affects: list[str] = Field(default_factory=list)

class UpdateScript(BaseEffect):
    effect_type: Literal["update_script"] = "update_script"
    stage: Literal["scenario"] = "scenario"
    affects: list[str] = ["script", "otio_timeline"]
    scene_id: str
    new_text: str

class AdjustAudioTiming(BaseEffect):
    effect_type: Literal["adjust_audio_timing"] = "adjust_audio_timing"
    stage: Literal["audio"] = "audio"
    affects: list[str] = ["audio_file", "otio_timeline"]
    clip_id: str
    offset_ms: int

class RenderVideoSegment(BaseEffect):
    effect_type: Literal["render_video_segment"] = "render_video_segment"
    stage: Literal["video"] = "video"
    affects: list[str] = ["video_file"]
    segment_id: str
    prompt: str
    duration_sec: float

class MergeIntoOTIO(BaseEffect):
    effect_type: Literal["merge_into_otio"] = "merge_into_otio"
    stage: Literal["otio"] = "otio"
    affects: list[str] = ["otio_timeline"]
    segment_id: str
    start_frame: int

class ExecuteRawBash(BaseEffect):
    """Escape hatch. Agent can always drop to raw bash."""
    effect_type: Literal["execute_raw_bash"] = "execute_raw_bash"
    stage: Literal["any"] = "any"
    affects: list[str] = ["anything"]
    command: str
    justification: str
    risk_level: Literal["low", "medium", "high"] = "medium"

class RequestHumanApproval(BaseEffect):
    effect_type: Literal["request_human_approval"] = "request_human_approval"
    stage: Literal["any"] = "any"
    gate_name: str
    proposed_effect: Union[RenderVideoSegment, AdjustAudioTiming]
    reason: str

class DoNothing(BaseEffect):
    effect_type: Literal["do_nothing"] = "do_nothing"
    stage: Literal["any"] = "any"
    reason: str

class RaiseError(BaseEffect):
    effect_type: Literal["raise_error"] = "raise_error"
    stage: Literal["any"] = "any"
    message: str
    severity: Literal["low", "medium", "high", "critical"] = "high"

Effect = Union[
    UpdateScript, AdjustAudioTiming, RenderVideoSegment,
    MergeIntoOTIO, ExecuteRawBash, RequestHumanApproval,
    DoNothing, RaiseError,
]
```

---

## The Observation (Feedback Loop)

Agents must know what happened. Without observations, they are blind.

```python
class Observation(BaseModel):
    action_type: Literal["observation"] = "observation"
    success: bool
    summary: str                    # human-readable
    details: str | None = None      # stdout, stderr, file diff
    affected_files: list[str] = Field(default_factory=list)
    otio_diff: str | None = None    # e.g. "+clip at frame 1840"
    handler: str                    # which handler ran
```

**Handler always returns Observation:**
```python
class BashHandler:
    async def handle(self, effect: ExecuteRawBash) -> Observation:
        try:
            result = subprocess.run(
                effect.command, shell=True, capture_output=True,
                text=True, timeout=60
            )
            return Observation(
                success=result.returncode == 0,
                summary=f"exit code {result.returncode}",
                details=result.stdout + result.stderr,
                affected_files=detect_changed_files(),
                handler="bash"
            )
        except Exception as e:
            return Observation(success=False, summary=str(e), handler="bash")
```

The observation is sent back to the agent as raw text. The agent reads it, reasons, and emits new raw text. The loop continues.

---

## The Dependency Graph + Engine

**Static declaration** (defined once in code):
```python
# What each effect requires to be enabled, and what it enables next
GRAPH = {
    "UpdateScript": {
        "requires": [],
        "enables": ["ReviewScript", "AdjustAudioTiming"]
    },
    "AdjustAudioTiming": {
        "requires": ["UpdateScript"],
        "enables": ["RenderVideoSegment", "MergeIntoOTIO"]
    },
    "RenderVideoSegment": {
        "requires": ["AdjustAudioTiming"],
        "enables": ["MergeIntoOTIO"]
    },
    "MergeIntoOTIO": {
        "requires": ["AdjustAudioTiming", "RenderVideoSegment"],
        "enables": []
    },
    "ExecuteRawBash": {
        "requires": [],
        "enables": []  # wildcard — handler decides
    },
}
```

**Dynamic engine** (runtime):
```python
class DependencyEngine:
    def __init__(self, graph: dict, otio_path: str):
        self.graph = graph
        self.satisfied: set[str] = set()
        self.otio_path = otio_path

    def feed_raw_text(self, raw_text: str) -> dict:
        """Core: throw raw text at the graph."""
        effect = extract_action(raw_text)  # local parser
        name = effect.__class__.__name__

        # Check if enabled
        reqs = self.graph.get(name, {}).get("requires", [])
        if not all(r in self.satisfied for r in reqs):
            return {
                "action_parsed": name,
                "enabled": False,
                "missing_requirements": [r for r in reqs if r not in self.satisfied],
            }

        # Mark satisfied
        self.satisfied.add(name)
        return {
            "action_parsed": name,
            "enabled": True,
            "satisfied": list(self.satisfied),
        }

    def currently_enabled(self) -> list[str]:
        """What effects can the agent legally propose right now?"""
        enabled = []
        for name, meta in self.graph.items():
            reqs = meta.get("requires", [])
            if all(r in self.satisfied for r in reqs) and name not in self.satisfied:
                enabled.append(name)
        return enabled
```

**Usage in the pipeline:**
```python
# Every VM runs its own engine (or one central engine)
engine = DependencyEngine(GRAPH, otio_path="timeline.otio")

# Agent receives raw text from another VM
result = engine.feed_raw_text("I need to merge the new audio at frame 1840")
# → {"action_parsed": "MergeIntoOTIO", "enabled": False,
#     "missing_requirements": ["RenderVideoSegment"]}

# Agent learns: must render video first. Proposes that.
result = engine.feed_raw_text("Render video segment v2 with prompt 'slow pan'")
# → {"action_parsed": "RenderVideoSegment", "enabled": True, ...}

# Now merge is enabled
result = engine.feed_raw_text("Merge segment v2 at frame 1840")
# → {"action_parsed": "MergeIntoOTIO", "enabled": True, ...}
```

---

## Why This Is a State Machine (But Richer)

**Classic FSM:** Fixed states + fixed transitions.  
**This system:**
- Graph = all possible states and transitions (declared once)
- `satisfied` set = current logical state
- Effects = the only allowed transitions
- OTIO file = external mutable world state
- Observations = feedback that changes what is known

**The loop:**
1. World starts with some OTIO state
2. Engine computes which effects are enabled
3. Agent proposes an effect via raw text
4. Parser extracts typed effect
5. Engine validates against graph + OTIO
6. Handler executes → mutates OTIO → returns Observation
7. Observation sent back as raw text
8. Agent reads, reasons, proposes next effect
9. Repeat until graph is fully satisfied

**Effects change the world → which changes what effects are possible.**

---

## Effect Handlers (Composed, Monadic)

```python
from typing import Protocol

class EffectHandler(Protocol):
    async def handle(self, effect: Effect) -> Observation:
        ...

# Audit handler: always runs first
class AuditHandler:
    def __init__(self, next_handler: EffectHandler, store: EventStore):
        self._next = next_handler
        self._store = store

    async def handle(self, effect: Effect) -> Observation:
        self._store.append(AuditEntry(effect=effect))
        return await self._next.handle(effect)

# Dry-run handler: simulates without executing
class DryRunHandler:
    def __init__(self, next_handler: EffectHandler):
        self._next = next_handler

    async def handle(self, effect: Effect) -> Observation:
        if isinstance(effect, (RenderVideoSegment, ExecuteRawBash, ProvisionWorker)):
            return Observation(success=True, summary="dry-run", handler="dry_run")
        return await self._next.handle(effect)

# Human gate handler: blocks high-risk effects
class HumanGateHandler:
    def __init__(self, next_handler: EffectHandler, approval_queue: Queue):
        self._next = next_handler
        self._queue = approval_queue

    async def handle(self, effect: Effect) -> Observation:
        if isinstance(effect, ExecuteRawBash) and effect.risk_level == "high":
            await self._queue.put(effect)
            return Observation(success=False, summary="awaiting_human_approval", handler="gate")
        return await self._next.handle(effect)

# Compose: audit → dry_run → gate → real
handler = AuditHandler(
    DryRunHandler(
        HumanGateHandler(VMHandler(), approval_queue)
    ),
    store=event_store
)
```

---

## Testing: Fault Injection, No Mocks

**Philosophy:** Every test is a mini production run. Predefined failure modes are successful tests.

```python
class FaultInjectingHandler:
    """Wraps a real handler but injects failures at specified effects."""
    def __init__(self, real_handler: EffectHandler, faults: dict):
        self._real = real_handler
        self._faults = faults  # {effect_name: Observation(failure)}

    async def handle(self, effect: Effect) -> Observation:
        name = effect.__class__.__name__
        if name in self._faults:
            return self._faults[name]
        return await self._real.handle(effect)

# Test: TTS worker fails, pipeline provisions new worker and retries
async def test_tts_worker_failure_recovery():
    store = EventStore()
    handler = FaultInjectingHandler(
        DryRunHandler(VMHandler()),
        faults={"RenderTTS": Observation(success=False, summary="CUDA OOM")}
    )
    engine = DependencyEngine(GRAPH, otio_path="/tmp/test.otio")

    # Run full pipeline
    engine.satisfied.add("UpdateScript")  # pre-condition
    result = engine.feed_raw_text("Render TTS for scene 1")
    assert result["enabled"] == True

    obs = await handler.handle(RenderTTS(scene_id="S1", text="hello", voice="default"))
    assert obs.success == False  # fault injected

    # Pipeline should detect failure, provision new worker, retry
    # Assert on event store
    events = store.read_stream()
    assert events[0].effect.effect_type == "render_tts"
    assert events[0].observation.success == False
```

**No mocks.** The `FaultInjectingHandler` is a real handler. It interprets effects. The test asserts on the event stream.

---

## Fractal Composition

**Leaf effects:** Self-contained. Single input, single output.
- `RenderTTS(text) → wav_path`
- `RenderVideo(prompt) → mp4_path`
- `ValidateTiming(audio, expected) → pass/fail`

**Leaf handlers:** Self-contained. Tested individually.
- `TTSHandler` — tested with DryRunHandler + fault injection
- `VMHandler` — provisions real VM on Vast.ai, asserts it boots

**Stage nodes:** Container units. Combine leaf effects into a stage.
- `AudioStage` = RenderTTS(×N) → ValidateTiming → MergeIntoOTIO

**Pipeline graph:** Top-level container. Combines stages.
- `DocumentaryGraph` = Scenario → Audio → Video → Assembly

**Testing at every level:**
- Leaf = mini production run
- Stage = runs its effects through composed handlers
- Pipeline = runs full stages through DryRunHandler (fast) or real handlers (slow)

---

## What Gets Thrown Away

| Current Component | Why It Dies |
|-------------------|-------------|
| DeepAgents / LangGraph orchestrator | LLM should not orchestrate deterministic flow |
| Strands GraphBuilder | String-based edges, restricted namespace, opaque execution |
| `agent.state` JSON restriction | State lives in OTIO + DependencyEngine |
| RecoveryShell ad-hoc logic | Deterministic replay through event store |
| Direct HTTP tool calls | Become typed Effects handled by VMHandler |
| JSON on the wire between VMs | Raw text only; local parsing everywhere |

---

## What Stays

| Component | Role in New Architecture |
|-----------|--------------------------|
| Strands `@tool` | Wrapped as leaf handlers or used inside creative LLM agents |
| OTIO format | Central state — the single source of truth |
| Vast.ai provisioning | `VMHandler` provisions workers; `WorkerPool` tracks them |
| LLM scenario/visual agents | Produce creative content, not operational decisions |
| SnapshotStore | Upgraded to append-only (Intent, Handler, Result, Observation) log |
| `instructor` + Pydantic | The parser layer: raw text → typed Effect |

---

## Implementation Phases

| Phase | Work | Days |
|-------|------|------|
| 1 | Define Effect ADT + parser (`extract_action`) + Observation | 2 |
| 2 | Implement handlers: VM, TTS, Video, OTIO, Bash | 3 |
| 3 | Build DependencyEngine + graph declaration | 2 |
| 4 | Wire event store (append-only Intent+Result log) | 2 |
| 5 | Fault-injection testing framework | 2 |
| 6 | HITL integration (RequestHumanApproval → external controller) | 2 |
| 7 | Migrate existing `@tool` functions to handler pattern | 2 |
| **Total** | | **~15 days** |

---

## Summary

**Algebraic effects** = the only legal ways to touch OTIO (typed + handled).  
**Dependency solver** = given the current OTIO state, which of those ways are currently allowed?  
**Intent parsing** = LLM reasons naturally; parser extracts typed effects locally.  
**Observations** = handlers always report back, so agents know what happened.  
**Event store** = append-only log of everything for recovery, audit, and debugging.

Together these turn free-roaming LLM agents into a controlled, auditable, iterative movie production pipeline with OTIO as the single source of truth.
