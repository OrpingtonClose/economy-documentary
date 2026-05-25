#!/usr/bin/env python3
"""Standalone LTX-2.3 runner. Called via bash from the VM agent.

Usage:
    python run_ltx_2_3.py --prompt "Rainbow over mountains" --duration 5 --output /workspace/out.mp4
"""
from __future__ import annotations

import argparse
import gc
import logging
import os
import subprocess
import sys
import uuid

# Add LTX-2 venv site-packages so ltx_pipelines is importable
_LTX_VENV = f"/workspace/ltx-2-repo/.venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
if os.path.isdir(_LTX_VENV) and _LTX_VENV not in sys.path:
    sys.path.insert(0, _LTX_VENV)

import numpy as np
import torch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="LTX-2.3 standalone runner")
    parser.add_argument("--prompt", required=True, help="Video generation prompt")
    parser.add_argument("--negative", default="", help="Negative prompt")
    parser.add_argument("--duration", type=float, default=5.0, help="Duration in seconds")
    parser.add_argument("--width", type=int, default=512, help="Width in pixels")
    parser.add_argument("--height", type=int, default=320, help="Height in pixels")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--steps", type=int, default=30, help="Inference steps")
    parser.add_argument("--output", required=True, help="Output MP4 path")
    args = parser.parse_args()

    models_dir = "/workspace/models"

    # Find model path
    candidate_ltx23 = os.path.join(models_dir, "ltx23")
    candidate_ltx2 = os.path.join(models_dir, "ltx2")
    if os.path.isdir(candidate_ltx23):
        model_path = candidate_ltx23
    elif os.path.isdir(candidate_ltx2):
        model_path = candidate_ltx2
    else:
        model_path = models_dir

    ckpt_path = os.path.join(model_path, "ltx-2.3-22b-dev.safetensors")
    gemma_root = os.path.join(model_path, "gemma")

    if not os.path.isfile(ckpt_path):
        logger.error("Checkpoint not found: %s", ckpt_path)
        return 1
    if not os.path.isdir(gemma_root):
        logger.error("Text encoder weights not found: %s", gemma_root)
        return 1

    # Verify model pins before loading
    from pathlib import Path
    from model_pin import LTX_VIDEO_PIN, LTX_VIDEO_GEMMA_PIN, verify_pin
    try:
        verify_pin(LTX_VIDEO_PIN, Path(model_path))
        verify_pin(LTX_VIDEO_GEMMA_PIN, Path(gemma_root))
    except Exception as exc:
        logger.error("Model pin verification failed: %s", exc)
        return 1

    logger.info("Loading LTX-2.3 from %s ...", model_path)

    from ltx_pipelines.ti2vid_one_stage import TI2VidOneStagePipeline
    from ltx_core.components.guiders import MultiModalGuiderParams

    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()

    pipe = TI2VidOneStagePipeline(
        checkpoint_path=ckpt_path,
        gemma_root=gemma_root,
        loras=[],
    )

    fps = 24
    raw_frames = int(args.duration * fps)
    num_frames = max(9, ((raw_frames - 1) // 8) * 8 + 1)

    negative = args.negative
    baseline = "worst quality, inconsistent motion, blurry, jittery, distorted, static, low resolution, morphing, warping, flicker, text, watermark, logo"
    negative = f"{negative}, {baseline}" if negative else baseline

    logger.info("Generating: %d frames (%.1fs), %dx%d, seed=%d", num_frames, num_frames / fps, args.width, args.height, args.seed)
    t0 = __import__("time").time()

    video_guider = MultiModalGuiderParams(
        cfg_scale=3.0, stg_scale=1.0, rescale_scale=0.7,
        modality_scale=3.0, skip_step=0, stg_blocks=[28],
    )
    audio_guider = MultiModalGuiderParams(
        cfg_scale=7.0, stg_scale=1.0, rescale_scale=0.7,
        modality_scale=3.0, skip_step=0, stg_blocks=[28],
    )

    video_iter, _audio = pipe(
        prompt=args.prompt, negative_prompt=negative, seed=args.seed,
        height=args.height, width=args.width, num_frames=num_frames, frame_rate=fps,
        num_inference_steps=args.steps,
        video_guider_params=video_guider, audio_guider_params=audio_guider, images=[],
    )

    chunks = []
    for chunk in video_iter:
        chunks.append(chunk.cpu().numpy())
    frames = np.concatenate(chunks, axis=0)

    gc.collect()
    torch.cuda.empty_cache()

    elapsed = __import__("time").time() - t0
    logger.info("Generated in %.1fs", elapsed)

    # Encode to MP4 via ffmpeg
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    result = subprocess.run(
        [
            "ffmpeg", "-y", "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{frames.shape[2]}x{frames.shape[1]}",
            "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            args.output,
        ],
        input=frames.tobytes(), capture_output=True,
    )

    if result.returncode != 0:
        logger.error("ffmpeg failed: %s", (result.stderr or b"")[:500])
        return 1

    logger.info("Done: %s (%d bytes)", args.output, os.path.getsize(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
