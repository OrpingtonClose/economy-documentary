#!/usr/bin/env python3
"""
V5 Video Stitching Script — runs on Vast.ai VM.
Downloads clips + narration from B2, stitches, uploads final to B2.
"""
import json
import os
import subprocess
import sys
import re
import time

# B2 credentials
B2_KEY_ID = "${B2_KEY_ID}"
B2_APP_KEY = "${B2_APP_KEY}"
BUCKET = "economy-vid-assets"

# Paths on VM
WORK_DIR = "/root/v5_stitch"
CLIPS_DIR = os.path.join(WORK_DIR, "clips")
SCRIPT_PATH = os.path.join(WORK_DIR, "v5_script.json")
NARRATION_PATH = os.path.join(WORK_DIR, "v5_narration.wav")
OUTPUT_DIR = os.path.join(WORK_DIR, "output")

TTS_VOLUME = 1.0
TARGET_LUFS = -16

def run(cmd, **kwargs):
    desc = ' '.join(str(c) for c in cmd[:6])
    print(f"  $ {desc}...", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.get('timeout', 3600))
    if r.returncode != 0:
        print(f"  ERROR (rc={r.returncode}): {r.stderr[:500]}", flush=True)
    return r

def sh(cmd, timeout=300):
    print(f"  $ {cmd[:80]}...", flush=True)
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        print(f"  ERROR: {r.stderr[:300]}", flush=True)
    return r

def main():
    t0 = time.time()
    os.makedirs(CLIPS_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Step 0: Install dependencies
    print("=== Step 0: Installing dependencies ===", flush=True)
    sh("apt-get update -qq && apt-get install -y -qq ffmpeg python3-pip > /dev/null 2>&1", timeout=120)
    sh("pip install b2 --quiet", timeout=120)

    # Step 1: Authorize B2
    print("\n=== Step 1: Authorizing B2 ===", flush=True)
    sh(f"b2 account authorize {B2_KEY_ID} {B2_APP_KEY}", timeout=30)

    # Step 2: Download script JSON
    print("\n=== Step 2: Downloading script JSON ===", flush=True)
    sh(f"b2 file download b2://{BUCKET}/v5/v5_script.json {SCRIPT_PATH}", timeout=30)

    # Step 3: Download narration WAV (655MB)
    print("\n=== Step 3: Downloading narration WAV ===", flush=True)
    sh(f"b2 file download b2://{BUCKET}/v5/v5_narration.wav {NARRATION_PATH}", timeout=300)
    
    narr_size = os.path.getsize(NARRATION_PATH) / 1024 / 1024
    print(f"  Narration: {narr_size:.1f}MB", flush=True)

    # Step 4: Download all 334 clips from B2
    print("\n=== Step 4: Downloading 334 clips from B2 ===", flush=True)
    t_dl = time.time()
    
    # Use b2 sync for efficiency
    sh(f"b2 sync b2://{BUCKET}/v5_clips/ {CLIPS_DIR}/", timeout=600)
    
    clip_count = len([f for f in os.listdir(CLIPS_DIR) if f.endswith('.mp4')])
    print(f"  Downloaded {clip_count} clips in {time.time()-t_dl:.0f}s", flush=True)

    # Load script
    with open(SCRIPT_PATH) as f:
        script = json.load(f)

    all_clips = []
    for seg in script["segments"]:
        for clip in seg["clips"]:
            all_clips.append(clip)
    print(f"  Total clips in script: {len(all_clips)}", flush=True)

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

    print(f"  Available: {len(clip_files)}, Missing: {len(missing)}", flush=True)
    if missing:
        print(f"  MISSING: {missing}", flush=True)
        print("  Cannot proceed with missing clips. Exiting.", flush=True)
        sys.exit(1)

    # Step 5: Concatenate video clips
    print(f"\n=== Step 5: Concatenating {len(clip_files)} clips ===", flush=True)
    t_cat = time.time()
    concat_list = os.path.join(OUTPUT_DIR, "concat.txt")
    with open(concat_list, 'w') as f:
        for path in clip_files:
            f.write(f"file '{path}'\n")

    raw_video = os.path.join(OUTPUT_DIR, "v5_raw.mp4")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
         "-c", "copy", raw_video], timeout=600)
    print(f"  Concat done in {time.time()-t_cat:.0f}s", flush=True)

    # Get video duration
    dur_result = run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                      "-of", "csv=p=0", raw_video])
    video_dur = float(dur_result.stdout.strip())
    print(f"  Raw video: {video_dur:.1f}s ({video_dur/60:.1f}min)", flush=True)

    # Get narration duration
    dur_result = run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                      "-of", "csv=p=0", NARRATION_PATH])
    narr_dur = float(dur_result.stdout.strip())
    print(f"  Narration: {narr_dur:.1f}s ({narr_dur/60:.1f}min)", flush=True)

    # Step 6: Add narration audio
    print(f"\n=== Step 6: Adding narration audio ===", flush=True)
    final_dur = min(video_dur, narr_dur)
    print(f"  Final duration: {final_dur:.1f}s ({final_dur/60:.1f}min)", flush=True)

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

    # Step 7: Normalize audio (loudnorm -16 LUFS)
    print(f"\n=== Step 7: Normalizing audio to {TARGET_LUFS} LUFS ===", flush=True)

    # First pass - measure
    measure_result = run(["ffmpeg", "-i", mixed_output, "-af",
                          f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:print_format=json",
                          "-f", "null", "-"], timeout=1800)

    stderr = measure_result.stderr
    json_match = re.search(r'\{[^}]*"input_i"[^}]*\}', stderr, re.DOTALL)

    final_output = os.path.join(OUTPUT_DIR, "v5_final.mp4")

    if json_match:
        stats = json.loads(json_match.group())
        print(f"  Measured I={stats.get('input_i','?')}, TP={stats.get('input_tp','?')}, LRA={stats.get('input_lra','?')}", flush=True)

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
        print("  Could not parse loudnorm stats, using single-pass", flush=True)
        run(["ffmpeg", "-y", "-i", mixed_output,
             "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             final_output], timeout=1800)

    # Clean up intermediate files
    for f in [raw_video, mixed_output, concat_list]:
        try:
            os.remove(f)
        except:
            pass

    # Step 8: Upload final video to B2
    print(f"\n=== Step 8: Uploading final video to B2 ===", flush=True)
    t_up = time.time()
    sh(f"b2 file upload {BUCKET} {final_output} v5/v5_final.mp4", timeout=600)
    print(f"  Upload done in {time.time()-t_up:.0f}s", flush=True)

    # Final stats
    dur_result = run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration,size",
                      "-of", "json", final_output])
    info = json.loads(dur_result.stdout)
    fd = float(info['format']['duration'])
    fs = int(info['format']['size']) / 1024 / 1024

    elapsed = time.time() - t0
    print(f"\n{'='*50}", flush=True)
    print(f"FINAL VIDEO COMPLETE", flush=True)
    print(f"Duration: {fd:.1f}s ({fd/60:.1f}min)", flush=True)
    print(f"Size: {fs:.1f}MB", flush=True)
    print(f"Location: B2 {BUCKET}/v5/v5_final.mp4", flush=True)
    print(f"Total time: {elapsed:.0f}s ({elapsed/60:.1f}min)", flush=True)
    print(f"{'='*50}", flush=True)

if __name__ == "__main__":
    main()
