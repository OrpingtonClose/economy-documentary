#!/usr/bin/env python3
"""
V3: Given LTX-2.3 max recommended frames = 257 (10.7s at 24fps),
plan how to cover each clip's narration with multiple sub-clips.
Strategy:
- Each narration segment gets split into sub-clips of max 257 frames (10.7s)
- Last sub-clip gets extra margin for trimming
- Frame chaining: extract last frame of each sub-clip as starting frame for next
"""

import json
import math

with open("/home/user/workspace/v5_clip_plan.json") as f:
    plan = json.load(f)

MAX_FRAMES = 257  # LTX-2.3 sweet spot
MAX_DURATION = MAX_FRAMES / 24.0  # 10.708s
FPS = 24

# Also support 193 frames (8.04s) for shorter clips
FRAME_OPTIONS = [257, 193, 129]  # 10.7s, 8.0s, 5.4s

total_subclips = 0
total_frames_to_generate = 0

for clip in plan["clips"]:
    narr_dur = clip["narr_duration"]
    
    if narr_dur <= 0:
        clip["sub_clips"] = [{
            "frames": 129,
            "duration": 129/24,
            "type": "standalone"
        }]
        total_subclips += 1
        total_frames_to_generate += 129
        continue
    
    # How many 257-frame clips do we need?
    # Each sub-clip provides ~10.5s usable (keeping 0.2s overlap for frame extraction)
    usable_per_subclip = MAX_DURATION - 0.2  # 10.5s usable
    
    n_subclips = math.ceil(narr_dur / usable_per_subclip)
    
    if n_subclips == 1:
        # Single clip - just make sure it's long enough
        if narr_dur <= 5.0:
            frames = 129  # 5.4s
        elif narr_dur <= 7.5:
            frames = 193  # 8.0s
        else:
            frames = 257  # 10.7s
        
        clip["sub_clips"] = [{
            "frames": frames,
            "duration": frames/24,
            "type": "standalone"
        }]
        total_subclips += 1
        total_frames_to_generate += frames
    else:
        # Multiple sub-clips needed
        subs = []
        for si in range(n_subclips):
            if si == 0:
                stype = "first"
            elif si == n_subclips - 1:
                stype = "last"
            else:
                stype = "middle"
            
            subs.append({
                "frames": 257,
                "duration": 257/24,
                "type": stype,
            })
        
        clip["sub_clips"] = subs
        total_subclips += n_subclips
        total_frames_to_generate += 257 * n_subclips

# Statistics
print(f"Total clips: {len(plan['clips'])}")
print(f"Total sub-clips to generate: {total_subclips}")
print(f"Total frames to generate: {total_frames_to_generate}")
print(f"Total generation video time: {total_frames_to_generate/24:.0f}s ({total_frames_to_generate/24/60:.1f}min)")

# Sub-clip count distribution
subclip_counts = {}
for clip in plan["clips"]:
    n = len(clip["sub_clips"])
    subclip_counts[n] = subclip_counts.get(n, 0) + 1
print("\nSub-clips per clip:")
for n in sorted(subclip_counts.keys()):
    print(f"  {n} sub-clips: {subclip_counts[n]} clips")

# Estimate generation time
# RTX PRO 6000 Blackwell: ~50s per 257-frame clip
est_time_per_clip = 50  # seconds
n_257_clips = sum(1 for c in plan["clips"] for s in c["sub_clips"] if s["frames"] == 257)
n_193_clips = sum(1 for c in plan["clips"] for s in c["sub_clips"] if s["frames"] == 193)
n_129_clips = sum(1 for c in plan["clips"] for s in c["sub_clips"] if s["frames"] == 129)

print(f"\n257-frame clips: {n_257_clips}")
print(f"193-frame clips: {n_193_clips}")
print(f"129-frame clips: {n_129_clips}")

est_total = n_257_clips * 50 + n_193_clips * 35 + n_129_clips * 25
print(f"\nEstimated generation time (1 GPU): {est_total}s ({est_total/60:.0f}min, {est_total/3600:.1f}hr)")
for n_gpus in [2, 3, 4, 5]:
    print(f"  With {n_gpus} GPUs: {est_total/n_gpus/60:.0f}min ({est_total/n_gpus/3600:.1f}hr)")

# But frame chaining means sub-clips within a clip must be sequential
# Only clips themselves can be parallelized
# So estimate based on parallel clips with sequential sub-clips
max_subclips_per_clip = max(len(c["sub_clips"]) for c in plan["clips"])
print(f"\nMax sub-clips in a single clip: {max_subclips_per_clip}")

# With 334 clips and N GPUs, each GPU gets ~334/N clips
# But each clip may take 1-3 sequential generations
# Actual time = (sum of max per-clip-time across all clips assigned to GPU)
clip_times = []
for clip in plan["clips"]:
    t = sum(50 if s["frames"] == 257 else 35 if s["frames"] == 193 else 25 for s in clip["sub_clips"])
    clip_times.append(t)

clip_times.sort(reverse=True)
for n_gpus in [2, 3, 4, 5]:
    # Greedy bin packing
    bins = [0] * n_gpus
    for ct in clip_times:
        min_bin = bins.index(min(bins))
        bins[min_bin] += ct
    wall_time = max(bins)
    print(f"  {n_gpus} GPUs wall time: {wall_time/60:.0f}min ({wall_time/3600:.1f}hr)")

# Save updated plan
with open("/home/user/workspace/v5_clip_plan.json", "w") as f:
    json.dump(plan, f, indent=2)

print(f"\nSaved updated v5_clip_plan.json with sub-clip plans")
