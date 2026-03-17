#!/usr/bin/env python3
"""
Merge chunked transcripts, align to script clips, compute per-clip durations.
Output: v5_clip_plan.json with per-clip narration timing + required frame counts.
"""

import json
import os
import re
from difflib import SequenceMatcher

# --- 1. Merge chunked transcripts with offset-adjusted timestamps ---
CHUNK_DURATION = 600.0  # 10 minutes per chunk
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

print(f"Total words in transcript: {len(all_words)}")
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

print(f"Total clips in script: {len(clips)}")

# --- 3. Build full transcript text and word index ---
# Normalize text for matching
def normalize(text):
    """Normalize text for fuzzy matching"""
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# Build the full transcript as a single string of normalized words
transcript_words_norm = [normalize(w["text"]) for w in all_words]
transcript_text = " ".join(transcript_words_norm)

# --- 4. Align each clip's narration to the transcript ---
clip_plan = []
search_start_idx = 0  # Word index to start searching from (sequential)

for ci, clip in enumerate(clips):
    narr = clip["narration"]
    narr_norm = normalize(narr)
    narr_words = narr_norm.split()
    
    if not narr_words:
        # Empty narration — use 3 seconds default
        clip_plan.append({
            "id": clip["id"],
            "act": clip["act"],
            "narration": narr,
            "prompt": clip["prompt"],
            "narr_start": 0,
            "narr_end": 0,
            "narr_duration": 3.0,
            "required_frames_24fps": 97,  # ~4s at 24fps (3s + 1s margin)
            "required_duration": 4.0,
        })
        continue
    
    # Search for the first few words of the narration in the transcript
    # Use a sliding window starting from search_start_idx
    first_words = " ".join(narr_words[:min(5, len(narr_words))])
    last_words = " ".join(narr_words[-min(5, len(narr_words)):])
    
    best_start = None
    best_end = None
    best_score = 0
    
    # Search window: from search_start_idx to search_start_idx + reasonable range
    search_end = min(search_start_idx + 500, len(all_words))
    
    # Find start: match first few words
    for wi in range(search_start_idx, search_end):
        window = " ".join(transcript_words_norm[wi:wi+len(narr_words[:5])])
        score = SequenceMatcher(None, first_words, window).ratio()
        if score > best_score and score > 0.5:
            best_score = score
            best_start = wi
    
    if best_start is None:
        # Wider search if not found nearby
        for wi in range(max(0, search_start_idx - 200), min(len(all_words), search_start_idx + 1000)):
            window = " ".join(transcript_words_norm[wi:wi+len(narr_words[:5])])
            score = SequenceMatcher(None, first_words, window).ratio()
            if score > best_score and score > 0.5:
                best_score = score
                best_start = wi
    
    if best_start is not None:
        # Estimate end position
        best_end = min(best_start + len(narr_words) + 5, len(all_words) - 1)
        
        # Fine-tune end by matching last words
        end_best_score = 0
        end_search_start = best_start + max(1, len(narr_words) - 10)
        end_search_end = min(best_start + len(narr_words) + 20, len(all_words))
        
        for wi in range(end_search_start, end_search_end):
            window = " ".join(transcript_words_norm[max(0, wi-len(narr_words[-5:])+1):wi+1])
            score = SequenceMatcher(None, last_words, window).ratio()
            if score > end_best_score and score > 0.5:
                end_best_score = score
                best_end = wi
        
        narr_start_time = all_words[best_start]["start"]
        narr_end_time = all_words[best_end]["end"]
        narr_duration = narr_end_time - narr_start_time
        
        # Update search position for next clip
        search_start_idx = best_end + 1
        
        # Required duration: narration duration + 0.5s margin for trimming
        required_duration = narr_duration + 0.5
        required_frames = int(required_duration * 24) + 1
        
        # LTX-2.3 generates in specific frame counts. Round up to nearest valid frame count.
        # Valid counts: 33*n+1 where n>=1, so 34, 67, 100, 133, 166, 199, 232, 265, 298, 331, 364, 397, 430, ...
        # But also the model has limits. Let's calculate what we need and figure out
        # if we need multiple sub-clips.
        
        clip_plan.append({
            "id": clip["id"],
            "act": clip["act"],
            "narration": narr,
            "prompt": clip["prompt"],
            "narr_start": round(narr_start_time, 3),
            "narr_end": round(narr_end_time, 3),
            "narr_duration": round(narr_duration, 3),
            "required_duration": round(required_duration, 3),
            "required_frames_24fps": required_frames,
            "transcript_word_start": best_start,
            "transcript_word_end": best_end,
            "match_score": round(best_score, 3),
        })
    else:
        # Fallback: estimate based on word count and speaking rate
        # Average speaking rate from the overall narration
        avg_rate = 7785.6 / 18801  # seconds per word
        est_duration = len(narr_words) * avg_rate
        
        clip_plan.append({
            "id": clip["id"],
            "act": clip["act"],
            "narration": narr,
            "prompt": clip["prompt"],
            "narr_start": -1,
            "narr_end": -1,
            "narr_duration": round(est_duration, 3),
            "required_duration": round(est_duration + 0.5, 3),
            "required_frames_24fps": int((est_duration + 0.5) * 24) + 1,
            "match_score": 0,
            "fallback": True,
        })
        # Still advance search position
        search_start_idx += len(narr_words)

# --- 5. Statistics ---
durations = [c["narr_duration"] for c in clip_plan]
total_narr = sum(durations)
matched = sum(1 for c in clip_plan if c.get("match_score", 0) > 0.5)
fallback = sum(1 for c in clip_plan if c.get("fallback", False))
unmatched = len(clip_plan) - matched - fallback

print(f"\n--- Alignment Results ---")
print(f"Matched clips: {matched}/{len(clip_plan)}")
print(f"Fallback (estimated): {fallback}")
print(f"Total narration duration from alignment: {total_narr:.1f}s ({total_narr/60:.1f}min)")
print(f"Expected: ~7785.6s (129.8min)")
print(f"Min clip duration: {min(durations):.2f}s")
print(f"Max clip duration: {max(durations):.2f}s")
print(f"Mean clip duration: {sum(durations)/len(durations):.2f}s")

# Duration distribution
brackets = [(0,10), (10,20), (20,30), (30,45), (45,60), (60,120)]
for lo, hi in brackets:
    count = sum(1 for d in durations if lo <= d < hi)
    print(f"  {lo}-{hi}s: {count} clips")

# --- 6. Save ---
output = {
    "total_clips": len(clip_plan),
    "total_narration_seconds": round(total_narr, 2),
    "matched_clips": matched,
    "fallback_clips": fallback,
    "clips": clip_plan,
}

with open("/home/user/workspace/v5_clip_plan.json", "w") as f:
    json.dump(output, f, indent=2)

print(f"\nSaved to v5_clip_plan.json")

# Show first 5 clips
print("\n--- Sample clips ---")
for c in clip_plan[:5]:
    print(f"{c['id']}: narr={c['narr_duration']:.1f}s, frames_needed={c['required_frames_24fps']}, "
          f"match={c.get('match_score', 'N/A')}")
