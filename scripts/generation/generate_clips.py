#!/usr/bin/env python3
"""
V5 Clip Generation Script - runs on Vast.ai VM.
Generates assigned clips using LTX-2.3 distilled + FP8.
Supports frame chaining for clips needing multiple sub-clips.
Uploads each completed clip to B2 immediately.

Usage: python3 generate_clips.py <assignments_file.json> [--start N] [--count M]
"""

import json
import os
import gc
import sys
import time

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
import hashlib
import base64
import subprocess
import logging
import traceback

import torch

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
logging.getLogger().setLevel(logging.WARNING)

# B2 credentials
B2_KEY_ID = "${B2_KEY_ID}"
B2_APP_KEY = "${B2_APP_KEY}"
B2_BUCKET = "economy-vid-assets"
B2_BUCKET_ID = "8023e9c8f670ec4f9fc5051f"

MODELS_DIR = "/root/models"
OUTPUT_DIR = "/root/ltx_gen/outputs"
FRAMES_DIR = "/root/ltx_gen/frames"
PROGRESS_FILE = "/root/ltx_gen/progress.json"

FPS = 24
WIDTH = 768
HEIGHT = 512
NEGATIVE_PROMPT = "blurry, low quality, distorted, text, watermark, logo, subtitles, words, letters, numbers, UI overlay, cartoon, anime, illustration, painting, drawing, screen with text, monitor with data"

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(FRAMES_DIR, exist_ok=True)

# ---- B2 Upload ----
_b2_auth_cache = {}

def b2_authorize():
    global _b2_auth_cache
    if _b2_auth_cache and time.time() - _b2_auth_cache.get("ts", 0) < 3600:
        return _b2_auth_cache
    auth = base64.b64encode(f"{B2_KEY_ID}:{B2_APP_KEY}".encode()).decode()
    result = subprocess.run(
        ["curl", "-s", "https://api.backblazeb2.com/b2api/v3/b2_authorize_account",
         "-H", f"Authorization: Basic {auth}"],
        capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    _b2_auth_cache = {
        "api_url": data["apiInfo"]["storageApi"]["apiUrl"],
        "auth_token": data["authorizationToken"],
        "ts": time.time()
    }
    return _b2_auth_cache

def b2_upload(local_path, b2_path):
    auth = b2_authorize()
    # Get upload URL
    result = subprocess.run(
        ["curl", "-s", "-X", "POST",
         f"{auth['api_url']}/b2api/v3/b2_get_upload_url",
         "-H", f"Authorization: {auth['auth_token']}",
         "-d", json.dumps({"bucketId": B2_BUCKET_ID})],
        capture_output=True, text=True
    )
    upload_data = json.loads(result.stdout)
    
    sha1 = hashlib.sha1(open(local_path, "rb").read()).hexdigest()
    file_size = os.path.getsize(local_path)
    content_type = "video/mp4" if local_path.endswith(".mp4") else "image/jpeg"
    
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", upload_data["uploadUrl"],
         "-H", f"Authorization: {upload_data['authorizationToken']}",
         "-H", f"X-Bz-File-Name: {b2_path}",
         "-H", f"Content-Type: {content_type}",
         "-H", f"Content-Length: {file_size}",
         "-H", f"X-Bz-Content-Sha1: {sha1}",
         "--data-binary", f"@{local_path}"],
        capture_output=True, text=True, timeout=300
    )
    return json.loads(result.stdout)

def get_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip()) if result.stdout.strip() else 0

def extract_last_frame(video_path, output_path):
    subprocess.run(
        ["ffmpeg", "-y", "-sseof", "-0.1", "-i", video_path,
         "-frames:v", "1", "-q:v", "2", output_path],
        capture_output=True, text=True
    )
    return os.path.exists(output_path)


# ---- Pipeline Loading ----
def load_pipeline():
    print("Loading LTX-2.3 DistilledPipeline with FP8...", flush=True)
    t0 = time.time()
    
    # ltx-core and ltx-pipelines are installed via pip -e, no sys.path needed
    from ltx_core.quantization.policy import QuantizationPolicy
    from ltx_pipelines.distilled import DistilledPipeline
    
    pipeline = DistilledPipeline(
        distilled_checkpoint_path=f"{MODELS_DIR}/ltx-2.3-22b-distilled.safetensors",
        spatial_upsampler_path=f"{MODELS_DIR}/ltx-2.3-spatial-upscaler-x2-1.0.safetensors",
        gemma_root=f"{MODELS_DIR}/gemma",
        loras=[],
        quantization=QuantizationPolicy.fp8_cast(),
    )
    
    print(f"Pipeline loaded in {time.time()-t0:.0f}s", flush=True)
    return pipeline


def generate_one_subclip(pipeline, prompt, num_frames, seed, input_image=None):
    """Generate a single sub-clip."""
    from ltx_core.model.video_vae import TilingConfig, get_video_chunks_number
    from ltx_pipelines.utils.media_io import encode_video
    from ltx_pipelines.utils.args import ImageConditioningInput
    
    tiling_config = TilingConfig.default()
    
    images = []
    if input_image is not None:
        # ImageConditioningInput(path, frame_idx, strength, crf)
        # frame_idx=0 = condition on first frame (continue from this image)
        # strength=1.0 = full conditioning
        images = [ImageConditioningInput(path=input_image, frame_idx=0, strength=1.0)]
    
    # Returns (decoded_video: Iterator[Tensor], decoded_audio: Audio)
    decoded_video, decoded_audio = pipeline(
        prompt=prompt,
        seed=seed,
        height=HEIGHT,
        width=WIDTH,
        num_frames=num_frames,
        frame_rate=FPS,
        images=images,
        tiling_config=tiling_config,
        enhance_prompt=False,
    )
    
    # Save to temp file
    temp_path = f"/tmp/gen_{seed}.mp4"
    video_chunks_number = get_video_chunks_number(num_frames, tiling_config)
    encode_video(
        video=decoded_video,
        fps=FPS,
        audio=decoded_audio,
        output_path=temp_path,
        video_chunks_number=video_chunks_number,
    )
    
    return temp_path


def process_clip(pipeline, clip_data, clip_index):
    """Process a single clip, potentially with frame chaining."""
    clip_id = clip_data["id"]
    prompt = clip_data["prompt"]
    sub_clips = clip_data["sub_clips"]
    narr_duration = clip_data["narr_duration"]
    
    print(f"\n{'='*60}")
    print(f"[{clip_index}] {clip_id}: {len(sub_clips)} sub-clips, {narr_duration:.1f}s narration")
    print(f"Prompt: {prompt[:100]}...")
    sys.stdout.flush()
    
    t0 = time.time()
    sub_clip_paths = []
    
    for si, sub in enumerate(sub_clips):
        num_frames = sub["frames"]
        seed = 42 + clip_index * 10 + si
        sub_type = sub["type"]
        
        print(f"  Sub-clip {si}: {num_frames}f ({num_frames/FPS:.1f}s), type={sub_type}", flush=True)
        
        input_image = None
        if sub_type in ("middle", "last") and sub_clip_paths:
            # Frame chain: use last frame of previous sub-clip
            prev_path = sub_clip_paths[-1]
            frame_path = f"{FRAMES_DIR}/{clip_id}_sub{si-1}_last.jpg"
            if extract_last_frame(prev_path, frame_path):
                input_image = frame_path
                print(f"    Frame chaining from {frame_path}", flush=True)
        
        sub_t0 = time.time()
        sub_path = generate_one_subclip(pipeline, prompt, num_frames, seed, input_image)
        sub_elapsed = time.time() - sub_t0
        
        actual_dur = get_duration(sub_path)
        print(f"    Generated: {actual_dur:.2f}s in {sub_elapsed:.0f}s", flush=True)
        
        # Move to proper location
        final_sub_path = f"{OUTPUT_DIR}/{clip_id}_sub{si}.mp4"
        os.rename(sub_path, final_sub_path)
        sub_clip_paths.append(final_sub_path)
        
        # Upload sub-clip to B2
        b2_path = f"v5_clips_v2/{clip_id}_sub{si}.mp4"
        try:
            b2_upload(final_sub_path, b2_path)
            print(f"    Uploaded to B2: {b2_path}", flush=True)
        except Exception as e:
            print(f"    B2 upload failed: {e}", flush=True)
        
        # Clean GPU memory
        gc.collect()
        torch.cuda.empty_cache()
    
    # Concatenate sub-clips if multiple
    if len(sub_clip_paths) == 1:
        final_path = sub_clip_paths[0]
    else:
        # Use TS intermediate concat
        concat_list = f"/tmp/{clip_id}_concat.txt"
        ts_paths = []
        for sp in sub_clip_paths:
            ts_path = sp.replace(".mp4", ".ts")
            subprocess.run(
                ["ffmpeg", "-y", "-i", sp, "-c", "copy", "-bsf:v", "h264_mp4toannexb",
                 "-f", "mpegts", ts_path],
                capture_output=True, text=True
            )
            ts_paths.append(ts_path)
        
        concat_input = "concat:" + "|".join(ts_paths)
        final_path = f"{OUTPUT_DIR}/{clip_id}_full.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-i", concat_input, "-c", "copy",
             "-bsf:a", "aac_adtstoasc", final_path],
            capture_output=True, text=True
        )
    
    # Trim to narration duration
    full_duration = get_duration(final_path)
    trimmed_path = f"{OUTPUT_DIR}/{clip_id}.mp4"
    
    if full_duration >= narr_duration:
        subprocess.run(
            ["ffmpeg", "-y", "-i", final_path, "-t", str(narr_duration),
             "-c", "copy", trimmed_path],
            capture_output=True, text=True
        )
        trimmed_dur = get_duration(trimmed_path)
        print(f"  Trimmed: {full_duration:.2f}s -> {trimmed_dur:.2f}s (target: {narr_duration:.1f}s)", flush=True)
    else:
        # Video too short - regenerate the last sub-clip with a different seed
        print(f"  WARNING: Video too short! {full_duration:.2f}s < {narr_duration:.1f}s target", flush=True)
        print(f"  Attempting regen with different seed...", flush=True)
        deficit = narr_duration - full_duration
        extra_frames = max(129, min(257, int(deficit * FPS) + 48))  # At least 129 frames extra
        retry_seed = 9999 + clip_index * 10
        regen_input = None
        if sub_clip_paths:
            regen_frame = f"{FRAMES_DIR}/{clip_id}_regen_last.jpg"
            if extract_last_frame(sub_clip_paths[-1], regen_frame):
                regen_input = regen_frame
        try:
            extra_path = generate_one_subclip(pipeline, prompt, extra_frames, retry_seed, regen_input)
            extra_dur = get_duration(extra_path)
            print(f"  Extra clip: {extra_dur:.2f}s ({extra_frames} frames)", flush=True)
            # Re-concat with extra
            extra_final = f"{OUTPUT_DIR}/{clip_id}_extra.mp4"
            os.rename(extra_path, extra_final)
            # TS concat original + extra
            ts1 = final_path.replace('.mp4', '.ts')
            ts2 = extra_final.replace('.mp4', '.ts')
            for src, dst in [(final_path, ts1), (extra_final, ts2)]:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", src, "-c", "copy", "-bsf:v", "h264_mp4toannexb",
                     "-f", "mpegts", dst], capture_output=True, text=True
                )
            concat_in = f"concat:{ts1}|{ts2}"
            extended_path = f"{OUTPUT_DIR}/{clip_id}_extended.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-i", concat_in, "-c", "copy",
                 "-bsf:a", "aac_adtstoasc", extended_path], capture_output=True, text=True
            )
            new_dur = get_duration(extended_path)
            if new_dur >= narr_duration:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", extended_path, "-t", str(narr_duration),
                     "-c", "copy", trimmed_path], capture_output=True, text=True
                )
                print(f"  Fixed! Extended to {new_dur:.2f}s, trimmed to {narr_duration:.1f}s", flush=True)
            else:
                os.rename(extended_path, trimmed_path)
                print(f"  Still short after regen: {new_dur:.2f}s < {narr_duration:.1f}s (using as-is)", flush=True)
            gc.collect()
            torch.cuda.empty_cache()
        except Exception as e:
            print(f"  Regen failed: {e}, using short clip as-is", flush=True)
            if final_path != trimmed_path:
                if os.path.exists(final_path):
                    os.rename(final_path, trimmed_path)
    
    # Upload final trimmed clip to B2
    b2_path = f"v5_clips_v2/{clip_id}.mp4"
    try:
        b2_upload(trimmed_path, b2_path)
        print(f"  Final uploaded to B2: {b2_path}", flush=True)
    except Exception as e:
        print(f"  B2 upload failed: {e}", flush=True)
    
    elapsed = time.time() - t0
    print(f"  Total time: {elapsed:.0f}s", flush=True)
    
    return {
        "clip_id": clip_id,
        "status": "completed",
        "elapsed": elapsed,
        "full_duration": full_duration,
        "trimmed_duration": get_duration(trimmed_path),
        "target_duration": narr_duration,
        "b2_path": b2_path,
    }


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 generate_clips.py <assignments.json> [--start N] [--count M]")
        sys.exit(1)
    
    assignments_path = sys.argv[1]
    start = 0
    count = None
    
    for i, arg in enumerate(sys.argv):
        if arg == "--start" and i + 1 < len(sys.argv):
            start = int(sys.argv[i + 1])
        if arg == "--count" and i + 1 < len(sys.argv):
            count = int(sys.argv[i + 1])
    
    with open(assignments_path) as f:
        assignments = json.load(f)
    
    clips = assignments["clips"]
    if count:
        clips = clips[start:start + count]
    else:
        clips = clips[start:]
    
    print(f"Assigned clips: {len(clips)}")
    print(f"Starting from: {start}")
    
    # Check what's already done
    done = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            progress = json.load(f)
        for r in progress.get("results", []):
            if r.get("status") == "completed":
                done.add(r["clip_id"])
    else:
        progress = {"results": [], "started": time.time()}
    
    remaining = [c for c in clips if c["id"] not in done]
    print(f"Already done: {len(done)}")
    print(f"Remaining: {len(remaining)}")
    
    if not remaining:
        print("All clips already generated!")
        return
    
    # Load pipeline once
    pipeline = load_pipeline()
    
    for i, clip in enumerate(remaining):
        try:
            result = process_clip(pipeline, clip, i + 1)
            progress["results"].append(result)
            save_progress(progress)
        except Exception as e:
            print(f"ERROR processing {clip['id']}: {e}", flush=True)
            traceback.print_exc()
            progress["results"].append({
                "clip_id": clip["id"],
                "status": "failed",
                "error": str(e),
            })
            save_progress(progress)
            # Try to recover
            gc.collect()
            torch.cuda.empty_cache()
    
    progress["completed"] = time.time()
    progress["total_elapsed"] = time.time() - progress["started"]
    save_progress(progress)
    print(f"\nAll done! Total time: {progress['total_elapsed']:.0f}s")


if __name__ == "__main__":
    main()
