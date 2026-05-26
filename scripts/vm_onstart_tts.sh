#!/usr/bin/env bash
# VM onstart for Qwen3-TTS workers.
# Sets up the environment so the VM agent can generate narration audio.
set -e
cd /workspace

apt-get update -qq
apt-get install -y -qq git curl wget ffmpeg libsndfile1 python3-pip

# Install PyTorch with CUDA 12.1 wheels
pip install -q --break-system-packages \
    torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cu121

# Install Qwen3-TTS package (PyPI distribution name: qwen-tts)
pip install -q --break-system-packages qwen-tts transformers accelerate

# Best-effort flash-attention for faster inference
pip install --no-build-isolation flash-attn || true

# Pre-download Qwen3-TTS model weights from HuggingFace
mkdir -p /workspace/models
python3 -c "
from huggingface_hub import snapshot_download
snapshot_download(
    'Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign',
    local_dir='/workspace/models/qwen3-tts-voicedesign',
)
"

# Clone the documentary repo for runner scripts (idempotent)
if [ ! -d /workspace/repo/.git ]; then
    git clone --depth 1 --branch strands-migration \
        https://github.com/OrpingtonClose/economy-documentary.git repo
fi

# Write DeepSeek API key for the VM agent
echo "$1" > /workspace/.deepseek_key

# Write Vast.ai credentials for the self-destruct monitor
echo "$2" > /workspace/.vast_api_key

# Start the VM agent
nohup python repo/scripts/vm_agent.py --port 8880 \
    > /workspace/agent.log 2>&1 &

echo "started"
