#!/usr/bin/env python3
"""
Batch video generation for V3 Economy Documentary.
Generates all 96 clips using LTX-Video 13B distilled on A100.
Each clip runs as a separate inference call with CPU offloading.
"""

import json
import os
import subprocess
import time
import sys

SCRIPT_PATH = "/root/v3_script.json"
CONFIG_PATH = "/root/v3_13b_config.yaml"
OUTPUT_BASE = "/root/v3_clips"
LTX_DIR = "/root/LTX-Video"
NEGATIVE_PROMPT = "blurry, low quality, distorted, text, watermark, logo, subtitles, words, letters, numbers, UI overlay, cartoon, anime, illustration, painting, drawing"

# Create output directory
os.makedirs(OUTPUT_BASE, exist_ok=True)

# Load script
with open(SCRIPT_PATH, 'r') as f:
    script = json.load(f)

# Collect all clips in order
clips = []
for segment in script['segments']:
    for clip in segment['clips']:
        clips.append(clip)

print(f"Total clips to generate: {len(clips)}")

# Check which clips already exist (for resume capability)
completed = set()
for fname in os.listdir(OUTPUT_BASE):
    if fname.endswith('.mp4'):
        clip_id = fname.replace('.mp4', '')
        completed.add(clip_id)

remaining = [c for c in clips if c['id'] not in completed]
print(f"Already completed: {len(completed)}")
print(f"Remaining: {len(remaining)}")

if not remaining:
    print("All clips already generated!")
    sys.exit(0)

# Track timing
start_time = time.time()
errors = []

for i, clip in enumerate(remaining):
    clip_id = clip['id']
    frames = clip['frames']
    prompt = clip['prompt']
    
    # Output path for this clip
    clip_output_dir = os.path.join(OUTPUT_BASE, f"temp_{clip_id}")
    os.makedirs(clip_output_dir, exist_ok=True)
    
    # Seed based on clip number for reproducibility
    seed = int(clip_id.replace('clip', '')) * 42
    
    print(f"\n{'='*60}")
    print(f"[{i+1}/{len(remaining)}] Generating {clip_id} ({frames} frames)")
    print(f"Prompt: {prompt[:80]}...")
    print(f"{'='*60}")
    
    clip_start = time.time()
    
    cmd = [
        "python3", "inference.py",
        "--pipeline_config", CONFIG_PATH,
        "--offload_to_cpu", "True",
        "--height", "512",
        "--width", "768",
        "--num_frames", str(frames),
        "--seed", str(seed),
        "--prompt", prompt,
        "--negative_prompt", NEGATIVE_PROMPT,
        "--output_path", clip_output_dir
    ]
    
    try:
        result = subprocess.run(
            cmd,
            cwd=LTX_DIR,
            capture_output=True,
            text=True,
            timeout=600  # 10 min timeout per clip
        )
        
        clip_elapsed = time.time() - clip_start
        
        if result.returncode == 0:
            # Find the generated mp4 file in the output dir
            generated = None
            for f in os.listdir(clip_output_dir):
                if f.endswith('.mp4'):
                    generated = os.path.join(clip_output_dir, f)
                    break
            
            if generated:
                # Move to final location
                final_path = os.path.join(OUTPUT_BASE, f"{clip_id}.mp4")
                os.rename(generated, final_path)
                print(f"  OK - {clip_elapsed:.1f}s - saved to {final_path}")
                
                # Clean up temp dir
                try:
                    os.rmdir(clip_output_dir)
                except:
                    pass
            else:
                print(f"  WARNING - No mp4 found in output dir after {clip_elapsed:.1f}s")
                print(f"  Contents: {os.listdir(clip_output_dir)}")
                errors.append((clip_id, "no_mp4_output"))
        else:
            clip_elapsed = time.time() - clip_start
            print(f"  FAILED - {clip_elapsed:.1f}s")
            print(f"  STDERR (last 500 chars): {result.stderr[-500:]}")
            errors.append((clip_id, result.stderr[-200:]))
            
    except subprocess.TimeoutExpired:
        clip_elapsed = time.time() - clip_start
        print(f"  TIMEOUT after {clip_elapsed:.1f}s")
        errors.append((clip_id, "timeout"))
    except Exception as e:
        clip_elapsed = time.time() - clip_start
        print(f"  ERROR - {clip_elapsed:.1f}s - {str(e)}")
        errors.append((clip_id, str(e)))
    
    # Progress report
    total_elapsed = time.time() - start_time
    completed_count = len(completed) + i + 1
    avg_per_clip = total_elapsed / (i + 1)
    remaining_count = len(remaining) - (i + 1)
    eta = avg_per_clip * remaining_count
    print(f"  Progress: {completed_count}/{len(clips)} | Avg: {avg_per_clip:.1f}s/clip | ETA: {eta/60:.1f}min")

# Final report
total_time = time.time() - start_time
print(f"\n{'='*60}")
print(f"BATCH COMPLETE")
print(f"Total time: {total_time/60:.1f} minutes")
print(f"Clips generated: {len(remaining) - len(errors)}/{len(remaining)}")
if errors:
    print(f"Errors ({len(errors)}):")
    for clip_id, err in errors:
        print(f"  {clip_id}: {err[:100]}")
print(f"{'='*60}")

# Verify final count
final_clips = [f for f in os.listdir(OUTPUT_BASE) if f.endswith('.mp4')]
print(f"\nTotal clips in {OUTPUT_BASE}: {len(final_clips)}")
