#!/bin/bash
set -e
cd /workspace/LTX-2

# Ensure uv is available
export PATH="/workspace/venv/bin:$PATH"
which uv || pip install uv

# Run uv sync for LTX-2.3
uv sync --frozen 2>&1 | tail -5

# Install qwen-tts into the uv venv
uv pip install qwen-tts soundfile --python .venv/bin/python3 2>&1 | tail -5

# Install sox
apt-get install -y -qq sox libsox-dev libsox-fmt-all 2>/dev/null || true

# Verify
source .venv/bin/activate
python3 -c '
from ltx_pipelines import TI2VidOneStagePipeline
from qwen_tts import Qwen3TTSModel
import torch
gpu = torch.cuda.get_device_name(0)
vram = torch.cuda.get_device_properties(0).total_memory / 1024**3
print(f"READY | {gpu} | {vram:.0f}GB VRAM | LTX-2.3 OK | Qwen3-TTS OK")
'
