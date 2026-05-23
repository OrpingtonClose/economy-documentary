# Radical Plan: Algebraic Effects Architecture for the Documentary Pipeline

## The User's Core Insight (from their own exploration)

> **LLM produces a pure "Intent" (Algebraic Data Type)**  
> **→ Effect Handler interprets that intent** (with full control over side effects)

This is the practical realization of **Algebraic Effects + Intents + Monadic Side-Effect Isolation**.

The user has already reasoned through the pattern with `instructor` + Pydantic ADTs + `assert_never` exhaustive matching. The question is not "should we do this?" but **"how does this supplant the current pipeline architecture?"**

---

## The Fundamental Problem with the Current Pipeline

**The LLM is doing two jobs:**
1. **Creative** — generating scenarios, visual concepts, narration (this is what LLMs are good at)
2. **Operational** — deciding "go to audio next," "retry this scene," "provision a worker" (this is what LLMs are terrible at)

The radical change: **Fire the LLM from its operational job. Replace it with a deterministic algebraic effects system.**

---

## The Four Frameworks — Re-assessed Through the Algebraic Effects Lens

### 1. Event Sourcing → The Intent Log

**Current:** `SnapshotStore` records what happened. OTIO file is ground truth.

**Radical:** The event store records **Intents and their Results**. The OTIO timeline is a **projection** of handled Intents.

```python
# Events are not "agent did X" — they are "Intent Y was handled with result Z"
@dataclass
class Event:
    sequence: int
    timestamp: float
    intent: Intent          # The pure ADT
    handler: str            # Which effect handler ran
    result: Result          # Success | Failure | AwaitingHuman
    side_effects: list      # VM created, file written, etc.
```

**Why this changes everything:**
- Recovery = replay Intents through handlers, not "guess what the LLM meant"
- Audit = every side effect is traceable to an Intent
- Testing = run the same Intent through `dry_run_handler` vs `real_execution_handler`
- Debugging = read the Intent stream, not a chat transcript

### 2. pydantic-graph → The Intent Producer (Not an LLM)

**Current:** Strands GraphBuilder or DeepAgents orchestrator decides transitions.

**Radical:** `pydantic-graph` nodes **produce Intents**. The state machine is the orchestrator. It is deterministic, type-safe, and never calls an LLM to decide "what next?"

```python
@dataclass
class AudioDispatchNode(BaseNode[PipelineState, None, Intent]):
    async def run(self, ctx: GraphRunContext[PipelineState]) -> TimingValidationNode:
        # The node PRODUCES Intents — it does not execute them
        for scene in ctx.state.scenes:
            intent = RenderTTS(scene_id=scene.id, text=scene.narration, voice=ctx.state.voice)
            ctx.state.pending_intents.append(intent)
        
        # Handlers execute the intents (concurrently, with retry, etc.)
        results = await ctx.state.effect_runner.run_all(ctx.state.pending_intents)
        
        # Transition is deterministic based on results, not LLM reasoning
        if all(r.is_ok() for r in results):
            return TimingValidationNode(results=results)
        return AudioRetryNode(failed=[r for r in results if r.is_err()])
```

**Why pydantic-graph specifically:**
- Return-type edges = the state machine CANNOT transition to an invalid node (enforced by mypy)
- `graph.iter()` = external controller can pause between Intents, inspect, inject human decisions
- Stateful = `PipelineState` carries native Python objects (no JSON restriction)
- Pure Python = `import json` inside nodes works normally

### 3. cave-agent → Unverified, but Conceptually Aligned

**Status:** No verifiable open-source repository found. GitHub search shows a topic with no associated repos and one vague description: "Stateful runtime management for LLM agents—inject, manipulate, and retrieve Python objects across turns."

**Assessment:** If this project exists, it may be an **effect handler runtime** — managing stateful agent execution with object injection across turns. This would align with the algebraic effects architecture. Without a concrete repository, it cannot be integrated.

**Verdict:** Monitor for emergence. The pipeline's custom `EffectRunner` (built on pydantic-graph + event sourcing) covers the same conceptual space.

### 4. DeepAgents → REPLACED by the Intent + Handler Pattern

**Current:** DeepAgents `create_deep_agent()` is the planned orchestrator. It uses LangGraph with LLM-driven transitions.

**Radical:** **Remove DeepAgents entirely.** The orchestrator is a `pydantic-graph` state machine. LLM agents are **leaves** that produce creative content, not the orchestrator.

**What replaces DeepAgents features:**

| DeepAgents Feature | Algebraic Effects Replacement |
|--------------------|-------------------------------|
| `write_todos` | State machine nodes track progress in `PipelineState` |
| `read_file/write_file` | `FileOperation` Intent handled by `FilesystemHandler` |
| `task()` subagent | Subgraph nodes that produce Intents for their domain |
| `interrupt_on` HITL | `AwaitHumanDecisionNode` — explicit pause in state machine |
| `MemoryMiddleware` | Event store replay + `PipelineState` accumulation |
| `FilesystemBackend` | `FileOperation` Intents + projection builders |

---

## The New Architecture: Algebraic Effects Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                     INTENT LAYER (Pure)                              │
│                                                                       │
│   pydantic-graph nodes produce Intents (Pydantic ADTs)               │
│   No side effects. No LLM orchestration. No subprocess.              │
│                                                                       │
│   [Idle] ──start──▶ [ScenarioNeeded]                                 │
│        │                    │                                         │
│        │                    ▼                                         │
│        │         GenerateScenarioIntent ──► LLM Agent (creative)     │
│        │                    │                                         │
│        │                    ▼                                         │
│        │         [AudioNeeded] ──► RenderTTSIntent (×N scenes)       │
│        │                    │                                         │
│        │                    ▼                                         │
│        │         [TimingCheck] ──► ValidateTimingIntent              │
│        │              fail │ pass                                     │
│        │         ┌────────┘   └────────► [VisualNeeded]              │
│        │         │                     [ProductionNeeded]             │
│        │         │                     [AssemblyNeeded]               │
│        └─────────┘                     [Done]                        │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼ Intents flow downward
┌─────────────────────────────────────────────────────────────────────┐
│                   EFFECT HANDLER LAYER (Impure, Controlled)          │
│                                                                       │
│   Each Intent is handled by a typed handler. Multiple handlers       │
│   can interpret the same Intent (dry-run, real, audit, mock).        │
│                                                                       │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌───────────┐  │
│   │ VMHandler   │  │ TTSHandler  │  │ VideoHandler│  │ FileHandler│  │
│   │ (Vast.ai)   │  │ (Qwen3-TTS) │  │ (LTX-2.3)   │  │ (OTIO,etc)│  │
│   └─────────────┘  └─────────────┘  └─────────────┘  └───────────┘  │
│                                                                       │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                  │
│   │ AuditHandler│  │ DryRunHandler│  │ HumanHandler│                  │
│   │ (log all)   │  │ (simulate)   │  │ (HITL pause)│                  │
│   └─────────────┘  └─────────────┘  └─────────────┘                  │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼ Results flow upward
┌─────────────────────────────────────────────────────────────────────┐
│                    EVENT STORE (Append-Only Log)                     │
│                                                                       │
│   (Intent, Handler, Result, SideEffects) tuples are appended.        │
│   OTIO timeline is a PROJECTION rebuilt from this log.               │
│                                                                       │
│   Recovery: replay log through handlers.                             │
│   Debug: read the Intent stream — not chat transcripts.              │
│   Test: run through DryRunHandler instead of VMHandler.              │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## The Intent ADT for the Documentary Pipeline

```python
from pydantic import BaseModel, Field
from typing import Literal, Union, assert_never

# ============================================================
# INTENTS (Pure ADTs — the LLM and state machine produce these)
# ============================================================

class GenerateScenario(BaseModel):
    action_type: Literal["generate_scenario"] = "generate_scenario"
    brief: str
    num_scenes: int = 5
    target_duration_sec: int = 300

class RenderTTS(BaseModel):
    action_type: Literal["render_tts"] = "render_tts"
    scene_id: str
    text: str
    voice: str

class RenderVideo(BaseModel):
    action_type: Literal["render_video"] = "render_video"
    scene_id: str
    prompt: str
    duration_sec: float
    lora_id: str = ""

class ProvisionWorker(BaseModel):
    action_type: Literal["provision_worker"] = "provision_worker"
    role: Literal["tts", "video"]
    gpu_type: str = "RTX_4090"
    min_vram_gb: int = 24

class ValidateTiming(BaseModel):
    action_type: Literal["validate_timing"] = "validate_timing"
    scene_id: str
    audio_path: str
    expected_duration: float

class RequestHumanApproval(BaseModel):
    action_type: Literal["request_human_approval"] = "request_human_approval"
    gate_name: str
    proposed_intent: Union[RenderVideo, RenderTTS]
    reason: str

class DoNothing(BaseModel):
    action_type: Literal["do_nothing"] = "do_nothing"
    reason: str

class RaiseError(BaseModel):
    action_type: Literal["raise_error"] = "raise_error"
    message: str
    severity: Literal["low", "medium", "high", "critical"] = "high"

Intent = Union[
    GenerateScenario,
    RenderTTS,
    RenderVideo,
    ProvisionWorker,
    ValidateTiming,
    RequestHumanApproval,
    DoNothing,
    RaiseError,
]
```

---

## Effect Handlers (Where Side Effects Live)

```python
from typing import Protocol

class EffectHandler(Protocol):
    """An effect handler interprets Intents. Multiple handlers can wrap."""
    async def handle(self, intent: Intent) -> Result:
        ...

class AuditHandler:
    """Always runs first. Logs every Intent."""
    def __init__(self, next_handler: EffectHandler, store: EventStore):
        self._next = next_handler
        self._store = store
    
    async def handle(self, intent: Intent) -> Result:
        self._store.append(AuditEntry(intent=intent))
        return await self._next.handle(intent)

class DryRunHandler:
    """Simulates side effects without executing. For testing."""
    def __init__(self, next_handler: EffectHandler):
        self._next = next_handler
    
    async def handle(self, intent: Intent) -> Result:
        if isinstance(intent, (ProvisionWorker, RenderVideo, RenderTTS)):
            return Result.ok(simulated=True)
        return await self._next.handle(intent)

class VMHandler:
    """Real Vast.ai provisioning and worker dispatch."""
    async def handle(self, intent: Intent) -> Result:
        match intent:
            case ProvisionWorker(role=role, gpu_type=gpu, min_vram_gb=vram):
                vm = await vastai.provision(gpu_type=gpu, min_vram=vram)
                return Result.ok(vm_id=vm.id, url=vm.url)
            case RenderTTS(scene_id=sid, text=text, voice=voice):
                worker = self._get_worker("tts")
                wav = await worker.tts(text, voice)
                return Result.ok(path=wav.path, duration_ms=wav.duration_ms)
            case RenderVideo(scene_id=sid, prompt=prompt, duration_sec=dur):
                worker = self._get_worker("video")
                mp4 = await worker.render(prompt, dur)
                return Result.ok(path=mp4.path, qa_score=mp4.qa_score)
            case _:
                raise ValueError(f"VMHandler cannot handle {intent.action_type}")
```

---

## Testing: Mini Production Runs with Real Failure Modes

**The user's testing philosophy:** No mocks. Every test is a mini production run. Predefined failure modes are successful tests.

**How this maps to algebraic effects:**

```python
# Test 1: TTS worker fails mid-render. Pipeline retries on new worker.
async def test_tts_worker_failure_recovery():
    store = EventStore()
    # DryRunHandler simulates everything EXCEPT injecting a worker failure
    handler = FaultInjectingHandler(
        DryRunHandler(VMHandler()),
        fault=RenderTTS(scene_id="S1", text="hello", voice="default"),
        fault_result=Result.err("CUDA OOM"),
    )
    
    state = PipelineState(effect_runner=handler, event_store=store)
    graph = build_pipeline_graph()
    
    # Run the full pipeline. The fault is injected at the TTS stage.
    # The state machine should: detect failure → provision new worker → retry.
    result = await graph.run(AudioDispatchNode(), state=state)
    
    # Assert: event stream contains ProvisionWorker, RenderTTS(fail), ProvisionWorker, RenderTTS(ok)
    events = store.read_stream()
    assert events[0].intent.action_type == "provision_worker"
    assert events[1].intent.action_type == "render_tts"
    assert events[1].result.is_err()
    assert events[2].intent.action_type == "provision_worker"
    assert events[3].intent.action_type == "render_tts"
    assert events[3].result.is_ok()
```

**No mocks.** The `FaultInjectingHandler` is a real effect handler. It interprets Intents. The test asserts on the Intent stream, not on mocked function calls.

---

## Migration: Fractal Units + Graph Containers

**The user's pattern:** Individual units of functionality are self-contained (inputs → outputs). Container units combine them into graphs. Fractal composition.

**How this maps:**

1. **Leaf Intents** — Self-contained. Single input, single output.
   - `RenderTTS(text) → wav_path`
   - `RenderVideo(prompt) → mp4_path`
   - `ValidateTiming(audio_path, expected) → pass/fail`

2. **Leaf Handlers** — Self-contained. Tested with DryRunHandler + fault injection.
   - `TTSHandlerTest` — runs RenderTTS through DryRunHandler, asserts on simulated output
   - `VMHandlerTest` — provisions a real VM on Vast.ai, asserts it boots and responds to /health

3. **Stage Nodes** (pydantic-graph) — Container units. Combine leaf Intents into a stage.
   - `AudioStageNode` = ProvisionWorker → RenderTTS(×N) → ValidateTiming
   - `VideoStageNode` = ProvisionWorker → RenderVideo(×N) → QAGate

4. **Pipeline Graph** — Top-level container. Combines stages.
   - `DocumentaryGraph` = Scenario → AudioStage → VideoStage → Assembly

**Testing at every level:**
- Leaf Intent + Handler = mini production run (may use real VM or DryRun)
- Stage Node = runs its Intents through composed handlers
- Pipeline Graph = runs full stages through DryRunHandler (fast) or real handlers (slow)

---

## Why This Is the Most Radical and Most Robust Change

| Property | Current (LLM Orchestrated) | Radical (Algebraic Effects) |
|----------|---------------------------|----------------------------|
| **Orchestration** | LLM chat transcript decides transitions | Deterministic state machine produces Intents |
| **Side effects** | LLM calls tools directly | Intents are pure data; handlers execute |
| **Recovery** | Hope OTIO is current; guess LLM intent | Replay Intent stream through handlers |
| **Testing** | Mock tool calls; assert on chat history | Run real handlers with fault injection; assert on Intent stream |
| **Debugging** | Read chat log | Read event log (structured, typed) |
| **HITL** | Exception-based interrupt | Explicit pause node in state machine |
| **Type safety** | String edge IDs, JSON state | `assert_never` exhaustive matching, native Python state |
| **Cost** | LLM tokens for every transition | Zero tokens for transitions |
| **Parallelism** | Blocking HTTP, agent loops | Concurrent Intent dispatch via `asyncio.gather` |

---

## What Gets Thrown Away

| Component | Why It Dies |
|-----------|-------------|
| DeepAgents / LangGraph orchestrator | LLM should not orchestrate deterministic flow |
| Strands GraphBuilder | String-based edges, restricted namespace, opaque execution |
| `agent.state` JSON restriction | State machine carries native Python objects |
| RecoveryShell ad-hoc logic | Deterministic replay replaces guessing |
| OTIO as ground truth | Becomes a projection of the event store |
| Direct HTTP tool calls | Become `RenderTTS` / `RenderVideo` Intents handled by VMHandler |

---

## What Stays

| Component | Why It Survives |
|-----------|-----------------|
| Strands `@tool` functions | Wrap them as Intents or use inside creative LLM agents |
| OTIO format | As a projection target and interchange format |
| Vast.ai provisioning logic | Becomes `VMHandler` |
| LLM scenario/visual agents | They become leaf nodes that produce creative content, not decisions |
| SnapshotStore schema | Upgraded to append-only Intent + Result log |

---

## Implementation Phases

**Phase 1: Intent ADT + Effect Handlers (3 days)**
- Define the full `Intent` union type
- Implement `VMHandler`, `TTSHandler`, `VideoHandler`, `FileHandler`
- Implement `AuditHandler`, `DryRunHandler`
- Build `EffectRunner` that composes handlers monadically

**Phase 2: pydantic-graph State Machine (3 days)**
- Define `PipelineState` dataclass
- Convert each stage to `BaseNode` subclasses
- Wire edges via return types
- Run shadow tests: old graph vs new graph on same inputs

**Phase 3: Event Store Supremacy (2 days)**
- Upgrade `SnapshotStore` to append-only Intent + Result log
- Build `OTIOProjection` that rebuilds timeline from events
- Verify: OTIO from projection == OTIO from file (parity test)

**Phase 4: Testing with Fault Injection (2 days)**
- Build `FaultInjectingHandler`
- Write tests for: worker failure, timeout, QA gate fail, human rejection
- All tests run full pipeline through DryRunHandler + injected faults

**Phase 5: HITL Integration (2 days)**
- Replace `interrupt_on` with `AwaitHumanDecisionNode`
- Build external controller that polls `graph.iter()`, renders UI, injects decision
- Test: pause at production gate → human edits prompt → resume → verify edit applied

**Total: ~12 days**

---

## The Honest Trade-off

**This is a 12-day rewrite that throws away months of DeepAgents integration.**

**Do this if:**
- You believe LLMs should generate content, not orchestrate pipelines
- You want crash recovery that actually works by replaying events
- You want to test pipeline logic without calling LLMs
- You want to debug by reading a structured event log, not a chat transcript
- You want type-safe transitions enforced by the compiler

**Do NOT do this if:**
- You need a working pipeline next week
- You are committed to DeepAgents/LangGraph for other reasons
- The current pipeline mostly works and you just need the json bug fixed

The fundamental question: **Do you want an LLM to orchestrate your pipeline, or do you want a deterministic algebraic effects system to orchestrate your pipeline?**

This plan chooses the algebraic effects system.
