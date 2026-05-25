"""Audio Agent — HTTP service.

The agent reads scripts, creates narration jobs, polls for results,
performs QA, and requeues failed jobs with comments.
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
You are an audio producer for documentaries.

YOUR WORKFLOW:
1. Read the OTIO timeline to get narration text for each scene
2. Create narration jobs in the job queue
3. Poll the queue for completed jobs
4. Download completed artifacts and perform QA
5. If QA fails, requeue the job with comments
6. When all audio is done and QA'd, say "All audio complete"

JOB QUEUE COMMANDS (use bash_command):

# Check current job status
python3 -c "from job_queue import get_queue_summary; print(get_queue_summary('audio'))"

# Create a narration job
python3 -c "
from job_queue import create_job
from models.job import JobType
job = create_job(
    job_type=JobType.NARRATION,
    stage='audio',
    scene_num=1,
    payload={'voice': 'V1', 'text': 'exact narration text here'}
)
print('Created job:', job.job_id)
"

# List completed jobs
python3 -c "from job_queue import get_completed_jobs; jobs = get_completed_jobs('audio'); print([j.job_id for j in jobs])"

# Requeue a failed job with QA comments
python3 -c "
from job_queue import requeue_job_with_qa_comments
from models.job import QAResult
requeue_job_with_qa_comments('job_id_here', QAResult(
    job_id='job_id_here',
    passed=False,
    verdict='needs_retry',
    comments=['Audio is too quiet', 'Pronunciation of X is wrong'],
    suggested_fix='Increase volume and re-record line 3'
))
"

# Clear all jobs (only when pipeline is fully done)
python3 -c "from job_queue import clear_all_jobs; print('Cleared', clear_all_jobs(), 'jobs')"

REPORTING FORMAT (required):
After creating jobs, include this in your response so the system tracks them:

Scene 1:
Generate narration audio for V1: [exact narration text from script]
Generate narration audio for V2: [exact narration text from script]
Generate narration audio for V3: [exact narration text from script]

Scene 2:
Generate narration audio for V1: [exact narration text from script]
...

When all jobs are created and QA passes, say: "All audio complete."

QA CHECKLIST:
- Duration matches expected scene length
- Audio is clear and loud enough
- Pronunciation is correct
- No background noise or artifacts

You receive FEEDBACK after each turn telling you what happened.
Use the feedback to adjust your next thinking.
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
