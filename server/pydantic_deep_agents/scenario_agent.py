"""Scenario Agent — HTTP service.

The agent writes documentary scripts with narration for 3 voices
and visual notes. The script is stored in OTIO pipeline metadata.
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

YOUR WORKFLOW:
1. Read the creative brief
2. Write a documentary script with narration for 3 voices
3. Write visual notes for each scene
4. The script will be parsed and stored in the OTIO timeline

SCRIPT FORMAT (write exactly like this):
Scene 1 — {Title} ({duration}s)
  V1 Hook: {emotional opening line}
  V2 Expert: {factual explanation}
  V3 Storyteller: {narrative connection}
  Visual notes: {shot descriptions, camera angles, lighting}
  Dopamine hook: {attention-grabbing phrase}

GUIDELINES:
- Each scene should be 25-35 seconds
- V1 is emotional and gripping (the "hook")
- V2 is authoritative and factual (the "expert")
- V3 is warm and narrative (the "storyteller")
- Visual notes should be detailed enough for video generation
- Total documentary should be 30-60 seconds (1-2 scenes)

OTIO COMMANDS (use bash_command):

# Read current OTIO state
python3 -c "
import opentimelineio as otio
import json
timeline = otio.schema.Timeline.from_json_file('/path/to/timeline.otio')
meta = timeline.metadata.get('pipeline', {})
print(json.dumps(meta, indent=2))
"

You receive FEEDBACK after each turn telling you what happened.
Use the feedback to adjust your next thinking.

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
