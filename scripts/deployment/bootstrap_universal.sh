#!/bin/bash
# Universal bootstrap - handles all states: fresh, broken, partial
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
export HF_TOKEN="${HF_TOKEN}"

echo "=== BOOTSTRAP START $(date) ==="

# Fix clock
apt-get install -y -qq ntpdate 2>/dev/null || true
ntpdate -s pool.ntp.org 2>/dev/null || hwclock --hctosys 2>/dev/null || true

# B2 auth
pip install -q b2 b2sdk 2>/dev/null || true
b2 account authorize ${B2_KEY_ID} ${B2_APP_KEY} 2>/dev/null || echo "B2 auth deferred"

# LTX-2 repo + venv
if [ ! -f /root/LTX-2/.venv/bin/python ]; then
    cd /root
    [ -d LTX-2 ] || git clone --depth 1 https://github.com/Lightricks/LTX-Video.git LTX-2
    cd /root/LTX-2
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
    pip install -e ".[inference]"
    pip install requests b2sdk huggingface_hub
    echo "OK: venv created"
else
    echo "OK: venv exists"
fi

source /root/LTX-2/.venv/bin/activate

# Model checkpoint - remove if 0 bytes or corrupted, then download
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
        echo "FAIL: checkpoint download failed (${SIZE} bytes)"
        rm -f "$MODEL"
        exit 1
    fi
    echo "OK: checkpoint ($(du -sh $MODEL | awk '{print $1}'))"
else
    echo "OK: checkpoint exists ($(du -sh $MODEL | awk '{print $1}'))"
fi

# Text encoder
if [ ! -f /root/models/text_encoder/config.json ]; then
    echo "Downloading text encoder (~23GB)..."
    python3 -c "
from huggingface_hub import snapshot_download
import shutil, os
snapshot_download('Lightricks/LTX-2.3',
                  local_dir='/root/models/ltx23_full',
                  allow_patterns=['text_encoder/*'],
                  token='$HF_TOKEN')
src = '/root/models/ltx23_full/text_encoder'
if os.path.exists(src):
    os.makedirs('/root/models/text_encoder', exist_ok=True)
    shutil.rmtree('/root/models/text_encoder', ignore_errors=True)
    shutil.move(src, '/root/models/text_encoder')
    shutil.rmtree('/root/models/ltx23_full', ignore_errors=True)
print('OK: text encoder')
"
else
    echo "OK: text encoder exists"
fi

echo "=== BOOTSTRAP COMPLETE $(date) ==="
