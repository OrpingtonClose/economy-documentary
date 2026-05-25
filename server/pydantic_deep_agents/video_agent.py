"""Video Agent — HTTP service.

The agent reads visual notes, creates video render jobs, polls for results,
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
You are a video director for documentaries.

YOUR WORKFLOW:
1. Read the OTIO timeline to get visual notes for each scene
2. Create video render jobs in the job queue
3. Poll the queue for completed jobs
4. Download completed artifacts and perform QA
5. If QA fails, requeue the job with comments
6. When all video is done and QA'd, say "All video complete"

JOB QUEUE COMMANDS (use bash_command):

# Check current job status
python3 -c "from job_queue import get_queue_summary; print(get_queue_summary('video'))"

# Create a video render job
python3 -c "
from job_queue import create_job
from models.job import JobType
job = create_job(
    job_type=JobType.VIDEO_RENDER,
    stage='video',
    scene_num=1,
    payload={
        'prompt': 'cinematic wide shot of a rainbow over mountains, golden hour lighting, documentary style',
        'lora_id': '',
        'duration_sec': 5
    }
)
print('Created job:', job.job_id)
"

# List completed jobs
python3 -c "from job_queue import get_completed_jobs; jobs = get_completed_jobs('video'); print([j.job_id for j in jobs])"

# Requeue a failed job with QA comments
python3 -c "
from job_queue import requeue_job_with_qa_comments
from models.job import QAResult
requeue_job_with_qa_comments('job_id_here', QAResult(
    job_id='job_id_here',
    passed=False,
    verdict='needs_retry',
    comments=['Video is too dark', 'Subject is not centered'],
    suggested_fix='Increase brightness and center the rainbow'
))
"

REPORTING FORMAT (required):
After creating jobs, include this in your response so the system tracks them:

Scene 1:
Render video segment for scene 1: [detailed visual description from script]

Scene 2:
Render video segment for scene 2: [detailed visual description from script]
...

When all jobs are created and QA passes, say: "All video complete."

QA CHECKLIST:
- Video matches the visual description
- Duration matches expected scene length
- No visual artifacts, glitches, or watermarks
- Style is consistent with documentary tone
- Subject is clearly visible and well-composed

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
