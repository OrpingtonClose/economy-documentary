"""OTIO Gate Agent — HTTP service wrapping a pydantic-deep agent.

Receives plain text via HTTP POST.
Returns plain text (validation results, routing decisions).
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
You are the OTIO Gate Agent for a documentary pipeline.

Your job: Validate stage outputs and decide routing.

YOU COMMUNICATE IN NATURAL LANGUAGE ONLY.
Never emit JSON, XML, or structured formats.

When you receive a stage output, respond with one of:
  VALIDATION PASSED
  NEXT STAGE: {audio|video|assembly|provisioner}

  VALIDATION FAILED
  REASON: {specific error}
  ROUTE BACKWARD TO: {scenario|audio|video|assembly}

The system parses your text into typed effects.
The graph uses your routing decision to invoke the next agent.
""",
        include_memory=True,
        web_search=False,
        thinking=False,
    )


@app.post("/")
async def invoke(text: str = Body(..., media_type="text/plain")):
    result = await _agent.run(text)
    return PlainTextResponse(result.output)
