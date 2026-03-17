#!/usr/bin/env python3
"""Download YouTube audio and transcribe with faster-whisper on GPU."""
import os
import json
import subprocess
import sys

AUDIO_DIR = "/workspace/audio"
TRANSCRIPT_DIR = "/workspace/transcripts"
VIDEO_LIST = "/workspace/video_list.json"
RESULTS_FILE = "/workspace/transcription_results.json"

os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(TRANSCRIPT_DIR, exist_ok=True)

with open(VIDEO_LIST) as f:
    videos = json.load(f)

print(f"Processing {len(videos)} videos...", flush=True)

from faster_whisper import WhisperModel
model = WhisperModel("large-v3", device="cuda", compute_type="float16")
print("Faster-Whisper model loaded!", flush=True)

results = []
failed = []

for i, v in enumerate(videos):
    vid_id = v["video_id"]
    channel = v["channel"]
    title = v["title"]
    
    transcript_path = os.path.join(TRANSCRIPT_DIR, f"{vid_id}.txt")
    audio_path = os.path.join(AUDIO_DIR, f"{vid_id}.mp3")
    
    if os.path.exists(transcript_path) and os.path.getsize(transcript_path) > 100:
        print(f"[{i+1}/{len(videos)}] SKIP (exists): {vid_id}", flush=True)
        with open(transcript_path) as f:
            content = f.read()
        results.append({"video_id": vid_id, "channel": channel, "title": title, "chars": len(content)})
        continue
    
    print(f"[{i+1}/{len(videos)}] DL: {vid_id} - {title[:60]}", flush=True)
    
    try:
        cmd = [
            "yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "9",
            "--postprocessor-args", "-ac 1 -ar 16000",
            "-o", audio_path,
            "--no-playlist", "--quiet",
            f"https://www.youtube.com/watch?v={vid_id}"
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    except Exception as e:
        err_msg = str(e)
        if hasattr(e, 'stderr') and e.stderr:
            err_msg = e.stderr.decode()[:300]
        print(f"  FAIL DL: {err_msg[:100]}", flush=True)
        failed.append({"video_id": vid_id, "channel": channel, "title": title, "error": f"download: {err_msg[:200]}"})
        continue
    
    try:
        segments, info = model.transcribe(audio_path, beam_size=5, language="en")
        text = " ".join([seg.text.strip() for seg in segments])
        
        with open(transcript_path, "w") as f:
            f.write(f"Channel: {channel}\nTitle: {title}\n\n{text}")
        
        results.append({"video_id": vid_id, "channel": channel, "title": title, "chars": len(text)})
        print(f"  OK: {len(text)} chars", flush=True)
    except Exception as e:
        print(f"  FAIL TR: {str(e)[:100]}", flush=True)
        failed.append({"video_id": vid_id, "channel": channel, "title": title, "error": f"transcribe: {str(e)[:200]}"})
    
    if os.path.exists(audio_path):
        os.remove(audio_path)

with open(RESULTS_FILE, "w") as f:
    json.dump({"completed": results, "failed": failed}, f, indent=2)

print(f"\n=== DONE === Completed: {len(results)} | Failed: {len(failed)}", flush=True)
for fl in failed:
    print(f"  FAILED: {fl['video_id']} - {fl['error'][:80]}", flush=True)
