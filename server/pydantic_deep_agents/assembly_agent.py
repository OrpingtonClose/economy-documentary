"""Assembly Agent — HTTP service wrapping a pydantic-deep agent.

Receives plain text via HTTP POST.
Returns plain text (assembly instructions, ffmpeg commands).
Uses deepseek/deepseek-v4-flash.
"""

from __future__ import annotations

from fastapi import Body, FastAPI
from fastapi.responses import PlainTextResponse
from pydantic_deep import create_deep_agent

app = FastAPI()


@app.on_event("startup")
async def _startup():
    global _agent
    _agent = create_deep_agent(
        model="deepseek/deepseek-v4-flash",
        instructions="""
You are the Assembly Agent for a documentary pipeline.

Your job: Merge audio and video clips into the final documentary.

YOU COMMUNICATE IN NATURAL LANGUAGE ONLY.
Never emit JSON, XML, or structured formats.

When you receive clip information, respond with:
  Merge into OTIO: scene {N} audio={path} video={path}

You can also request bash commands for ffmpeg:
  Execute bash: ffmpeg -i {audio} -i {video} -c copy {output}

The system will parse your text into typed effects and execute them.

RULES:
- Specify exact file paths.
- Specify timing and ordering.
- Final output must be a single MP4 file.
""",
        include_memory=True,
        web_search=False,
        thinking=False,
    )


@app.post("/")
async def invoke(text: str = Body(..., media_type="text/plain")):
    result = await _agent.run(text)
    return PlainTextResponse(result.output)
