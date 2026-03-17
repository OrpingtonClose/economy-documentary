#!/bin/bash
# Bootstrap a VM for LTX-2.3 video generation
# Run: bash bootstrap.sh
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "=== Step 1: System deps ==="
apt-get update -qq
apt-get install -y -qq ffmpeg git wget python3-pip python3-venv
echo "OK: system deps"

echo "=== Step 2: Dirs ==="
mkdir -p /root/models /root/clips_out /root/embeddings_cache

echo "=== Step 3: B2 CLI ==="
pip install -q b2 2>/dev/null || pip install -q b2
b2 account authorize ${B2_KEY_ID} ${B2_APP_KEY}
echo "OK: b2"

echo "=== Step 4: LTX-2 venv ==="
if [ ! -f /root/LTX-2/.venv/bin/python ]; then
    cd /root
    git clone --depth 1 https://github.com/Lightricks/LTX-Video.git LTX-2
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

echo "=== Step 5: Download checkpoint (43GB) ==="
if [ ! -f /root/models/ltx-2.3-22b-dev.safetensors ]; then
    wget -q --show-progress \
      "https://huggingface.co/Lightricks/LTX-Video-2.3-22B-Dev/resolve/main/ltx-2.3-22b-dev.safetensors" \
      -O /root/models/ltx-2.3-22b-dev.safetensors
    echo "OK: checkpoint"
else
    echo "OK: checkpoint exists"
fi

echo "=== Step 6: Download text encoder (23GB) ==="
if [ ! -f /root/models/text_encoder/config.json ]; then
    source /root/LTX-2/.venv/bin/activate
    python3 -c "
from huggingface_hub import snapshot_download
import shutil, os
snapshot_download('Lightricks/LTX-Video-2.3-22B-Dev',
                  local_dir='/root/models/te_tmp',
                  allow_patterns=['text_encoder/*'],
                  token='${HF_TOKEN}')
src = '/root/models/te_tmp/text_encoder'
if os.path.exists(src):
    if os.path.exists('/root/models/text_encoder'):
        shutil.rmtree('/root/models/text_encoder')
    shutil.move(src, '/root/models/text_encoder')
    shutil.rmtree('/root/models/te_tmp', ignore_errors=True)
print('OK: text encoder')
"
else
    echo "OK: text encoder exists"
fi

echo "=== BOOTSTRAP COMPLETE ==="
