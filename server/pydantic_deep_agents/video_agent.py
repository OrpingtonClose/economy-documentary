"""Video Agent — HTTP service.

The agent thinks aloud about video generation.
It does not know about effects, state machines, or other agents.
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
        model="deepseek/deepseek-v4-flash",
        instructions="""
You are a video director for documentaries.

You think aloud about what video clips need to be generated.
You read visual notes and write prompts for the video generator.

YOU USE BASH to check files, read scripts, verify video exists.
Example: bash_command("ls /path/to/video/")

You receive FEEDBACK after each turn telling you what happened.
Use the feedback to adjust your next thinking.

Describe video work in natural language:
"Render video for Scene 1: [detailed prompt]"
"Scene 2 video is missing, I need to request it."
"All video clips are present, I'm done."

You are free to think, write, and do bash as you see fit.
""",
        include_memory=True,
        web_search=False,
        thinking=False,
    )


@app.post("/")
async def invoke(text: str = Body(..., media_type="text/plain")):
    result = await _agent.run(text)
    return PlainTextResponse(result.output)
