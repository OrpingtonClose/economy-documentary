#!/usr/bin/env python3
"""
V2: Sequential alignment of script clips to transcript words.
Since clips are in order and narration is sequential, we just need to find
where each clip's narration starts and ends in the word stream.
"""

import json
import re
from difflib import SequenceMatcher

# --- 1. Merge chunked transcripts ---
CHUNK_DURATION = 600.0
NUM_CHUNKS = 13

all_words = []
for i in range(NUM_CHUNKS):
    path = f"/home/user/workspace/chunk_{i:03d}.json"
    with open(path) as f:
        chunk = json.load(f)
    offset = i * CHUNK_DURATION
    for w in chunk["words"]:
        all_words.append({
            "text": w["text"],
            "start": w["start"] + offset,
            "end": w["end"] + offset,
        })

print(f"Total transcript words: {len(all_words)}")
print(f"Time range: {all_words[0]['start']:.2f}s - {all_words[-1]['end']:.2f}s")

# --- 2. Load script clips ---
with open("/home/user/workspace/v5_script.json") as f:
    script = json.load(f)

clips = []
for seg in script["segments"]:
    for clip in seg["clips"]:
        clips.append({
            "id": clip["id"],
            "narration": clip["narration"],
            "prompt": clip["prompt"],
            "frames": clip["frames"],
            "act": seg["act"],
        })

print(f"Total script clips: {len(clips)}")

# --- 3. Normalize ---
def norm_word(w):
    return re.sub(r'[^\w]', '', w.lower())

def norm_words(text):
    return [norm_word(w) for w in text.split() if norm_word(w)]

# Build normalized transcript word list
t_words = [norm_word(w["text"]) for w in all_words]

# --- 4. Sequential alignment ---
# Strategy: For each clip, count how many words its narration has.
# Greedily consume that many words from the transcript.
# But we need to handle mismatches (ASR might split/merge words differently).
# 
# Better approach: concatenate all clip narrations to get full text,
# align full text to transcript, then split at clip boundaries.

# Concatenate all narrations with boundary markers
all_narr_words = []
clip_word_boundaries = []  # (start_word_idx, end_word_idx) in all_narr_words
for clip in clips:
    start_idx = len(all_narr_words)
    words = norm_words(clip["narration"])
    all_narr_words.extend(words)
    end_idx = len(all_narr_words) - 1
    clip_word_boundaries.append((start_idx, end_idx, len(words)))

print(f"Total narration words (from script): {len(all_narr_words)}")

# --- 5. Align narration words to transcript words ---
# Use a greedy sequential matcher since both are in the same order
# For each narration word, find the best matching transcript word 
# starting from the current position

def align_sequences(narr_words, trans_words, max_skip=5):
    """
    Align narration words to transcript words sequentially.
    Returns: list of transcript indices for each narration word.
    -1 means no match found.
    """
    alignments = []
    t_pos = 0
    
    for ni, nw in enumerate(narr_words):
        if ni % 2000 == 0:
            print(f"  Aligning word {ni}/{len(narr_words)} (t_pos={t_pos})")
        
        best_match = -1
        best_score = 0
        
        # Search forward from current position
        search_end = min(t_pos + max_skip + 3, len(trans_words))
        for ti in range(t_pos, search_end):
            tw = trans_words[ti]
            if nw == tw:
                best_match = ti
                best_score = 1.0
                break
            # Fuzzy match for ASR differences
            score = SequenceMatcher(None, nw, tw).ratio()
            if score > best_score and score > 0.6:
                best_score = score
                best_match = ti
        
        if best_match >= 0:
            alignments.append(best_match)
            t_pos = best_match + 1
        else:
            # Try wider search
            wider_end = min(t_pos + 20, len(trans_words))
            for ti in range(t_pos, wider_end):
                tw = trans_words[ti]
                if nw == tw:
                    best_match = ti
                    break
                score = SequenceMatcher(None, nw, tw).ratio()
                if score > best_score and score > 0.7:
                    best_score = score
                    best_match = ti
            
            if best_match >= 0:
                alignments.append(best_match)
                t_pos = best_match + 1
            else:
                alignments.append(-1)  # No match, will interpolate later
    
    return alignments

print("\nAligning narration to transcript...")
alignments = align_sequences(all_narr_words, t_words)

matched = sum(1 for a in alignments if a >= 0)
print(f"Matched: {matched}/{len(alignments)} ({100*matched/len(alignments):.1f}%)")

# --- 6. Interpolate missing alignments ---
# Fill -1s with linear interpolation between known points
def interpolate_alignments(aligns):
    result = list(aligns)
    # Forward fill
    last_known = 0
    for i in range(len(result)):
        if result[i] >= 0:
            last_known = result[i]
        else:
            result[i] = last_known
    return result

alignments_filled = interpolate_alignments(alignments)

# --- 7. Extract per-clip timing ---
clip_plan = []
for ci, clip in enumerate(clips):
    start_nw, end_nw, n_words = clip_word_boundaries[ci]
    
    if n_words == 0:
        clip_plan.append({
            "id": clip["id"],
            "act": clip["act"],
            "narration": clip["narration"],
            "prompt": clip["prompt"],
            "narr_start": 0,
            "narr_end": 0,
            "narr_duration": 3.0,
            "required_duration": 4.0,
            "required_frames_24fps": 97,
        })
        continue
    
    # Get transcript word indices for this clip's first and last narration words
    t_start_idx = alignments_filled[start_nw]
    t_end_idx = alignments_filled[end_nw]
    
    # Get timestamps from the transcript words
    narr_start_time = all_words[t_start_idx]["start"]
    narr_end_time = all_words[min(t_end_idx, len(all_words)-1)]["end"]
    narr_duration = narr_end_time - narr_start_time
    
    # If duration seems too short (< 2s for non-trivial narration), estimate
    if narr_duration < 2.0 and n_words > 5:
        avg_rate = 7785.6 / 18801
        narr_duration = n_words * avg_rate
        narr_start_time = -1
        narr_end_time = -1
    
    # Required: generate clip that's longer than narration, then trim
    # Add 1.5s margin minimum, or 15% of duration, whichever is larger
    margin = max(1.5, narr_duration * 0.15)
    required_duration = narr_duration + margin
    required_frames = int(required_duration * 24) + 1
    
    clip_plan.append({
        "id": clip["id"],
        "act": clip["act"],
        "narration": clip["narration"],
        "prompt": clip["prompt"],
        "narr_start": round(narr_start_time, 3),
        "narr_end": round(narr_end_time, 3),
        "narr_duration": round(narr_duration, 3),
        "required_duration": round(required_duration, 3),
        "required_frames_24fps": required_frames,
        "narration_word_count": n_words,
    })

# --- 8. Statistics ---
durations = [c["narr_duration"] for c in clip_plan]
total_narr = sum(durations)

print(f"\n--- Results ---")
print(f"Total narration duration: {total_narr:.1f}s ({total_narr/60:.1f}min)")
print(f"Expected: ~7785.6s (129.8min)")
print(f"Min clip: {min(durations):.2f}s")
print(f"Max clip: {max(durations):.2f}s")
print(f"Mean clip: {sum(durations)/len(durations):.2f}s")

# Duration brackets
brackets = [(0,10), (10,15), (15,20), (20,25), (25,30), (30,45), (45,60), (60,120)]
for lo, hi in brackets:
    count = sum(1 for d in durations if lo <= d < hi)
    print(f"  {lo}-{hi}s: {count} clips")

# Frame count analysis
req_frames = [c["required_frames_24fps"] for c in clip_plan]
print(f"\nRequired frames: min={min(req_frames)}, max={max(req_frames)}, mean={sum(req_frames)/len(req_frames):.0f}")
print(f"Total required frames: {sum(req_frames)}")
print(f"Total required video: {sum(req_frames)/24:.0f}s ({sum(req_frames)/24/60:.1f}min)")

# LTX-2.3 frame count constraints
# Valid: 33*n + 1 for n=1,2,3,... -> 34, 67, 100, 133, 166, 199, 232, 265, 298, 331, 364, 397, 430, 463, 496, 529, 562, 595, 628, 661, 694, 727, 760, 793
# Max tested: 257 frames (10.7s) and 193 frames (8.0s). Let's see what we need.
valid_frames = [33*n + 1 for n in range(1, 30)]
print(f"\nLTX valid frame counts: {valid_frames[:15]}...")

# For each clip, find the minimum valid frame count >= required
for cp in clip_plan:
    req = cp["required_frames_24fps"]
    # Find smallest valid frame count >= req
    chosen = None
    for vf in valid_frames:
        if vf >= req:
            chosen = vf
            break
    if chosen is None:
        # Need multiple sub-clips
        # Split into sub-clips of max valid frames
        max_vf = valid_frames[-1]  # 793
        n_subclips = (req + max_vf - 1) // max_vf
        cp["generation_strategy"] = "multi_clip"
        cp["sub_clips"] = n_subclips
        cp["frames_per_subclip"] = max_vf
    else:
        cp["generation_strategy"] = "single_clip"
        cp["ltx_frames"] = chosen
        cp["ltx_duration"] = round(chosen / 24, 2)

# Stats on generation strategy
single = sum(1 for c in clip_plan if c["generation_strategy"] == "single_clip")
multi = sum(1 for c in clip_plan if c["generation_strategy"] == "multi_clip")
print(f"\nSingle-clip generation: {single}")
print(f"Multi-clip generation: {multi}")

# Frame count distribution for single clips
if single > 0:
    frame_counts = {}
    for c in clip_plan:
        if c["generation_strategy"] == "single_clip":
            fc = c["ltx_frames"]
            frame_counts[fc] = frame_counts.get(fc, 0) + 1
    for fc in sorted(frame_counts.keys()):
        print(f"  {fc} frames ({fc/24:.1f}s): {frame_counts[fc]} clips")

# --- 9. Save ---
output = {
    "total_clips": len(clip_plan),
    "total_narration_seconds": round(total_narr, 2),
    "clips": clip_plan,
}

with open("/home/user/workspace/v5_clip_plan.json", "w") as f:
    json.dump(output, f, indent=2)

print(f"\nSaved v5_clip_plan.json")
