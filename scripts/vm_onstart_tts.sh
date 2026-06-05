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

# Install Qwen3-TTS package (PyPI distribution name: qwen-tts) and whisperx
pip install -q --break-system-packages qwen-tts transformers accelerate whisperx

# Best-effort flash-attention for faster inference - only pre-compiled wheels to avoid compilation timeouts
pip install -q --break-system-packages --only-binary :all: flash-attn || true

# Create models directory (weights will be copied via vastai copy from B2)
mkdir -p /workspace/models

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
    > /workspace/worker.log 2>&1 &

touch /workspace/.bootstrap_complete
echo "started"

