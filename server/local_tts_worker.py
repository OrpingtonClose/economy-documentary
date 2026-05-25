"""Local TTS worker — pulls narration jobs from queue, generates audio via edge-tts.

No GPU required. No Vast.ai costs. Pulls from SQLite queue, generates WAV,
marks jobs complete.

Usage:
    python local_tts_worker.py --output-dir ./pipeline_output/audio

This is a development/testing worker. For production, use GPU workers on Vast.ai.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import edge_tts

from job_queue import claim_next_pending_job, mark_job_completed, mark_job_failed
from models.job import JobType


VOICE_MAP = {
    "V1": "en-US-GuyNeural",
    "V2": "en-US-AriaNeural",
    "V3": "en-GB-SoniaNeural",
}


async def _generate_audio(text: str, voice: str, output_path: str) -> None:
    """Generate audio using edge-tts."""
    voice_id = VOICE_MAP.get(voice, "en-US-GuyNeural")
    communicate = edge_tts.Communicate(text, voice_id)
    await communicate.save(output_path)


async def process_one_job(stage: str, output_dir: str) -> bool:
    """Process one pending narration job. Returns True if a job was processed."""
    job = claim_next_pending_job(stage)
    if job is None:
        return False

    print(f"[WORKER] Processing {job.job_id} (scene {job.scene_num}, {job.job_type.value})")

    try:
        payload = job.payload
        voice = payload.get("voice", "V1")
        text = payload.get("text", "")

        if not text:
            mark_job_failed(job.job_id, "Empty narration text")
            return True

        # Generate filename
        os.makedirs(output_dir, exist_ok=True)
        wav_path = os.path.join(output_dir, f"scene{job.scene_num}_{voice}.wav")
        mp3_path = wav_path + ".mp3"

        # Generate audio (edge-tts outputs MP3)
        print(f"[WORKER] Generating audio for scene {job.scene_num}, voice {voice}")
        print(f"[WORKER] Text: {text[:60]}...")
        await _generate_audio(text, voice, mp3_path)

        if not os.path.exists(mp3_path):
            mark_job_failed(job.job_id, "Audio file not generated")
            return True

        # Convert MP3 → WAV (16-bit mono PCM) for ffmpeg compatibility
        import subprocess
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", mp3_path,
                "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
                wav_path,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            mark_job_failed(job.job_id, f"ffmpeg conversion failed: {result.stderr}")
            return True

        # Clean up MP3
        os.remove(mp3_path)

        # Mark complete
        mark_job_completed(job.job_id, wav_path)
        print(f"[WORKER] Complete: {wav_path}")
        return True

    except Exception as exc:
        mark_job_failed(job.job_id, str(exc))
        print(f"[WORKER] Failed: {exc}")
        return True


async def run_worker(
    stage: str = "audio",
    output_dir: str = "./pipeline_output/audio",
    poll_interval: float = 2.0,
    max_jobs: int | None = None,
) -> None:
    """Run the worker loop until no more jobs or max_jobs reached."""
    print(f"[WORKER] Starting local TTS worker for stage='{stage}'")
    print(f"[WORKER] Output dir: {output_dir}")
    print(f"[WORKER] Poll interval: {poll_interval}s")

    processed = 0
    while True:
        did_work = await process_one_job(stage, output_dir)
        if did_work:
            processed += 1
            if max_jobs is not None and processed >= max_jobs:
                print(f"[WORKER] Reached max_jobs ({max_jobs}), stopping")
                break
        else:
            # No jobs available — check if we should exit
            from job_queue import get_queue_summary
            summary = get_queue_summary(stage)
            pending = summary.get("pending", 0) + summary.get("needs_retry", 0)
            running = summary.get("running", 0)

            if pending == 0 and running == 0:
                print("[WORKER] No pending jobs and nothing running. Exiting.")
                break

            print(f"[WORKER] No jobs available. Waiting {poll_interval}s...")
            await asyncio.sleep(poll_interval)

    print(f"[WORKER] Processed {processed} jobs. Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local TTS Worker")
    parser.add_argument("--stage", default="audio")
    parser.add_argument("--output-dir", default="./pipeline_output/audio")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--max-jobs", type=int, default=None)
    args = parser.parse_args()

    asyncio.run(
        run_worker(
            stage=args.stage,
            output_dir=args.output_dir,
            poll_interval=args.poll_interval,
            max_jobs=args.max_jobs,
        )
    )
