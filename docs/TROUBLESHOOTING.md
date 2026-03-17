# Troubleshooting Guide

Common problems encountered during production and their solutions.

---

## CUDA Out of Memory (OOM)

### Symptom
```
torch.cuda.OutOfMemoryError: CUDA out of memory.
Tried to allocate X GiB. GPU 0 has a total capacity of 80.00 GiB...
```

### Cause
Text encoder (Gemma-3-12B, ~24GB) and transformer (22B, ~44GB) can't coexist in 80GB VRAM. PyTorch's memory cleanup is unreliable.

### Solution
Use the subprocess isolation pattern in `generate_video_v3.py`:
- Text encoding runs in `encode_text.py` as a subprocess
- Subprocess exits → OS reclaims all GPU memory
- Main process loads transformer into clean VRAM

If OOM happens DURING denoising (after subprocess):
1. Check for zombie processes: `ps aux | grep python`
2. Check VRAM before transformer load: `nvidia-smi`
3. Reduce resolution from 768×512 to 640×384
4. Reduce frames from 121 to 97

---

## Non-Monotonic DTS / Broken Timestamps

### Symptom
- Final video reports 120 minutes but audio is only 60 minutes
- Second half of video is silent
- `ffmpeg` warns: "non monotonically increasing dts"

### Cause
Direct concatenation of H.264 MP4 files with `ffmpeg -f concat` or `-stream_loop` corrupts timestamps.

### Solution
Use MPEG-TS intermediate format:
```bash
# Convert each scene to TS
for i in $(seq -w 1 42); do
  ffmpeg -y -i scene_${i}_final.mp4 \
    -c:v libx264 -preset fast -crf 18 \
    -c:a aac -b:a 192k \
    -bsf:v h264_mp4toannexb -f mpegts \
    scene_${i}.ts
done

# Concatenate via concat protocol
TS_LIST=$(for i in $(seq -w 1 42); do echo -n "scene_${i}.ts|"; done | sed 's/|$//')
ffmpeg -y -i "concat:$TS_LIST" -c:v libx264 -crf 18 -c:a aac final.mp4
```

---

## Gemma-3-12B Download Fails

### Symptom
`huggingface-cli download` hangs or fails with disk space error.

### Cause
VM has insufficient disk space. Gemma-3-12B needs ~23GB, plus HuggingFace caches files during download.

### Solution
```bash
# Check disk space
df -h

# Free space by clearing caches
rm -rf /root/.cache/pip /root/.cache/huggingface/hub
rm -rf /tmp/*

# Download with --local-dir-use-symlinks False to avoid cache duplication
huggingface-cli download google/gemma-3-12b-it \
  --local-dir /root/models/text_encoder \
  --token $HF_TOKEN \
  --ignore-patterns "*.gguf *.bin" \
  --local-dir-use-symlinks False
```

---

## LTX-2 Package Import Errors

### Symptom
```
ModuleNotFoundError: No module named 'ltx_pipelines'
```
or
```
ModuleNotFoundError: No module named 'ltx_core'
```

### Cause
LTX-2 repo has two internal packages that need separate installation.

### Solution
```bash
cd /path/to/LTX-2

# Method 1: uv (preferred)
uv sync --frozen
source .venv/bin/activate

# Method 2: Manual pip install
pip install -e packages/ltx-core --no-deps
pip install -e packages/ltx-pipelines --no-deps
```

---

## Qwen3-TTS: No Audio Output

### Symptom
TTS generates silence or very short audio.

### Cause
- Text too long (>4000 chars) for a single generation call
- Missing VoiceDesign model (using Base instead)

### Solution
- Always split text at sentence boundaries into ≤4000 char chunks
- Use `Qwen3-TTS-12Hz-1.7B-VoiceDesign`, not `Base`
- Ensure `non_streaming_mode=True` for consistent output

---

## Vast.ai SSH Connection Refused

### Symptom
```
ssh: connect to host sshX.vast.ai port XXXXX: Connection refused
```

### Cause
- VM is still starting up (onstart script running)
- VM was destroyed or preempted
- Network issues

### Solution
```bash
# Check VM status via API
curl -s "https://console.vast.ai/api/v0/instances/?api_key=$VAST_API_KEY" | jq '.instances[] | {id, status: .actual_status, ssh_host, ssh_port}'

# Wait for "running" status, then retry SSH
# Typical startup time: 2-5 minutes
```

---

## Video Generation Produces Black/Corrupt Clips

### Symptom
Output MP4 is all black or has visual artifacts.

### Cause
- Prompt too long (>250 words) — truncated improperly
- Seed collision between clips
- VRAM fragmentation affecting denoising

### Solution
1. Ensure prompts are ≤250 words (the script truncates at 180 words as safety)
2. Use deterministic unique seeds: `seed_base + scene_num * 100 + clip_idx`
3. Call `gc.collect(); torch.cuda.empty_cache(); torch.cuda.synchronize()` between clips

---

## B2 Upload Timeout

### Symptom
```
b2 file upload: ConnectionError or timeout
```

### Solution
```bash
# Re-authenticate
b2 account authorize $KEY_ID $APP_KEY

# Upload with retry
for i in 1 2 3; do
  b2 file upload economy-vid-assets path/to/file.mp4 remote/path.mp4 && break
  echo "Retry $i..."
  sleep 10
done
```

---

## Frame.io OAuth Token Expired

### Symptom
```
401 Unauthorized: {"error": "invalid_token"}
```

### Cause
Frame.io V4 OAuth tokens expire. The `frameio_upload.py` script auto-refreshes, but the refresh token itself can expire after extended periods.

### Solution
Re-authorize via browser:
1. Navigate to Adobe IMS authorization URL with your client_id
2. Complete OAuth flow
3. Exchange authorization code for new tokens
4. Update `frameio_tokens.json` on all VMs

Note: Frame.io V4 uses Adobe IMS, not the old Frame.io auth. Client credentials grant is NOT supported — must use authorization code flow.

---

## YouTube Upload Stuck "Processing"

### Symptom
YouTube shows "Processing" for hours after upload.

### Cause
- Very long video (>60 minutes) takes YouTube longer to process
- Non-standard resolution (768×512 is not a standard YouTube format)

### Solution
- Wait up to 24 hours for processing to complete
- If still stuck after 24 hours, delete and re-upload
- Consider re-encoding to a standard resolution (1280×720 or 1920×1080) before upload, though the user has explicitly forbidden upscaling
