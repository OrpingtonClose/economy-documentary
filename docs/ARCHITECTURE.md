# Architecture Deep Dive

This document explains every technical decision, model interaction, and infrastructure pattern used in the documentary pipeline. It is written for an AI agent that needs to reproduce or extend this work.

---

## 1. Model Stack

### LTX-2.3 22B-dev (Video Generation)

- **Repository**: https://github.com/Lightricks/LTX-2
- **Checkpoint**: `ltx-2.3-22b-dev.safetensors` (43GB, BF16)
- **HuggingFace**: `Lightricks/LTX-2.3`
- **Architecture**: Text-to-video diffusion transformer with joint audio-video generation
- **Pipeline class**: `TI2VidOneStagePipeline` (one-stage, not two-stage)

The LTX-2.3 repo is a monorepo with two internal packages:
- `packages/ltx-core/` — Core model components (transformer, VAE, schedulers, text encoders)
- `packages/ltx-pipelines/` — High-level pipeline utilities (`ModelLedger`, `denoise_audio_video`, etc.)

**Installation**:
```bash
git clone https://github.com/Lightricks/LTX-2.git
cd LTX-2
uv sync --frozen        # Uses the lockfile for exact reproducibility
source .venv/bin/activate
```

If `uv sync` doesn't work (e.g., on older systems), install the packages manually:
```bash
cd packages/ltx-core && pip install -e . --no-deps
cd ../ltx-pipelines && pip install -e . --no-deps
```

### Gemma-3-12B-IT (Text Encoder)

- **HuggingFace**: `google/gemma-3-12b-it`
- **Size**: ~23GB (5 safetensors shards)
- **Hidden size**: 3840, 48 layers
- **Role**: Encodes text prompts into embeddings consumed by the LTX-2.3 transformer
- **VRAM**: ~24GB when loaded

The text encoder is accessed through `ModelLedger`:
```python
ledger = ModelLedger(
    dtype=torch.bfloat16, device="cuda",
    checkpoint_path="/path/to/ltx-2.3-22b-dev.safetensors",
    gemma_root_path="/path/to/gemma3",
    loras=[], quantization=None,
)
text_encoder = ledger.text_encoder()
raw_output = text_encoder.encode(prompt)  # Returns (hidden_states, attention_mask)
```

Then processed through the embeddings processor:
```python
emb_proc = ledger.gemma_embeddings_processor()
context = emb_proc.process_hidden_states(*raw_output)
# context.video_encoding → used for video denoising guidance
# context.audio_encoding → used for audio denoising guidance
```

### Qwen3-TTS VoiceDesign (Narration)

- **Package**: `qwen-tts` (pip installable) or https://github.com/QwenLM/Qwen3-TTS
- **Model**: `Qwen3-TTS-12Hz-1.7B-VoiceDesign`
- **Tokenizer**: `Qwen3-TTS-Tokenizer-12Hz`
- **Size**: ~3.4GB
- **VRAM**: ~4GB in BF16

VoiceDesign allows creating voices from text descriptions without reference audio:
```python
from qwen_tts import Qwen3TTSModel
model = Qwen3TTSModel.from_pretrained(
    "/path/to/qwen-tts-voicedesign",
    device_map="cuda:0",
    torch_dtype=torch.bfloat16,
)
wavs, sr = model.generate_voice_design(
    text="Hello, this is a test.",
    instruct="A calm, authoritative male voice in his 50s...",
    language="English",
    non_streaming_mode=True,
    do_sample=True, top_k=50, top_p=0.9,
    temperature=0.7, repetition_penalty=1.1,
)
```

---

## 2. The Subprocess Isolation Pattern (CRITICAL)

This is the most important technical innovation in the pipeline.

### The Problem

LTX-2.3 video generation requires two massive model loads:
1. **Gemma-3-12B text encoder**: ~24GB VRAM
2. **22B transformer**: ~44GB VRAM

Total: ~68GB — doesn't fit on an 80GB A100 simultaneously.

PyTorch's `del` + `torch.cuda.empty_cache()` does NOT reliably free all VRAM. Memory fragmentation, CUDA context overhead, and PyTorch's memory allocator leave ~5-15GB residual after "freeing" the text encoder. This causes OOM when loading the transformer.

### The Solution

Run the text encoder in a **separate Python subprocess**. When the subprocess exits, the operating system reclaims ALL GPU memory — no fragmentation, no residual allocations.

**File: `encode_text.py`** (subprocess)
```python
# Loads Gemma-3-12B, encodes prompt, saves to .pt file, prints "ENCODED_OK", exits
# When this process exits, ALL GPU memory from text encoding is freed
```

**File: `generate_video_v3.py`** (main process)
```python
# For each clip:
#   1. Spawn subprocess: python encode_text.py → saves encoded.pt → exits
#   2. Main process: load encoded.pt, load transformer (44GB), denoise, decode
#   3. Free transformer, repeat
```

### VRAM Profile Per Clip

```
Time    VRAM    What's happening
────    ────    ────────────────
0s      0 GB    Subprocess starts
5s      24 GB   Gemma-3-12B loaded in subprocess
30s     24 GB   Text encoded, .pt saved
30s     0 GB    Subprocess exits, OS reclaims all VRAM
31s     0.1 GB  Main process loads encoded .pt (tiny tensors)
35s     44 GB   Main process loads transformer
180s    42.8 GB Peak during 30-step Euler denoising
185s    2 GB    Transformer freed, VAE decode
200s    0 GB    Clip saved, cleanup complete
```

Peak: 42.8 GB on A100 80GB — leaves comfortable headroom.

---

## 3. Video Generation Pipeline Detail

### Input: `scene_prompts.json`

Each entry:
```json
{
  "scene_num": 1,
  "title": "Cold Open: The Iran War Begins",
  "duration_sec": 60,
  "clips": [
    {
      "clip_idx": 1,
      "prompt": "A hand reaches for a coffee mug on a worn kitchen table..."
    },
    {
      "clip_idx": 2,
      "prompt": "Close-up of a newspaper headline about Iran..."
    }
  ]
}
```

### Prompt Conversion (`convert_prompts_v2.py`)

Converts the SCENARIO.MD visual descriptions into LTX-2.3 optimized prompts:

1. Parses "CAUSAL BEATS" tables from visual descriptions
2. Extracts narrative context paragraphs
3. For each beat, enriches with:
   - Environmental/atmospheric details (keyword-matched: kitchen → warm light, trading floor → blue monitors)
   - Camera movement (varies by clip index: establishing, push-in, tracking, dolly, pull-back)
   - Style string: "Photorealistic cinematic documentary footage shot on Arri Alexa Mini with Cooke anamorphic lenses..."
4. Targets 150-250 words per prompt

### Diffusion Parameters

```python
# Scheduler
scheduler = LTX2Scheduler()  # Custom Euler implementation
sigmas = scheduler.execute(steps=30)  # 30 denoising steps

# Guidance
video_guider = MultiModalGuiderParams(
    cfg_scale=3.0,      # Classifier-free guidance scale for video
    stg_scale=1.0,      # STG (Spatio-Temporal Guidance) scale
    rescale_scale=0.7,   # CFG rescaling to prevent oversaturation
    modality_scale=3.0,  # Cross-modal guidance
    skip_step=0,
    stg_blocks=[28],     # Which transformer block to apply STG
)
audio_guider = MultiModalGuiderParams(
    cfg_scale=7.0,      # Higher CFG for audio (more adherent to prompt)
    stg_scale=1.0,
    rescale_scale=0.7,
    modality_scale=3.0,
    skip_step=0,
    stg_blocks=[28],
)
```

### Output Format

Each clip: 121 frames at 24fps = 5.04 seconds, 768×512 resolution, H.264 + AAC.

---

## 4. TTS Pipeline Detail

### Voice Design

Three voices defined by text descriptions (no reference audio needed):

| Voice | Role | Description |
|-------|------|-------------|
| V1 | Curious Challenger | Young male, late 20s, conversational, fast-paced, American |
| V2 | Patient Explainer | 50s male, calm, authoritative, measured, British |
| V3 | Encouraging Guide | 40s female, warm, reassuring, medium pace, American |

### Processing Flow

1. Load `scenes_parsed.json` — each scene has `voice_blocks` with V1/V2/V3 text
2. For each voice block:
   - Split text at sentence boundaries into chunks ≤4000 chars
   - Generate each chunk via `model.generate_voice_design()`
   - Concatenate chunks with 0.3s silence between them
   - Save per-voice WAV files
3. Concatenate all voice WAVs for the scene with 1.0s silence between voices
4. Save scene narration WAV + metadata JSON

### Audio Format
- Sample rate: 24000 Hz
- Format: WAV, PCM float32, mono
- Typically 1-3 minutes per scene

---

## 5. Mixing and Stitching

### Per-Scene Mixing (`mix_and_stitch.py`)

For each scene:
1. Get video duration and narration duration
2. If video ≥ narration: trim video to narration length
3. If video < narration: **THIS IS THE LOOPING PROBLEM** — currently uses `stream_loop` (MUST BE FIXED)
4. Mix: `ffmpeg -i video -i narration -map 0:v -map 1:a -c:v libx264 -crf 18 -c:a aac`

### Final Stitching (MPEG-TS Method)

Direct concatenation of H.264 MP4s causes timestamp corruption. The fix:

```bash
# Step 1: Convert each scene to MPEG-TS
ffmpeg -i scene_01.mp4 -c:v libx264 -crf 18 -c:a aac -bsf:v h264_mp4toannexb -f mpegts scene_01.ts

# Step 2: Concatenate via concat protocol
ffmpeg -i "concat:scene_01.ts|scene_02.ts|...|scene_42.ts" -c:v libx264 -crf 18 -c:a aac final.mp4
```

This re-timestamps everything, eliminating non-monotonic DTS issues.

---

## 6. Infrastructure Patterns

### Vast.ai VM Lifecycle

1. **Search**: `curl https://console.vast.ai/api/v0/bundles?q=...` — filter by GPU type, VRAM, disk, bandwidth
2. **Rent**: `PUT /api/v0/asks/{offer_id}/` with image, disk, onstart script
3. **SSH**: Each VM gets a unique `ssh_host:port` combo (e.g., `ssh6.vast.ai:11952`)
4. **Destroy**: `DELETE /api/v0/instances/{instance_id}/` — STOP THE BILLING

### Parallel Generation Strategy

42 scenes distributed across N VMs (we used 6):
- VM1: Scenes 1-7
- VM2: Scenes 8-14
- VM3: Scenes 15-21
- VM4: Scenes 22-28
- VM5: Scenes 29-35
- VM6: Scenes 36-42

Each VM independently runs TTS → Video Gen → Mix for its scenes. Results collected via SCP.

### SSH Access Pattern

```bash
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o LogLevel=ERROR"

# Generate SSH key for Vast.ai
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""

# Copy to VM during bootstrap
scp $SSH_OPTS -P $PORT file root@$HOST:/workspace/

# Run command
ssh $SSH_OPTS -p $PORT root@$HOST 'command'
```

### Storage: Backblaze B2

```bash
# Authenticate
b2 account authorize $KEY_ID $APP_KEY

# Upload
b2 file upload economy-vid-assets path/to/file.mp4 remote/path/file.mp4

# Public URL pattern
https://f004.backblazeb2.com/file/economy-vid-assets/{path}
```

---

## 7. Model Download Paths

All models are downloaded from HuggingFace:

```bash
export HF_TOKEN="your_token"

# LTX-2.3 22B checkpoint (43GB)
huggingface-cli download Lightricks/LTX-2.3 ltx-2.3-22b-dev.safetensors \
  --local-dir /workspace/models/ltx23

# Gemma-3-12B text encoder (23GB, 5 shards)
huggingface-cli download google/gemma-3-12b-it \
  --local-dir /workspace/models/gemma3 \
  --ignore-patterns "*.gguf *.bin"

# Qwen3-TTS VoiceDesign (3.4GB)
huggingface-cli download Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign \
  --local-dir /workspace/models/qwen-tts-voicedesign

# Qwen3-TTS Tokenizer
huggingface-cli download Qwen/Qwen3-TTS-Tokenizer-12Hz \
  --local-dir /workspace/models/qwen-tts-tokenizer
```

---

## 8. The V6 Pipeline (Blackwell VMs)

A more advanced pipeline was developed for RTX PRO 6000 Blackwell GPUs (96GB VRAM):

### Key Differences from V3

1. **Pre-computed embeddings**: `v6_encode_prompts.py` encodes ALL prompts upfront and caches them to disk (`/root/embeddings_cache/{md5}_v.pt` and `_a.pt`). This avoids loading/unloading the text encoder per clip.

2. **Image conditioning**: Each clip uses the last frame of the previous clip as an image conditioning input for visual continuity. This creates smooth transitions between clips.

3. **Multi sub-clip generation**: Long clips are generated as multiple 257-frame sub-clips (each ~10.7s), then concatenated. The last frame of each sub-clip seeds the next.

4. **Integrated Frame.io upload**: Each clip is automatically uploaded to Frame.io with embedded metadata and a production comment.

5. **Deterministic seeds**: `seed = int(md5(clip_id)[:8], 16) % 2^31 + sub_index` for reproducibility.

---

## 9. Error Handling and Recovery

### Disk Space

Gemma-3-12B download (~23GB) can fail on VMs with <50GB free disk. Fix:
```bash
# Clear pip cache and old models
rm -rf /root/.cache/pip /root/.cache/huggingface/hub
rm -rf /workspace/venv  # If rebuilding
# Free ~35GB typically
```

### CUDA OOM During Denoising

If you get OOM during the 30-step denoising (after text encoder phase):
1. Check VRAM with `nvidia-smi` — should be <1GB before transformer load
2. If residual VRAM: the subprocess didn't exit cleanly. Check for zombie processes.
3. Reduce resolution: try 640×384 instead of 768×512
4. Reduce `num_frames`: try 97 (4s) instead of 121 (5s)

### Stale Progress Files

VMs track progress in `v6_progress.json`. If a VM crashes and restarts, entries in `failed` may be stale (clips that actually succeeded on a previous run). The `--resume` flag skips any clip with an output file on disk, regardless of the progress JSON.

### B2 Upload Failures

B2 uploads occasionally fail with timeout errors. The generation scripts continue even if upload fails — clips are saved locally. Use `b2 file upload` manually for any missed uploads.
