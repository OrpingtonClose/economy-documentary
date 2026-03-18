#!/usr/bin/env python3
"""
WAR ECONOMY — A100 80GB Video Generation Script
=================================================
Runs LTX-2.3 (22B, bf16, single-stage, no distillation, no upscaler)
on an assigned subset of clips.

Usage:
  python3 a100_generate.py --manifest clips_vm0.json --output-dir /workspace/outputs
  python3 a100_generate.py --manifest clips_vm0.json --output-dir /workspace/outputs --test-only
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

# ============================================================
# CONFIG — LTX-2.3 single-stage, full quality
# ============================================================
LTX_CHECKPOINT = "/workspace/models/ltx-2.3-22b-dev.safetensors"
GEMMA_ROOT = "/workspace/models/gemma-3-12b-it-qat-q4_0-unquantized"

# LTX-2.3 recommended defaults (from constants.py)
LTX_CONFIG = {
    "height": 512,
    "width": 768,
    "num_frames": 121,       # ~5s at 24fps
    "frame_rate": 24.0,
    "num_inference_steps": 30,  # LTX-2.3 default
    # Guider params (LTX-2.3 defaults)
    "video_cfg_scale": 3.0,
    "video_stg_scale": 1.0,
    "video_rescale": 0.7,
    "video_modality_scale": 3.0,
    "video_stg_blocks": [28],  # LTX-2.3 uses block 28
    "audio_cfg_scale": 7.0,
    "audio_stg_scale": 1.0,
    "audio_rescale": 0.7,
    "audio_modality_scale": 3.0,
    "audio_stg_blocks": [28],
}

NEGATIVE_PROMPT = (
    "blurry, low quality, text, watermark, letters, words, subtitles, logo, "
    "static, frozen, looping, repeated frames, shaky, glitchy, worst quality, "
    "deformed, distorted, motion smear, motion artifacts"
)


def generate_clip_via_cli(prompt: str, output_path: str, seed: int, image_path: str = None) -> bool:
    """Generate a single ~5s clip using the ltx_pipelines CLI."""
    cmd = [
        sys.executable, "-m", "ltx_pipelines.ti2vid_one_stage",
        "--checkpoint-path", LTX_CHECKPOINT,
        "--gemma-root", GEMMA_ROOT,
        "--prompt", prompt,
        "--negative-prompt", NEGATIVE_PROMPT,
        "--output-path", output_path,
        "--seed", str(seed),
        "--height", str(LTX_CONFIG["height"]),
        "--width", str(LTX_CONFIG["width"]),
        "--num-frames", str(LTX_CONFIG["num_frames"]),
        "--frame-rate", str(LTX_CONFIG["frame_rate"]),
        "--num-inference-steps", str(LTX_CONFIG["num_inference_steps"]),
        "--video-cfg-guidance-scale", str(LTX_CONFIG["video_cfg_scale"]),
        "--video-stg-guidance-scale", str(LTX_CONFIG["video_stg_scale"]),
        "--video-rescale-scale", str(LTX_CONFIG["video_rescale"]),
        "--a2v-guidance-scale", str(LTX_CONFIG["video_modality_scale"]),
        "--video-stg-blocks", str(LTX_CONFIG["video_stg_blocks"][0]),
        "--audio-cfg-guidance-scale", str(LTX_CONFIG["audio_cfg_scale"]),
        "--audio-stg-guidance-scale", str(LTX_CONFIG["audio_stg_scale"]),
        "--audio-rescale-scale", str(LTX_CONFIG["audio_rescale"]),
        "--v2a-guidance-scale", str(LTX_CONFIG["audio_modality_scale"]),
        "--audio-stg-blocks", str(LTX_CONFIG["audio_stg_blocks"][0]),
    ]

    # Add image conditioning for frame-chaining
    # CLI format: --image PATH FRAME_IDX STRENGTH [CRF]
    if image_path and os.path.exists(image_path):
        cmd.extend(["--image", image_path, "0", "1.0", "33"])

    log.info(f"Running: {' '.join(cmd[:6])}... → {output_path}")
    t0 = time.time()

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        elapsed = time.time() - t0

        if proc.returncode == 0 and os.path.exists(output_path):
            size_mb = os.path.getsize(output_path) / 1e6
            log.info(f"  ✓ Generated in {elapsed:.1f}s ({size_mb:.1f} MB)")
            return True
        else:
            log.error(f"  ✗ Failed (exit {proc.returncode}) after {elapsed:.1f}s")
            if proc.stderr:
                log.error(f"  stderr: {proc.stderr[-2000:]}")
            if proc.stdout:
                log.error(f"  stdout (last 500): {proc.stdout[-500:]}")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"  ✗ Timeout after 900s")
        return False


def extract_last_frame(video_path: str, frame_path: str) -> bool:
    """Extract the last frame from a video clip for frame-chaining."""
    cmd = [
        "ffmpeg", "-y", "-sseof", "-0.1",
        "-i", video_path,
        "-frames:v", "1", "-q:v", "2",
        frame_path
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
        return os.path.exists(frame_path)
    except Exception:
        return False


def concat_subclips(sub_paths: list, output_path: str, target_duration: float) -> bool:
    """Concatenate sub-clips and trim to target duration."""
    if len(sub_paths) == 1:
        # Just trim the single clip
        cmd = [
            "ffmpeg", "-y", "-i", sub_paths[0],
            "-t", str(target_duration), "-c", "copy",
            output_path
        ]
    else:
        # Create concat list
        concat_file = output_path.replace(".mp4", "_concat.txt")
        with open(concat_file, "w") as f:
            for p in sub_paths:
                f.write(f"file '{p}'\n")

        raw_path = output_path.replace(".mp4", "_raw.mp4")
        # Concat
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file, "-c", "copy", raw_path
        ], capture_output=True, timeout=60)

        # Trim
        cmd = [
            "ffmpeg", "-y", "-i", raw_path,
            "-t", str(target_duration), "-c", "copy",
            output_path
        ]

    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
        return os.path.exists(output_path)
    except Exception:
        return False


def process_clip(clip_data: dict, output_dir: str) -> dict:
    """Process a single clip (may involve multiple LTX generations for frame-chaining)."""
    clip_id = clip_data["clip_id"]
    prompt = clip_data["prompt"]
    target_dur = clip_data["target_duration_sec"]
    ltx_count = clip_data["ltx_clips_needed"]
    seed_base = clip_data.get("seed", hash(clip_id) % 2**31)

    final_path = os.path.join(output_dir, f"{clip_id}.mp4")

    # Skip if already generated
    if os.path.exists(final_path):
        log.info(f"[skip] {clip_id} already exists")
        return {"clip_id": clip_id, "status": "skipped", "path": final_path}

    log.info(f"\n{'='*60}")
    log.info(f"Clip: {clip_id} | {target_dur}s | {ltx_count} LTX generations")
    log.info(f"{'='*60}")

    sub_paths = []
    clip_start = time.time()

    for sub_idx in range(ltx_count):
        sub_path = os.path.join(output_dir, f"{clip_id}_sub{sub_idx:02d}.mp4")
        seed = seed_base + sub_idx

        if sub_idx == 0:
            # Text-to-video (first segment)
            success = generate_clip_via_cli(prompt, sub_path, seed)
        else:
            # Frame-chaining: extract last frame from previous clip
            prev_path = sub_paths[-1]
            frame_path = os.path.join(output_dir, f"{clip_id}_sub{sub_idx-1:02d}_lastframe.jpg")

            if not extract_last_frame(prev_path, frame_path):
                log.error(f"  ✗ Could not extract last frame from {prev_path}")
                break

            # Image-to-video continuation
            continuation_prompt = f"Camera continues, scene continues naturally. {prompt}"
            success = generate_clip_via_cli(continuation_prompt, sub_path, seed, image_path=frame_path)

        if not success:
            log.error(f"  ✗ Sub-clip {sub_idx} failed")
            break

        sub_paths.append(sub_path)

    # Concatenate and trim
    if len(sub_paths) == ltx_count:
        if concat_subclips(sub_paths, final_path, target_dur):
            elapsed = time.time() - clip_start
            log.info(f"✓ {clip_id} complete in {elapsed:.1f}s ({target_dur}s video)")
            return {"clip_id": clip_id, "status": "complete", "path": final_path, "time": elapsed}

    return {"clip_id": clip_id, "status": "failed", "sub_clips_done": len(sub_paths)}


def main():
    parser = argparse.ArgumentParser(description="A100 80GB LTX-2.3 Video Generation")
    parser.add_argument("--manifest", required=True, help="JSON file with clip assignments for this VM")
    parser.add_argument("--output-dir", default="/workspace/outputs", help="Output directory for clips")
    parser.add_argument("--test-only", action="store_true", help="Generate only first clip as test")
    parser.add_argument("--resume", action="store_true", help="Skip already-generated clips")
    parser.add_argument("--gpu", type=int, default=None, help="GPU index for multi-GPU machines (sets CUDA_VISIBLE_DEVICES)")
    args = parser.parse_args()

    # Pin to specific GPU on multi-GPU machines
    if args.gpu is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)
        log.info(f"Pinned to GPU {args.gpu}")

    os.makedirs(args.output_dir, exist_ok=True)

    # Load clip manifest
    with open(args.manifest) as f:
        clips = json.load(f)

    total_clips = len(clips)
    total_gens = sum(c["ltx_clips_needed"] for c in clips)

    log.info(f"{'='*60}")
    log.info(f"WAR ECONOMY — LTX-2.3 Video Generation")
    log.info(f"{'='*60}")
    log.info(f"Clips assigned: {total_clips}")
    log.info(f"LTX generations: {total_gens}")
    log.info(f"Output dir: {args.output_dir}")
    log.info(f"Test only: {args.test_only}")

    if args.test_only:
        clips = clips[:1]
        log.info("TEST MODE: Processing only first clip")

    # Process clips
    results = []
    start_time = time.time()

    for idx, clip_data in enumerate(clips):
        log.info(f"\n[{idx+1}/{len(clips)}] Processing {clip_data['clip_id']}...")
        result = process_clip(clip_data, args.output_dir)
        results.append(result)

        # Save progress checkpoint
        checkpoint = {
            "total": total_clips,
            "processed": idx + 1,
            "completed": sum(1 for r in results if r["status"] == "complete"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "elapsed_sec": time.time() - start_time,
            "results": results,
        }
        with open(os.path.join(args.output_dir, "generation_progress.json"), "w") as f:
            json.dump(checkpoint, f, indent=2)

    # Summary
    elapsed = time.time() - start_time
    completed = sum(1 for r in results if r["status"] == "complete")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"] == "skipped")

    log.info(f"\n{'='*60}")
    log.info(f"GENERATION COMPLETE")
    log.info(f"{'='*60}")
    log.info(f"Total: {len(clips)} | Complete: {completed} | Failed: {failed} | Skipped: {skipped}")
    log.info(f"Time: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    main()
