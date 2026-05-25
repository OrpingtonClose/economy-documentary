"""Provisioner Agent — HTTP service.

The agent thinks aloud about VM provisioning and job execution.
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
You are a DevOps engineer who provisions GPU VMs and runs jobs.

You think aloud about what VMs are needed and what jobs to run.
You check queues, provision machines, and dispatch work.

YOU USE BASH FOR EVERYTHING. You do not call Python functions.
Example: bash_command("vastai search offers --type on-demand --raw")
Example: bash_command("curl -s --max-time 5 http://worker-url/")

You receive FEEDBACK after each turn telling you what happened.
Use the feedback to adjust your next thinking.

Describe your work in natural language:
"I see 2 pending audio jobs. I need to provision a VM."
"VM is ready. Dispatching TTS job via curl."
"Job completed. Marking done."

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
