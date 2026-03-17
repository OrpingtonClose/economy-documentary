#!/bin/bash
# Install ltx-core and ltx-pipelines from the LTX-2 repo
set -euo pipefail

echo "=== Installing ltx-core and ltx-pipelines ==="

# Clone the full LTX-2 repo if not already done
if [ ! -d /root/LTX-2-repo ]; then
    git clone --depth 1 https://github.com/Lightricks/LTX-2.git /root/LTX-2-repo
fi

source /root/LTX-2/.venv/bin/activate

# Install ltx-core first (dependency)
cd /root/LTX-2-repo/packages/ltx-core
pip install -e . 2>&1 | tail -5

# Install ltx-pipelines
cd /root/LTX-2-repo/packages/ltx-pipelines
pip install -e . 2>&1 | tail -5

# Verify
python3 -c "from ltx_pipelines.utils import ModelLedger; print('OK: ltx_pipelines works')"
python3 -c "from ltx_core.components.diffusion_steps import EulerDiffusionStep; print('OK: ltx_core works')"

# Download text encoder model (Gemma-3-12B for LTX-2.3)
# Check what the ModelLedger expects
python3 -c "
import inspect
from ltx_pipelines.utils import ModelLedger
# Find the text_encoder method
src = inspect.getsource(ModelLedger.text_encoder)
print(src[:2000])
"

echo "=== INSTALL COMPLETE ==="
