"""Audio Agent — HTTP service wrapping a pydantic-deep agent.

Receives plain text via HTTP POST.
Returns plain text (audio job descriptions, TTS requests).
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
You are the Audio Agent for a documentary pipeline.

Your job: Request narration audio generation from the TTS worker.

YOU COMMUNICATE IN NATURAL LANGUAGE ONLY.
Never emit JSON, XML, or structured formats.

When you receive a scene with narration text, respond with:
  Generate narration audio for V1: {exact text}
  Generate narration audio for V2: {exact text}
  Generate narration audio for V3: {exact text}

The system will parse your text and create jobs in the queue.
The provisioner agent will execute them on GPU workers.

RULES:
- Copy narration text EXACTLY — do not paraphrase.
- Specify voice (V1/V2/V3) clearly.
- If audio already exists, say "Audio already generated for scene X".
""",
        include_memory=True,
        web_search=False,
        thinking=False,
    )


@app.post("/")
async def invoke(text: str = Body(..., media_type="text/plain")):
    result = await _agent.run(text)
    return PlainTextResponse(result.output)
