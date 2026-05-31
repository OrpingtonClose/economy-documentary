> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture Proposal — Unified pydantic-ai Documentary Pipeline

> A concrete, implementable design that makes the pipeline actually work. Every agent is a full pydantic-ai agent. Every component uses the same framework. No exceptions.

---

## 0. The Core Insight

**The current architecture is fractured.**

| Problem | Root Cause | Fix |
|---|---|---|
| Strands Graph is opaque | Custom graph engine with no persistence | pydantic-graph (native snapshots + resume) |
| VM agent is "not an LLM" | Contradicts canonical architecture | VM agent IS a pydantic-ai agent |
| Effect parsing uses regex | Violates "types implied in text" | instructor in `Mode.JSON` (semantic extraction) |
| Tool guardrails are hard blocks | Code constrains agent decisions | `prepare_tools` filters pre-model; agent still decides |
| State machine is the orchestrator | Violates architecture | State machine constructs prompts; orchestrator decides |
| No parallel execution | Sequential graph | pydantic-graph `broadcast`/`join` for audio+video |
| No persistence | `resume=False` hardcoded | `BaseStatePersistence` + `iter_from_persistence()` |
| Tracing is SQLite | User wants JSONL | `sys.monitoring` + JSONL per run |

**The fix: Unify everything under pydantic-ai + pydantic-graph + instructor.**

---

## 1. The Unified Agent Model

### 1.1 Every Agent is a pydantic-ai Agent

```
Pipeline Agents (orchestrator host)
├── Scenario Agent      → pydantic-ai Agent, model: deepseek-v4-flash
├── Audio Agent         → pydantic-ai Agent, model: deepseek-v4-flash
├── Video Agent         → pydantic-ai Agent, model: deepseek-v4-flash
├── Provisioner Agent   → pydantic-ai Agent, model: deepseek-v4-flash
├── OTIO Gate Agent     → pydantic-ai Agent, model: deepseek-v4-flash
├── Assembly Agent      → pydantic-ai Agent, model: deepseek-v4-flash
├── Maintainer Agent    → pydantic-ai Agent, model: deepseek-v4-flash
└── Orchestrator Agent  → pydantic-ai Agent, model: deepseek-v4-flash

VM Agents (GPU instances)
├── TTS VM Agent        → pydantic-ai Agent, model: deepseek-v4-flash
└── LTX VM Agent        → pydantic-ai Agent, model: deepseek-v4-flash
```

**Why deepseek-v4-flash for ALL agents?**
- $0.10/M input, $0.20/M output — 35–100x cheaper than OpenAI/Anthropic
- 284B total params, 13B activated — efficient for both reasoning and extraction
- 1M context window — enough for complex prompts and QA context
- Fast enough for real-time decisions
- One model string everywhere — no fragmentation, no confusion

### 1.2 The VM Agent is NOT a "Dumb Inference Endpoint"

The canonical architecture says: **"It is a LLM, how can't it be otherwise."**

The VM agent is a full pydantic-ai agent that:
1. **Boots** — installs deps, loads inference models
2. **Self-monitors** — uses reasoning to decide if it is healthy
3. **Pulls jobs** — from the pipeline's HTTP surface
4. **QA's output** — uses `result_validator` to judge its own work
5. **Retries** — uses `retries=` parameter for self-correction
6. **Reports** — sends `JobCompleted` or `JobFailed` back to pipeline
7. **Self-destructs** — reasons about overseer absence, decides to die

```python
from pydantic_ai import Agent, RunContext, ModelRetry
from pydantic import BaseModel, Field

class JobResult(BaseModel):
    status: Literal["completed", "failed"]
    artifact_url: str = ""
    error_message: str = ""
    qa_score: float = Field(ge=0.0, le=1.0)

class VMDeps:
    worker_url: str
    overseer_url: str
    last_overseer_poll: float
    model_loaded: bool = False

vm_agent = Agent(
    'deepseek/deepseek-v4-flash',
    deps_type=VMDeps,
    output_type=JobResult,
    retries=2,
    system_prompt="""
    You are a GPU worker agent. Your job is to:
    1. Run inference jobs (TTS or video generation)
    2. QA your own output using result_validator
    3. Report results back to the pipeline
    4. Monitor the overseer heartbeat
    5. Self-destruct if the overseer is gone for ~15 minutes
    """,
)

@vm_agent.tool
async def run_inference(ctx: RunContext[VMDeps], job_text: str) -> str:
    """Run TTS or LTX inference. Returns raw bytes or path."""
    ...

@vm_agent.tool
async def check_overseer(ctx: RunContext[VMDeps]) -> str:
    """Poll the overseer. Returns health status."""
    ...

@vm_agent.result_validator
async def validate_output(ctx: RunContext[VMDeps], result: JobResult) -> JobResult:
    """Self-QA: judge output quality. Reject if below threshold."""
    if result.qa_score < 0.7:
        raise ModelRetry(f"QA score {result.qa_score} below threshold. Retry with adjustments.")
    return result
```

### 1.3 No Framework Fragmentation

| Current | Proposed |
|---|---|
| Strands agents for pipeline | pydantic-ai agents for pipeline |
| Custom HTTP worker (no LLM) | pydantic-ai agent on VM |
| Strands Graph (opaque) | pydantic-graph (native snapshots) |
| instructor for parsing | instructor for parsing (keep) |
| sqlite tracing | JSONL tracing |

**One framework. One mental model. One API.**

---

## 2. The Unified Orchestration Layer: pydantic-graph

### 2.1 Graph Topology

```
StartNode → ScenarioStep → OtioGateStep
                              │
                              ▼
                    ┌─────────────────┐
                    │     Fork        │ ← broadcast to audio + video
                    └─────────────────┘
                          │       │
                          ▼       ▼
                    AudioStep   VideoStep  ← parallel, concurrent
                          │       │
                          ▼       ▼
                    ┌─────────────────┐
                    │      Join       │ ← collect results
                    └─────────────────┘
                              │
                              ▼
                        OtioGateStep
                              │
                              ▼
                        AssemblyStep
                              │
                              ▼
                          EndNode
```

### 2.2 Parallel Execution: Broadcast + Join

pydantic-graph's `GraphRun` uses `anyio.create_task_group()` for **true parallel execution**.

```python
from dataclasses import dataclass
from pydantic_graph import Graph, GraphRunContext, BaseNode, End
from pydantic_graph.beta import GraphBuilder
from pydantic_graph.beta.join import reduce_list_append

@dataclass
class PipelineState:
    events: list[Effect] = field(default_factory=list)
    timeline_path: str = ""
    run_id: str = ""
    brief: str = ""

@dataclass
class PipelineDeps:
    model: str = "deepseek-v4-flash"
    api_key: str = ""

# GraphBuilder v2 API
builder = GraphBuilder(state_type=PipelineState, deps_type=PipelineDeps)

@builder.step
async def scenario_step(ctx: GraphRunContext[PipelineState, PipelineDeps]) -> OtioGateStep:
    agent = construct_agent("scenario", ctx.state, ctx.deps)
    result = await agent.run(ctx.state.brief)
    effects = parse_effects(result.output)
    ctx.state.events.extend(effects)
    return OtioGateStep()

@builder.step
async def otio_gate_step(ctx: GraphRunContext[PipelineState, PipelineDeps]) -> BroadcastNode:
    # OTIO gate validates effects, rebuilds read models
    ...
    return BroadcastNode()  # forks to audio + video

@builder.step
async def audio_step(ctx: GraphRunContext[PipelineState, PipelineDeps]) -> list[Effect]:
    agent = construct_agent("audio", ctx.state, ctx.deps)
    result = await agent.run(build_state_summary(ctx.state))
    effects = parse_effects(result.output)
    return effects

@builder.step
async def video_step(ctx: GraphRunContext[PipelineState, PipelineDeps]) -> list[Effect]:
    agent = construct_agent("video", ctx.state, ctx.deps)
    result = await agent.run(build_state_summary(ctx.state))
    effects = parse_effects(result.output)
    return effects

# Broadcast from OtioGateStep to both AudioStep and VideoStep
builder.add(
    builder.edge_from(OtioGateStep).broadcast(
        lambda b: [b.to(audio_step), b.to(video_step)]
    )
)

# Join: collect results from both branches
collect = builder.join(reduce_list_append, initial_factory=list[Effect])
builder.add(builder.edge_from(audio_step).to(collect))
builder.add(builder.edge_from(video_step).to(collect))

# After join, go to next OtioGateStep
builder.add(builder.edge_from(collect).to(OtioGateStep))

graph = builder.build()
```

### 2.3 State Persistence: Exact-Moment Resume

pydantic-graph v1 (deprecated but functional) has `BaseStatePersistence`:

```python
from pydantic_graph.persistence import BaseStatePersistence

class JsonlGraphPersistence(BaseStatePersistence[PipelineState, list[Effect]]):
    """Store snapshots to JSONL for exact-moment resume."""

    async def snapshot_node(self, run_id: str, node_id: str, state: PipelineState):
        snapshot = {
            "type": "node",
            "run_id": run_id,
            "node_id": node_id,
            "state": state_to_dict(state),
            "timestamp": time.time(),
        }
        append_jsonl(f"runs/{run_id}/snapshots.jsonl", snapshot)

    async def load_next(self, run_id: str) -> NodeSnapshot | None:
        # Find the latest 'created' snapshot for this run
        snapshots = read_jsonl(f"runs/{run_id}/snapshots.jsonl")
        for snap in reversed(snapshots):
            if snap["type"] == "node":
                return NodeSnapshot(
                    run_id=run_id,
                    node_id=snap["node_id"],
                    state=state_from_dict(snap["state"]),
                )
        return None

# Resume from exact moment
persistence = JsonlGraphPersistence()
async for node in graph.iter_from_persistence(persistence, run_id="abc123"):
    result = await graph_run.next(node)
```

**Key property:** Snapshots before every node. Resume from any point. No lost work.

---

## 3. The Unified Prompt System: State Machine + pydantic-ai

### 3.1 Dynamic System Prompt Construction

The state machine is a **prompt construction engine**, not the orchestrator.

```python
from pydantic_ai import Agent, RunContext
from statemachine import StateChart

class ProductionStateChart(StateChart):
    # ... states and transitions ...
    pass

# Rebuild state machine from events on every cycle
sm = rebuild_state_machine(events)
current_state = sm.current_state.id  # e.g., "audio_production"

# Construct agent with state-aware prompts
agent = Agent(
    'deepseek/deepseek-v4-flash',
    deps_type=PipelineState,
    prepare_tools=prepare_tools_for_state,
)

@agent.system_prompt
def base_persona(ctx: RunContext[PipelineState]) -> str:
    return "You are the audio agent. You formulate TTS jobs."

@agent.system_prompt
def state_instructions(ctx: RunContext[PipelineState]) -> str:
    # Agent NEVER sees the state name — only the instructions
    return STATE_INSTRUCTIONS[current_state]

@agent.system_prompt
def context_window(ctx: RunContext[PipelineState]) -> str:
    return build_state_summary(ctx.deps)
```

### 3.2 Per-State Tool Guardrails

```python
from pydantic_ai import Agent, RunContext
from pydantic_ai.tools import ToolDefinition

STATE_TOOL_ALLOWLISTS: dict[str, set[str]] = {
    "script_draft": {"write_file", "read_file"},
    "audio_production": {"queue_job", "read_file", "check_b2"},
    "provisioning": {"bash", "read_file"},
    "qa": {"read_file", "check_b2", "requeue_job"},
    "assembly": {"bash", "read_file", "check_b2", "merge_clips"},
}

def prepare_tools_for_state(
    ctx: RunContext[PipelineState],
    tool_defs: list[ToolDefinition]
) -> list[ToolDefinition] | None:
    allowed = STATE_TOOL_ALLOWLISTS.get(current_state, set())
    return [t for t in tool_defs if t.name in allowed]

agent = Agent(
    'deepseek/deepseek-v4-flash',
    deps_type=PipelineState,
    prepare_tools=prepare_tools_for_state,
)
```

**Key property:** Model never sees disallowed tools. If agent tries to call one, pydantic-ai rejects with explanation.

---

## 4. The Unified Effect Parser: instructor + DeepSeek v4-flash

### 4.1 Semantic Extraction, Not Tool Calling

```python
import instructor
from openai import OpenAI
from pydantic import BaseModel, Field
from typing import Literal, Annotated

class UpdateScript(BaseModel):
    type: Literal["UpdateScript"] = "UpdateScript"
    script_text: str

class GenerateNarrationAudio(BaseModel):
    type: Literal["GenerateNarrationAudio"] = "GenerateNarrationAudio"
    line_id: str
    narration_text: str
    voice_profile: str

Effect = Annotated[
    UpdateScript | GenerateNarrationAudio | ...,  # all effect types
    Field(discriminator="type")
]

# instructor in semantic mode (NOT tool calling)
client = instructor.from_openai(
    OpenAI(api_key=deepseek_key, base_url="https://api.deepseek.com"),
    mode=instructor.Mode.JSON,  # ← semantic extraction
)

def parse_effects(agent_text: str) -> list[Effect]:
    """Semantic extraction: read agent text, comprehend intent, emit typed effects."""
    effects = client.chat.completions.create(
        model="deepseek-v4-flash",
        response_model=list[Effect],
        messages=[
            {
                "role": "system",
                "content": (
                    "Read the agent's message and extract all effects implied in the text. "
                    "The agent describes actions in natural language. "
                    "Map those descriptions to typed effect objects."
                ),
            },
            {"role": "user", "content": agent_text},
        ],
        max_retries=2,
    )
    return effects
```

### 4.2 Why instructor, Not pydantic-ai, for Parsing

| | instructor | pydantic-ai |
|---|---|---|
| **Primary focus** | Structured extraction | Agent framework |
| **Best for** | "Text → typed object" | "Agent decides → calls tools" |
| **Validation** | Automatic + re-ask | Automatic + self-correction |
| **Use in pipeline** | Effect parser (reads agent text) | All agents (produce text) |

**The pipeline uses both:**
- **pydantic-ai** — agents reason, decide, write text
- **instructor** — parser reads that text, extracts effects

### 4.3 ClarificationRequest on Exhaustion

```python
from instructor.core.exceptions import InstructorRetryException

def parse_effects_safe(agent_text: str) -> list[Effect]:
    try:
        return parse_effects(agent_text)
    except InstructorRetryException as e:
        return [ClarificationRequest(
            type="ClarificationRequest",
            reason=f"Could not parse after {e.n_attempts} attempts.",
            original_text=agent_text,
        )]
```

---

## 5. The Unified Communication Protocol: Plain Text

### 5.1 HTTP Surface: GET / and POST / Only

Every agent exposes:
- `GET /` — probe, no trace, returns plain text status
- `POST /` — instruction, leaves trace in agent context

```python
from fastapi import FastAPI
from pydantic_ai import Agent

app = FastAPI()
agent: Agent | None = None

@app.get("/")
async def probe() -> str:
    """Probe: no trace, no state change."""
    return f"ok deepseek-v4-flash tts={'yes' if model_loaded else 'no'} vram={vram_used}/{vram_total}GB"

@app.post("/")
async def instruct(text: str) -> str:
    """Instruction: leaves trace in agent context."""
    result = await agent.run(text)
    return result.output  # plain text
```

### 5.2 No JSON Between Agents

- Agent writes natural language
- Pipeline reads natural language
- instructor extracts types from natural language
- JSON exists only inside the parser (invisible to agents)

---

## 6. The Unified Tracing System: sys.monitoring + JSONL

### 6.1 Automatic Low-Level Tracing

```python
import sys
import json
import time
from pathlib import Path

class JsonlTracer:
    """Trace all function calls in key modules to JSONL."""

    def __init__(self, output_path: Path, target_modules: set[str]):
        self.output_path = output_path
        self.target_modules = target_modules
        self.tool_id = sys.monitoring.CUSTOM_TOOLS + 1
        self._file = open(output_path, "a")

    def start(self):
        sys.monitoring.use_tool_id(self.tool_id, "pipeline_tracer")
        sys.monitoring.set_events(self.tool_id, sys.monitoring.PY_START | sys.monitoring.PY_RETURN)
        sys.monitoring.register_callback(self.tool_id, sys.monitoring.PY_START, self._on_start)
        sys.monitoring.register_callback(self.tool_id, sys.monitoring.PY_RETURN, self._on_return)

    def _on_start(self, code, instruction_offset):
        filename = code.co_filename
        if any(m in filename for m in self.target_modules):
            self._file.write(json.dumps({
                "event": "call",
                "function": code.co_name,
                "file": filename,
                "line": code.co_firstlineno,
                "timestamp": time.time(),
            }) + "\n")
            self._file.flush()
        return sys.monitoring.DISABLE

    def _on_return(self, code, instruction_offset, retval):
        # ... similar ...
        return sys.monitoring.DISABLE
```

### 6.2 Explicit High-Level Tracing

```python
class UnifiedTracer:
    """Explicit semantic events: tool calls, VM ops, agent decisions."""

    def __init__(self, run_id: str):
        self.path = Path(f"runs/{run_id}/events.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a")

    def log(self, event_type: str, **kwargs):
        self._file.write(json.dumps({
            "type": event_type,
            "timestamp": time.time(),
            **kwargs,
        }) + "\n")
        self._file.flush()

    def tool_call(self, agent: str, tool: str, args: dict):
        self.log("tool_call", agent=agent, tool=tool, args=args)

    def agent_decision(self, agent: str, decision: str, reason: str):
        self.log("agent_decision", agent=agent, decision=decision, reason=reason)

    def vm_op(self, op: str, vm_id: str, status: str):
        self.log("vm_op", op=op, vm_id=vm_id, status=status)
```

---

## 7. The Unified VM Lifecycle

### 7.1 VM Agent as Full pydantic-ai Agent

```python
# Runs ON the GPU VM, not in the pipeline
vm_agent = Agent(
    'deepseek/deepseek-v4-flash',
    deps_type=VMDeps,
    output_type=JobResult,
    retries=2,
    system_prompt="""
    You are a GPU worker agent. Your job is to:
    1. Monitor the overseer heartbeat via GET /
    2. Pull jobs from the pipeline via POST /
    3. Run inference (TTS or LTX)
    4. QA your own output using result_validator
    5. Report JobCompleted or JobFailed back to pipeline
    6. If overseer is gone for ~15 minutes, self-destruct
    """,
)

@vm_agent.tool
async def run_tts(ctx: RunContext[VMDeps], narration_text: str, voice: str) -> str:
    """Run Qwen3-TTS inference. Returns WAV bytes or B2 URL."""
    ...

@vm_agent.tool
async def run_ltx(ctx: RunContext[VMDeps], prompt: str, duration: float) -> str:
    """Run LTX-2.3 inference. Returns MP4 bytes or B2 URL."""
    ...

@vm_agent.tool
async def poll_overseer(ctx: RunContext[VMDeps]) -> str:
    """GET / on overseer. Returns status text."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(ctx.deps.overseer_url, timeout=5.0)
    return resp.text

@vm_agent.result_validator
async def validate_job(ctx: RunContext[VMDeps], result: JobResult) -> JobResult:
    """Self-QA: reject if output is garbage."""
    if result.status == "completed" and result.qa_score < 0.7:
        raise ModelRetry(f"QA score {result.qa_score} too low. Adjust and retry.")
    return result
```

### 7.2 Self-Destruct is Agentic, Not Timer-Based

```python
@vm_agent.tool
async def decide_self_destruct(ctx: RunContext[VMDeps]) -> str:
    """Reason about whether to self-destruct."""
    minutes_since_poll = (time.time() - ctx.deps.last_overseer_poll) / 60
    if minutes_since_poll > 15:
        # Agent decides: overseer is gone, I should die
        await destroy_vm()
        return "self_destructed"
    return f"overseer_alive last_seen={minutes_since_poll:.1f}min_ago"
```

**The agent reasons about intervention.** No procedural timer. No "if idle > 30 min, destroy." The agent decides.

---

## 8. The Unified Configuration Model

### 8.1 No Environment Variables

```python
# Hardcoded at module level
_DEFAULT_MODEL = "deepseek-v4-flash"
_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEFAULT_OUTPUT_DIR = "/Users/orpington/Documents/documentary-pipeline"
_DEFAULT_BUDGET = 5.0
_DEFAULT_MAX_NODES = 200
_API_KEY_PATH = os.path.expanduser("~/api_keys/LLMS/deepseek_api.txt")
```

### 8.2 One Model, All Roles

```python
from pydantic_ai.models.openai import OpenAIModel

# All agents use the same model
pipeline_model = OpenAIModel(
    'deepseek/deepseek-v4-flash',
    base_url='https://api.deepseek.com',
    api_key=read_key(),
)

# Effect parser uses the same model
parser_model = OpenAIModel(
    'deepseek/deepseek-v4-flash',
    base_url='https://api.deepseek.com',
    api_key=read_key(),
)
```

---

## 9. Implementation Roadmap

### Phase 1: Foundation (1 week)
- [ ] Install pydantic-ai + pydantic-graph in venv
- [ ] Create `PipelineState` dataclass
- [ ] Create `PipelineDeps` dataclass
- [ ] Create base agent factory with dynamic prompts
- [ ] Port scenario agent to pydantic-ai

### Phase 2: Graph + Parallelism (1 week)
- [ ] Define pydantic-graph nodes for all 7 agent types
- [ ] Implement broadcast/join for audio+video
- [ ] Implement `JsonlGraphPersistence`
- [ ] Wire `iter_from_persistence()` for resume

### Phase 3: Effect Parser (3 days)
- [ ] Define Pydantic effect models with discriminated unions
- [ ] Wire instructor in `Mode.JSON`
- [ ] Implement `parse_effects_safe()` with `ClarificationRequest`

### Phase 4: VM Agents (1 week)
- [ ] Port `gpu_worker.py` to pydantic-ai agent
- [ ] Implement self-monitoring tools
- [ ] Implement self-destruct decision
- [ ] Implement self-QA via `result_validator`

### Phase 5: Observability (3 days)
- [ ] Implement `sys.monitoring` auto-tracer
- [ ] Implement `UnifiedTracer` for semantic events
- [ ] Wire JSONL output to B2

### Phase 6: Integration (1 week)
- [ ] End-to-end test: brief → script → audio+video → assembly → MP4
- [ ] Verify resume from snapshot
- [ ] Verify parallel execution
- [ ] Verify VM agent self-destruct

---

## 10. Research Context

This proposal draws from 24+ searches across Brave, Exa, and Perplexity:

| Source | Key Finding |
|---|---|
| DeepSeek API Docs | v4-flash: $0.10/M input, $0.20/M output; 284B total, 13B activated; deepseek-chat deprecated |
| pydantic-ai Docs | Capabilities bundle tools/prompts/settings; `prepare_tools` for dynamic filtering; `result_validator` for self-QA |
| pydantic-graph Docs | `broadcast`/`join` for true parallel execution; `BaseStatePersistence` for exact-moment resume |
| Perplexity: pydantic-ai vs instructor | "Instructor for extraction, PydanticAI for agents" — both use Pydantic, different optimization targets |
| Perplexity: pydantic-ai handoff | Programmatic handoff (app code decides), not automatic; tool-based delegation for sub-tasks |
| Perplexity: pydantic-ai RunContext | `deps` for per-run context; separate state container for durable/concurrent state |
| Perplexity: pydantic-ai self-healing | `ModelRetry` for transient failures; Logfire/Sentry for observability; evals for regression detection |
| Perplexity: pydantic-ai HTTP tools | `@agent.tool` with `httpx.AsyncClient` for async HTTP calls |
| Brave: pydantic-ai graph End | `BaseNode[StateT, DepsT, InputT, End[OutputT]]` for terminal nodes |
| Brave: pydantic-ai FallbackModel | Different `ModelSettings` per model in fallback chain |

---

*Proposal version: 2026-05-17*
