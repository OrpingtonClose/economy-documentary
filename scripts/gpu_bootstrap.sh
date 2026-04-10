#!/usr/bin/env bash
# GPU VM Bootstrap Script
# Run this on a freshly provisioned Vast.ai GPU VM to set up
# the documentary pipeline's video + TTS generation environment.
#
# Usage:
#   B2_KEY_ID=... B2_APPLICATION_KEY=... bash gpu_bootstrap.sh
#
# Models are pulled from Backblaze B2 (pre-cached) for speed.
# Falls back to HuggingFace if B2 credentials are not set.
#
# Disk budget (selective download — skips duplicate text_encoder format):
#   text_encoder (transformers fmt): ~46.6 GB
#   transformer  (diffusers fmt):    ~37.8 GB
#   vae + audio_vae + vocoder:       ~ 2.7 GB
#   connectors + latent_upsampler:   ~ 3.9 GB
#   Qwen3-TTS:                       ~ 4.3 GB
#   Total models:                    ~95.3 GB
#   OS + software + output:          ~30   GB
#   Minimum disk required:           ~125  GB  (recommend 200+)

set -euo pipefail

echo "=== GPU Bootstrap: Documentary Pipeline ==="
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
df -h /

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    git \
    python3-pip \
    wget \
    curl

# ---------------------------------------------------------------------------
# Working directories
# ---------------------------------------------------------------------------
mkdir -p /workspace/{models,output}
cd /workspace

# ---------------------------------------------------------------------------
# Python dependencies (install into system python — ephemeral VM)
# ---------------------------------------------------------------------------
pip install --no-cache-dir \
    'torch>=2.3.0' \
    'torchaudio>=2.3.0' \
    --index-url https://download.pytorch.org/whl/cu121

# diffusers >= 0.37.0 required for LTX2Pipeline
pip install --no-cache-dir \
    'diffusers>=0.37.0' \
    'transformers>=4.49.0' \
    'accelerate>=0.33.0' \
    'safetensors>=0.4.0' \
    'sentencepiece>=0.2.0' \
    'soundfile>=0.12.0' \
    'numpy>=1.26.0,<2.0.0' \
    'fastapi>=0.100.0' \
    'uvicorn>=0.20.0' \
    'pydantic>=2.0.0' \
    'b2>=4.0.0'

# ---------------------------------------------------------------------------
# B2 model download — selective (skip duplicate formats to save ~50 GB)
# ---------------------------------------------------------------------------
B2_BUCKET="ltx2-models-orpington"

if [ -n "${B2_KEY_ID:-}" ] && [ -n "${B2_APPLICATION_KEY:-}" ]; then
    echo "=== Downloading models from B2 (selective) ==="
    export B2_APPLICATION_KEY_ID="$B2_KEY_ID"
    b2 account authorize "$B2_KEY_ID" "$B2_APPLICATION_KEY"

    # --- Qwen3-TTS (~4.3 GB) ---
    if [ ! -f /workspace/models/qwen3-tts/model.safetensors ]; then
        echo "--- Qwen3-TTS (~4.3 GB) ---"
        mkdir -p /workspace/models/qwen3-tts
        b2 sync --threads 8 "b2://${B2_BUCKET}/qwen3-tts/" /workspace/models/qwen3-tts/
    else
        echo "Qwen3-TTS already present."
    fi

    # --- LTX-2.3 diffusers components ---
    LTX_DIR=/workspace/models/ltx2
    mkdir -p "$LTX_DIR"

    # Root config
    echo "--- LTX-2.3 config ---"
    b2 file download "b2://${B2_BUCKET}/ltx2/model_index.json" \
        "$LTX_DIR/model_index.json" 2>/dev/null || true

    # text_encoder: ONLY transformers format (model-*.safetensors)
    # The diffusion_pytorch_model-*.safetensors are a duplicate set (~50 GB) — skip them
    echo "--- text_encoder (~46.6 GB, transformers format only) ---"
    mkdir -p "$LTX_DIR/text_encoder"
    for f in config.json generation_config.json model.safetensors.index.json; do
        if [ ! -f "$LTX_DIR/text_encoder/$f" ]; then
            b2 file download "b2://${B2_BUCKET}/ltx2/text_encoder/$f" \
                "$LTX_DIR/text_encoder/$f" 2>/dev/null || true
        fi
    done
    # Download model shards selectively (transformers format only)
    # b2 ls returns full paths like "ltx2/text_encoder/model-00001-of-00011.safetensors"
    # so we extract just the basename for matching
    b2 ls "b2://${B2_BUCKET}/ltx2/text_encoder/" 2>/dev/null | while read -r fullpath; do
        filename=$(basename "$fullpath")
        case "$filename" in
            model-*.safetensors)
                if [ ! -f "$LTX_DIR/text_encoder/$filename" ]; then
                    echo "  downloading: $filename"
                    b2 file download "b2://${B2_BUCKET}/ltx2/text_encoder/$filename" \
                        "$LTX_DIR/text_encoder/$filename"
                else
                    echo "  already have: $filename"
                fi
                ;;
            diffusion_pytorch_model*)
                echo "  skipping duplicate format: $filename"
                ;;
        esac
    done

    # transformer: all files (diffusers format, ~37.8 GB)
    echo "--- transformer (~37.8 GB) ---"
    mkdir -p "$LTX_DIR/transformer"
    b2 sync --threads 8 "b2://${B2_BUCKET}/ltx2/transformer/" "$LTX_DIR/transformer/"

    # Small components (< 3 GB each)
    for subdir in scheduler tokenizer vae audio_vae vocoder connectors latent_upsampler; do
        if b2 ls "b2://${B2_BUCKET}/ltx2/${subdir}/" &>/dev/null; then
            echo "--- ${subdir} ---"
            mkdir -p "$LTX_DIR/${subdir}"
            b2 sync --threads 8 "b2://${B2_BUCKET}/ltx2/${subdir}/" "$LTX_DIR/${subdir}/"
        fi
    done

    echo ""
    echo "=== Download complete ==="
    du -sh /workspace/models/ltx2/ /workspace/models/qwen3-tts/ 2>/dev/null
    df -h /
else
    echo "WARNING: B2 credentials not set. Downloading from HuggingFace instead."
    pip install --no-cache-dir huggingface_hub

    python3 -c "
from huggingface_hub import snapshot_download
import os

if not os.path.exists('/workspace/models/qwen3-tts/model.safetensors'):
    print('Downloading Qwen3-TTS from HuggingFace...')
    snapshot_download('Qwen/Qwen3-TTS-12Hz-1.7B-Base',
                      local_dir='/workspace/models/qwen3-tts')

if not os.path.exists('/workspace/models/ltx2/model_index.json'):
    print('Downloading LTX-2.3 from HuggingFace...')
    snapshot_download('dg845/LTX-2.3-Diffusers',
                      local_dir='/workspace/models/ltx2')
"
fi

# ---------------------------------------------------------------------------
# Verify GPU
# ---------------------------------------------------------------------------
python3 -c "
import torch
print(f'CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)}')
    props = torch.cuda.get_device_properties(0)
    print(f'VRAM: {props.total_memory / 1e9:.1f} GB')
"

# ---------------------------------------------------------------------------
# Verify critical model files exist
# ---------------------------------------------------------------------------
echo ""
echo "=== Model verification ==="
OK=true
for f in \
    /workspace/models/qwen3-tts/model.safetensors \
    /workspace/models/ltx2/model_index.json \
    /workspace/models/ltx2/text_encoder/config.json \
    /workspace/models/ltx2/text_encoder/model.safetensors.index.json \
    /workspace/models/ltx2/transformer/config.json \
    /workspace/models/ltx2/vae/config.json; do
    if [ -f "$f" ]; then
        echo "  OK: $f"
    else
        echo "  MISSING: $f"
        OK=false
    fi
done

if [ "$OK" = false ]; then
    echo "ERROR: Some model files are missing!"
    exit 1
fi

echo ""
echo "=== Bootstrap complete ==="
echo "To start the GPU worker:"
echo "  python /workspace/gpu_worker.py --port 8880"
