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

set -euo pipefail

echo "=== GPU Bootstrap: Documentary Pipeline ==="
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
apt-get update && apt-get install -y \
    ffmpeg \
    git \
    python3-pip \
    python3-venv \
    wget \
    curl \
    rsync

# ---------------------------------------------------------------------------
# Working directories
# ---------------------------------------------------------------------------
mkdir -p /workspace/{models,output,pipeline}
cd /workspace

# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------
python3 -m venv /workspace/.venv
source /workspace/.venv/bin/activate

# Install PyTorch with CUDA
pip install --no-cache-dir \
    'torch>=2.3.0,<2.5.0' \
    'torchvision>=0.18.0,<0.20.0' \
    'torchaudio>=2.3.0,<2.5.0' \
    --index-url https://download.pytorch.org/whl/cu121

# Install dependencies for LTX-2.3 + Qwen3-TTS + worker
pip install --no-cache-dir \
    'diffusers>=0.31.0,<0.35.0' \
    'transformers>=4.44.0,<4.50.0' \
    'accelerate>=0.33.0,<0.40.0' \
    'safetensors>=0.4.0,<1.0.0' \
    'sentencepiece>=0.2.0,<1.0.0' \
    'soundfile>=0.12.0,<1.0.0' \
    'numpy>=1.26.0,<2.0.0' \
    'fastapi>=0.100.0' \
    'uvicorn>=0.20.0' \
    'pydantic>=2.0.0' \
    'b2sdk>=2.0.0' \
    'b2>=4.0.0'

# ---------------------------------------------------------------------------
# B2 model download (fast, from cache)
# ---------------------------------------------------------------------------
B2_BUCKET="ltx2-models-orpington"

if [ -n "${B2_KEY_ID:-}" ] && [ -n "${B2_APPLICATION_KEY:-}" ]; then
    echo "=== Downloading models from B2 ==="
    export B2_APPLICATION_KEY_ID="$B2_KEY_ID"

    # Authorize B2
    b2 account authorize "$B2_KEY_ID" "$B2_APPLICATION_KEY"

    # Download Qwen3-TTS (small, ~3.5GB)
    if [ ! -f /workspace/models/qwen3-tts/model.safetensors ]; then
        echo "Downloading Qwen3-TTS..."
        mkdir -p /workspace/models/qwen3-tts
        b2 sync --threads 8 "b2://${B2_BUCKET}/qwen3-tts/" /workspace/models/qwen3-tts/
        echo "Qwen3-TTS downloaded."
    else
        echo "Qwen3-TTS already present."
    fi

    # Download LTX-2.3 distilled checkpoint
    if [ ! -f /workspace/models/ltx2/ltx-2-19b-distilled.safetensors ] && \
       [ ! -f /workspace/models/ltx2/ltx-2-19b-distilled-fp8.safetensors ]; then
        echo "Downloading LTX-2.3 model files..."
        mkdir -p /workspace/models/ltx2

        # Download the distilled fp8 version (27GB, fits in 24GB VRAM)
        b2 file download "b2://${B2_BUCKET}/ltx2/ltx-2-19b-distilled-fp8.safetensors" \
            /workspace/models/ltx2/ltx-2-19b-distilled-fp8.safetensors

        # Download supporting files
        for f in model_index.json .gitattributes LICENSE README.md; do
            b2 file download "b2://${B2_BUCKET}/ltx2/$f" \
                "/workspace/models/ltx2/$f" 2>/dev/null || true
        done

        # Download subdirectories
        for subdir in scheduler text_encoder tokenizer transformer audio_vae connectors latent_upsampler; do
            if b2 ls "b2://${B2_BUCKET}/ltx2/${subdir}/" &>/dev/null; then
                echo "  Downloading ltx2/${subdir}/..."
                mkdir -p "/workspace/models/ltx2/${subdir}"
                b2 sync --threads 8 \
                    "b2://${B2_BUCKET}/ltx2/${subdir}/" \
                    "/workspace/models/ltx2/${subdir}/"
            fi
        done

        echo "LTX-2.3 downloaded."
    else
        echo "LTX-2.3 already present."
    fi
else
    echo "WARNING: B2 credentials not set. Downloading from HuggingFace instead."
    echo "Set B2_KEY_ID and B2_APPLICATION_KEY for faster downloads."

    pip install huggingface_hub

    python3 -c "
from huggingface_hub import snapshot_download
import os

if not os.path.exists('/workspace/models/qwen3-tts/model.safetensors'):
    print('Downloading Qwen3-TTS from HuggingFace...')
    snapshot_download('Qwen/Qwen3-TTS-12Hz-1.7B-Base',
                      local_dir='/workspace/models/qwen3-tts')

if not os.path.exists('/workspace/models/ltx2/model_index.json'):
    print('Downloading LTX-2.3 from HuggingFace...')
    snapshot_download('CalamitousFelicitousness/LTX-2.3-distilled-Diffusers',
                      local_dir='/workspace/models/ltx2')
"
fi

# ---------------------------------------------------------------------------
# Clone LTX-2.3 inference code (official repo)
# ---------------------------------------------------------------------------
if [ ! -d /workspace/ltx-video ]; then
    echo "Cloning LTX-Video inference code..."
    git clone --depth 1 https://github.com/Lightricks/LTX-Video.git /workspace/ltx-video
    cd /workspace/ltx-video && pip install -e . 2>/dev/null || true
    cd /workspace
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
    print(f'VRAM: {props.total_mem / 1e9:.1f} GB')
"

echo ""
echo "=== Bootstrap complete ==="
echo "To start the GPU worker:"
echo "  source /workspace/.venv/bin/activate"
echo "  python /workspace/gpu_worker.py --port 8880"
