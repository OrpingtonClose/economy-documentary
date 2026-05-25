"""Scenario Agent — HTTP service wrapping a pydantic-deep agent.

Receives plain text via HTTP POST.
Returns plain text (script proposals, narration, visual notes).
Uses deepseek/deepseek-v4-flash.
"""

from __future__ import annotations

import os

from fastapi import Body, FastAPI
from fastapi.responses import PlainTextResponse
from pydantic_deep import create_deep_agent

# API key is passed at startup, not from env
_api_key: str = ""

app = FastAPI()


@app.on_event("startup")
async def _startup():
    global _agent
    _agent = create_deep_agent(
        model="deepseek/deepseek-v4-flash",
        instructions="""
You are the Scenario Agent for a documentary pipeline.

Your job: Write documentary scripts with narration for 3 voices and visual notes.

VOICES:
- V1 Hook: Emotional, dopamine-driven opening. Grabs attention in 5 seconds.
- V2 Expert: Authoritative, factual. Explains the science/mechanism.
- V3 Storyteller: Human, narrative. Connects to culture, emotion, meaning.

OUTPUT FORMAT (plain text only):
Scene {N} — {Title} ({duration}s)
  V1 Hook: {text}
  V2 Expert: {text}
  V3 Storyteller: {text}
  Visual notes: {shot descriptions}
  Dopamine hook: {phrase}
  Pronunciation hints: {word=IPA}

RULES:
- You communicate in NATURAL LANGUAGE ONLY.
- Never emit JSON, XML, or structured formats.
- The system parses your text into typed effects.
- Be specific about shots, timing, and emotional beats.
- Use the exact format above so the parser can extract your intent.
""",
        include_memory=True,
        web_search=True,
        thinking=False,
    )


@app.post("/")
async def invoke(text: str = Body(..., media_type="text/plain")):
    result = await _agent.run(text)
    return PlainTextResponse(result.output)
