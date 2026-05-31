"""Scenario agent HTTP service.

Presents scene scripts written by the director. Uses pydantic-deep
with a script reader tool and todo tracking for quasi-deterministic
scene sequencing.
"""

from fastapi import FastAPI
from pydantic import BaseModel
from pydantic_deep import DeepAgentDeps, StateBackend, create_deep_agent

from server.capabilities.causal_log import CausalLogCapability
from server.todo_persistence import load_todos, save_todos
from server.tools.script_reader import script_reader_tool

app = FastAPI()
AGENT_NAME = "scenario"


class WakePayload(BaseModel):
    run_id: str
    notification_type: str = "wake"
    context: dict = {}


SCENARIO_AGENT_PROMPT = """You are the scenario narrator for the documentary pipeline.

Your job is to present scene scripts written by the director.

You have these tools:
- read_script(task_id, run_id): Retrieves the director's authored text for a scene
- list_todos(): Shows your current task list
- complete_todo(id): Marks a scene as presented

Workflow:
1. Check your task list for pending scenes with list_todos()
2. For the next pending scene, retrieve the script with read_script
3. Present the narration naturally, preserving the director's intent
4. Mark the scene complete with complete_todo

Guidelines:
- Present the director's text faithfully. Do not invent scenes or alter facts.
- You may add natural transitions between segments.
- You emit ONLY natural language. No JSON, XML, markers, or structured formats.
- Your output is parsed by a semantic extractor. Present clearly.
- If a scene is missing from the script, say so and move to the next.
"""


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
        include_teams=False,
        include_improve=False,
        include_liteparse=False,
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
