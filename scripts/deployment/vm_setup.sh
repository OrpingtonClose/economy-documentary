#!/bin/bash
set -e

echo "=== VM SETUP: LTX-2.3 + Qwen3-TTS ==="
echo "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo 'checking...')"

# Wait for apt lock to be free (onstart script may still be running)
for i in $(seq 1 30); do
    if ! fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
        break
    fi
    echo "Waiting for apt lock... ($i/30)"
    sleep 10
done

# Install system deps
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl wget ffmpeg libsndfile1 > /dev/null 2>&1
echo "System deps installed."

# Create workspace
mkdir -p /workspace/models /workspace/outputs /workspace/scripts
cd /workspace

# Setup Python venv
python3 -m venv /workspace/venv
source /workspace/venv/bin/activate
pip install --upgrade pip setuptools wheel > /dev/null 2>&1

# Install LTX-2 pipeline
echo "=== Installing LTX-2 pipeline ==="
cd /workspace
git clone https://github.com/Lightricks/LTX-2.git 2>/dev/null || (cd LTX-2 && git pull)
cd LTX-2
pip install uv 2>/dev/null
# Install with pip directly instead of uv for reliability
pip install -e ".[xformers]" 2>/dev/null || pip install -e . 2>/dev/null || echo "LTX-2 pip install had issues, trying alternate..."

# If the above didn't work, install core deps manually
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124 2>/dev/null || true
pip install diffusers transformers accelerate safetensors sentencepiece protobuf einops 2>/dev/null || true
pip install soundfile scipy 2>/dev/null || true

# Install Qwen3-TTS
echo "=== Installing Qwen3-TTS ==="
pip install qwen-tts 2>/dev/null || pip install git+https://github.com/QwenLM/Qwen3-TTS.git 2>/dev/null || echo "Trying alternate qwen-tts install..."
pip install flash-attn --no-build-isolation 2>/dev/null || echo "flash-attn install skipped (will use sdpa)"

# Install huggingface CLI for model downloads
pip install huggingface_hub[cli] 2>/dev/null

# Download models
echo "=== Downloading LTX-2.3 full BF16 model ==="
export HF_TOKEN="${HF_TOKEN:-}"
huggingface-cli download Lightricks/LTX-2.3 ltx-2.3-22b-dev.safetensors --local-dir /workspace/models/ltx23 --local-dir-use-symlinks False &
LTX_PID=$!

echo "=== Downloading LTX-2.3 VAE and text encoder ==="
# Download Gemma text encoder
huggingface-cli download google/gemma-3-4b-it --local-dir /workspace/models/gemma3 --local-dir-use-symlinks False &
GEMMA_PID=$!

echo "=== Downloading Qwen3-TTS models ==="
huggingface-cli download Qwen/Qwen3-TTS-Tokenizer-12Hz --local-dir /workspace/models/qwen-tts-tokenizer --local-dir-use-symlinks False &
TTS_TOK_PID=$!

huggingface-cli download Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign --local-dir /workspace/models/qwen-tts-voicedesign --local-dir-use-symlinks False &
TTS_VD_PID=$!

huggingface-cli download Qwen/Qwen3-TTS-12Hz-1.7B-Base --local-dir /workspace/models/qwen-tts-base --local-dir-use-symlinks False &
TTS_BASE_PID=$!

# Wait for all downloads
echo "Waiting for model downloads..."
wait $LTX_PID && echo "LTX-2.3 model downloaded." || echo "LTX-2.3 download may have failed."
wait $GEMMA_PID && echo "Gemma-3 encoder downloaded." || echo "Gemma download may have failed."
wait $TTS_TOK_PID && echo "Qwen TTS tokenizer downloaded." || echo "TTS tokenizer download may have failed."
wait $TTS_VD_PID && echo "Qwen TTS VoiceDesign downloaded." || echo "TTS VoiceDesign download may have failed."
wait $TTS_BASE_PID && echo "Qwen TTS Base downloaded." || echo "TTS Base download may have failed."

echo "=== Checking GPU ==="
nvidia-smi

echo "=== Setup complete ==="
echo "Models in /workspace/models/"
ls -la /workspace/models/
