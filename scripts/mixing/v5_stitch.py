#!/usr/bin/env python3
"""
V5 Video Stitching Script
Concatenates clips in order, mixes with TTS narration, normalizes audio.
"""
import json
import os
import subprocess
import sys

# Config
SCRIPT_PATH = "/root/v5_script.json"
CLIPS_DIR = "/root/v5_clips"
NARRATION_PATH = "/root/v5_narration.wav"
OUTPUT_DIR = "/root/v5_output"
B2_BUCKET = "economy-vid-assets"
B2_KEY_ID = "${B2_KEY_ID}"
B2_APP_KEY = "${B2_APP_KEY}"
TTS_VOLUME = 1.0
NATIVE_AUDIO_VOLUME = 0.15
TARGET_LUFS = -16

def run(cmd, **kwargs):
    print(f"  $ {' '.join(cmd[:5])}...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.get('timeout', 600))
    if r.returncode != 0:
        print(f"  ERROR: {r.stderr[:300]}")
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
    available = set(os.listdir(CLIPS_DIR)) if os.path.exists(CLIPS_DIR) else set()
    missing = []
    clip_files = []
    for clip in all_clips:
        fname = f"{clip['id']}.mp4"
        if fname in available:
            clip_files.append(os.path.join(CLIPS_DIR, fname))
        else:
            missing.append(clip['id'])
    
    print(f"Available clips: {len(clip_files)}, Missing: {len(missing)}")
    if missing:
        print(f"Missing clips: {missing[:20]}...")
    
    if not clip_files:
        print("ERROR: No clips available!")
        return
    
    # Download any missing clips from B2
    if missing:
        print(f"\nDownloading {len(missing)} missing clips from B2...")
        for clip_id in missing:
            remote = f"v5/clips/{clip_id}.mp4"
            local = os.path.join(CLIPS_DIR, f"{clip_id}.mp4")
            result = run(["b2", "file", "download", f"b2://economy-vid-assets/{remote}", local], timeout=60)
            if result.returncode == 0:
                clip_files.append(local)
                print(f"  Downloaded {clip_id}")
            else:
                print(f"  Failed to download {clip_id}")
    
    # Re-sort clip files in order
    clip_order = {clip['id']: i for i, clip in enumerate(all_clips)}
    clip_files_sorted = sorted(clip_files, key=lambda f: clip_order.get(os.path.basename(f).replace('.mp4',''), 999))
    
    # Step 1: Concatenate video clips
    print(f"\n=== Step 1: Concatenating {len(clip_files_sorted)} clips ===")
    concat_list = os.path.join(OUTPUT_DIR, "concat.txt")
    with open(concat_list, 'w') as f:
        for path in clip_files_sorted:
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
    
    # Step 2: Mix TTS with native audio, trim to shorter duration
    print(f"\n=== Step 2: Mixing audio ===")
    final_dur = min(video_dur, narr_dur)
    print(f"Final duration: {final_dur:.1f}s ({final_dur/60:.1f}min)")
    
    mixed_output = os.path.join(OUTPUT_DIR, "v5_mixed.mp4")
    run(["ffmpeg", "-y",
         "-i", raw_video,
         "-i", NARRATION_PATH,
         "-t", str(final_dur),
         "-filter_complex",
         f"[0:a]volume={NATIVE_AUDIO_VOLUME}[bg];"
         f"[1:a]volume={TTS_VOLUME}[tts];"
         f"[bg][tts]amix=inputs=2:duration=shortest[mixed]",
         "-map", "0:v",
         "-map", "[mixed]",
         "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k",
         mixed_output], timeout=1800)
    
    # Step 3: Normalize audio (loudnorm -16 LUFS)
    print(f"\n=== Step 3: Normalizing audio to {TARGET_LUFS} LUFS ===")
    
    # First pass - measure
    measure_result = run(["ffmpeg", "-i", mixed_output, "-af",
                          f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:print_format=json",
                          "-f", "null", "-"], timeout=1800)
    
    # Parse loudnorm stats from stderr
    import re
    stderr = measure_result.stderr
    # Find the JSON block
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
    
    # Upload to B2
    print(f"\n=== Uploading to B2 ===")
    run(["b2", "account", "authorize", B2_KEY_ID, B2_APP_KEY])
    run(["b2", "file", "upload", "--quiet", B2_BUCKET, final_output, "v5/v5_final.mp4"], timeout=600)
    run(["b2", "file", "upload", "--quiet", B2_BUCKET, SCRIPT_PATH, "v5/v5_script.json"], timeout=60)
    
    print("\n=== DONE ===")

if __name__ == "__main__":
    main()
