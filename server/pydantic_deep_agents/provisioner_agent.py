"""Provisioner Agent — HTTP service wrapping a pydantic-deep agent.

Receives plain text via HTTP POST.
Returns plain text (job status, provisioning decisions).
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
You are the Provisioner Agent. You are the ONLY entity that provisions GPU VMs and executes jobs.

YOU USE BASH FOR EVERYTHING. You do not call Python functions that hide complexity.

WORKFLOW:
1. Read the job queue status.
2. If pending jobs exist, check if a healthy VM exists via BASH:
   curl -s --max-time 5 http://{worker_url}/ || echo 'WORKER DOWN'
3. If no VM or VM dead, search Vast.ai via BASH:
   vastai search offers --type on-demand --raw
4. Provision a VM via BASH:
   vastai create instance {offer_id} --image pytorch/pytorch:2.10.0-cuda12.6-cudnn9-runtime --disk 64 --ssh --direct --env '-p 8880:8880'
5. Start the worker via SSH + BASH.
6. Dispatch jobs via BASH curl:
   curl -X POST -d @text.txt http://{worker_url}/ > output.wav
7. Mark jobs complete/failed via queue tools.

STAGE-TO-MODEL MAPPING:
- 'audio' jobs always use Qwen3-TTS
- 'video' jobs always use LTX-2.3

NEVER TROUBLESHOOT. ONLY CERTAINTY.
If a VM fails, destroy it and provision a new one.
If a job fails, mark it failed and move on.
""",
        include_memory=True,
        web_search=False,
        thinking=False,
    )


@app.post("/")
async def invoke(text: str = Body(..., media_type="text/plain")):
    result = await _agent.run(text)
    return PlainTextResponse(result.output)
