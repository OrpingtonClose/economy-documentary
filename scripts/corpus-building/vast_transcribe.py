#!/usr/bin/env python3
"""Download YouTube audio and transcribe with WhisperX on GPU."""
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

# Load video list
with open(VIDEO_LIST) as f:
    videos = json.load(f)

print(f"Processing {len(videos)} videos...")

# Load WhisperX model once
import whisperx
import torch

device = "cuda" if torch.cuda.is_available() else "cpu"
compute_type = "float16" if device == "cuda" else "int8"
print(f"Using device: {device}, compute_type: {compute_type}")

model = whisperx.load_model("large-v3", device, compute_type=compute_type)
print("WhisperX model loaded!")

results = []
failed = []

for i, v in enumerate(videos):
    vid_id = v["video_id"]
    channel = v["channel"]
    title = v["title"]
    
    transcript_path = os.path.join(TRANSCRIPT_DIR, f"{vid_id}.txt")
    audio_path = os.path.join(AUDIO_DIR, f"{vid_id}.mp3")
    
    # Skip if already transcribed
    if os.path.exists(transcript_path) and os.path.getsize(transcript_path) > 100:
        print(f"[{i+1}/{len(videos)}] SKIP (exists): {vid_id} - {title[:50]}")
        with open(transcript_path) as f:
            content = f.read()
        results.append({"video_id": vid_id, "channel": channel, "title": title, "chars": len(content)})
        continue
    
    print(f"[{i+1}/{len(videos)}] Downloading: {vid_id} - {title[:60]}")
    
    # Download audio
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
        print(f"  FAILED download: {e}")
        failed.append({"video_id": vid_id, "channel": channel, "title": title, "error": f"download: {str(e)[:200]}"})
        continue
    
    # Transcribe
    try:
        audio = whisperx.load_audio(audio_path)
        result = model.transcribe(audio, batch_size=16)
        text = " ".join([seg["text"].strip() for seg in result["segments"]])
        
        with open(transcript_path, "w") as f:
            f.write(f"Channel: {channel}\nTitle: {title}\n\n{text}")
        
        results.append({"video_id": vid_id, "channel": channel, "title": title, "chars": len(text)})
        print(f"  OK: {len(text)} chars")
    except Exception as e:
        print(f"  FAILED transcribe: {e}")
        failed.append({"video_id": vid_id, "channel": channel, "title": title, "error": f"transcribe: {str(e)[:200]}"})
    
    # Cleanup audio to save disk
    if os.path.exists(audio_path):
        os.remove(audio_path)

# Save results
with open(RESULTS_FILE, "w") as f:
    json.dump({"completed": results, "failed": failed}, f, indent=2)

print(f"\n=== DONE ===")
print(f"Completed: {len(results)}")
print(f"Failed: {len(failed)}")
for fl in failed:
    print(f"  FAILED: {fl['video_id']} - {fl['error'][:80]}")
