> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Operator-Authored Content Pipeline

## Status: Production Feature Design

Date: 2026-05-27

This document describes a production workflow where human operators author documentary content in YAML, and agents retrieve and present that content as natural language. This is not a testing framework. It is a real production capability for directors and writers who want precise control over narration, scene structure, and pipeline configuration.

---

## 1. Production Use Case

A documentary director writes scene narration, expert commentary, and visual notes in structured YAML files. The scenario agent's job is to read these authored scripts and present them as natural language that feeds into the semantic parser and ultimately the OTIO timeline.

**Why an agent instead of direct YAML-to-effect conversion?**
- The parser expects natural language text, not structured YAML.
- The agent can adapt presentation based on pipeline state (GSA projections).
- The agent can handle edge cases (missing content, variable substitution, context-sensitive narration).
- The agent's output is indistinguishable from an LLM-generated script — it goes through the same parser, handler, and event store.

**Why todo tracking?**
- Documentaries have many scenes. The agent tracks which scenes have been presented.
- Multiple pipeline runs may be active. Per-run todo isolation prevents cross-contamination.
- The operator can inspect todo state to see pipeline progress.

**Why causal logging?**
- Production observability. Every agent action is recorded for audit, debugging, and cost analysis.
- Operators can trace which scene was presented when, by which agent, with what latency.

---

## 2. Ecosystem Verification

### 2.1 What Exists (Verified Against pydantic-deep==0.3.19)

| Component | Source | Verified |
|---|---|---|
| `AbstractCapability` | `pydantic_ai.capabilities` | Yes — base class with lifecycle hooks |
| `before_run` / `after_run` | `AbstractCapability` | Yes |
| `wrap_model_request` | `AbstractCapability` | Yes — can intercept model calls |
| `before_tool_execute` / `after_tool_execute` | `AbstractCapability` | Yes |
| `TodoToolset` | `pydantic_ai_todo` | Yes — add_todo, complete_todo, list_todos |
| `ContextManagerCapability` | `pydantic_ai_summarization` | Yes — auto context compression |
| `CostTracking` | `pydantic_ai_shields` | Yes — token/cost tracking |
| `HooksCapability` | `pydantic_deep.capabilities.hooks` | Yes — PRE_TOOL_USE, POST_TOOL_USE, etc. |
| `AgentMemoryToolset` | `pydantic_deep.toolsets.memory` | Yes — persistent memory files |
| `CheckpointToolset` | `pydantic_deep.toolsets.checkpointing` | Yes — save/restore checkpoints |

### 2.2 What Does Not Exist

| Invented in Previous Draft | Reality |
|---|---|
| `Component` class | No such class. Use `AbstractCapability`. |
| `ContentCapability` | No such class. Build a `Tool`, not a capability. |
| `CausalLogCapability` | No such class. Build real `AbstractCapability` subclass. |
| `LLMCapability` | No such class. Model passed via `model=` parameter. |
| `TodoCapability` | No such class. It's `TodoToolset`. |
| `AgentContext` with `.payload` | No such class. Use `RunContext` with `.deps`. |

---

## 3. Script Reader Tool

A real pydantic-ai tool that retrieves operator-authored content:

```python
# server/tools/script_reader.py
from pathlib import Path
import yaml
from pydantic_ai import Tool, RunContext
from pydantic_deep.deps import DeepAgentDeps

_SCRIPT_DIR = Path("authoring/agents")

async def read_script(ctx: RunContext[DeepAgentDeps], task_id: str, run_id: str) -> str:
    """Retrieve the director's authored script for a specific scene or task.
    
    The director writes natural language narration in YAML files.
    This tool reads the authored text, performs variable substitution,
    and returns it for presentation.
    
    Args:
        task_id: The scene or task identifier (e.g., "scene_1_opening")
        run_id: The pipeline run identifier for variable substitution
    
    Returns:
        The authored natural language text, ready for presentation.
    """
    agent_name = getattr(ctx.deps, 'agent_name', 'unknown')
    script_path = _SCRIPT_DIR / f"{agent_name}.yaml"
    
    if not script_path.exists():
        return f"No script file found for agent {agent_name}."
    
    script = yaml.safe_load(script_path.read_text())
    
    for entry in script.get("entries", []):
        if entry.get("task_id") == task_id:
            text = entry.get("text", "")
            # Variable substitution: {run_id}, {agent_name}, etc.
            return text.format(
                run_id=run_id,
                agent_name=agent_name,
            )
    
    return f"No script entry found for task '{task_id}'."

script_reader_tool = Tool(read_script, takes_ctx=True)
```

**Authoring format:**

```yaml
# authoring/agents/scenario.yaml
agent: scenario
entries:
  - task_id: "scene_1_opening"
    text: |
      Scene 1, V1 Hook narration: "In 1924, the world stood at a crossroads."
      V2 Expert narration: "The Dawes Plan of 1924 restructured German reparations."
      V3 Storyteller narration: "Imagine a banker in 1924, staring at ledgers..."
      Visual notes: Close-up of ledger pages, archival footage of Berlin.
      Dopamine hook: "What if the entire world economy depended on one man's signature?"
      Duration: 30 seconds.
      
  - task_id: "scene_2_crisis"
    text: |
      Scene 2, V1 Hook narration: "Then came the crash."
      V2 Expert narration: "Black Tuesday, October 29, 1929."
      V3 Storyteller narration: "The ticker tape machines could not keep up."
      Visual notes: Slow-motion ticker tape, panicked traders.
      Dopamine hook: "In 24 hours, $14 billion vanished."
      Duration: 25 seconds.
```

---

## 4. Agent Design

### 4.1 Role-Based System Prompt

The agent is framed as a "script narrator" — a production role, not a test harness:

```python
SCENARIO_AGENT_PROMPT = """You are the scenario narrator for the documentary pipeline.

Your job is to present scene scripts written by the director.

You have two tools:
- read_script(task_id, run_id): Retrieves the director's authored text for a scene
- list_todos(): Shows your current task list
- complete_todo(id): Marks a scene as presented

Workflow:
1. Check your task list for pending scenes
2. For the next pending scene, retrieve the script with read_script
3. Present the narration naturally, preserving the director's intent and voice
4. Mark the scene complete

Guidelines:
- Present the director's text faithfully. Do not invent scenes or alter facts.
- You may add natural transitions between segments ("Now, the expert explains...")
- You emit ONLY natural language. No JSON, XML, markers, or structured formats.
- Your output is parsed by a semantic extractor. Present clearly so it extracts correctly.
- If a scene is missing from the script, say so and move to the next.
"""
```

### 4.2 Todo Persistence

Agents track scenes via the todo list. Todos persist across wake cycles:

```python
# server/todo_persistence.py
import json
from pathlib import Path

_TODO_DIR = Path("./agent_workspace/.todos")

def load_todos(run_id: str, agent_name: str) -> list[dict]:
    path = _TODO_DIR / f"{run_id}_{agent_name}.json"
    if path.exists():
        return json.loads(path.read_text())
    return _initialize_todos_from_script(agent_name)

def save_todos(run_id: str, agent_name: str, todos: list) -> None:
    _TODO_DIR.mkdir(parents=True, exist_ok=True)
    path = _TODO_DIR / f"{run_id}_{agent_name}.json"
    serializable = []
    for t in todos:
        if hasattr(t, 'model_dump'):
            serializable.append(t.model_dump())
        elif hasattr(t, '__dict__'):
            serializable.append({k: v for k, v in t.__dict__.items() if not k.startswith('_')})
        else:
            serializable.append(dict(t))
    path.write_text(json.dumps(serializable, default=str))

def _initialize_todos_from_script(agent_name: str) -> list[dict]:
    script_path = Path("authoring/agents") / f"{agent_name}.yaml"
    if not script_path.exists():
        return []
    script = yaml.safe_load(script_path.read_text())
    todos = []
    for i, entry in enumerate(script.get("entries", [])):
        todos.append({
            "id": entry.get("task_id", f"task_{i}"),
            "title": entry.get("task_id", f"Scene {i}"),
            "status": "pending",
        })
    return todos
```

### 4.3 HTTP Service Integration

The agent is exposed as an HTTP service per architecture requirements:

```python
# server/agents/scenario_agent.py
from fastapi import FastAPI
from pydantic import BaseModel
from pydantic_deep import create_deep_agent, DeepAgentDeps, StateBackend
from server.tools.script_reader import script_reader_tool
from server.capabilities.causal_log import CausalLogCapability
from server.todo_persistence import load_todos, save_todos

app = FastAPI()
AGENT_NAME = "scenario"

class WakePayload(BaseModel):
    run_id: str
    notification_type: str = "wake"
    context: dict = {}

@app.get("/")
async def health():
    return {"status": "ok", "agent": AGENT_NAME}

@app.post("/")
async def wake(payload: WakePayload):
    run_id = payload.run_id
    todos = load_todos(run_id, AGENT_NAME)
    
    agent = create_deep_agent(
        model="openrouter:deepseek/deepseek-v4-flash",
        model_settings={"temperature": 0.0},
        tools=[script_reader_tool],
        capabilities=[CausalLogCapability(run_id=run_id)],
        instructions=SCENARIO_AGENT_PROMPT,
        backend=StateBackend(root_dir="./agent_workspace"),
        include_subagents=False,
        web_search=False,
        web_fetch=False,
    )
    
    deps = DeepAgentDeps(backend=StateBackend(root_dir="./agent_workspace"))
    deps.todos = todos
    deps.agent_name = AGENT_NAME
    deps.run_id = run_id
    
    result = await agent.run(
        f"Present the next scene for run {run_id}.",
        deps=deps,
    )
    
    save_todos(run_id, AGENT_NAME, deps.todos)
    return {"text": result.output}
```

---

## 5. CausalLogCapability (Production Observability)

Real `AbstractCapability` subclass for logging agent lifecycle:

```python
# server/capabilities/causal_log.py
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import RunContext

class CausalLogCapability(AbstractCapability[Any]):
    """Production observability capability.
    
    Records agent lifecycle events to JSONL for audit, debugging,
    and performance analysis. Runs in all environments.
    """
    
    def __init__(self, run_id: str, output_dir: str = "./causal_logs"):
        self.run_id = run_id
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.output_dir / f"{run_id}.jsonl"
        self._sequence = 0
    
    def _emit(self, event_type: str, data: dict) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "seq": self._sequence,
            "type": event_type,
            "run_id": self.run_id,
            **data,
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        self._sequence += 1
    
    async def before_run(self, ctx: RunContext[Any]) -> None:
        agent = getattr(ctx.deps, 'agent_name', 'unknown')
        self._emit("agent_wake", {"agent": agent, "run_step": ctx.run_step})
    
    async def after_run(self, ctx: RunContext[Any], *, result: Any) -> Any:
        agent = getattr(ctx.deps, 'agent_name', 'unknown')
        output = str(result.output) if hasattr(result, 'output') else str(result)
        self._emit("agent_output", {
            "agent": agent,
            "text_length": len(output),
            "text_preview": output[:200],
        })
        return result
    
    async def wrap_model_request(self, ctx, *, request_context, handler) -> Any:
        agent = getattr(ctx.deps, 'agent_name', 'unknown')
        model_name = getattr(getattr(request_context, 'model', None), 'model_name', 'unknown')
        self._emit("model_request_start", {"agent": agent, "model": model_name})
        start = datetime.now(timezone.utc)
        response = await handler(request_context)
        latency_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        self._emit("model_request_end", {
            "agent": agent,
            "model": model_name,
            "latency_ms": latency_ms,
        })
        return response
    
    async def before_tool_execute(self, ctx, *, call, tool_def, args) -> Any:
        agent = getattr(ctx.deps, 'agent_name', 'unknown')
        self._emit("tool_call_start", {
            "agent": agent,
            "tool": call.tool_name,
        })
        return args
    
    async def after_tool_execute(self, ctx, *, call, tool_def, args, result) -> Any:
        agent = getattr(ctx.deps, 'agent_name', 'unknown')
        self._emit("tool_call_end", {
            "agent": agent,
            "tool": call.tool_name,
        })
        return result
    
    async def on_tool_execute_error(self, ctx, *, call, tool_def, args, error) -> Any:
        agent = getattr(ctx.deps, 'agent_name', 'unknown')
        self._emit("tool_call_error", {
            "agent": agent,
            "tool": call.tool_name,
            "error_type": type(error).__name__,
        })
        raise error
```

---

## 6. Ecosystem Components Assessment

### 6.1 TodoToolset (pydantic-ai-todo) — USE

**Status:** Included by default. Provides `add_todo`, `complete_todo`, `list_todos`.

**Use:** Scene tracking. Each authored scene is a todo. The agent completes them sequentially.

### 6.2 ContextManagerCapability (pydantic-ai-summarization) — USE

**Status:** Included by default (`context_manager=True`).

**Use:** Long documentaries have many scenes. Message history grows. ContextManager auto-compresses before token limits are hit.

### 6.3 CostTracking (pydantic-ai-shields) — USE

**Status:** Included by default (`cost_tracking=True`).

**Use:** Monitor production costs. The architecture has budget projection — cost tracking validates actual spend against projected.

### 6.4 HooksCapability (pydantic-deep) — USE FOR SAFETY

**Status:** Optional.

**Use:** Security hooks. Deny dangerous tools (e.g., `execute`) in environments where they should not run.

```python
from pydantic_deep import Hook, HookEvent, HooksCapability

Hook(
    event=HookEvent.PRE_TOOL_USE,
    handler=lambda inp: HookResult(allow=inp.tool_name != "execute"),
)
```

### 6.5 CheckpointToolset (pydantic-deep) — MAYBE USE

**Status:** Optional (`include_checkpoints=True`).

**Use:** Save conversation state after each scene. If an agent crashes mid-pipeline, rewind and resume. Useful for long documentary productions.

### 6.6 AgentMemoryToolset (pydantic-deep) — SKIP

**Status:** Optional (`include_memory=True` by default in some configs).

**Verdict:** Redundant. Pipeline state lives in EventStoreDB. Agent memory files add another state source.

### 6.7 SubAgentToolset (subagents_pydantic_ai) — SKIP

**Status:** Included by default (`include_subagents=True`).

**Verdict:** Disable with `include_subagents=False`. Our architecture uses external HTTP agents, not internal subagents.

### 6.8 TeamToolset (pydantic-deep) — SKIP

**Status:** Optional (`include_teams=False` by default).

**Verdict:** Parallelism handled by Provisioner + VM workers. Agent teams would be a second mechanism.

### 6.9 ImproveToolset (pydantic-deep) — SKIP

**Status:** Optional (`include_improve=False` by default).

**Verdict:** Self-improvement is not relevant for script narration.

### 6.10 BrowserToolset / LiteparseToolset — SKIP

**Verdict:** Not relevant for documentary pipeline.

---

## 7. Pipeline Orchestrator

The orchestrator is a production automation component that replaces manual operator intervention. It is also a pydantic-deep agent.

### 7.1 Tools

```python
# server/tools/orchestrator_tools.py
from pydantic_ai import Tool
import httpx

async def query_gsa(run_id: str, gsa_url: str = "http://localhost:8000") -> dict:
    """Query the Global State Agent for current pipeline projections."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{gsa_url}/?run_id={run_id}")
        return resp.json()

async def wake_agent(agent_url: str, payload: dict) -> str:
    """Send wake notification to an agent."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(agent_url, json=payload)
        return f"Wake sent. Status: {resp.status_code}"

query_gsa_tool = Tool(query_gsa)
wake_agent_tool = Tool(wake_agent)
```

### 7.2 System Prompt

```python
ORCHESTRATOR_PROMPT = """You are the pipeline orchestrator.

Your job is to manage the documentary pipeline by waking agents when conditions are met.

You have two tools:
- query_gsa(run_id): Returns current pipeline state (projections, events, budget)
- wake_agent(agent_url, payload): Sends wake notification to an agent

Rules (prompt-based, no state machine):
- If no PipelineStarted event exists, wake the scenario agent.
- If script has unfilled slots, wake the scenario agent.
- If script is complete but audio has dirty blocks, wake the audio agent.
- If audio reconciliation is complete but video is unfilled, wake the video agent.
- If all slots are filled, wake the assembly agent.
- If PipelineComplete exists, take no action.

Always query GSA before deciding. Do not assume state.
Emit the agent name to wake, or "none" if no action is needed.
"""
```

### 7.3 Event-Driven Execution

The orchestrator makes one decision per event:

```python
# server/orchestrator/handler.py
from pydantic_deep import create_deep_agent, DeepAgentDeps, StateBackend
from server.tools.orchestrator_tools import query_gsa_tool, wake_agent_tool
from server.capabilities.causal_log import CausalLogCapability

async def handle_pipeline_event(run_id: str, event_kind: str):
    """Handle a single pipeline event by invoking the orchestrator agent."""
    agent = create_deep_agent(
        model="openrouter:deepseek/deepseek-v4-flash",
        model_settings={"temperature": 0.0},
        tools=[query_gsa_tool, wake_agent_tool],
        capabilities=[CausalLogCapability(run_id=run_id)],
        instructions=ORCHESTRATOR_PROMPT,
        backend=StateBackend(root_dir="./agent_workspace"),
        include_subagents=False,
        web_search=False,
        web_fetch=False,
    )
    
    deps = DeepAgentDeps(backend=StateBackend(root_dir="./agent_workspace"))
    deps.agent_name = "orchestrator"
    deps.run_id = run_id
    
    result = await agent.run(
        f"Pipeline event: {event_kind} for run {run_id}. Query GSA and decide.",
        deps=deps,
    )
    
    return result.output.strip()
```

**Infrastructure subscription loop:**

```python
# server/orchestrator/subscriber.py
from server.orchestrator.handler import handle_pipeline_event

async def run_orchestrator_subscription(run_id: str):
    from server.event_store import get_event_store_client
    esdb = get_event_store_client()
    
    async for event in esdb.subscribe_to_stream(f"pipeline-{run_id}"):
        decision = await handle_pipeline_event(run_id, event.kind)
        
        if decision.lower() == "none":
            continue
        elif decision.lower().startswith("wake "):
            target = decision.split("wake ")[1].strip()
            # URL resolution and POST handled by the agent's wake_agent tool
            print(f"Orchestrator directed wake: {target}")
        elif event.kind == "PipelineComplete":
            print(f"Pipeline {run_id} complete.")
            break
```

---

## 8. Causal Log Inspector

Production tooling for operators to inspect pipeline execution:

```python
# server/causal_log_inspector.py
import json
from pathlib import Path
from collections import defaultdict

def inspect_run(run_id: str, log_dir: str = "./causal_logs") -> dict:
    """Inspect a pipeline run from its causal log.
    
    Returns event timeline, agent latencies, tool call counts,
    and error summary. Used by operators for debugging.
    """
    path = Path(log_dir) / f"{run_id}.jsonl"
    if not path.exists():
        return {"error": f"No causal log found for run {run_id}"}
    
    events = [json.loads(line) for line in path.read_text().strip().split("\n") if line.strip()]
    
    timeline = [(e["ts"], e["type"], e.get("agent", "system")) for e in events]
    
    agent_latencies = defaultdict(list)
    for e in events:
        if e["type"] == "model_request_end" and "latency_ms" in e:
            agent_latencies[e.get("agent", "unknown")].append(e["latency_ms"])
    
    tool_counts = defaultdict(int)
    for e in events:
        if e["type"] == "tool_call_start":
            tool_counts[e.get("tool", "unknown")] += 1
    
    errors = [e for e in events if e["type"] in ("agent_error", "tool_call_error")]
    
    return {
        "run_id": run_id,
        "total_events": len(events),
        "timeline": timeline,
        "agent_latencies": {k: {"count": len(v), "avg_ms": sum(v)/len(v)} for k, v in agent_latencies.items()},
        "tool_counts": dict(tool_counts),
        "errors": len(errors),
        "error_details": errors,
    }

if __name__ == "__main__":
    import sys
    run_id = sys.argv[1] if len(sys.argv) > 1 else "prod-001"
    report = inspect_run(run_id)
    print(json.dumps(report, indent=2, default=str))
```

---

## 9. Minimal Agent Configuration

```python
from pydantic_deep import create_deep_agent, DeepAgentDeps, StateBackend
from server.capabilities.causal_log import CausalLogCapability
from server.tools.script_reader import script_reader_tool

agent = create_deep_agent(
    model="openrouter:deepseek/deepseek-v4-flash",
    model_settings={"temperature": 0.0},
    tools=[script_reader_tool],
    capabilities=[CausalLogCapability(run_id=run_id)],
    instructions=AGENT_PROMPT,
    backend=StateBackend(root_dir="./agent_workspace"),
    
    # Production essentials
    include_todo=True,
    context_manager=True,
    cost_tracking=True,
    
    # Remove unnecessary complexity
    include_subagents=False,
    include_teams=False,
    include_improve=False,
    include_liteparse=False,
    web_search=False,
    web_fetch=False,
)
```

---

## 10. File Structure

```
server/
├── agents/
│   ├── scenario_agent.py          # FastAPI + pydantic-deep agent
│   ├── audio_agent.py             # FastAPI + pydantic-deep agent
│   ├── video_agent.py             # FastAPI + pydantic-deep agent
│   ├── assembly_agent.py          # FastAPI + pydantic-deep agent
│   └── provisioner_agent.py       # FastAPI + pydantic-deep agent (real bash)
│
├── capabilities/
│   └── causal_log.py              # REAL AbstractCapability subclass
│
├── tools/
│   ├── script_reader.py           # read_script (real pydantic-ai Tool)
│   └── orchestrator_tools.py      # query_gsa, wake_agent (real pydantic-ai Tools)
│
├── todo_persistence.py            # Load/save todos keyed by run_id+agent
│
├── orchestrator/
│   ├── handler.py                 # One decision per event (agentic)
│   └── subscriber.py              # ESDB subscription loop (infrastructure)
│
└── causal_log_inspector.py        # Production observability tooling

authoring/
└── agents/
    ├── scenario.yaml              # Director-authored scripts
    ├── audio.yaml
    ├── video.yaml
    ├── assembly.yaml
    └── provisioner.yaml

causal_logs/
└── {run_id}.jsonl                 # Production audit logs
```

---

## 11. No Production Code Changes

All additions in new directories. No modifications to existing production code.

---

## 12. Verification via Causal Log

Pipeline correctness is verified by inspecting the causal log — a production observability artifact:

```python
# Example: Verify a run completed expected sequence
def verify_run_sequence(run_id: str) -> bool:
    report = inspect_run(run_id)
    
    # Check for errors
    if report["errors"] > 0:
        print(f"ERRORS: {report['errors']} errors detected")
        return False
    
    # Check all agents participated
    expected_agents = {"scenario", "audio", "video", "assembly", "orchestrator"}
    actual_agents = set(report["agent_latencies"].keys())
    if not expected_agents.issubset(actual_agents):
        print(f"MISSING AGENTS: {expected_agents - actual_agents}")
        return False
    
    # Check tool usage
    if report["tool_counts"].get("read_script", 0) < 1:
        print("No script reads detected")
        return False
    
    print(f"Run {run_id}: VERIFIED")
    return True
```

This verification uses production logs, not test assertions. The same inspector runs in production to validate pipeline health.
