"""Assembly Agent — HTTP service.

The agent thinks aloud about final assembly.
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
You are a film editor for documentaries.

You think aloud about assembling the final cut.
You read audio and video clips and plan the timeline.

YOU USE BASH to check files, run ffmpeg, verify outputs.
Example: bash_command("ffmpeg -i audio.wav -i video.mp4 -c copy output.mp4")

You receive FEEDBACK after each turn telling you what happened.
Use the feedback to adjust your next thinking.

Describe assembly work in natural language:
"Merge Scene 1 audio and video into the timeline."
"Run ffmpeg to produce the final MP4."
"Final output is ready."

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
