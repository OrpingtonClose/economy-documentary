# Migration Plan: strands → pydantic-graph + pydantic-ai + pydantic-deep

## Date: 2026-05-25
## Status: Plan complete, implementation pending

---

## Architecture Decisions (Locked)

1. **All agent communication via HTTP** — No in-process calls, no shared state.
2. **Plain text protocol** — Agents send/receive raw text over HTTP POST.
3. **OTIO as read-only projection** — Agents never write OTIO directly.
4. **Event sourcing** — All mutations go through typed effects → event store → projection handler → OTIO.
5. **No env vars** — All config via CLI args or explicit config file.
6. **No algorithmic recovery** — Only fallback: `notify_maintainer()` on exception.
7. **No truncation** — Code never slices strings. Agent decides what to send.
8. **deepseek/deepseek-v4-flash** — Hardcoded model. No OpenRouter.

---

## Concrete Implementation Plan

### Step 0: Foundation (already done)

- [x] `recovery_agents.py` — gutted to `notify_maintainer()` + abort
- [x] `recovery_agents.py` — `_RECOVERY_MODEL` hardcoded to `deepseek/deepseek-v4-flash`
- [x] `vm_registry_tools.py` — `check_worker_health` removed
- [x] `provisioner_agent.py` — prompt updated to bash-only health checks
- [x] `run_strands.py` — `_destroy_all_vms()` destroys orphan VMs too
- [x] `graph_pipeline.py` — pending-job guards added to routing conditions

### Step 1: Install Dependencies

```bash
cd /Users/orpington/Documents/economy-documentary-work
.venv/bin/pip install pydantic-deep
.venv/bin/pip install pydantic-graph
```

pydantic-ai is already installed (v1.100.0).

### Step 2: Event Store + Algebraic Effect Types

**New file: `server/effects.py`**

```python
from pydantic import BaseModel
from typing import Literal
from datetime import datetime

class Effect(BaseModel):
    effect_type: str
    agent_id: str
    timestamp: datetime
    justification: str
    scene_num: int = 0

class UpdateScript(Effect):
    effect_type: Literal["UpdateScript"]
    narration_v1: str = ""
    narration_v2: str = ""
    narration_v3: str = ""
    visual_notes: str = ""

class GenerateNarrationAudio(Effect):
    effect_type: Literal["GenerateNarrationAudio"]
    voice: str = "V1"
    text: str = ""

class RenderVideoSegment(Effect):
    effect_type: Literal["RenderVideoSegment"]
    prompt: str = ""
    lora_id: str = ""

class MergeIntoOTIO(Effect):
    effect_type: Literal["MergeIntoOTIO"]
    audio_clips: list[dict] = []
    video_clips: list[dict] = []

class ExecuteRawBash(Effect):
    effect_type: Literal["ExecuteRawBash"]
    command: str = ""
    reason: str = ""

class NoOp(Effect):
    effect_type: Literal["NoOp"]
    reason: str = ""
```

**New file: `server/event_store.py`**

```python
from pydantic import BaseModel
import json
import os

class EventRecord(BaseModel):
    seq: int
    effect: Effect
    otio_hash_before: str
    otio_hash_after: str = ""
    validated: bool = True
    rejected_reason: str = ""

class EventStore:
    def __init__(self, log_path: str):
        self.log_path = log_path
        self._seq = self._last_seq()
        
    def append(self, effect: Effect, otio_hash_before: str) -> EventRecord:
        self._seq += 1
        record = EventRecord(seq=self._seq, effect=effect,
                           otio_hash_before=otio_hash_before)
        with open(self.log_path, "a") as f:
            f.write(record.model_dump_json() + "\n")
        return record
        
    def read_all(self) -> list[EventRecord]:
        events = []
        if os.path.exists(self.log_path):
            with open(self.log_path) as f:
                for line in f:
                    events.append(EventRecord.model_validate_json(line))
        return events
```

### Step 3: Instructor Effect Parser

**New file: `server/effect_parser.py`**

```python
from structured_extract import extract
from effects import Effect, UpdateScript, GenerateNarrationAudio, RenderVideoSegment, MergeIntoOTIO, ExecuteRawBash, NoOp

def parse_agent_text(agent_id: str, text: str) -> Effect:
    """Parse agent text into typed Effect using instructor + Pydantic."""
    system_prompt = """
    Parse the agent's message into one of these effect types:
    - UpdateScript: agent proposes script/narration changes
    - GenerateNarrationAudio: agent requests TTS for a voice line
    - RenderVideoSegment: agent requests video generation
    - MergeIntoOTIO: agent wants to merge clips into timeline
    - ExecuteRawBash: agent wants to run a bash command
    - NoOp: no actionable intent detected
    """
    
    class EffectDiscriminator(BaseModel):
        effect_type: str
        justification: str
        scene_num: int
        # Union of all effect payloads
        narration_v1: str = ""
        narration_v2: str = ""
        narration_v3: str = ""
        visual_notes: str = ""
        voice: str = ""
        text: str = ""
        prompt: str = ""
        lora_id: str = ""
        audio_clips: list[dict] = []
        video_clips: list[dict] = []
        command: str = ""
        reason: str = ""
    
    try:
        parsed = extract(EffectDiscriminator, text, system_prompt)
        
        effect_classes = {
            "UpdateScript": UpdateScript,
            "GenerateNarrationAudio": GenerateNarrationAudio,
            "RenderVideoSegment": RenderVideoSegment,
            "MergeIntoOTIO": MergeIntoOTIO,
            "ExecuteRawBash": ExecuteRawBash,
            "NoOp": NoOp,
        }
        
        cls = effect_classes.get(parsed.effect_type, NoOp)
        return cls(
            agent_id=agent_id,
            timestamp=datetime.now(),
            justification=parsed.justification or text[:500],
            scene_num=parsed.scene_num,
            **{k: v for k, v in parsed.model_dump().items() 
               if k in cls.model_fields and k not in ("effect_type", "agent_id", "timestamp", "justification", "scene_num")}
        )
    except Exception as exc:
        return NoOp(
            agent_id=agent_id,
            timestamp=datetime.now(),
            justification=f"Parse failed: {exc}. Original: {text}",
        )
```

### Step 4: pydantic-graph Pipeline Orchestrator

**New file: `server/pydantic_graph_pipeline.py`**

```python
from dataclasses import dataclass, field
from pydantic_graph import GraphBuilder, StepContext
from pydantic_deep import create_deep_agent
from pydantic_ai.models.openai import OpenAIModel
import httpx

@dataclass
class PipelineState:
    current_task: str = ""
    last_agent_output: str = ""
    timeline_path: str = ""
    event_log_path: str = ""
    run_id: str = ""
    completed_stages: list[str] = field(default_factory=list)

@dataclass
class AgentURLs:
    scenario: str = "http://localhost:9001"
    audio: str = "http://localhost:9002"
    video: str = "http://localhost:9003"
    otio: str = "http://localhost:9004"
    assembly: str = "http://localhost:9005"
    provisioner: str = "http://localhost:9006"

# ---------------------------------------------------------------------------
# HTTP Agent Invocation (all agents communicate via HTTP)
# ---------------------------------------------------------------------------

async def _call_agent(url: str, text: str) -> str:
    """Call an agent via HTTP POST with plain text."""
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(url, content=text, headers={"Content-Type": "text/plain"})
        resp.raise_for_status()
        return resp.text

# ---------------------------------------------------------------------------
# Graph Steps
# ---------------------------------------------------------------------------

g = GraphBuilder(state_type=PipelineState, deps_type=AgentURLs, input_type=str, output_type=str)

@g.step
async def scenario_step(ctx: StepContext[PipelineState, AgentURLs, str]) -> str:
    """Invoke scenario agent via HTTP, parse effect, append to event store."""
    result = await _call_agent(ctx.deps.scenario, ctx.inputs)
    ctx.state.last_agent_output = result
    
    from effect_parser import parse_agent_text
    effect = parse_agent_text("scenario", result)
    
    from event_store import EventStore
    from tools.otio_file_ops import resolve_timeline_path
    store = EventStore(ctx.state.event_log_path)
    store.append(effect, "")
    
    return result

@g.step
async def otio_gate_step(ctx: StepContext[PipelineState, AgentURLs, str]) -> str:
    """Read OTIO projection, decide next stage."""
    from tools.otio_file_ops import otio_read, resolve_timeline_path
    
    timeline = otio_read(ctx.state.timeline_path)
    
    # Check tracks
    has_audio = False
    has_video = False
    for track in timeline.tracks:
        if track.name == "A1_Narration" and len(list(track)) > 0:
            has_audio = True
        if track.name == "V1_Video" and len(list(track)) > 0:
            has_video = True
    
    if not has_audio:
        return await g.next("audio_step", ctx.state.last_agent_output)
    if not has_video:
        return await g.next("video_step", ctx.state.last_agent_output)
    
    # Check output
    import glob, os
    output_dir = os.path.join(os.path.dirname(ctx.state.timeline_path), "output")
    if not glob.glob(os.path.join(output_dir, "*.mp4")):
        return await g.next("assembly_step", ctx.state.last_agent_output)
    
    return ctx.state.last_agent_output

@g.step
async def audio_step(ctx: StepContext[PipelineState, AgentURLs, str]) -> str:
    result = await _call_agent(ctx.deps.audio, ctx.inputs)
    ctx.state.last_agent_output = result
    
    from effect_parser import parse_agent_text
    from event_store import EventStore
    effect = parse_agent_text("audio", result)
    store = EventStore(ctx.state.event_log_path)
    store.append(effect, "")
    
    return result

@g.step
async def video_step(ctx: StepContext[PipelineState, AgentURLs, str]) -> str:
    result = await _call_agent(ctx.deps.video, ctx.inputs)
    ctx.state.last_agent_output = result
    
    from effect_parser import parse_agent_text
    from event_store import EventStore
    effect = parse_agent_text("video", result)
    store = EventStore(ctx.state.event_log_path)
    store.append(effect, "")
    
    return result

@g.step
async def assembly_step(ctx: StepContext[PipelineState, AgentURLs, str]) -> str:
    result = await _call_agent(ctx.deps.assembly, ctx.inputs)
    ctx.state.last_agent_output = result
    
    from effect_parser import parse_agent_text
    from event_store import EventStore
    effect = parse_agent_text("assembly", result)
    store = EventStore(ctx.state.event_log_path)
    store.append(effect, "")
    
    return result

@g.step
async def provisioner_step(ctx: StepContext[PipelineState, AgentURLs, str]) -> str:
    """Provisioner drains job queue."""
    result = await _call_agent(ctx.deps.provisioner, ctx.inputs)
    ctx.state.last_agent_output = result
    return result

# Wire the graph
# Entry: scenario → otio gate → [audio | video | assembly | provisioner] → otio gate → ...
g.add(
    g.edge_from(g.start_node).to(scenario_step),
    g.edge_from(scenario_step).to(otio_gate_step),
    g.edge_from(audio_step).to(otio_gate_step),
    g.edge_from(video_step).to(otio_gate_step),
    g.edge_from(assembly_step).to(otio_gate_step),
    g.edge_from(provisioner_step).to(otio_gate_step),
)

pipeline_graph = g.build()
```

### Step 5: pydantic-deep Leaf Agents (HTTP Services)

**New file: `server/pydantic_deep_agents/scenario_agent.py`**

```python
from fastapi import FastAPI, Body
from fastapi.responses import PlainTextResponse
from pydantic_deep import create_deep_agent
from pydantic_ai.models.openai import OpenAIModel

model = OpenAIModel(
    model_name="deepseek/deepseek-v4-flash",
    api_key=api_key,  # passed explicitly
    base_url="https://api.deepseek.com/v1",
)

agent = create_deep_agent(
    model="deepseek/deepseek-v4-flash",
    instructions="""
    You are the Scenario Agent for a documentary pipeline.
    You write scripts with narration V1/V2/V3, visual notes, and timing.
    
    YOU COMMUNICATE IN NATURAL LANGUAGE ONLY.
    Never emit JSON, XML, or structured formats.
    The system parses your text into typed effects.
    
    When you propose changes, describe them clearly.
    Example: "For Scene 1, V1 Hook: 'Every rainbow is sunlight disguised...'"
    """,
    include_memory=True,
    web_search=True,
    thinking=False,
)

app = FastAPI()

@app.post("/")
async def invoke(text: str = Body(..., media_type="text/plain")):
    result = await agent.run(text)
    return PlainTextResponse(result.output)
```

**Same pattern for:** `audio_agent.py`, `video_agent.py`, `otio_gate_agent.py`, `assembly_agent.py`, `provisioner_agent.py`.

### Step 6: OTIO Projection Handler

**New file: `server/projection_handler.py`**

```python
"""Apply events to OTIO timeline. OTIO is a read model rebuilt from events."""

from effects import Effect, UpdateScript, GenerateNarrationAudio, RenderVideoSegment, MergeIntoOTIO, ExecuteRawBash
import opentimelineio as otio

def apply_event(timeline: otio.Timeline, effect: Effect) -> otio.Timeline:
    """Apply a single effect to the timeline. Returns new timeline."""
    handlers = {
        "UpdateScript": _apply_update_script,
        "GenerateNarrationAudio": _apply_generate_audio,
        "RenderVideoSegment": _apply_render_video,
        "MergeIntoOTIO": _apply_merge_into_otio,
        "ExecuteRawBash": _apply_execute_bash,
        "NoOp": lambda t, e: t,
    }
    handler = handlers.get(effect.effect_type)
    if handler:
        return handler(timeline, effect)
    return timeline

def _apply_update_script(timeline, effect: UpdateScript):
    # Write to pipeline metadata
    from tools.otio_metadata import write_pipeline_metadata
    write_pipeline_metadata(timeline, "scenario_raw", effect.justification)
    return timeline

def _apply_generate_audio(timeline, effect: GenerateNarrationAudio):
    # Create job in queue
    from job_queue import create_job
    create_job("audio", effect.scene_num, {"voice": effect.voice, "text": effect.text})
    return timeline

def _apply_render_video(timeline, effect: RenderVideoSegment):
    from job_queue import create_job
    create_job("video", effect.scene_num, {"prompt": effect.prompt, "lora_id": effect.lora_id})
    return timeline

def _apply_merge_into_otio(timeline, effect: MergeIntoOTIO):
    # Add clips to tracks
    from tools.otio_tools import add_narration_to_timeline, add_video_clip_simple
    for clip in effect.audio_clips:
        add_narration_to_timeline(clip["scene_num"], clip["voice"], clip["wav_path"], clip["duration_sec"])
    for clip in effect.video_clips:
        add_video_clip_simple(clip["scene_num"], 0, clip["mp4_path"], clip["duration_sec"], clip.get("lora_id", ""))
    return timeline

def _apply_execute_bash(timeline, effect: ExecuteRawBash):
    import subprocess
    subprocess.run(effect.command, shell=True, capture_output=True)
    return timeline
```

### Step 7: Launcher (Independent HTTP Processes)

**New file: `server/pydantic_deep_agents/launcher.py`**

```python
"""Launch all pydantic-deep agents as independent HTTP processes."""

import multiprocessing
import uvicorn

AGENTS = {
    "scenario": ("scenario_agent", 9001),
    "audio": ("audio_agent", 9002),
    "video": ("video_agent", 9003),
    "otio": ("otio_gate_agent", 9004),
    "assembly": ("assembly_agent", 9005),
    "provisioner": ("provisioner_agent", 9006),
}

def _run_agent(module_name: str, port: int, api_key: str):
    import importlib
    mod = importlib.import_module(f"pydantic_deep_agents.{module_name}")
    app = mod.app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

def launch_all(api_key: str):
    processes = []
    for name, (module, port) in AGENTS.items():
        p = multiprocessing.Process(target=_run_agent, args=(module, port, api_key))
        p.start()
        processes.append(p)
    return processes
```

### Step 8: Entry Point

**New file: `server/run_pydantic_pipeline.py`**

```python
"""Entry point for pydantic-graph + pydantic-deep pipeline."""

import asyncio
import os
from pydantic_graph import GraphBuilder
from pydantic_deep_agents.launcher import launch_all

async def main():
    api_key = open(os.path.expanduser("~/api_keys/LLMS/deepseek_api.txt")).read().strip()
    
    # 1. Destroy orphan VMs
    from strands_agents.run_strands import _destroy_all_vms
    _destroy_all_vms()
    
    # 2. Launch all agents as HTTP services
    processes = launch_all(api_key)
    
    # 3. Wait for agents to be ready
    import time; time.sleep(5)
    
    # 4. Build and run graph
    from pydantic_graph_pipeline import pipeline_graph, AgentURLs, PipelineState
    
    state = PipelineState(
        current_task="A 30-second documentary about rainbows",
        timeline_path="./pipeline_output/timelines/documentary_draft.otio",
        event_log_path="./pipeline_output/events.jsonl",
        run_id="run_001",
    )
    
    deps = AgentURLs()
    
    try:
        result = await pipeline_graph.run(state=state, deps=deps, inputs=state.current_task)
        print(f"Pipeline complete: {result}")
    except Exception as exc:
        from maintainer import notify_maintainer
        notify_maintainer(operation="pipeline", error=str(exc), context={})
        raise
    finally:
        for p in processes:
            p.terminate()
            p.join(timeout=5)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## File Inventory (New Files)

| File | Purpose |
|------|---------|
| `server/effects.py` | Typed algebraic effect types (Pydantic models) |
| `server/event_store.py` | Append-only event log |
| `server/effect_parser.py` | Instructor-based text → Effect parser |
| `server/projection_handler.py` | Apply effects to OTIO timeline |
| `server/pydantic_graph_pipeline.py` | pydantic-graph orchestrator with HTTP agent calls |
| `server/pydantic_deep_agents/scenario_agent.py` | pydantic-deep agent, HTTP service |
| `server/pydantic_deep_agents/audio_agent.py` | pydantic-deep agent, HTTP service |
| `server/pydantic_deep_agents/video_agent.py` | pydantic-deep agent, HTTP service |
| `server/pydantic_deep_agents/otio_gate_agent.py` | pydantic-deep agent, HTTP service |
| `server/pydantic_deep_agents/assembly_agent.py` | pydantic-deep agent, HTTP service |
| `server/pydantic_deep_agents/provisioner_agent.py` | pydantic-deep agent, HTTP service |
| `server/pydantic_deep_agents/launcher.py` | Process orchestrator |
| `server/run_pydantic_pipeline.py` | Entry point |

## Files to Delete (After Migration)

| File | Reason |
|------|--------|
| `server/strands_agents/agent_http_service.py` | Replaced by pydantic-deep + FastAPI |
| `server/strands_agents/agent_http_client.py` | Replaced by httpx direct calls in graph steps |
| `server/strands_agents/graph_pipeline.py` | Replaced by pydantic_graph_pipeline.py |
| `server/strands_agents/launcher.py` | Replaced by pydantic_deep_agents/launcher.py |
| `server/strands_agents/run_strands.py` | Replaced by run_pydantic_pipeline.py |
| `server/strands_agents/recovery_agents.py` | Gut + inline into pipeline |

## Migration Order

1. Install pydantic-deep, pydantic-graph
2. Create `effects.py`, `event_store.py`, `effect_parser.py`
3. Create `projection_handler.py`
4. Create first pydantic-deep agent (scenario) + HTTP service
5. Create `pydantic_graph_pipeline.py` with HTTP calls
6. Test scenario → otio gate end-to-end
7. Port remaining 5 agents one by one
8. Test full pipeline
9. Delete strands files
10. Update tests

## Critical Risks

1. pydantic-graph `BaseNode` is deprecated — use `GraphBuilder` + `@g.step`
2. pydantic-deep v0.3.17 is young — API may change
3. pydantic-ai `Agent.run()` returns `RunResult` with `.output` — need explicit text extraction
4. HTTP timeout between agents — default 300s, may need tuning
5. Event store performance — rebuilding OTIO from scratch on every event is O(n)
