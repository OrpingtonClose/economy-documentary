#!/bin/bash
# Prepare VM for generation: install ltx packages, upgrade transformers, start generation
# Args: --start N --end M
set -uo pipefail
source /root/LTX-2/.venv/bin/activate

# Install ltx packages if needed
if ! python3 -c "from ltx_pipelines.utils import ModelLedger" 2>/dev/null; then
    if [ ! -d /root/LTX-2-new/packages ]; then
        git clone --depth 1 https://github.com/Lightricks/LTX-2.git /root/LTX-2-new 2>&1 | tail -1
    fi
    cd /root/LTX-2-new/packages/ltx-core && pip install -e . --no-deps 2>&1 | tail -1
    cd /root/LTX-2-new/packages/ltx-pipelines && pip install -e . --no-deps 2>&1 | tail -1
    pip install -q scipy accelerate av tqdm 2>/dev/null
fi

# Upgrade transformers if < 4.52
TRANS=$(python3 -c 'import transformers; print(transformers.__version__)')
if python3 -c "v='$TRANS'; parts=v.split('.'); exit(0 if int(parts[0])*1000+int(parts[1])>=4052 else 1)"; then
    echo "transformers OK: $TRANS"
else
    echo "Upgrading transformers..."
    pip install 'transformers==4.52.4' 2>&1 | tail -3
fi

# Create output dirs
mkdir -p /root/clips_out /root/embeddings_cache /dev/shm/ltx_clips

echo "=== READY, starting generation $@ ==="
cd /root
exec /root/LTX-2/.venv/bin/python3 v6_generate_v3.py "$@"
