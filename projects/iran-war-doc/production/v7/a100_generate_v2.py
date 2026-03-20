#!/usr/bin/env python3
"""
WAR ECONOMY — Single-Process LTX-2.3 Video Generation
======================================================
Loads the pipeline ONCE, then generates all clips in-process.
Eliminates ~40s model reload per generation (saves ~7.4 GPU-hours over 665 gens).

Usage:
  python3 a100_generate_v2.py --manifest clips_vm_a.json --output-dir /workspace/outputs
  python3 a100_generate_v2.py --manifest clips_vm_a.json --output-dir /workspace/outputs --gpu 0
  python3 a100_generate_v2.py --manifest clips_vm_a.json --output-dir /workspace/outputs --start-at 5
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import tempfile
from pathlib import Path

import torch

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

# ============================================================
# CONFIG — LTX-2.3 single-stage, full quality, bf16, no quant
# ============================================================
LTX_CHECKPOINT = "/workspace/models/ltx-2.3-22b-dev.safetensors"
GEMMA_ROOT = "/workspace/models/gemma-3-12b-it-qat-q4_0-unquantized"

HEIGHT = 512
WIDTH = 768
NUM_FRAMES = 121        # ~5.04s at 24fps
FRAME_RATE = 24.0
NUM_INFERENCE_STEPS = 30

# Guider params (LTX-2.3 defaults)
VIDEO_GUIDER = {
    "cfg_scale": 3.0,
    "stg_scale": 1.0,
    "rescale_scale": 0.7,
    "modality_scale": 3.0,
    "skip_step": 0,
    "stg_blocks": [28],
}
AUDIO_GUIDER = {
    "cfg_scale": 7.0,
    "stg_scale": 1.0,
    "rescale_scale": 0.7,
    "modality_scale": 3.0,
    "skip_step": 0,
    "stg_blocks": [28],
}

NEGATIVE_PROMPT = (
    "blurry, low quality, text, watermark, letters, words, subtitles, logo, "
    "static, frozen, looping, repeated frames, shaky, glitchy, worst quality, "
    "deformed, distorted, motion smear, motion artifacts"
)

# ============================================================
# B2 INLINE UPLOAD CONFIG
# ============================================================
B2_KEY_ID = "B2_KEY_ID"
B2_APP_KEY = "B2_APP_KEY"
B2_BUCKET = "economy-vid-assets"
B2_PREFIX = "v7_war_economy"
B2_AUTHORIZED = False


def b2_authorize():
    """Authorize B2 CLI (idempotent — only runs once)."""
    global B2_AUTHORIZED
    if B2_AUTHORIZED:
        return True
    try:
        result = subprocess.run(
            ["b2", "authorize-account", B2_KEY_ID, B2_APP_KEY],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            B2_AUTHORIZED = True
            log.info("B2 authorized successfully")
            return True
        else:
            log.error(f"B2 auth failed: {result.stderr}")
            return False
    except Exception as e:
        log.error(f"B2 auth exception: {e}")
        return False


def upload_clip_to_b2(clip_path: str, clip_id: str, scene_num: str):
    """Embed metadata via ffmpeg then upload to B2. Non-blocking on failure."""
    if not b2_authorize():
        log.warning(f"B2 not authorized — skipping upload for {clip_id}")
        return False

    meta_path = clip_path.replace(".mp4", "_b2.mp4")
    try:
        # Embed production metadata
        meta_comment = (
            f"LTX-2.3 | 768x512 | 121frames | 30steps | cfg3.0 | "
            f"stg1.0/block28 | scene{scene_num} fill | bf16 full quality"
        )
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", clip_path,
            "-metadata", f"title={clip_id}",
            "-metadata", f"comment={meta_comment}",
            "-metadata", "artist=War Economy Documentary Pipeline",
            "-c", "copy", meta_path,
        ]
        subprocess.run(ffmpeg_cmd, capture_output=True, timeout=30)

        upload_src = meta_path if os.path.exists(meta_path) else clip_path
        remote_path = f"{B2_PREFIX}/{clip_id}.mp4"

        result = subprocess.run(
            ["b2", "upload-file", B2_BUCKET, upload_src, remote_path],
            capture_output=True, text=True, timeout=120,
        )

        # Cleanup temp file
        if os.path.exists(meta_path):
            os.remove(meta_path)

        if result.returncode == 0:
            log.info(f"  ↑ {clip_id} uploaded to B2")
            return True
        else:
            log.warning(f"  ↑ {clip_id} B2 upload failed: {result.stderr[:200]}")
            return False

    except Exception as e:
        log.warning(f"  ↑ {clip_id} B2 upload exception: {e}")
        if os.path.exists(meta_path):
            os.remove(meta_path)
        return False


def extract_last_frame_from_tensor(video_tensor, save_path: str):
    """Extract last frame from decoded video tensor and save as JPEG.
    
    video_tensor: Iterator[torch.Tensor] or torch.Tensor with shape (frames, H, W, C)
    """
    import torchvision.io as tvio
    from PIL import Image
    import numpy as np

    # The video is an iterator of chunks; collect all frames
    if hasattr(video_tensor, '__next__') or hasattr(video_tensor, '__iter__'):
        frames = []
        for chunk in video_tensor:
            frames.append(chunk)
        video = torch.cat(frames, dim=0)  # (F, H, W, C)
    else:
        video = video_tensor

    # Last frame
    last_frame = video[-1]  # (H, W, C) — float [0, 1] or uint8
    if last_frame.dtype == torch.float32 or last_frame.dtype == torch.bfloat16:
        last_frame = (last_frame.float().clamp(0, 1) * 255).byte()
    
    # Save as JPEG via PIL
    arr = last_frame.cpu().numpy()
    img = Image.fromarray(arr)
    img.save(save_path, quality=95)
    return save_path


def extract_last_frame_ffmpeg(video_path: str, frame_path: str) -> bool:
    """Extract last frame from video file using ffmpeg."""
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
        cmd = [
            "ffmpeg", "-y", "-i", sub_paths[0],
            "-t", str(target_duration), "-c", "copy",
            output_path
        ]
    else:
        concat_file = output_path.replace(".mp4", "_concat.txt")
        with open(concat_file, "w") as f:
            for p in sub_paths:
                f.write(f"file '{p}'\n")

        raw_path = output_path.replace(".mp4", "_raw.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file, "-c", "copy", raw_path
        ], capture_output=True, timeout=60)

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


@torch.inference_mode()
def main():
    parser = argparse.ArgumentParser(description="Single-process LTX-2.3 generation")
    parser.add_argument("--manifest", required=True, help="JSON clip manifest")
    parser.add_argument("--output-dir", default="/workspace/outputs", help="Output directory")
    parser.add_argument("--gpu", type=int, default=None, help="GPU index (for multi-GPU machines)")
    parser.add_argument("--start-at", type=int, default=0, help="Resume from clip index N (0-based)")
    parser.add_argument("--test-only", action="store_true", help="Only generate first clip")
    args = parser.parse_args()

    # Pin to GPU
    if args.gpu is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)
        log.info(f"Pinned to GPU {args.gpu}")

    os.makedirs(args.output_dir, exist_ok=True)

    # Load manifest
    with open(args.manifest) as f:
        clips = json.load(f)

    total_clips = len(clips)
    total_gens = sum(c["ltx_clips_needed"] for c in clips)

    log.info(f"{'='*60}")
    log.info(f"WAR ECONOMY — LTX-2.3 Single-Process Generation")
    log.info(f"{'='*60}")
    log.info(f"Clips: {total_clips} | LTX generations: {total_gens}")
    log.info(f"Output: {args.output_dir}")
    log.info(f"Start at: {args.start_at} | Test only: {args.test_only}")
    log.info(f"Loading pipeline (one-time)...")

    # ==========================================
    # LOAD PIPELINE ONCE
    # ==========================================
    load_start = time.time()

    from ltx_core.components.guiders import MultiModalGuiderParams
    from ltx_pipelines.ti2vid_one_stage import TI2VidOneStagePipeline
    from ltx_pipelines.utils.args import ImageConditioningInput
    from ltx_pipelines.utils.media_io import encode_video

    pipeline = TI2VidOneStagePipeline(
        checkpoint_path=LTX_CHECKPOINT,
        gemma_root=GEMMA_ROOT,
        loras=(),
        quantization=None,
    )

    load_elapsed = time.time() - load_start
    log.info(f"Pipeline loaded in {load_elapsed:.1f}s")

    # Build guider params
    video_guider_params = MultiModalGuiderParams(**VIDEO_GUIDER)
    audio_guider_params = MultiModalGuiderParams(**AUDIO_GUIDER)

    # ==========================================
    # GENERATION LOOP
    # ==========================================
    results = []
    gen_count = 0
    start_time = time.time()

    if args.test_only:
        clips = clips[:1]

    for idx, clip_data in enumerate(clips):
        clip_id = clip_data["clip_id"]
        prompt = clip_data["prompt"]
        target_dur = clip_data["target_duration_sec"]
        ltx_count = clip_data["ltx_clips_needed"]
        seed_base = clip_data.get("seed", hash(clip_id) % 2**31)

        final_path = os.path.join(args.output_dir, f"{clip_id}.mp4")

        # Skip completed clips (resume support)
        if os.path.exists(final_path) and os.path.getsize(final_path) > 1000:
            log.info(f"[{idx+1}/{len(clips)}] {clip_id} — already exists, skipping")
            results.append({"clip_id": clip_id, "status": "skipped"})
            continue

        # Skip clips before start-at index
        if idx < args.start_at:
            log.info(f"[{idx+1}/{len(clips)}] {clip_id} — before start-at {args.start_at}, skipping")
            results.append({"clip_id": clip_id, "status": "skipped"})
            continue

        log.info(f"\n{'='*60}")
        log.info(f"[{idx+1}/{len(clips)}] {clip_id} | {target_dur}s | {ltx_count} sub-clips")
        log.info(f"{'='*60}")

        sub_paths = []
        clip_start = time.time()
        clip_failed = False

        for sub_idx in range(ltx_count):
            sub_path = os.path.join(args.output_dir, f"{clip_id}_sub{sub_idx:02d}.mp4")
            seed = seed_base + sub_idx

            # Build image conditioning for frame-chaining
            images = []
            if sub_idx > 0 and sub_paths:
                # Extract last frame from previous sub-clip file
                prev_path = sub_paths[-1]
                frame_path = os.path.join(args.output_dir, f"{clip_id}_sub{sub_idx-1:02d}_lastframe.jpg")
                if extract_last_frame_ffmpeg(prev_path, frame_path):
                    images = [ImageConditioningInput(
                        path=frame_path,
                        frame_idx=0,
                        strength=1.0,
                        crf=33,
                    )]
                    gen_prompt = f"Camera continues, scene continues naturally. {prompt}"
                else:
                    log.error(f"  ✗ Could not extract last frame from {prev_path}")
                    clip_failed = True
                    break
            else:
                gen_prompt = prompt

            log.info(f"  Sub {sub_idx}/{ltx_count-1} | seed={seed} | img_cond={'yes' if images else 'no'}")
            t0 = time.time()

            try:
                video, audio = pipeline(
                    prompt=gen_prompt,
                    negative_prompt=NEGATIVE_PROMPT,
                    seed=seed,
                    height=HEIGHT,
                    width=WIDTH,
                    num_frames=NUM_FRAMES,
                    frame_rate=FRAME_RATE,
                    num_inference_steps=NUM_INFERENCE_STEPS,
                    video_guider_params=video_guider_params,
                    audio_guider_params=audio_guider_params,
                    images=images,
                )

                # Encode to file
                encode_video(
                    video=video,
                    fps=FRAME_RATE,
                    audio=audio,
                    output_path=sub_path,
                    video_chunks_number=1,
                )

                elapsed = time.time() - t0
                size_mb = os.path.getsize(sub_path) / 1e6
                gen_count += 1
                log.info(f"  ✓ Sub {sub_idx} done in {elapsed:.1f}s ({size_mb:.1f} MB) [{gen_count} total gens]")
                sub_paths.append(sub_path)

            except Exception as e:
                elapsed = time.time() - t0
                log.error(f"  ✗ Sub {sub_idx} failed after {elapsed:.1f}s: {e}")
                clip_failed = True
                break

        # Concatenate and trim
        if not clip_failed and len(sub_paths) == ltx_count:
            if concat_subclips(sub_paths, final_path, target_dur):
                clip_elapsed = time.time() - clip_start
                log.info(f"✓ {clip_id} complete in {clip_elapsed:.1f}s ({target_dur}s video)")
                results.append({
                    "clip_id": clip_id, "status": "complete",
                    "path": final_path, "time": clip_elapsed,
                    "sub_clips": ltx_count,
                })

                # INLINE B2 UPLOAD — immediately after generation
                _scene_match = re.search(r'scene_(\d+)', clip_id)
                _scene_num = _scene_match.group(1) if _scene_match else "00"
                upload_clip_to_b2(final_path, clip_id, _scene_num)

                # Cleanup sub-clips to save disk
                for sp in sub_paths:
                    os.remove(sp) if os.path.exists(sp) else None
                # Cleanup temp files
                for ext in ["_concat.txt", "_raw.mp4"]:
                    tf = final_path.replace(".mp4", ext)
                    os.remove(tf) if os.path.exists(tf) else None
                for si in range(ltx_count):
                    lf = os.path.join(args.output_dir, f"{clip_id}_sub{si:02d}_lastframe.jpg")
                    os.remove(lf) if os.path.exists(lf) else None
            else:
                log.error(f"✗ {clip_id} concat failed")
                results.append({"clip_id": clip_id, "status": "failed", "sub_clips_done": len(sub_paths)})
        else:
            results.append({"clip_id": clip_id, "status": "failed", "sub_clips_done": len(sub_paths)})

        # Save progress checkpoint
        checkpoint = {
            "total": total_clips,
            "processed": idx + 1,
            "completed": sum(1 for r in results if r["status"] == "complete"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "generations_done": gen_count,
            "elapsed_sec": time.time() - start_time,
            "results": results,
        }
        with open(os.path.join(args.output_dir, "generation_progress.json"), "w") as f:
            json.dump(checkpoint, f, indent=2)

    # Final summary
    elapsed = time.time() - start_time
    completed = sum(1 for r in results if r["status"] == "complete")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"] == "skipped")

    log.info(f"\n{'='*60}")
    log.info(f"GENERATION COMPLETE")
    log.info(f"{'='*60}")
    log.info(f"Total: {len(clips)} | Complete: {completed} | Failed: {failed} | Skipped: {skipped}")
    log.info(f"LTX generations: {gen_count}")
    log.info(f"Time: {elapsed:.0f}s ({elapsed/60:.1f} min, {elapsed/3600:.1f} hr)")
    if gen_count > 0:
        log.info(f"Avg per generation: {elapsed/gen_count:.1f}s (vs ~598s with subprocess)")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    main()
