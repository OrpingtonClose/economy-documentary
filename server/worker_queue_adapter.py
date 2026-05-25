"""Queue adapter for existing workers (Qwen3-TTS, LTX-Video).

Wraps the real worker engines and makes them pull from the SQLite job queue
instead of receiving HTTP requests. This bridges the v2 job queue architecture
with the existing worker implementations.

NO MOCKS — uses the real engine (Qwen3-TTS on GPU, stub for local dev).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from job_queue import claim_next_pending_job, mark_job_running, mark_job_completed, mark_job_failed
from models.job import JobType

logger = logging.getLogger(__name__)


def _resolve_engine(engine_type: str):
    """Resolve engine by type string."""
    if engine_type == "qwen3":
        try:
            from strands_agents.qwen3_tts_worker.engine import Qwen3TTSEngine  # noqa: PLC0415
            return Qwen3TTSEngine()
        except ImportError:
            logger.warning("Qwen3 backend not installed, falling back to stub")
            from strands_agents.qwen3_tts_worker.engine import StubTTSEngine  # noqa: PLC0415
            return StubTTSEngine()
    elif engine_type == "stub":
        from strands_agents.qwen3_tts_worker.engine import StubTTSEngine  # noqa: PLC0415
        return StubTTSEngine()
    else:
        raise ValueError(f"Unknown engine type: {engine_type}")


def _synthesize_tts(
    engine,
    text: str,
    voice_id: str,
    output_path: str,
) -> bool:
    """Run TTS synthesis and save to disk."""
    from strands_agents.qwen3_tts_worker.engine import SynthesisRequest

    try:
        result = engine.synthesize(
            SynthesisRequest(
                text=text,
                voice_id=voice_id,
                language="en",
            )
        )
    except Exception as exc:
        logger.error("Synthesis failed: %s", exc)
        return False

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(result.wav_bytes)

    logger.info(
        "Synthesized: voice=%s, duration=%.2fs, bytes=%d, path=%s",
        voice_id, result.duration_s, len(result.wav_bytes), output_path,
    )
    return True


def run_tts_worker(
    engine_type: str = "stub",
    output_dir: str = "./pipeline_output/audio",
    poll_interval: float = 2.0,
    max_jobs: int | None = None,
) -> int:
    """Run TTS worker that pulls from job queue.

    Returns number of jobs processed.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("Starting TTS worker (engine=%s)", engine_type)
    engine = _resolve_engine(engine_type)
    logger.info("Engine ready: %s", engine.engine_id)

    processed = 0
    while True:
        job = claim_next_pending_job("audio")
        if job is None:
            # No jobs — check if we should exit
            from job_queue import get_queue_summary
            summary = get_queue_summary("audio")
            pending = summary.get("pending", 0) + summary.get("needs_retry", 0)
            running = summary.get("running", 0)

            if pending == 0 and running == 0:
                logger.info("No pending or running jobs. Exiting.")
                break

            logger.info("No jobs available. Waiting %.1fs...", poll_interval)
            time.sleep(poll_interval)
            continue

        logger.info(
            "Processing %s (scene=%d, type=%s)",
            job.job_id, job.scene_num, job.job_type.value,
        )

        mark_job_running(job.job_id, worker_id=f"tts-{engine.engine_id}")

        payload = job.payload
        voice = payload.get("voice", "V1")
        text = payload.get("text", "")

        if not text:
            mark_job_failed(job.job_id, "Empty narration text")
            continue

        output_path = os.path.join(
            output_dir,
            f"scene{job.scene_num}_{voice}.wav",
        )

        ok = _synthesize_tts(engine, text, voice, output_path)
        if ok:
            mark_job_completed(job.job_id, output_path)
            processed += 1
        else:
            mark_job_failed(job.job_id, "Synthesis failed")

        if max_jobs is not None and processed >= max_jobs:
            logger.info("Reached max_jobs (%d), stopping", max_jobs)
            break

    logger.info("Processed %d jobs. Done.", processed)
    return processed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TTS Worker Queue Adapter")
    parser.add_argument("--engine", default="stub", choices=["qwen3", "stub"])
    parser.add_argument("--output-dir", default="./pipeline_output/audio")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--max-jobs", type=int, default=None)
    args = parser.parse_args()

    run_tts_worker(
        engine_type=args.engine,
        output_dir=args.output_dir,
        poll_interval=args.poll_interval,
        max_jobs=args.max_jobs,
    )
