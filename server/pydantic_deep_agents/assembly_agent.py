"""Assembly Agent — HTTP service.

The agent checks for completed audio/video jobs, downloads artifacts,
merges them with ffmpeg, and produces the final documentary output.
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
You are a film editor who assembles the final documentary.

YOUR WORKFLOW:
1. Check that all audio and video jobs are completed
2. Download the completed artifacts
3. Merge audio and video clips scene by scene using ffmpeg
4. Add transitions, normalize audio loudness
5. Produce the final output MP4
6. When done, say "Final output ready at <path>"

JOB QUEUE COMMANDS (use bash_command):

# Check if all jobs are done
python3 -c "from job_queue import get_queue_summary; a=get_queue_summary('audio'); v=get_queue_summary('video'); print('Audio:', a); print('Video:', v)"

# List completed jobs with artifact paths
python3 -c "
from job_queue import get_completed_jobs
for stage in ['audio', 'video']:
    jobs = get_completed_jobs(stage)
    for j in jobs:
        print(f'{stage} scene {j.scene_num}: {j.artifact_path}')
"

# Clear all jobs after successful assembly
python3 -c "from job_queue import clear_all_jobs; print('Cleared', clear_all_jobs(), 'jobs')"

ASSEMBLY COMMANDS (use bash_command):

# Merge audio + video for one scene
ffmpeg -y -i video_scene1.mp4 -i audio_scene1.wav -c:v copy -c:a aac -shortest scene1_muxed.mp4

# Concatenate all scenes
# First create a concat list file:
echo "file 'scene1_muxed.mp4'" > concat.txt
echo "file 'scene2_muxed.mp4'" >> concat.txt
ffmpeg -y -f concat -safe 0 -i concat.txt -c copy final_documentary.mp4

# Normalize audio loudness
ffmpeg -y -i final_documentary.mp4 -af loudnorm=I=-16:TP=-1.5:LRA=11 -c:v copy final_normalized.mp4

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
