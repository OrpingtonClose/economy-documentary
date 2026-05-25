"""Full end-to-end pipeline test with REAL agents + STUB workers.

Flow:
1. Scenario agent writes script
2. Audio agent reads script, creates narration jobs
3. TTS worker (stub) processes jobs
4. Video agent reads script, creates video jobs
5. Video worker (stub) processes jobs
6. Assembly agent merges audio + video

NO MOCKS for agents (real DeepSeek API).
STUB engines for workers (no GPU needed).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_pipeline_v2 import run_pipeline
from job_queue import clear_all_jobs, get_queue_summary
from pydantic_deep_agents.launcher import launch_all, wait_for_agents, terminate_all
from worker_queue_adapter import run_tts_worker, run_video_worker


async def test_full_pipeline_stub_workers():
    """Run full pipeline with real agents and stub workers."""
    print("=" * 70)
    print("FULL END-TO-END: Real Agents + Stub Workers")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"[SETUP] Output dir: {tmpdir}")

        # 1. Clear queue
        clear_all_jobs()
        print("[SETUP] Queue cleared")

        # 2. Launch agents
        print("[1/6] Launching agents...")
        processes = launch_all()
        ready = await wait_for_agents(processes, timeout=60)
        if not ready:
            print("[ERROR] Agents failed to start")
            terminate_all(processes)
            return
        print(f"[1/6] {len(processes)} agents ready")

        try:
            # 3. Run pipeline orchestrator (stops after scenario+audio+video)
            print("[2/6] Running pipeline orchestrator...")
            result = await run_pipeline(
                brief="A 30-second documentary about rainbows",
                output_dir=tmpdir,
                max_cycles=10,  # Enough for scenario + audio + video
            )
            print(f"[2/6] Orchestrator: {result}")

            # 4. Run TTS worker to process audio jobs
            print("[3/6] Running TTS worker (stub)...")
            audio_processed = run_tts_worker(
                engine_type="stub",
                output_dir=os.path.join(tmpdir, "audio"),
                poll_interval=1.0,
                max_jobs=10,
            )
            print(f"[3/6] TTS processed {audio_processed} jobs")

            # 5. Run video worker to process video jobs
            print("[4/6] Running video worker (stub)...")
            video_processed = run_video_worker(
                engine_type="stub",
                output_dir=os.path.join(tmpdir, "video"),
                poll_interval=1.0,
                max_jobs=10,
            )
            print(f"[4/6] Video processed {video_processed} jobs")

            # 6. Check queue state
            print("[5/6] Checking queue state...")
            audio_summary = get_queue_summary("audio")
            video_summary = get_queue_summary("video")
            print(f"  Audio: {audio_summary}")
            print(f"  Video: {video_summary}")

            # 7. Verify artifacts exist
            print("[6/6] Verifying artifacts...")
            audio_files = []
            video_files = []
            for root, _, files in os.walk(tmpdir):
                for f in files:
                    path = os.path.join(root, f)
                    if f.endswith(".wav"):
                        audio_files.append(path)
                    elif f.endswith(".mp4"):
                        video_files.append(path)

            print(f"  Audio files: {len(audio_files)}")
            for f in audio_files:
                size = os.path.getsize(f)
                print(f"    {os.path.basename(f)}: {size} bytes")

            print(f"  Video files: {len(video_files)}")
            for f in video_files:
                size = os.path.getsize(f)
                print(f"    {os.path.basename(f)}: {size} bytes")

            # Assertions
            assert audio_processed > 0, "Expected at least 1 audio job"
            assert video_processed > 0, "Expected at least 1 video job"
            assert len(audio_files) > 0, "Expected audio artifacts"
            assert len(video_files) > 0, "Expected video artifacts"

            print("\n[PASS] Full pipeline test passed!")

        finally:
            print("[CLEANUP] Terminating agents...")
            terminate_all(processes)
            clear_all_jobs()
            print("[CLEANUP] Done")


if __name__ == "__main__":
    asyncio.run(test_full_pipeline_stub_workers())
