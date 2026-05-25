"""Scenario Agent — HTTP service.

The agent thinks aloud about documentary scripts.
It does not know about effects, state machines, or other agents.
It receives feedback after each turn and adjusts.
"""

from __future__ import annotations

from fastapi import Body, FastAPI
from fastapi.responses import PlainTextResponse
from pydantic_deep import create_deep_agent

app = FastAPI()

_agent = None


@app.on_event("startup")
async def _startup():
    global _agent
    _agent = create_deep_agent(
        model="deepseek:deepseek-v4-flash",
        instructions="""
You are a documentary scriptwriter.

You think aloud about what the documentary should contain.
You write narration for 3 voices and visual notes.

YOU USE BASH to check files, read scripts, verify outputs.
Example: bash_command("cat /path/to/file.txt")

You receive FEEDBACK after each turn telling you what happened.
Use the feedback to adjust your next thinking.

Write scripts in this natural format:
Scene {N} — {Title} ({duration}s)
  V1 Hook: {emotional opening}
  V2 Expert: {factual explanation}
  V3 Storyteller: {narrative connection}
  Visual notes: {shot descriptions}
  Dopamine hook: {attention grabber}

You are free to think, write, and do bash as you see fit.
""",
        include_memory=True,
        include_subagents=False,
        web_search=False,
        web_fetch=False,
        thinking=False,
    )


@app.post("/")
async def invoke(text: str = Body(..., media_type="text/plain")):
    result = await _agent.run(text, deps=_agent.deps_type())
    return PlainTextResponse(result.output)
