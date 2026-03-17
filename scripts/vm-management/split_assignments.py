#!/usr/bin/env python3
"""Split clip plan into 3 VM assignments."""

import json

with open("/home/user/workspace/v5_clip_plan.json") as f:
    plan = json.load(f)

clips = plan["clips"]
n = len(clips)

# Split into 3 roughly equal groups
# Group by estimated generation time (sub-clip count × 50s)
clip_times = []
for c in clips:
    t = len(c["sub_clips"]) * 50  # ~50s per sub-clip
    clip_times.append((t, c))

# Sort by time descending for better load balancing
clip_times.sort(key=lambda x: -x[0])

# Greedy bin packing
bins = [[] for _ in range(3)]
bin_times = [0] * 3

for t, c in clip_times:
    min_bin = bin_times.index(min(bin_times))
    bins[min_bin].append(c)
    bin_times[min_bin] += t

for i in range(3):
    # Sort clips within each bin by clip ID for ordered processing
    bins[i].sort(key=lambda c: c["id"])
    
    assignment = {"vm_index": i, "clips": bins[i]}
    with open(f"/home/user/workspace/vm{i}_assignments.json", "w") as f:
        json.dump(assignment, f, indent=2)
    
    total_subclips = sum(len(c["sub_clips"]) for c in bins[i])
    total_narr = sum(c["narr_duration"] for c in bins[i])
    print(f"VM{i}: {len(bins[i])} clips, {total_subclips} sub-clips, "
          f"{total_narr:.0f}s narration, est_time={bin_times[i]}s ({bin_times[i]/60:.0f}min)")

print(f"\nTotal: {sum(len(b) for b in bins)} clips")
