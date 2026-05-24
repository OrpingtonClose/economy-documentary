# HTTP Base Protocol for Agent Communication

## Status

Proposal — seeks approval before implementation.

## Problem

The migration to Strands Graph replaced independent HTTP-addressable agents with in-process `Agent` objects. The plain-text GET/POST protocol (commit `3dac816`) was lost. Agents can no longer be observed or instructed from outside the process.

## Proposal

**HTTP is the ONLY execution route. No in-process fallback. No mocks.**

Every `GraphNode.executor` is an `AgentHTTPClient` proxy. Each agent runs as an independent HTTP service on its own port. The Graph orchestrates by POSTing plain text between agents. There is no alternative transport.

## Protocol

Every agent exposes a FastAPI app on a dedicated port. All endpoints speak **free-flowing plain text** (`text/plain`). No structured status lines. No JSON envelopes. No protobuf. No enforced format whatsoever.

### `GET /`

**Semantics:** Inspect the agent. Create a lightweight **copy** of the agent with its current context, answer the request, then let the copy perish. The running agent is **never interrupted**.

**Request:** None (empty body).

**Response:** `text/plain` free-flowing text. The agent says whatever it wants about its current state, thinking, mood, pending work. There is NO format. No prefixes. No key=value pairs. Just text.

Example:
```
I'm the scenario agent. Currently idle. Last task was about ants — I generated 3 scenes but the OTIO gate rejected them for missing pronunciation hints. Waiting for the graph to re-invoke me with the errors.
```

### `POST /`

**Semantics:** Interrupt whatever the agent is currently doing, process the raw text body as the new task, and return the result.

**Request:** `text/plain` body. Free-flowing text. The receiving agent parses it with the `instructor` library to extract structured meaning (function calls, arguments, intent).

**Response:** `text/plain` free-flowing text. The agent's response. If the agent called tools, their results are included as natural language in the response.

Example request:
```
Generate a 3-scene documentary about ants. Style: cinematic. Each scene needs V1 Hook, V2 Expert, and V3 Storyteller narration blocks. Include pronunciation hints for all scientific terms.
```

Example response:
```
Done. Three scenes generated, total duration 105 seconds. Saved checkpoint. Visual style: cinematic macro photography with shallow depth of field. All pronunciation hints included (e.g. formicidae = /fɔːrˈmɪsɪdiː/).
```

## Cross-Agent Function Calls via `instructor`

When Agent A needs to invoke a tool that targets Agent B:

1. Agent A formats its arguments as **free-flowing text** (not JSON, not structured — just natural language describing what it wants)
2. Agent A POSTs that text to Agent B's HTTP endpoint
3. Agent B receives the raw text and uses `instructor` to parse it into its own tool schema
4. Agent B executes the tool, formats the result as free-flowing text, and returns it

This is the same as a human typing instructions to an agent. The transport is HTTP; the language is natural text; the parsing is `instructor`.

## Architecture

### Agent Service

Each agent is a real `strands.Agent` wrapped in a FastAPI app:

```python
# server/strands_agents/agent_http_service.py
from fastapi import FastAPI, Request
from fastapi.responses import Response
from strands import Agent

def build_agent_app(agent: Agent, name: str) -> FastAPI:
    app = FastAPI(title=f"agent-{name}")

    @app.get("/")
    def _health() -> Response:
        # Clone agent context, answer, discard clone
        # Never touch the running agent
        return Response(content=f"Agent {name} is here.", media_type="text/plain")

    @app.post("/")
    async def _invoke(request: Request) -> Response:
        body = await request.body()
        text = body.decode("utf-8").strip()
        # Interrupt current work, invoke agent with new text
        result = await agent.invoke_async(text)
        return Response(content=str(result), media_type="text/plain")

    return app
```

### HTTP Client Proxy

The Graph's `GraphNode.executor` is an `AgentHTTPClient` that satisfies the `AgentBase` protocol:

```python
# server/strands_agents/agent_http_client.py
class AgentHTTPClient:
    """Looks like a strands.Agent to the Graph. Talks HTTP underneath."""

    def __init__(self, base_url: str, name: str):
        self.base_url = base_url
        self.name = name

    async def stream_async(self, prompt, **kwargs):
        async with httpx.AsyncClient() as client:
            text = _content_blocks_to_text(prompt)
            resp = await client.post(f"{self.base_url}/", content=text)
        yield {"result": _wrap_result(resp.text)}

    async def invoke_async(self, prompt, **kwargs):
        # collect from stream_async
        ...
```

### Graph Builder

`build_documentary_graph` always builds `AgentHTTPClient` instances:

```python
nodes = {
    SCENARIO: GraphNode(node_id=SCENARIO, executor=AgentHTTPClient("http://localhost:9001", SCENARIO)),
    AUDIO:    GraphNode(node_id=AUDIO,    executor=AgentHTTPClient("http://localhost:9002", AUDIO)),
    ...
}
```

The Graph itself needs no changes. Edges, conditions, hooks, interrupts — all work identically over HTTP.

### Launcher

A single script starts all agent services:

```bash
python -m strands_agents.launcher \
  --scenario-port 9001 \
  --audio-port 9002 \
  --video-port 9003 \
  --otio-port 9004 \
  --assembly-port 9005 \
  --provisioner-port 9006
```

Each service runs in its own process (`multiprocessing`). The main process blocks until SIGINT.

## Why This Works

1. **Agents stay agentic.** Each service is a real `strands.Agent` with its own system prompt, tools, memory, and state.
2. **No mock frameworks.** The HTTP layer is the actual transport. The Graph does not know the difference.
3. **Intervention is free.** `POST /` to any agent interrupts and redirects it. `GET /` inspects without disturbing.
4. **`instructor` for parsing.** Agents receive raw text and use `instructor` to extract structured meaning. No transport-level schema.
5. **The current pipeline is preserved.** `graph_pipeline.py`, `run_strands.py`, routing conditions, hooks — none of it changes except the executor type.

## Execution

```bash
# Start all agent services
python -m strands_agents.launcher --api-key $API_KEY

# Run the pipeline (agents must be running)
python -m strands_agents.run_strands "documentary about ants" -k $API_KEY
```

## Rejected Alternatives

- **File-backed interrupt store** — adds complexity, requires polling, agents are not truly addressable.
- **In-process hooks with HTTP router** — the Graph and agents share one process; you cannot POST to an agent from a separate CLI run.
- **Message bus (Redis/WebSocket)** — adds infrastructure dependency. HTTP is sufficient.
