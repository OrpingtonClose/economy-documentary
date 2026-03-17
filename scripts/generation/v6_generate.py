#!/usr/bin/env python3
"""
V6 Clip Generator — Uses LTX-2 monorepo's TI2VidOneStagePipeline
Generates clips from v5_clip_plan.json, uploads each to B2 immediately.
Designed to run on a single GPU with sequential memory management.

Usage:
  python v6_generate.py --start 0 --end 334 [--resume]
  
Environment vars:
  B2_KEY_ID, B2_APP_KEY — Backblaze B2 credentials
  HF_TOKEN — HuggingFace token for model download
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import hashlib
from pathlib import Path

import torch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/root/v6_generation.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────
MODEL_DIR = Path("/root/models")
CHECKPOINT_PATH = MODEL_DIR / "ltx-2-19b-dev.safetensors"
GEMMA_ROOT = MODEL_DIR / "text_encoder"
CLIP_PLAN = Path("/root/v5_clip_plan.json")
OUTPUT_DIR = Path("/root/clips_out")
PROGRESS_FILE = Path("/root/v6_progress.json")

B2_BUCKET = "economy-vid-assets"
B2_PREFIX = "v5_clips_v2"

# ── LTX-2.3 Default Params (from constants.py) ───────────────────────────
DEFAULT_NEGATIVE_PROMPT = (
    "blurry, out of focus, overexposed, underexposed, low contrast, washed out colors, excessive noise, "
    "grainy texture, poor lighting, flickering, motion blur, distorted proportions, unnatural skin tones, "
    "deformed facial features, asymmetrical face, missing facial features, extra limbs, disfigured hands, "
    "wrong hand count, artifacts around text, inconsistent perspective, camera shake, incorrect depth of "
    "field, background too sharp, background clutter, distracting reflections, harsh shadows, inconsistent "
    "lighting direction, color banding, cartoonish rendering, 3D CGI look, unrealistic materials, uncanny "
    "valley effect, incorrect ethnicity, wrong gender, exaggerated expressions, wrong gaze direction, "
    "mismatched lip sync, silent or muted audio, distorted voice, robotic voice, echo, background noise, "
    "off-sync audio, incorrect dialogue, added dialogue, repetitive speech, jittery movement, awkward "
    "pauses, incorrect timing, unnatural transitions, inconsistent framing, tilted camera, flat lighting, "
    "inconsistent tone, cinematic oversaturation, stylized filters, or AI artifacts."
)

# ── B2 Upload ─────────────────────────────────────────────────────────────

def b2_upload(local_path: Path, remote_key: str) -> bool:
    """Upload a file to B2 using b2 CLI."""
    try:
        cmd = [
            "b2", "file", "upload",
            "--no-progress",
            B2_BUCKET,
            str(local_path),
            remote_key,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            log.info(f"  ✓ Uploaded to b2://{B2_BUCKET}/{remote_key}")
            return True
        else:
            log.error(f"  ✗ B2 upload failed: {result.stderr}")
            return False
    except Exception as e:
        log.error(f"  ✗ B2 upload exception: {e}")
        return False


# ── Progress Tracking ─────────────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {"completed": [], "failed": []}

def save_progress(progress: dict):
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))


# ── Pipeline Initialization ──────────────────────────────────────────────

def init_pipeline():
    """Initialize TI2VidOneStagePipeline from LTX-2 monorepo."""
    from ltx_pipelines.ti2vid_one_stage import TI2VidOneStagePipeline
    
    log.info("Initializing TI2VidOneStagePipeline...")
    log.info(f"  Checkpoint: {CHECKPOINT_PATH}")
    log.info(f"  Gemma root: {GEMMA_ROOT}")
    
    pipeline = TI2VidOneStagePipeline(
        checkpoint_path=str(CHECKPOINT_PATH),
        gemma_root=str(GEMMA_ROOT),
        loras=[],
        quantization=None,  # Full bf16, no quantization per user request
    )
    log.info("Pipeline initialized successfully.")
    return pipeline


# ── Single Clip Generation ───────────────────────────────────────────────

def generate_sub_clip(
    pipeline,
    prompt: str,
    num_frames: int,
    seed: int,
    output_path: Path,
    image_path: str = None,
    frame_rate: float = 24.0,
):
    """Generate a single sub-clip using the one-stage pipeline."""
    from ltx_core.components.guiders import MultiModalGuiderParams
    from ltx_pipelines.utils.media_io import encode_video
    
    # LTX-2.3 defaults for one-stage
    video_guider = MultiModalGuiderParams(
        cfg_scale=3.0,
        stg_scale=1.0,
        rescale_scale=0.7,
        modality_scale=3.0,
        skip_step=0,
        stg_blocks=[28],  # LTX-2.3 default
    )
    audio_guider = MultiModalGuiderParams(
        cfg_scale=7.0,
        stg_scale=1.0,
        rescale_scale=0.7,
        modality_scale=3.0,
        skip_step=0,
        stg_blocks=[28],
    )
    
    # Prepare image conditioning if we have a last frame
    images = []
    if image_path and Path(image_path).exists():
        from ltx_pipelines.utils.args import ImageConditioningInput
        images = [ImageConditioningInput(
            path=str(image_path),
            frame_idx=0,
            strength=1.0,
        )]
    
    video_iter, audio = pipeline(
        prompt=prompt,
        negative_prompt=DEFAULT_NEGATIVE_PROMPT,
        seed=seed,
        height=512,    # Stage 1 default
        width=768,     # Stage 1 default
        num_frames=num_frames,
        frame_rate=frame_rate,
        num_inference_steps=30,  # LTX-2.3 default
        video_guider_params=video_guider,
        audio_guider_params=audio_guider,
        images=images,
    )
    
    encode_video(
        video=video_iter,
        fps=frame_rate,
        audio=audio,
        output_path=str(output_path),
        video_chunks_number=1,
    )
    
    return output_path


def extract_last_frame(video_path: Path, output_path: Path) -> bool:
    """Extract the last frame from a video for continuity conditioning."""
    try:
        cmd = [
            "ffmpeg", "-y", "-sseof", "-0.1",
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "2",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and output_path.exists()
    except Exception:
        return False


def get_clip_duration(video_path: Path) -> float:
    """Get duration of a video file in seconds."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


# ── Main Generation Loop ────────────────────────────────────────────────

def generate_clip(pipeline, clip: dict, clip_idx: int, last_frame_path: str = None):
    """
    Generate all sub-clips for a single clip entry, concatenate them,
    trim to required duration, upload to B2.
    """
    clip_id = clip["id"]
    prompt = clip["prompt"]
    required_dur = clip["required_duration"]
    sub_clips = clip.get("sub_clips", [])
    
    log.info(f"[{clip_idx+1}/334] Generating {clip_id}: {len(sub_clips)} sub-clips, target {required_dur:.1f}s")
    log.info(f"  Prompt: {prompt[:100]}...")
    
    # Deterministic seed from clip_id
    seed = int(hashlib.md5(clip_id.encode()).hexdigest()[:8], 16) % (2**31)
    
    clip_dir = OUTPUT_DIR / clip_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    
    sub_paths = []
    prev_frame = last_frame_path  # For continuity from previous clip
    
    for si, sub in enumerate(sub_clips):
        sub_frames = sub["frames"]
        sub_type = sub["type"]
        sub_path = clip_dir / f"sub_{si:02d}.mp4"
        
        # Use last-frame conditioning for continuity sub-clips
        img_cond = None
        if sub_type in ("middle", "last") and prev_frame:
            img_cond = prev_frame
        elif sub_type == "first" and last_frame_path:
            # Continuity from previous clip's last frame
            img_cond = last_frame_path
        
        log.info(f"  Sub-clip {si}: {sub_frames} frames, type={sub_type}, img_cond={'yes' if img_cond else 'no'}")
        
        # Generate with slightly more frames to ensure we have enough
        # LTX needs (8k+1) frames
        gen_frames = sub_frames
        # Ensure it's 8k+1
        k = (gen_frames - 1) // 8
        gen_frames = 8 * k + 1
        if gen_frames < 9:
            gen_frames = 9  # minimum
        
        try:
            generate_sub_clip(
                pipeline=pipeline,
                prompt=prompt,
                num_frames=gen_frames,
                seed=seed + si,
                output_path=sub_path,
                image_path=img_cond,
            )
        except torch.cuda.OutOfMemoryError:
            log.error(f"  OOM on sub-clip {si}! Trying with fewer frames...")
            torch.cuda.empty_cache()
            # Try with minimum frames
            gen_frames = max(9, gen_frames // 2)
            k = (gen_frames - 1) // 8
            gen_frames = 8 * k + 1
            try:
                generate_sub_clip(
                    pipeline=pipeline,
                    prompt=prompt,
                    num_frames=gen_frames,
                    seed=seed + si,
                    output_path=sub_path,
                    image_path=img_cond,
                )
            except Exception as e2:
                log.error(f"  Failed even with {gen_frames} frames: {e2}")
                return None
        
        if sub_path.exists():
            sub_paths.append(sub_path)
            # Extract last frame for next sub-clip's continuity
            frame_out = clip_dir / f"last_frame_{si:02d}.jpg"
            if extract_last_frame(sub_path, frame_out):
                prev_frame = str(frame_out)
        else:
            log.error(f"  Sub-clip {si} not generated!")
            return None
    
    if not sub_paths:
        return None
    
    # Concatenate sub-clips if multiple
    final_path = clip_dir / f"{clip_id}_raw.mp4"
    if len(sub_paths) == 1:
        sub_paths[0].rename(final_path)
    else:
        # Use ffmpeg concat demuxer
        concat_list = clip_dir / "concat.txt"
        with open(concat_list, "w") as f:
            for sp in sub_paths:
                f.write(f"file '{sp}'\n")
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(final_path),
        ]
        subprocess.run(cmd, capture_output=True, timeout=60)
    
    if not final_path.exists():
        log.error(f"  Concatenation failed for {clip_id}")
        return None
    
    # Check duration and trim to required length (never stretch!)
    actual_dur = get_clip_duration(final_path)
    trimmed_path = clip_dir / f"{clip_id}.mp4"
    
    if actual_dur < required_dur * 0.95:
        # Too short — this is a problem. Log it but keep going.
        log.warning(f"  ⚠ {clip_id} is {actual_dur:.1f}s but needs {required_dur:.1f}s — TOO SHORT")
        # Still use it, but flag for regeneration
        final_path.rename(trimmed_path)
    elif actual_dur > required_dur + 0.5:
        # Trim to exact required duration
        cmd = [
            "ffmpeg", "-y",
            "-i", str(final_path),
            "-t", str(required_dur),
            "-c", "copy",
            str(trimmed_path),
        ]
        subprocess.run(cmd, capture_output=True, timeout=60)
        log.info(f"  Trimmed {actual_dur:.1f}s → {required_dur:.1f}s")
    else:
        final_path.rename(trimmed_path)
    
    if not trimmed_path.exists():
        log.error(f"  Final file missing for {clip_id}")
        return None
    
    # Upload to B2 (video only, we don't need the LTX audio)
    # Strip audio since we have our own narration
    noaudio_path = clip_dir / f"{clip_id}_noaudio.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(trimmed_path),
        "-an",  # Remove audio
        "-c:v", "copy",
        str(noaudio_path),
    ]
    subprocess.run(cmd, capture_output=True, timeout=60)
    
    upload_path = noaudio_path if noaudio_path.exists() else trimmed_path
    b2_key = f"{B2_PREFIX}/{clip_id}.mp4"
    b2_upload(upload_path, b2_key)
    
    # Extract last frame for next clip's continuity
    clip_last_frame = clip_dir / "last_frame_final.jpg"
    extract_last_frame(trimmed_path, clip_last_frame)
    
    # Clean up intermediate files to save disk
    for p in sub_paths:
        if p.exists():
            p.unlink()
    if final_path.exists():
        final_path.unlink()
    if trimmed_path.exists() and upload_path != trimmed_path:
        trimmed_path.unlink()
    
    return str(clip_last_frame) if clip_last_frame.exists() else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0, help="Start clip index (inclusive)")
    parser.add_argument("--end", type=int, default=334, help="End clip index (exclusive)")
    parser.add_argument("--resume", action="store_true", help="Skip already-completed clips")
    args = parser.parse_args()
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Load clip plan
    with open(CLIP_PLAN) as f:
        plan = json.load(f)
    clips = plan["clips"]
    
    log.info(f"V6 Generation: clips {args.start}-{args.end} of {len(clips)}")
    log.info(f"GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NO GPU'}")
    if torch.cuda.is_available():
        log.info(f"VRAM: {torch.cuda.get_device_properties(0).total_mem / 1e9:.1f}GB")
    
    # Load progress
    progress = load_progress()
    completed_set = set(progress["completed"])
    
    # Initialize pipeline
    pipeline = init_pipeline()
    
    last_frame = None
    total_time = 0
    generated = 0
    
    for i in range(args.start, min(args.end, len(clips))):
        clip = clips[i]
        clip_id = clip["id"]
        
        if args.resume and clip_id in completed_set:
            log.info(f"[{i+1}/334] Skipping {clip_id} (already completed)")
            # Try to load last frame for continuity
            lf = OUTPUT_DIR / clip_id / "last_frame_final.jpg"
            if lf.exists():
                last_frame = str(lf)
            continue
        
        t0 = time.time()
        try:
            last_frame = generate_clip(pipeline, clip, i, last_frame)
            elapsed = time.time() - t0
            total_time += elapsed
            generated += 1
            
            if last_frame:
                progress["completed"].append(clip_id)
                log.info(f"  ✓ {clip_id} done in {elapsed:.0f}s (avg {total_time/generated:.0f}s/clip)")
            else:
                progress["failed"].append(clip_id)
                log.error(f"  ✗ {clip_id} FAILED after {elapsed:.0f}s")
            
            save_progress(progress)
            
        except Exception as e:
            log.error(f"  ✗ {clip_id} EXCEPTION: {e}")
            progress["failed"].append(clip_id)
            save_progress(progress)
            # Clear CUDA cache and continue
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
    
    log.info(f"\n{'='*60}")
    log.info(f"Generation complete: {generated} clips in {total_time/3600:.1f}h")
    log.info(f"Completed: {len(progress['completed'])}, Failed: {len(progress['failed'])}")
    if progress["failed"]:
        log.info(f"Failed clips: {progress['failed']}")


if __name__ == "__main__":
    with torch.inference_mode():
        main()
