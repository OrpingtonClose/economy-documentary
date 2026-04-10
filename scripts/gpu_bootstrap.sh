#!/usr/bin/env bash
# GPU VM Bootstrap Script
# Run this on a freshly provisioned GPU VM (e.g., Vast.ai)
# to set up the video generation environment.

set -euo pipefail

echo "=== GPU Bootstrap: Documentary Pipeline ==="

# System packages
apt-get update && apt-get install -y \
    ffmpeg \
    git \
    python3-pip \
    python3-venv \
    wget \
    curl

# Create working directory
mkdir -p /workspace/pipeline
cd /workspace/pipeline

# Python environment
python3 -m venv .venv
source .venv/bin/activate

# Install PyTorch with CUDA (pinned for reproducible builds)
pip install 'torch>=2.3.0,<2.5.0' 'torchvision>=0.18.0,<0.20.0' 'torchaudio>=2.3.0,<2.5.0' --index-url https://download.pytorch.org/whl/cu121

# Install diffusers and dependencies for LTX-2.3
pip install \
    'diffusers>=0.31.0,<0.35.0' \
    'transformers>=4.44.0,<4.50.0' \
    'accelerate>=0.33.0,<0.40.0' \
    'safetensors>=0.4.0,<1.0.0' \
    'sentencepiece>=0.2.0,<1.0.0' \
    'soundfile>=0.12.0,<1.0.0' \
    'numpy>=1.26.0,<2.0.0'

# Pre-download LTX-2.3 model (if env var set)
if [ -n "${LTX_MODEL_ID:-}" ]; then
    python3 -c "
import os
from diffusers import LTXPipeline
import torch
model_id = os.environ['LTX_MODEL_ID']
print(f'Downloading LTX model: {model_id}...')
pipe = LTXPipeline.from_pretrained(model_id, torch_dtype=torch.bfloat16)
print('Model downloaded successfully.')
"
fi

echo "=== Bootstrap complete ==="
echo "Ready for video generation jobs."
