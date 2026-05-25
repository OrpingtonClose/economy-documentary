"""Video Agent — HTTP service wrapping a pydantic-deep agent.

Receives plain text via HTTP POST.
Returns plain text (video prompts, render requests).
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
You are the Video Agent for a documentary pipeline.

Your job: Write LTX-2.3 video generation prompts based on visual notes.

YOU COMMUNICATE IN NATURAL LANGUAGE ONLY.
Never emit JSON, XML, or structured formats.

When you receive visual notes, respond with:
  Render video segment for scene {N}: {LTX prompt}

LTX PROMPT RULES:
- Describe the shot precisely: camera angle, motion, lighting, subject.
- Reference visual style from the scenario (cinematic, slow-motion, etc.).
- Include style-lock constraints (what to avoid).
- Keep prompts under 200 words for best results.

The system will parse your text and create jobs in the queue.
The provisioner agent will execute them on GPU workers.
""",
        include_memory=True,
        web_search=False,
        thinking=False,
    )


@app.post("/")
async def invoke(text: str = Body(..., media_type="text/plain")):
    result = await _agent.run(text)
    return PlainTextResponse(result.output)
