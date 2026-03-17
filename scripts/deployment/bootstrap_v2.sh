#!/bin/bash
# Bootstrap V2 — Uses correct LTX-2 repo with ltx_pipelines/ltx_core
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
export HF_TOKEN="${HF_TOKEN}"

echo "=== BOOTSTRAP V2 START $(date) ==="

# Fix clock
apt-get install -y -qq ntpdate 2>/dev/null || true
ntpdate -s pool.ntp.org 2>/dev/null || hwclock --hctosys 2>/dev/null || true

# B2 auth
pip install -q b2 b2sdk 2>/dev/null || true
b2 account authorize ${B2_KEY_ID} ${B2_APP_KEY} 2>/dev/null || echo "B2 auth deferred"

# Clone correct LTX-2 repo (monorepo with ltx-core + ltx-pipelines)
if [ ! -d /root/LTX-2-new/packages ]; then
    cd /root
    rm -rf LTX-2-new
    git clone --depth 1 https://github.com/Lightricks/LTX-2.git LTX-2-new 2>&1
    echo "OK: LTX-2 repo cloned"
else
    echo "OK: LTX-2 repo exists"
fi

# Setup venv (reuse existing if it has torch)
VENV=/root/LTX-2/.venv
if [ ! -f $VENV/bin/python ] || ! $VENV/bin/python -c "import torch" 2>/dev/null; then
    echo "Setting up venv..."
    rm -rf $VENV /root/LTX-2
    mkdir -p /root/LTX-2
    python3 -m venv $VENV
    source $VENV/bin/activate
    pip install --upgrade pip
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
    pip install requests b2sdk huggingface_hub scipy accelerate av tqdm pillow einops sentencepiece
    pip install safetensors transformers imageio[ffmpeg]
    echo "OK: venv created"
else
    source $VENV/bin/activate
    # Install missing deps
    pip install -q scipy accelerate av tqdm pillow einops sentencepiece safetensors 2>/dev/null || true
    echo "OK: venv exists"
fi

source $VENV/bin/activate

# Install ltx-core and ltx-pipelines from local repo
if ! python3 -c "from ltx_pipelines.utils import ModelLedger" 2>/dev/null; then
    echo "Installing ltx-core..."
    cd /root/LTX-2-new/packages/ltx-core
    pip install -e . --no-deps 2>&1 | tail -2
    echo "Installing ltx-pipelines..."
    cd /root/LTX-2-new/packages/ltx-pipelines
    pip install -e . --no-deps 2>&1 | tail -2
    echo "OK: ltx packages installed"
else
    echo "OK: ltx packages already installed"
fi

# Model checkpoint
mkdir -p /root/models
MODEL=/root/models/ltx-2.3-22b-dev.safetensors
if [ -f "$MODEL" ]; then
    SIZE=$(stat -c%s "$MODEL" 2>/dev/null || echo 0)
    if [ "$SIZE" -lt 1000000000 ]; then
        echo "Model file corrupted (${SIZE} bytes), removing..."
        rm -f "$MODEL"
    fi
fi
if [ ! -f "$MODEL" ]; then
    echo "Downloading checkpoint (43GB)..."
    wget -q --show-progress \
      "https://huggingface.co/Lightricks/LTX-2.3/resolve/main/ltx-2.3-22b-dev.safetensors" \
      -O "$MODEL"
    SIZE=$(stat -c%s "$MODEL" 2>/dev/null || echo 0)
    if [ "$SIZE" -lt 1000000000 ]; then
        echo "FAIL: checkpoint download failed"
        rm -f "$MODEL"
        exit 1
    fi
    echo "OK: checkpoint ($(du -sh $MODEL | awk '{print $1}'))"
else
    echo "OK: checkpoint exists ($(du -sh $MODEL | awk '{print $1}'))"
fi

# Gemma 3 12B text encoder
if [ ! -f /root/models/text_encoder/tokenizer.model ]; then
    echo "Downloading Gemma 3 12B text encoder (~23GB)..."
    python3 -c "
from huggingface_hub import snapshot_download
path = snapshot_download(
    'google/gemma-3-12b-it',
    local_dir='/root/models/text_encoder',
    token='$HF_TOKEN',
    ignore_patterns=['*.gguf', '*.bin'],
)
print(f'OK: text encoder downloaded to {path}')
"
else
    echo "OK: text encoder exists"
fi

echo "=== BOOTSTRAP V2 COMPLETE $(date) ==="
