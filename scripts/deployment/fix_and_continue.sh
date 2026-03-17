#!/bin/bash
# Fix bootstrap issues and continue setup on a VM
# Run: bash fix_and_continue.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Fix clock (for B2)
apt-get install -y -qq ntpdate 2>/dev/null || true
ntpdate -s pool.ntp.org 2>/dev/null || hwclock --hctosys 2>/dev/null || true

# B2
pip install -q b2 2>/dev/null || true
b2 account authorize ${B2_KEY_ID} ${B2_APP_KEY} 2>/dev/null || echo "B2 auth failed (will retry later)"

# LTX-2 venv
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
fi

source /root/LTX-2/.venv/bin/activate

# Download model - correct repo is Lightricks/LTX-2.3
if [ ! -f /root/models/ltx-2.3-22b-dev.safetensors ]; then
    echo "Downloading checkpoint..."
    mkdir -p /root/models
    # Use wget with correct URL
    wget -q --show-progress \
      "https://huggingface.co/Lightricks/LTX-2.3/resolve/main/ltx-2.3-22b-dev.safetensors" \
      -O /root/models/ltx-2.3-22b-dev.safetensors
    echo "OK: checkpoint"
else
    echo "OK: checkpoint exists ($(du -sh /root/models/ltx-2.3-22b-dev.safetensors | awk '{print $1}'))"
fi

# Download text encoder from correct repo
if [ ! -f /root/models/text_encoder/config.json ]; then
    echo "Downloading text encoder..."
    python3 -c "
from huggingface_hub import snapshot_download
import shutil, os
snapshot_download('Lightricks/LTX-2.3',
                  local_dir='/root/models/ltx23_full',
                  allow_patterns=['text_encoder/*'],
                  token='${HF_TOKEN}')
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

echo "=== FIX AND CONTINUE COMPLETE ==="
