#!/usr/bin/env python3
"""
V5 Video Stitching Script - Local workspace version
Concatenates clips in order, overlays TTS narration, normalizes audio.
"""
import json
import os
import subprocess
import sys
import re

# Config - workspace paths
SCRIPT_PATH = "/home/user/workspace/v5_script.json"
CLIPS_DIR = "/home/user/workspace/v5_clips"
NARRATION_PATH = "/home/user/workspace/v5_narration.wav"
OUTPUT_DIR = "/home/user/workspace/v5_output"
TTS_VOLUME = 1.0
TARGET_LUFS = -16

def run(cmd, **kwargs):
    desc = ' '.join(str(c) for c in cmd[:6])
    print(f"  $ {desc}...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.get('timeout', 1800))
    if r.returncode != 0:
        print(f"  ERROR (rc={r.returncode}): {r.stderr[:500]}")
    return r

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Load script
    with open(SCRIPT_PATH) as f:
        script = json.load(f)
    
    # Collect clips in order
    all_clips = []
    for seg in script["segments"]:
        for clip in seg["clips"]:
            all_clips.append(clip)
    
    print(f"Total clips in script: {len(all_clips)}")
    
    # Check which clips exist
    available = set(os.listdir(CLIPS_DIR))
    clip_files = []
    missing = []
    for clip in all_clips:
        fname = f"{clip['id']}.mp4"
        if fname in available:
            clip_files.append(os.path.join(CLIPS_DIR, fname))
        else:
            missing.append(clip['id'])
    
    print(f"Available clips: {len(clip_files)}, Missing: {len(missing)}")
    if missing:
        print(f"Missing clips: {missing}")
        return
    
    # Step 1: Concatenate video clips
    print(f"\n=== Step 1: Concatenating {len(clip_files)} clips ===")
    concat_list = os.path.join(OUTPUT_DIR, "concat.txt")
    with open(concat_list, 'w') as f:
        for path in clip_files:
            f.write(f"file '{path}'\n")
    
    raw_video = os.path.join(OUTPUT_DIR, "v5_raw.mp4")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
         "-c", "copy", raw_video], timeout=600)
    
    # Get video duration
    dur_result = run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                      "-of", "csv=p=0", raw_video])
    video_dur = float(dur_result.stdout.strip())
    print(f"Raw video: {video_dur:.1f}s ({video_dur/60:.1f}min)")
    
    # Get narration duration
    dur_result = run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                      "-of", "csv=p=0", NARRATION_PATH])
    narr_dur = float(dur_result.stdout.strip())
    print(f"Narration: {narr_dur:.1f}s ({narr_dur/60:.1f}min)")
    
    # Step 2: Add narration audio to video
    # LTX-2.3 clips have no audio, so we just add the narration as the audio track
    print(f"\n=== Step 2: Adding narration audio ===")
    final_dur = min(video_dur, narr_dur)
    print(f"Final duration: {final_dur:.1f}s ({final_dur/60:.1f}min)")
    
    mixed_output = os.path.join(OUTPUT_DIR, "v5_mixed.mp4")
    run(["ffmpeg", "-y",
         "-i", raw_video,
         "-i", NARRATION_PATH,
         "-t", str(final_dur),
         "-map", "0:v",
         "-map", "1:a",
         "-af", f"volume={TTS_VOLUME}",
         "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k",
         mixed_output], timeout=1800)
    
    # Step 3: Normalize audio (loudnorm -16 LUFS)
    print(f"\n=== Step 3: Normalizing audio to {TARGET_LUFS} LUFS ===")
    
    # First pass - measure
    measure_result = run(["ffmpeg", "-i", mixed_output, "-af",
                          f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:print_format=json",
                          "-f", "null", "-"], timeout=1800)
    
    stderr = measure_result.stderr
    json_match = re.search(r'\{[^}]*"input_i"[^}]*\}', stderr, re.DOTALL)
    
    final_output = os.path.join(OUTPUT_DIR, "v5_final.mp4")
    
    if json_match:
        stats = json.loads(json_match.group())
        print(f"  Measured I={stats.get('input_i','?')}, TP={stats.get('input_tp','?')}, LRA={stats.get('input_lra','?')}")
        
        # Second pass - apply
        run(["ffmpeg", "-y", "-i", mixed_output,
             "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:"
                    f"measured_I={stats['input_i']}:"
                    f"measured_TP={stats['input_tp']}:"
                    f"measured_LRA={stats['input_lra']}:"
                    f"measured_thresh={stats['input_thresh']}:"
                    f"offset={stats['target_offset']}:"
                    f"linear=true:print_format=summary",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             final_output], timeout=1800)
    else:
        print("  Could not parse loudnorm stats, using single-pass")
        run(["ffmpeg", "-y", "-i", mixed_output,
             "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             final_output], timeout=1800)
    
    # Clean up intermediate files
    try:
        os.remove(raw_video)
        os.remove(mixed_output)
        os.remove(concat_list)
    except:
        pass
    
    # Final stats
    dur_result = run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration,size",
                      "-of", "json", final_output])
    info = json.loads(dur_result.stdout)
    final_dur = float(info['format']['duration'])
    final_size = int(info['format']['size']) / 1024 / 1024
    
    print(f"\n=== FINAL VIDEO ===")
    print(f"Duration: {final_dur:.1f}s ({final_dur/60:.1f}min)")
    print(f"Size: {final_size:.1f}MB")
    print(f"Path: {final_output}")

if __name__ == "__main__":
    main()
