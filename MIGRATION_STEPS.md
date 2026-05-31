> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Step-by-Step Migration: strands → pydantic-graph + pydantic-ai + pydantic-deep

## PRINCIPLE: ALL AGENT COMMUNICATION VIA HTTP

No in-process calls. No shared state. Every agent is an independent HTTP service.
The graph orchestrator calls agents via HTTP POST. Agents respond with plain text.

---

## PRE-MIGRATION INVENTORY

### strands imports across codebase (66 references, 38 files)

**Category A: Core framework imports**
```
from strands import Agent                    → pydantic_ai.Agent OR create_deep_agent
from strands import tool                     → @agent.tool or @agent.tool_plain
from strands import ToolContext              → RunContext[DepsType]
from strands import tool, ToolContext        → pydantic_ai.RunContext
from strands.multiagent.graph import Graph   → pydantic_graph.GraphBuilder
from strands.multiagent.base import Status   → pydantic_graph.End or custom enum
from strands.multiagent.graph import GraphEdge, GraphNode, GraphState → pydantic_graph types
from strands.hooks import HookProvider       → pydantic_deep HookEvent
from strands.agent.conversation_manager import SlidingWindowConversationManager → pydantic_deep context manager
from strands.models.openai import OpenAIModel → pydantic_ai.models.openai.OpenAIModel
```

**Category B: HTTP service layer**
```
agent_http_service.py  → FastAPI + pydantic-deep agent
agent_http_client.py   → httpx.AsyncClient (direct HTTP calls, no protocol wrapper)
```

**Category C: Agent builders (must be rewritten)**
```
graph_pipeline.py      → pydantic_graph_pipeline.py
scenario_agent.py      → pydantic_deep_agents/scenario_agent.py
content_analyst.py     → pydantic_deep_agents/content_analyst.py
visual_concepter.py    → pydantic_deep_agents/visual_concepter.py
scenario_refiner.py    → pydantic_deep_agents/scenario_refiner.py
provisioner_agent.py   → pydantic_deep_agents/provisioner_agent.py
stages/*.py            → pydantic_deep_agents/*_agent.py
```

**Category D: Hooks (migrate to pydantic-deep capabilities)**
```
hooks/pipeline_hooks.py     → pydantic_deep HookEvent handlers
hooks/contracts.py          → Pydantic validators
hooks/otio_contracts.py     → Pydantic validators
```

**Category E: Tools (re-decorate)**
```
vm_registry_tools.py   → @agent.tool_plain
research_tools.py      → @agent.tool_plain (built into pydantic-deep)
search_tools.py        → DELETE (pydantic-deep has web_search)
task_tools.py          → @agent.tool
technique_tools.py     → @agent.tool
```

---

## STEP 0: STATIC ANALYSIS BASELINE

**Before any code changes:**

```bash
# 0.1 Count all strands references
grep -rn "from strands import\|from strands\.\|import strands" server/ --include="*.py" | grep -v __pycache__ > /tmp/strands_refs.txt
wc -l /tmp/strands_refs.txt

# 0.2 Count all @tool decorators
grep -rn "@tool" server/ --include="*.py" | grep -v __pycache__ | wc -l

# 0.3 Count all Graph usages
grep -rn "Graph(" server/ --include="*.py" | grep -v __pycache__ | wc -l

# 0.4 Count all Agent( usages
grep -rn "Agent(" server/ --include="*.py" | grep -v __pycache__ | wc -l

# 0.5 List all files with tool_context
grep -rln "tool_context" server/ --include="*.py" | grep -v __pycache__

# 0.6 List all files with agent.state
grep -rln "agent\.state" server/ --include="*.py" | grep -v __pycache__
```

**Verification:** Record counts. These should all reach zero by end of migration.

---

## STEP 1: INSTALL DEPENDENCIES

```bash
cd /Users/orpington/Documents/economy-documentary-work
.venv/bin/pip install pydantic-deep
.venv/bin/pip install pydantic-graph
```

**Verification:**
```bash
.venv/bin/python -c "from pydantic_deep import create_deep_agent; print('pydantic-deep OK')"
.venv/bin/python -c "from pydantic_graph import GraphBuilder; print('pydantic-graph OK')"
.venv/bin/python -c "from pydantic_ai import Agent; print('pydantic-ai OK')"
```

**Static analysis:** Confirm new packages are importable. No code changes yet.

---

## STEP 2: CREATE ALGEBRAIC EFFECT TYPES

**File:** `server/effects.py`

Create typed Pydantic models for all possible agent intentions.

**Why:** Every agent text message gets parsed into one of these effects.
The effect is the ONLY thing that can mutate the system.

**Effects needed:**
- UpdateScript (scenario changes)
- GenerateNarrationAudio (TTS request)
- RenderVideoSegment (video request)
- MergeIntoOTIO (timeline assembly)
- ExecuteRawBash (bash command)
- NoOp (unparseable)

**Verification:**
```bash
.venv/bin/python -c "from effects import UpdateScript, GenerateNarrationAudio; print(UpdateScript(effect_type='UpdateScript', agent_id='test', timestamp=__import__('datetime').datetime.now(), justification='test'))"
```

**Static analysis:** All effect types must be instantiable and serializable.

---

## STEP 3: CREATE EVENT STORE

**File:** `server/event_store.py`

Append-only log. Every validated effect becomes an immutable event.

**Requirements:**
- Append: O(1)
- Read all: O(n)
- JSONL format (one JSON object per line)
- Atomic append (file append is atomic on POSIX)

**Verification:**
```bash
.venv/bin/python -c "
from event_store import EventStore
from effects import NoOp
import tempfile, os
log = tempfile.mktemp()
store = EventStore(log)
e = NoOp(agent_id='test', timestamp=__import__('datetime').datetime.now(), justification='test')
r = store.append(e, 'hash_before')
print(f'seq={r.seq}')
events = store.read_all()
print(f'count={len(events)}')
os.unlink(log)
"
```

**Static analysis:** Event count must equal append count. No data loss.

---

## STEP 4: CREATE EFFECT PARSER

**File:** `server/effect_parser.py`

Instructor + Pydantic parser. Raw agent text → typed Effect.

**Requirements:**
- Never crash on bad input → return NoOp
- Use existing `structured_extract.extract()`
- Single function: `parse_agent_text(agent_id: str, text: str) -> Effect`

**Verification:**
```bash
.venv/bin/python -c "
from effect_parser import parse_agent_text
e = parse_agent_text('scenario', 'For Scene 1, V1 Hook: Hello world')
print(f'type={e.effect_type}')
e2 = parse_agent_text('audio', 'Generate audio for voice V1: Hello')
print(f'type={e2.effect_type}')
e3 = parse_agent_text('garbage', '!!!@#$%')
print(f'type={e3.effect_type}')
"
```

**Static analysis:** All outputs must be valid Effect instances.

---

## STEP 5: CREATE OTIO PROJECTION HANDLER

**File:** `server/projection_handler.py`

Apply effects to OTIO timeline. OTIO is a read model rebuilt from events.

**Requirements:**
- Pure function: `(timeline, effect) -> new_timeline`
- Never write OTIO directly from agents
- Only this file touches OTIO

**Handlers:**
- UpdateScript → write pipeline metadata
- GenerateNarrationAudio → create job in queue
- RenderVideoSegment → create job in queue
- MergeIntoOTIO → add clips to tracks
- ExecuteRawBash → subprocess.run (for non-OTIO side effects)
- NoOp → identity

**Verification:**
```bash
.venv/bin/python -c "
import opentimelineio as otio
from projection_handler import apply_event
from effects import NoOp
t = otio.schema.Timeline(name='test')
t2 = apply_event(t, NoOp(agent_id='test', timestamp=__import__('datetime').datetime.now(), justification='noop'))
print(f'same={t is not t2}')
"
```

**Static analysis:** Handler must be pure (no side effects except ExecuteRawBash).

---

## STEP 6: CREATE FIRST PYDANTIC-DEEP AGENT (SCENARIO)

**File:** `server/pydantic_deep_agents/scenario_agent.py`

HTTP service wrapping a pydantic-deep agent.

**Requirements:**
- FastAPI app with single POST /
- Receives plain text, returns plain text
- Uses deepseek/deepseek-v4-flash
- No tool_context, no agent.state blackboard
- Memory via pydantic-deep include_memory

**Code:**
```python
from fastapi import FastAPI, Body
from fastapi.responses import PlainTextResponse
from pydantic_deep import create_deep_agent

agent = create_deep_agent(
    model="deepseek/deepseek-v4-flash",
    instructions="You are the Scenario Agent...",
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

**Verification:**
```bash
# Start agent
.venv/bin/python -c "
import uvicorn
from pydantic_deep_agents.scenario_agent import app
uvicorn.run(app, host='127.0.0.1', port=9001)
" &
sleep 5
curl -X POST http://localhost:9001/ -H "Content-Type: text/plain" -d "Write a 30-second documentary about rainbows"
```

**Static analysis:** Response must be plain text, not JSON.

---

## STEP 7: CREATE PYDANTIC-GRAPH ORCHESTRATOR

**File:** `server/pydantic_graph_pipeline.py`

Graph that calls agents via HTTP. No in-process agent calls.

**Requirements:**
- GraphBuilder with @g.step functions
- Each step calls agent via httpx.AsyncClient
- State carries: current_task, last_output, timeline_path, event_log_path
- Routing based on OTIO projection state

**Key code:**
```python
async def _call_agent(url: str, text: str) -> str:
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(url, content=text, headers={"Content-Type": "text/plain"})
        return resp.text
```

**Verification:**
```bash
# With scenario agent running on port 9001:
.venv/bin/python -c "
import asyncio
from pydantic_graph_pipeline import pipeline_graph, PipelineState, AgentURLs

async def test():
    state = PipelineState(current_task='test', timeline_path='/tmp/test.otio', event_log_path='/tmp/test.jsonl')
    deps = AgentURLs(scenario='http://localhost:9001')
    result = await pipeline_graph.run(state=state, deps=deps, inputs='test')
    print(result)

asyncio.run(test())
"
```

**Static analysis:** Graph must complete without errors. HTTP calls must succeed.

---

## STEP 8: MIGRATE REMAINING 5 AGENTS

Repeat Step 6 for:
- audio_agent.py (port 9002)
- video_agent.py (port 9003)
- otio_gate_agent.py (port 9004)
- assembly_agent.py (port 9005)
- provisioner_agent.py (port 9006)

**Verification per agent:**
```bash
# Start agent, test HTTP endpoint, kill agent
for port in 9002 9003 9004 9005 9006; do
  curl -s -o /dev/null -w "%{http_code}" http://localhost:$port/ || echo "FAIL $port"
done
```

**Static analysis:** All 6 agents must respond to HTTP POST with 200 OK and plain text.

---

## STEP 9: CREATE LAUNCHER

**File:** `server/pydantic_deep_agents/launcher.py`

Spawn all 6 agents as independent processes.

**Requirements:**
- multiprocessing.Process per agent
- Each process runs uvicorn with its own FastAPI app
- Configurable ports and API key

**Verification:**
```bash
.venv/bin/python -c "
from pydantic_deep_agents.launcher import launch_all
procs = launch_all(api_key='test')
import time; time.sleep(3)
for p in procs:
    print(f'pid={p.pid} alive={p.is_alive()}')
    p.terminate()
    p.join(timeout=5)
"
```

**Static analysis:** All 6 processes must start and respond to HTTP within 5 seconds.

---

## STEP 10: CREATE ENTRY POINT

**File:** `server/run_pydantic_pipeline.py`

Main entry point. Orchestrates: destroy orphans → launch agents → run graph.

**Verification:**
```bash
# Dry run (no actual GPU jobs)
.venv/bin/python server/run_pydantic_pipeline.py --dry-run --brief "test documentary"
```

**Static analysis:** Must complete without unhandled exceptions.

---

## STEP 11: STATIC ANALYSIS VERIFICATION

**After all code is written:**

```bash
# 11.1 Confirm zero strands references remain
grep -rn "from strands import\|from strands\.\|import strands" server/ --include="*.py" | grep -v __pycache__ | grep -v "pydantic_"
# Expected: empty output

# 11.2 Confirm all agents use HTTP
grep -rn "httpx.AsyncClient" server/pydantic_graph_pipeline.py
# Expected: found

grep -rn "invoke_async\|stream_async\|AgentBase" server/ --include="*.py" | grep -v __pycache__
# Expected: empty (no strands protocol usage)

# 11.3 Confirm effect types are complete
.venv/bin/python -c "from effects import Effect; print(Effect.__subclasses__())"
# Expected: list of all effect subclasses

# 11.4 Confirm event store round-trip
.venv/bin/python -c "
from event_store import EventStore
from effects import NoOp
import tempfile
log = tempfile.mktemp()
store = EventStore(log)
for i in range(10):
    store.append(NoOp(agent_id='test', timestamp=__import__('datetime').datetime.now(), justification=str(i)), 'hash')
events = store.read_all()
assert len(events) == 10, f'Expected 10, got {len(events)}'
assert events[-1].seq == 10, f'Expected seq 10, got {events[-1].seq}'
print('PASS')
"

# 11.5 pyright on all new files
.venv/bin/pyright server/effects.py server/event_store.py server/effect_parser.py server/projection_handler.py server/pydantic_graph_pipeline.py server/pydantic_deep_agents/ 2>&1 | tail -20
# Expected: 0 errors
```

---

## STEP 12: DELETE STRANDS FILES

**Files to delete (after verification passes):**

```bash
rm server/strands_agents/agent_http_service.py
rm server/strands_agents/agent_http_client.py
rm server/strands_agents/agent_intervention.py
rm server/strands_agents/graph_pipeline.py
rm server/strands_agents/launcher.py
rm server/strands_agents/run_strands.py
rm server/strands_agents/recovery_agents.py
rm server/strands_agents/scenario_agent.py
rm server/strands_agents/content_analyst.py
rm server/strands_agents/visual_concepter.py
rm server/strands_agents/scenario_refiner.py
rm server/strands_agents/provisioner_agent.py
rm server/strands_agents/stages/scenario_stage.py
rm server/strands_agents/stages/audio_stage.py
rm server/strands_agents/stages/video_stage.py
rm server/strands_agents/stages/visual_stage.py
rm server/strands_agents/stages/production_stage.py
rm server/strands_agents/stages/assembly_stage.py
rm server/search_tools.py
rm server/tools/provisioner_tools.py
```

**Verification:**
```bash
grep -rn "strands" server/ --include="*.py" | grep -v __pycache__ | grep -v "pydantic_"
# Expected: empty
```

---

## STEP 13: FINAL END-TO-END TEST

```bash
# 13.1 Full pipeline with no GPU jobs (dry run)
.venv/bin/python server/run_pydantic_pipeline.py --dry-run --brief "A 30-second documentary about rainbows"

# 13.2 Verify event log was created
head -5 server/pipeline_output/events.jsonl

# 13.3 Verify OTIO was rebuilt from events
ls -la server/pipeline_output/timelines/documentary_draft.otio

# 13.4 Verify no orphaned VMs
vastai show instances --raw | python -c "import sys,json; d=json.load(sys.stdin); print(f'instances={len(d) if isinstance(d,list) else 0}')"
# Expected: 0 instances (or only ones from this run)
```

---

## IMPLICATION MAP: Every Change and Its Consequences

### Changing `strands.Agent` → `create_deep_agent()`

**Direct implications:**
- No `agent.state` blackboard → use explicit deps dataclass
- No `hooks=` parameter → use pydantic-deep HookEvent or pre/post wrappers
- No `conversation_manager=` → use pydantic-deep context manager
- No `name=` parameter → identify agents by URL/port

**Indirect implications:**
- All tools using `tool_context.agent.state` must be rewritten
- Agent memory module can be deleted (pydantic-deep has built-in memory)
- SlidingWindowConversationManager can be deleted

### Changing `strands.Graph` → `pydantic_graph.GraphBuilder`

**Direct implications:**
- No `GraphEdge`/`GraphNode` classes → use `@g.step` functions
- No `max_node_executions` → use pydantic-ai UsageLimits
- No `reset_on_revisit` → state is fresh per graph.run()
- No `entry_points` → g.start_node is the entry

**Indirect implications:**
- Routing conditions become return type annotations on step functions
- RecoveryShell becomes graph.iter() with manual node selection
- All 8 pipeline hooks must be reimplemented

### Changing `@tool` → `@agent.tool`

**Direct implications:**
- 182 tool signatures change
- `tool_context` parameter becomes `ctx: RunContext[DepsType]`
- Tool registration changes from list to decorator

**Indirect implications:**
- All tool tests must be updated
- Tool schema generation is automatic (no `to_schema()` needed)
- Tool execution logging changes (pydantic-ai has built-in tracing)

### Changing `AgentHTTPClient` → `httpx.AsyncClient`

**Direct implications:**
- No `AgentBase` protocol → direct HTTP calls
- No `invoke_async()`/`stream_async()` → `client.post()`
- No `__call__()` wrapper → direct function calls

**Indirect implications:**
- No 2-second throttle → add if needed in graph step
- No 300-second timeout → configurable per httpx client
- No `AgentResult` wrapping → response.text is the result

---

## ROLLBACK PLAN

If migration fails at any step:

1. **Git branch:** Work on `pydantic-migration` branch. `strands-migration` remains untouched.
2. **File backup:** All deleted strands files are in git history. `git checkout strands-migration -- <file>` restores.
3. **Parallel run:** New pipeline runs alongside old. Compare outputs.
4. **Kill switch:** If new pipeline fails, fall back to old entry point.

---

## TIMELINE

| Step | Task | Duration | Verification |
|------|------|----------|--------------|
| 0 | Static analysis baseline | 2 hours | Reference counts recorded |
| 1 | Install dependencies | 30 min | All imports work |
| 2-5 | Core infrastructure (effects, store, parser, projection) | 1 day | Unit tests pass |
| 6 | First agent (scenario) | 4 hours | HTTP endpoint responds |
| 7 | Graph orchestrator | 6 hours | Graph runs end-to-end |
| 8 | Remaining 5 agents | 2 days | All HTTP endpoints respond |
| 9 | Launcher | 2 hours | All processes start |
| 10 | Entry point | 2 hours | Dry run completes |
| 11 | Static analysis verification | 4 hours | Zero strands refs |
| 12 | Delete old files | 1 hour | Confirmed gone |
| 13 | End-to-end test | 1 day | Full pipeline works |
| **Total** | | **~6 days** | |
