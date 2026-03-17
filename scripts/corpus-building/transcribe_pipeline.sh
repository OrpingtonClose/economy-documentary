#!/bin/bash
set -e

echo "=== Setting up transcription pipeline ==="

# Install dependencies
pip install -q yt-dlp whisperx 2>&1 | tail -3
apt-get update -qq && apt-get install -y -qq ffmpeg 2>&1 | tail -3

mkdir -p /workspace/audio /workspace/transcripts

echo "=== Downloading audio from YouTube videos ==="

# Read video IDs and download audio only
while IFS='|' read -r video_id channel title; do
    video_id=$(echo "$video_id" | xargs)
    channel=$(echo "$channel" | xargs)
    title=$(echo "$title" | xargs)
    
    if [ -f "/workspace/transcripts/${video_id}.txt" ]; then
        echo "SKIP (already done): $video_id"
        continue
    fi
    
    echo "Downloading: $video_id ($title)"
    yt-dlp -x --audio-format mp3 --audio-quality 9 \
        --postprocessor-args "-ac 1 -ar 16000" \
        -o "/workspace/audio/${video_id}.%(ext)s" \
        --no-playlist --quiet \
        "https://www.youtube.com/watch?v=${video_id}" 2>&1 || {
        echo "FAILED download: $video_id"
        echo "${video_id}|${channel}|${title}|DOWNLOAD_FAILED" >> /workspace/failed_downloads.txt
        continue
    }
    
    echo "Transcribing: $video_id"
    python3 -c "
import whisperx
import torch
import json

device = 'cuda' if torch.cuda.is_available() else 'cpu'
compute_type = 'float16' if device == 'cuda' else 'int8'

model = whisperx.load_model('large-v3', device, compute_type=compute_type)
audio = whisperx.load_audio('/workspace/audio/${video_id}.mp3')
result = model.transcribe(audio, batch_size=16)

text = ' '.join([seg['text'].strip() for seg in result['segments']])

with open('/workspace/transcripts/${video_id}.txt', 'w') as f:
    f.write('Channel: ${channel}\n')
    f.write('Title: ${title}\n\n')
    f.write(text)

print(f'OK: {len(text)} chars')
" 2>&1 || {
        echo "FAILED transcription: $video_id"
        echo "${video_id}|${channel}|${title}|TRANSCRIBE_FAILED" >> /workspace/failed_downloads.txt
    }
    
    # Clean up audio to save disk space
    rm -f "/workspace/audio/${video_id}.mp3"
    
done < /workspace/video_list.txt

echo "=== Transcription complete ==="
ls -la /workspace/transcripts/ | head -20
echo "Total transcripts: $(ls /workspace/transcripts/*.txt 2>/dev/null | wc -l)"
