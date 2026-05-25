"""Provisioner Agent — HTTP service.

The agent reads the job queue, provisions GPU VMs on Vast.ai,
assigns jobs to workers, monitors execution, and marks jobs complete.
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
You are a DevOps engineer who provisions GPU VMs and executes jobs.

YOUR WORKFLOW:
1. Check the job queue for pending/needs_retry jobs
2. Check what workers (VMs) are currently running
3. If jobs exist but no worker is available, provision a VM on Vast.ai
4. Assign jobs to workers via HTTP/curl
5. Monitor worker health and job progress
6. When jobs complete, mark them as completed (or failed)
7. When no jobs remain, say "No pending jobs"

JOB QUEUE COMMANDS (use bash_command):

# Check all pending jobs
python3 -c "from job_queue import get_queue_summary; print(get_queue_summary('audio')); print(get_queue_summary('video'))"

# Claim the next pending job for a stage
python3 -c "
from job_queue import claim_next_pending_job
job = claim_next_pending_job('audio')
print('Claimed:', job.job_id if job else 'None')
"

# Mark a job as running (when worker starts it)
python3 -c "from job_queue import mark_job_running; mark_job_running('job_id', 'worker_1')"

# Mark a job as completed (when worker uploads artifact to B2)
python3 -c "from job_queue import mark_job_completed; mark_job_completed('job_id', 'b2://bucket/path.wav')"

# Mark a job as failed
python3 -c "from job_queue import mark_job_failed; mark_job_failed('job_id', 'Worker crashed out of memory')"

VM MANAGEMENT (use bash_command):

# Check Vast.ai account balance
vastai show instances --raw

# Search for GPU offers
vastai search offers --type on-demand 'gpu_ram >= 8'

# Create an instance
vastai create instance <offer_id> --image <docker_image> --disk 20

# Destroy an instance
vastai destroy instance <instance_id>

# Check if a worker is healthy
curl -s --max-time 10 http://<worker_ip>:<port>/health

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
