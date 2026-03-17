#!/usr/bin/env python3
"""
Stitch V3 documentary: extend clips to match narration timing,
concat all, mix TTS (vol 1.0) with LTX native audio (vol 0.15).
"""
import subprocess, json, os, sys

CLIPS_DIR = "/home/user/workspace/v3_clips"
SCRIPT_PATH = "/home/user/workspace/v3_script.json"
TTS_PATH = "/home/user/workspace/qwen_narration.wav"
EXTENDED_DIR = "/home/user/workspace/v3_extended"
OUTPUT_PATH = "/home/user/workspace/v3_final.mp4"

os.makedirs(EXTENDED_DIR, exist_ok=True)

# TTS segment durations (from generation log)
tts_durations = {
    'seg01': 26.6, 'seg02': 3.7, 'seg03': 44.4, 'seg04': 25.3,
    'seg05': 65.5, 'seg06': 23.4, 'seg07': 48.9, 'seg08': 43.0,
    'seg09': 51.9, 'seg10_transition': 20.0, 'seg11': 77.3,
    'seg12': 35.8, 'seg13': 57.5, 'seg14': 52.6, 'seg15': 38.9,
    'seg16': 34.2, 'seg17': 34.0, 'seg18': 28.2, 'seg19': 25.0,
    'seg20_transition': 9.5, 'seg21': 56.6, 'seg22': 52.4,
    'seg23': 20.8, 'seg24': 40.2, 'seg25': 14.7, 'seg26_closing': 42.2,
}

# Inter-segment pause duration in the TTS audio
INTER_PAUSE = 0.8

# Load script
with open(SCRIPT_PATH) as f:
    script = json.load(f)

def get_duration(path):
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration', '-of', 'json', path],
        capture_output=True, text=True
    )
    return float(json.loads(result.stdout)['format']['duration'])

def get_video_info(path):
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_entries', 'stream=width,height,r_frame_rate', '-select_streams', 'v:0', '-of', 'json', path],
        capture_output=True, text=True
    )
    info = json.loads(result.stdout)['streams'][0]
    fps_parts = info['r_frame_rate'].split('/')
    fps = float(fps_parts[0]) / float(fps_parts[1])
    return int(info['width']), int(info['height']), fps

# Get clip durations
clip_durations = {}
for fname in os.listdir(CLIPS_DIR):
    if fname.endswith('.mp4'):
        clip_id = fname.replace('.mp4', '')
        clip_durations[clip_id] = get_duration(os.path.join(CLIPS_DIR, fname))

# Process segments: for each narrated segment, figure out total video time needed
# and extend the last clip if video is too short
concat_entries = []
current_tts_offset = 0.0
narrated_seg_index = 0

for seg in script['segments']:
    seg_id = seg['id']
    clips = seg['clips']
    tts_dur = tts_durations.get(seg_id, 0)
    
    # Calculate total video duration for this segment
    seg_video_dur = sum(clip_durations.get(c['id'], 0) for c in clips)
    
    # For narrated segments, add inter-pause after (except the last one)
    has_narration = tts_dur > 0
    if has_narration:
        narrated_seg_index += 1
    
    # Target duration for this segment's video
    # For narrated segments: match TTS duration + half of inter-pause on each side
    # For breathing segments: use native clip duration (they're visual pauses)
    if has_narration:
        target_dur = tts_dur + INTER_PAUSE  # include the trailing pause
    else:
        # Breathing pauses / silent segments: use 1.5x native duration for visual breathing room
        target_dur = seg_video_dur
    
    gap = target_dur - seg_video_dur
    
    if gap > 1.0 and has_narration and len(clips) > 0:
        # Need to extend the last clip of this segment
        last_clip = clips[-1]
        last_clip_path = os.path.join(CLIPS_DIR, f"{last_clip['id']}.mp4")
        last_clip_dur = clip_durations[last_clip['id']]
        
        # Extended duration for this clip
        new_dur = last_clip_dur + gap
        extended_path = os.path.join(EXTENDED_DIR, f"{last_clip['id']}_ext.mp4")
        
        # Get video properties
        w, h, fps = get_video_info(last_clip_path)
        
        # Use tpad filter to freeze last frame
        # tpad=stop_mode=clone:stop_duration=GAP
        print(f"  Extending {last_clip['id']}: {last_clip_dur:.1f}s -> {new_dur:.1f}s (+{gap:.1f}s)")
        
        cmd = [
            'ffmpeg', '-y', '-i', last_clip_path,
            '-vf', f'tpad=stop_mode=clone:stop_duration={gap:.2f}',
            '-af', f'apad=pad_dur={gap:.2f}',
            '-preset', 'fast', '-crf', '23', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',
            extended_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"    WARN: ffmpeg failed for {last_clip['id']}: {result.stderr[-200:]}")
            # Fall back to original
            extended_path = None
        
        # Add all clips except the last one as-is, then the extended version
        for c in clips[:-1]:
            cpath = os.path.join(CLIPS_DIR, f"{c['id']}.mp4")
            concat_entries.append(cpath)
        
        if extended_path and os.path.exists(extended_path):
            concat_entries.append(extended_path)
        else:
            concat_entries.append(last_clip_path)
    else:
        # No extension needed, add clips as-is
        for c in clips:
            cpath = os.path.join(CLIPS_DIR, f"{c['id']}.mp4")
            concat_entries.append(cpath)
    
    print(f"{seg_id}: tts={tts_dur:.1f}s video={seg_video_dur:.1f}s target={target_dur:.1f}s gap={gap:.1f}s")

# Now we need to re-encode all clips to consistent format before concat
# First, check if clips have audio streams
print(f"\nTotal concat entries: {len(concat_entries)}")

# Check first clip for audio
sample_clip = concat_entries[0]
probe = subprocess.run(
    ['ffprobe', '-v', 'quiet', '-show_streams', '-of', 'json', sample_clip],
    capture_output=True, text=True
)
streams = json.loads(probe.stdout)['streams']
has_audio = any(s['codec_type'] == 'audio' for s in streams)
print(f"Sample clip has audio: {has_audio}")

# Step 1: Normalize all clips to same format (re-encode for safe concat)
NORMALIZED_DIR = "/home/user/workspace/v3_normalized"
os.makedirs(NORMALIZED_DIR, exist_ok=True)

print("\nNormalizing clips for concat...")
normalized_entries = []
for i, cpath in enumerate(concat_entries):
    basename = os.path.basename(cpath).replace('.mp4', '')
    norm_path = os.path.join(NORMALIZED_DIR, f"{i:03d}_{basename}.mp4")
    
    if os.path.exists(norm_path):
        normalized_entries.append(norm_path)
        continue
    
    # Re-encode to consistent format
    # If no audio in source, generate silent audio
    if has_audio:
        cmd = [
            'ffmpeg', '-y', '-i', cpath,
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23', '-pix_fmt', 'yuv420p',
            '-r', '24',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
            norm_path
        ]
    else:
        cmd = [
            'ffmpeg', '-y', '-i', cpath,
            '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23', '-pix_fmt', 'yuv420p',
            '-r', '24',
            '-c:a', 'aac', '-b:a', '128k', '-shortest',
            norm_path
        ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"  ERROR normalizing {cpath}: {result.stderr[-200:]}")
        sys.exit(1)
    
    normalized_entries.append(norm_path)
    if (i + 1) % 10 == 0:
        print(f"  Normalized {i+1}/{len(concat_entries)}")

print(f"  Normalized {len(normalized_entries)} clips")

# Step 2: Create concat file
concat_file = "/home/user/workspace/v3_concat_final.txt"
with open(concat_file, 'w') as f:
    for npath in normalized_entries:
        f.write(f"file '{npath}'\n")

# Step 3: Concat all clips
concat_output = "/home/user/workspace/v3_concat.mp4"
print("\nConcatenating all clips...")
cmd = [
    'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_file,
    '-c', 'copy', concat_output
]
result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
if result.returncode != 0:
    print(f"ERROR concat: {result.stderr[-500:]}")
    sys.exit(1)

concat_dur = get_duration(concat_output)
print(f"Concat video duration: {concat_dur:.1f}s ({concat_dur/60:.1f} min)")

# Step 4: Mix TTS narration with native LTX audio
tts_dur_actual = get_duration(TTS_PATH)
print(f"TTS narration duration: {tts_dur_actual:.1f}s ({tts_dur_actual/60:.1f} min)")

print("\nMixing TTS (vol 1.0) with native LTX audio (vol 0.15)...")
cmd = [
    'ffmpeg', '-y',
    '-i', concat_output,
    '-i', TTS_PATH,
    '-filter_complex', 
    '[0:a]volume=0.15[bg];[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=1.0[vo];[bg][vo]amix=inputs=2:duration=longest[aout]',
    '-map', '0:v', '-map', '[aout]',
    '-c:v', 'copy',
    '-c:a', 'aac', '-b:a', '192k',
    OUTPUT_PATH
]
result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
if result.returncode != 0:
    print(f"ERROR mix: {result.stderr[-500:]}")
    sys.exit(1)

final_dur = get_duration(OUTPUT_PATH)
final_size = os.path.getsize(OUTPUT_PATH) / (1024*1024)
print(f"\nFINAL VIDEO: {OUTPUT_PATH}")
print(f"Duration: {final_dur:.1f}s ({final_dur/60:.1f} min)")
print(f"Size: {final_size:.1f} MB")
print("Done!")
