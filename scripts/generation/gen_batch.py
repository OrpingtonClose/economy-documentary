#!/usr/bin/env python3
"""Batch generate all clips using CLI subprocess calls."""
import json
import os
import subprocess
import time
import sys

OUTPUT_DIR = "/root/video_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

with open("/root/v2_script.json") as f:
    script = json.load(f)
with open("/root/tts_output/durations.json") as f:
    tts_durations = json.load(f)

clips = []
for seg in script["segments"]:
    for clip in seg["clips"]:
        clip_id = clip["clip_id"]
        tts_dur = tts_durations.get(clip_id, clip["duration_seconds"])
        clips.append({
            "clip_id": clip_id,
            "prompt": clip["ltx_prompt"],
            "tts_duration": tts_dur,
        })

def duration_to_frames(target_dur):
    n = max(1, round((target_dur * 24 - 1) / 8))
    frames = 8 * n + 1
    frames = max(73, min(257, frames))
    return frames

total = len(clips)
done_count = 0
start_all = time.time()

for i, clip in enumerate(clips):
    cid = clip["clip_id"]
    outpath = os.path.join(OUTPUT_DIR, f"{cid}.mp4")
    
    if os.path.exists(outpath) and os.path.getsize(outpath) > 10000:
        print(f"[{i+1}/{total}] {cid}: SKIP (exists)")
        done_count += 1
        continue
    
    tts_dur = clip["tts_duration"]
    num_frames = duration_to_frames(tts_dur)
    seed = 42 + i
    
    print(f"\n[{i+1}/{total}] {cid}: {num_frames}f ({num_frames/24:.1f}s for {tts_dur:.1f}s TTS)")
    sys.stdout.flush()
    
    t0 = time.time()
    cmd = [
        "python3", "-m", "ltx_pipelines.distilled",
        "--distilled-checkpoint-path", "/root/models/ltx-2.3-22b-distilled.safetensors",
        "--spatial-upsampler-path", "/root/models/ltx-2.3-spatial-upscaler-x2-1.0.safetensors",
        "--gemma-root", "/root/models/gemma",
        "--prompt", clip["prompt"],
        "--output-path", outpath,
        "--height", "512", "--width", "768",
        "--num-frames", str(num_frames),
        "--seed", str(seed),
        "--quantization", "fp8-cast",
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    elapsed = time.time() - t0
    
    if result.returncode == 0 and os.path.exists(outpath):
        size_mb = os.path.getsize(outpath) / 1024**2
        print(f"  OK: {size_mb:.1f}MB, {elapsed:.0f}s")
        done_count += 1
    else:
        print(f"  FAILED (rc={result.returncode}, {elapsed:.0f}s)")
        if result.stderr:
            # Print last few lines of error
            err_lines = result.stderr.strip().split("\n")
            for line in err_lines[-5:]:
                print(f"    {line}")
    
    sys.stdout.flush()

total_time = time.time() - start_all
print(f"\n{'='*60}")
print(f"DONE: {done_count}/{total} clips in {total_time/60:.1f} min")
existing = [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".mp4")]
print(f"Files in output dir: {len(existing)}")
