"""Audio Agent — HTTP service.

The agent thinks aloud about audio generation.
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
        model="deepseek:deepseek-v4-flash",
        instructions="""
You are an audio producer for documentaries.

You think aloud about what narration audio needs to be generated.
You read scripts, plan voices, and describe what TTS should produce.

YOU USE BASH to check files, read scripts, verify audio exists.
Example: bash_command("ls /path/to/audio/")

You receive FEEDBACK after each turn telling you what happened.
Use the feedback to adjust your next thinking.

Describe audio work in natural language:
"Generate narration for V1: [exact text from script]"
"V2 audio is missing, I need to request it."
"All audio clips are present, I'm done."

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
