# Economy Documentary — Full Production Environment Spec

## Project Overview

AI-generated economy documentary video. 334 clips across 7 acts, ~148 minutes total. Each clip is generated with LTX-2.3 22B-dev (full bf16, no distillation, no quantization, no upscaling) and synced to pre-existing narration audio.

**Hard constraints (user requirements):**
- Raw LTX-2.3 22B-dev only — no distillation, no fp8, no upscalers
- Never stretch video — generate longer and trim. If too short, regenerate
- Never loop/repeat clips
- All clips must reflect narration intimately and interestingly — "ADHD-person interesting"
- Scenes with human action, narrative sequences, visual storytelling — not static establishing shots
- No commercial APIs for media generation — only open models on vast.ai VMs
- No on-screen text/letters generation
- Audio (narration WAV) must not be touched

---

## Infrastructure

### Vast.ai VMs (active)

| VM | ID | GPU | VRAM | SSH | Clip Range | Step Speed |
|----|------|-----|------|-----|------------|------------|
| VM1 | 32900542 | RTX PRO 6000 S Blackwell | 96GB | `ssh -p 20542 root@ssh2.vast.ai -i ~/.ssh/vast_v3` | clips 1-168 | ~6.64s/step |
| VM2 | 32896208 | RTX PRO 6000 WS Blackwell | 96GB | `ssh -p 16208 root@ssh6.vast.ai -i ~/.ssh/vast_v3` | clips 169-334 | ~8.66s/step |

**Existing VM (DO NOT TOUCH):** ID 32876887, RTX 5070 Ti 16GB

**Vast.ai API key:** `${VAST_API_KEY}`
**Vast.ai credit remaining:** ~$70

### VM Environment

```
OS: Ubuntu 22.04 (overlay, 200GB disk)
Python: 3.10.12 (venv at /root/LTX-2/.venv/bin/python)
PyTorch: 2.9.1+cu128, CUDA 12.8
ffmpeg: 4.4.2
b2 CLI: 4.6.0
LTX-2 repo: /root/LTX-2/ (commit ae855f8)
```

### Model Files on Each VM

```
/root/models/ltx-2.3-22b-dev.safetensors    — 43GB (bf16, the main transformer checkpoint)
/root/models/text_encoder/                    — 23GB (Gemma-3-12B, hidden_size=3840, 48 layers, 5 shards)
```

HuggingFace token (for model downloads): `${HF_TOKEN}`

---

## File Layout on Each VM

```
/root/
├── v6_generate_v2.py          # Main generation script
├── v6_encode_prompts.py       # Subprocess prompt encoder (avoids VRAM leak)
├── frameio_upload.py          # Frame.io V4 upload + metadata embedding + comments
├── frameio_tokens.json        # Adobe/Frame.io OAuth tokens (auto-refresh)
├── v5_clip_plan.json          # Full clip plan (334 clips, 908 sub-clips)
├── v6_progress.json           # Progress tracker: {"completed": [...], "failed": [...]}
├── start_gen.sh               # Startup script (VM-specific --start/--end args)
├── models/
│   ├── ltx-2.3-22b-dev.safetensors
│   └── text_encoder/           # Gemma-3-12B shards
├── embeddings_cache/           # Pre-computed prompt embeddings (_v.pt, _a.pt per prompt hash)
├── clips_out/                  # Generated clip outputs
│   └── clip{NNN}/
│       ├── sub_00.mp4          # Individual sub-clips (deleted after concat)
│       ├── clip{NNN}_raw.mp4   # Concatenated raw
│       ├── clip{NNN}.mp4       # Trimmed final
│       ├── clip{NNN}_noaudio.mp4  # Audio-stripped (uploaded)
│       ├── clip{NNN}_meta.json # Production metadata
│       └── last_frame_final.jpg # Last frame for next clip's image conditioning
├── v6_vm1_gen.log              # VM1 generation log (or v6_vm2_gen.log)
└── LTX-2/                     # LTX-2 repo with .venv
```

---

## Generation Pipeline

### Phase 1: Prompt Encoding (subprocess)

Script: `v6_encode_prompts.py`

1. Loads all unique prompts from `v5_clip_plan.json` + the negative prompt
2. Loads Gemma-3-12B text encoder via `ModelLedger`
3. Encodes each prompt → `(hidden_states_tuple, attention_mask)` → moves to CPU
4. Frees text encoder (avoids 66GB VRAM leak)
5. Loads embeddings processor (`ledger.gemma_embeddings_processor()`)
6. Processes raw outputs → `(video_encoding, audio_encoding)` → saves to `/root/embeddings_cache/{md5_hash}_v.pt` and `_a.pt`

This runs once at startup. Cached embeddings are reused across runs.

### Phase 2: Clip Generation (main loop)

Script: `v6_generate_v2.py --start N --end M --resume`

For each clip in range:

1. **Skip if already in progress.completed** (resume support)
2. **Sub-clip generation** (each clip has 1-4 sub-clips of 257 frames each):
   - Load pre-computed embeddings from cache
   - Load video encoder → compute image conditionings (for continuity via last frame) → free
   - Load transformer (38GB VRAM) → denoise 30 steps via Euler scheduler → free
   - Decode video via VAE + decode audio via vocoder
   - Save sub-clip MP4
   - Extract last frame for next sub-clip's image conditioning
3. **Concatenate** sub-clips via ffmpeg
4. **Trim** to required duration (never stretch — if too short, it's flagged as warning)
5. **Strip audio** (we overlay our own narration in final assembly)
6. **Build metadata JSON** with all generation parameters
7. **Upload to B2** (video + metadata JSON)
8. **Upload to Frame.io** (video only, with embedded MP4 metadata tags + comment)
9. **Save last frame** for continuity into next clip

### Generation Parameters

```python
model           = "ltx-2.3-22b-dev.safetensors" (46GB, bf16)
resolution      = 768x512
fps             = 24
frames_per_sub  = 257 (= 8*32+1, ~10.7 seconds per sub-clip)
denoising_steps = 30
scheduler       = LTX2Scheduler (Euler)
cfg_scale_video = 3.0
cfg_scale_audio = 7.0
stg_scale       = 1.0
rescale         = 0.7
stg_blocks      = [28]
dtype           = bfloat16
quantization    = None
image_cond      = last frame of previous clip/sub-clip (strength=1.0, frame_idx=0)
seed            = deterministic from clip_id: int(md5(clip_id)[:8], 16) % 2^31 + sub_index
```

### Negative Prompt (shared across all clips)

```
blurry, out of focus, overexposed, underexposed, low contrast, washed out colors, excessive noise, grainy texture, poor lighting, flickering, motion blur, distorted proportions, unnatural skin tones, deformed facial features, asymmetrical face, missing facial features, extra limbs, disfigured hands, wrong hand count, artifacts around text, inconsistent perspective, camera shake, incorrect depth of field, background too sharp, background clutter, distracting reflections, harsh shadows, inconsistent lighting direction, color banding, cartoonish rendering, 3D CGI look, unrealistic materials, uncanny valley effect, incorrect ethnicity, wrong gender, exaggerated expressions, wrong gaze direction, mismatched lip sync, silent or muted audio, distorted voice, robotic voice, echo, background noise, off-sync audio, incorrect dialogue, added dialogue, repetitive speech, jittery movement, awkward pauses, incorrect timing, unnatural transitions, inconsistent framing, tilted camera, flat lighting, inconsistent tone, cinematic oversaturation, stylized filters, or AI artifacts.
```

### VRAM Profile (per sub-clip generation cycle)

```
After video_encoder:    ~0 GB (freed)
After transformer load: ~38 GB
During denoising:       ~38 GB (peak)
After transformer free: ~0 GB
During decode:          ~2-4 GB
```

Peak: ~38 GB. The model loads/unloads the transformer for every sub-clip to avoid accumulation.

---

## Clip Plan Structure

File: `v5_clip_plan.json`

```json
{
  "clips": [
    {
      "id": "clip001",
      "act": "Act I: Cold Open — The Iran War Begins",
      "narration": "The text narrated during this clip...",
      "prompt": "Cinematic scene description for LTX...",
      "narr_start": 0.0,
      "narr_end": 11.7,
      "narr_duration": 11.7,
      "required_duration": 13.455,
      "narration_word_count": 25,
      "generation_strategy": "single_clip",
      "sub_clips": [
        {"frames": 257, "type": "standalone"}
      ]
    },
    {
      "id": "clip002",
      "sub_clips": [
        {"frames": 257, "type": "first"},
        {"frames": 257, "type": "last"}
      ]
    }
  ]
}
```

**Sub-clip types:**
- `standalone` — single sub-clip, no image conditioning from previous clip
- `first` — first sub-clip in multi-sub clip; image conditioned from previous clip's last frame
- `middle` — middle sub-clip; image conditioned from previous sub-clip's last frame
- `last` — final sub-clip; image conditioned from previous sub-clip's last frame

**Stats:**
- 334 total clips, 908 total sub-clips
- Average 2.7 sub-clips per clip
- Total required duration: 8887s (148.1 min)
- 7 acts: 54, 51, 46, 40, 49, 40, 54 clips

---

## Narration Audio

```
File:        v5_narration.wav (687 MB)
Duration:    7785.6s (129.8 min)
Format:      PCM signed 16-bit little-endian, 44100 Hz, mono
Location:    /home/user/workspace/v5_narration.wav (sandbox)
             NOT on VMs — used only in final assembly
Status:      FROZEN — do not modify
```

---

## Storage: Backblaze B2

```
Bucket:    economy-vid-assets
Bucket ID: 8023e9c8f670ec4f9fc5051f
keyID:     ${B2_KEY_ID}
appKey:    ${B2_APP_KEY}
```

**Paths:**
- `v5_clips_v2/{clip_id}.mp4` — video clips (audio stripped)
- `v5_clips_v2/{clip_id}_meta.json` — production metadata

**Public URL pattern:** `https://f004.backblazeb2.com/file/economy-vid-assets/v5_clips_v2/{clip_id}.mp4`

B2 CLI is pre-installed and authenticated on both VMs.

---

## Storage: Frame.io V4

### OAuth (Adobe IMS)

```
Client ID:     ${FRAMEIO_CLIENT_ID}
Client Secret: ${FRAMEIO_CLIENT_SECRET}
Token URL:     https://ims-na1.adobelogin.com/ims/token/v3
Grant type:    refresh_token
```

Tokens stored in `/root/frameio_tokens.json` on each VM. Auto-refresh with 5-min buffer before expiry.

### API Structure

```
API base:    https://api.frame.io/v4
Account:     ${FRAMEIO_ACCOUNT_ID}
Workspace:   e83be634-70bc-41d2-a6f9-1de087db3fde
Project:     d08165c1-00d0-4446-9a72-3b4b408dfa59 ("AI Pipeline")
Root folder: 7d82be23-d88e-484a-8b14-329b2cae62a2
Clips folder:06216ba5-fce7-47b3-b976-844f94aaf242 ("V6 Clips")
Share link:  https://f.io/pEY11TE3
```

### Upload Flow

1. Embed metadata in MP4 via ffmpeg (`-metadata title=... -metadata comment=... -c copy`)
2. Create file asset: `POST /v4/accounts/{acct}/folders/{folder}/files` → returns upload_urls
3. PUT chunks to presigned S3 URLs (25MB chunks)
4. Post comment on asset: `POST /v4/accounts/{acct}/files/{asset_id}/comments`

**No JSON metadata files in Frame.io** — metadata goes into video container tags only + comment.

### Metadata embedded in MP4 (ffmpeg tags)

| Tag | Content |
|-----|---------|
| title | `{clip_id} - Economy Documentary v6` |
| artist | `LTX-2.3-22B-dev (bf16)` |
| description | Act, narration excerpt, timeline position |
| comment | JSON of all generation params |
| synopsis | The generation prompt |
| copyright | `Pipeline v6 | bf16 | 768x512` |
| genre | `AI Generated Documentary` |

### Frame.io API Notes

- V4 metadata endpoint is read-only (all built-in fields have `mutable=False`)
- Custom field editing via API is "still under development"
- PATCH on files only accepts `name` field
- Comments API works: `POST /v4/accounts/{acct}/files/{asset_id}/comments` with `{"data": {"text": "..."}}`
- `source_url` field in file creation works for ingesting from B2 public URLs (returns 202)

---

## Current Progress (as of 2026-03-15 ~16:50 CET)

| VM | Range | Completed | Failed* | Current Clip | Avg Time/Clip |
|----|-------|-----------|---------|--------------|---------------|
| VM1 | 1-168 | 28 | 12* | ~clip029 | ~428-488s |
| VM2 | 169-334 | 18 | 12* | ~clip186 | ~811s |

*Failed entries (clip001-012) are stale from earlier OOM issues during restart, not actual current failures. The clips were regenerated successfully.

**Estimated completion:**
- VM1: ~15-17 hours remaining → Monday ~9 AM CET
- VM2: ~30-34 hours remaining → Tuesday ~2 AM CET

---

## Scripts

### v6_generate_v2.py
Main generation loop. See full source in workspace or on VMs at `/root/v6_generate_v2.py`.

**Launch:**
```bash
# VM1
cd /root && nohup /root/LTX-2/.venv/bin/python v6_generate_v2.py --start 0 --end 168 --resume > v6_vm1_gen.log 2>&1 &

# VM2
cd /root && nohup /root/LTX-2/.venv/bin/python v6_generate_v2.py --start 167 --end 334 --resume > v6_vm2_gen.log 2>&1 &
```

### v6_encode_prompts.py
Subprocess prompt encoder. Runs automatically at the start of v6_generate_v2.py. Caches embeddings to `/root/embeddings_cache/`.

### frameio_upload.py
Frame.io V4 uploader. Imported by v6_generate_v2.py. Handles:
- Token refresh
- MP4 metadata embedding via ffmpeg
- Chunked upload to Frame.io
- Comment posting with production metadata

---

## Workspace Files (sandbox, not on VMs)

```
/home/user/workspace/
├── v5_script.json          # 238KB — full script with all 334 clips
├── v5_narration.wav        # 687MB — narration audio (44100Hz mono PCM, DO NOT TOUCH)
├── v5_clip_plan.json       # Per-clip generation plan (334 clips, 908 sub-clips)
├── v6_generate_v2.py       # Generation script (deployed to VMs)
├── v6_encode_prompts.py    # Prompt encoder (deployed to VMs)
├── frameio_upload.py       # Frame.io uploader (deployed to VMs)
├── frameio_tokens.json     # V4 OAuth tokens
├── v5_final.mp4            # 1.31GB — the FLAWED old version (keep for reference)
└── frameio_share.json      # Share link info
```

---

## Adding a New VM

To add a 3rd (or Nth) VM and parallelize generation:

1. **Provision on Vast.ai:** Need ≥48GB VRAM (96GB preferred). RTX PRO 6000 Blackwell or A100/H100.
2. **Install LTX-2:**
   ```bash
   git clone https://github.com/Lightricks/LTX-2.git
   cd LTX-2 && python -m venv .venv && source .venv/bin/activate
   pip install -e .
   pip install b2 requests
   ```
3. **Download models:**
   ```bash
   mkdir -p /root/models/text_encoder
   # 22B checkpoint (43GB)
   huggingface-cli download Lightricks/LTX-Video-2.3 ltx-2.3-22b-dev.safetensors --local-dir /root/models --token ${HF_TOKEN}
   # Gemma-3-12B text encoder (23GB)
   huggingface-cli download Lightricks/LTX-Video-2.3 text_encoder/* --local-dir /root/models --token ${HF_TOKEN}
   ```
4. **Copy scripts from workspace:**
   ```bash
   scp v6_generate_v2.py v6_encode_prompts.py frameio_upload.py v5_clip_plan.json frameio_tokens.json root@NEW_VM:/root/
   ```
5. **Configure B2:**
   ```bash
   b2 account authorize ${B2_KEY_ID} ${B2_APP_KEY}
   ```
6. **Rebalance clip ranges** across all VMs — update `--start` and `--end` in start scripts
7. **Launch:**
   ```bash
   nohup /root/LTX-2/.venv/bin/python v6_generate_v2.py --start X --end Y --resume > v6_vmN_gen.log 2>&1 &
   ```

---

## Final Assembly (not yet done)

Once all 334 clips are generated:

1. Download all clips from B2 (or use workspace)
2. Concatenate in order: `clip001.mp4` through `clip334.mp4`
3. Overlay `v5_narration.wav` synced by each clip's `narr_start` time
4. Produce final MP4

This happens in the sandbox workspace, not on VMs.
