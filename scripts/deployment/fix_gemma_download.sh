#!/bin/bash
# Quick fix: download Gemma 3 12B using venv python
set -uo pipefail
source /root/LTX-2/.venv/bin/activate

# Install ltx packages if needed
if ! python3 -c "from ltx_pipelines.utils import ModelLedger" 2>/dev/null; then
    if [ -d /root/LTX-2-new/packages ]; then
        echo "Installing ltx packages..."
        cd /root/LTX-2-new/packages/ltx-core && pip install -e . --no-deps 2>&1 | tail -1
        cd /root/LTX-2-new/packages/ltx-pipelines && pip install -e . --no-deps 2>&1 | tail -1
        pip install -q scipy accelerate av tqdm 2>/dev/null
    else
        echo "LTX-2-new repo missing, cloning..."
        git clone --depth 1 https://github.com/Lightricks/LTX-2.git /root/LTX-2-new 2>&1 | tail -1
        cd /root/LTX-2-new/packages/ltx-core && pip install -e . --no-deps 2>&1 | tail -1
        cd /root/LTX-2-new/packages/ltx-pipelines && pip install -e . --no-deps 2>&1 | tail -1
        pip install -q scipy accelerate av tqdm 2>/dev/null
    fi
fi

# Download Gemma if not present
if [ ! -f /root/models/text_encoder/tokenizer.model ]; then
    echo "Downloading Gemma 3 12B (~23GB)..."
    python3 -c "
from huggingface_hub import snapshot_download
path = snapshot_download(
    'google/gemma-3-12b-it',
    local_dir='/root/models/text_encoder',
    token='${HF_TOKEN}',
    ignore_patterns=['*.gguf', '*.bin'],
)
print(f'OK: Gemma downloaded to {path}')
"
else
    echo "OK: Gemma already exists"
fi

echo "=== FIX COMPLETE $(date) ==="
