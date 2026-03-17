#!/bin/bash
# Complete VM setup: install packages + download text encoder
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
HF_TOKEN="${HF_TOKEN}"

echo "=== $(hostname) starting setup ==="

# Fix clock
apt-get install -y -qq ntpdate 2>/dev/null || true
ntpdate -s pool.ntp.org 2>/dev/null || true

# Ensure python3-venv
apt-get install -y -qq python3.10-venv 2>/dev/null || true

# Clone LTX-Video repo if needed (for editable install)
if [ ! -d /root/LTX-2 ]; then
    git clone --depth 1 https://github.com/Lightricks/LTX-Video.git /root/LTX-2
fi

# Create venv if needed
if [ ! -f /root/LTX-2/.venv/bin/python ]; then
    cd /root/LTX-2
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip
    pip install torch==2.6.0+cu124 torchvision==0.21.0+cu124 torchaudio==2.6.0+cu124 --index-url https://download.pytorch.org/whl/cu124
    pip install -e ".[inference]"
    pip install requests b2sdk huggingface_hub
else
    source /root/LTX-2/.venv/bin/activate
fi

# Clone LTX-2 monorepo for packages
if [ ! -d /root/LTX-2-repo ]; then
    git clone --depth 1 https://github.com/Lightricks/LTX-2.git /root/LTX-2-repo
fi

# Install ltx-core and ltx-pipelines without their heavy deps
pip install --no-deps -e /root/LTX-2-repo/packages/ltx-core 2>&1 | tail -2
pip install --no-deps -e /root/LTX-2-repo/packages/ltx-pipelines 2>&1 | tail -2

# Install additional runtime deps needed by ltx-core/ltx-pipelines
pip install accelerate scipy rich typer click 2>&1 | tail -2

# Verify packages work
python3 -c "from ltx_pipelines.utils import ModelLedger; print('OK: ltx_pipelines')" || { echo "FAIL: ltx_pipelines"; exit 1; }

# Download LTX-2.3 checkpoint if needed
mkdir -p /root/models
if [ ! -f /root/models/ltx-2.3-22b-dev.safetensors ] || [ $(stat -c%s /root/models/ltx-2.3-22b-dev.safetensors 2>/dev/null || echo 0) -lt 40000000000 ]; then
    echo "Downloading checkpoint (43GB)..."
    wget -q --show-progress \
      "https://huggingface.co/Lightricks/LTX-2.3/resolve/main/ltx-2.3-22b-dev.safetensors" \
      -O /root/models/ltx-2.3-22b-dev.safetensors
    echo "OK: checkpoint downloaded"
else
    echo "OK: checkpoint exists ($(du -sh /root/models/ltx-2.3-22b-dev.safetensors | awk '{print $1}'))"
fi

# Download Gemma-3 text encoder if needed
if [ ! -f /root/models/text_encoder/config.json ]; then
    echo "Downloading Gemma-3 text encoder..."
    python3 -c "
from huggingface_hub import snapshot_download
snapshot_download(
    'google/gemma-3-12b-it-qat-q4_0-unquantized',
    local_dir='/root/models/text_encoder',
    token='$HF_TOKEN'
)
print('OK: text encoder downloaded')
"
else
    echo "OK: text encoder exists"
fi

# B2 auth
pip install -q b2 2>/dev/null || true
b2 account authorize ${B2_KEY_ID} ${B2_APP_KEY} 2>/dev/null || echo "B2 auth failed"

echo "=== $(hostname) setup COMPLETE ==="
