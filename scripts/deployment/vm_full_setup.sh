#!/bin/bash
set -e
echo "=== Full LTX-2.3 Setup ==="
export DEBIAN_FRONTEND=noninteractive
export HF_TOKEN="${HF_TOKEN}"

MODELS_DIR="/root/models"
mkdir -p "$MODELS_DIR" /root/ltx_gen/outputs /root/ltx_gen/frames

# System deps
echo "[1/7] System deps..."
apt-get update -qq && apt-get install -y -qq git ffmpeg wget curl jq > /dev/null 2>&1

# Python deps
echo "[2/7] Extra Python deps..."
pip3 install --quiet huggingface_hub safetensors sentencepiece einops imageio imageio-ffmpeg pillow accelerate 2>/dev/null
# Pin transformers to 4.52.4 (required for Gemma3 compatibility with ltx-core)
pip3 install --quiet transformers==4.52.4 2>/dev/null

# Clone LTX-2 repo (the monorepo with ltx-core + ltx-pipelines)
echo "[3/7] Cloning LTX-2 repo..."
cd /root
if [ ! -d "LTX-2" ]; then
    git clone https://github.com/Lightricks/LTX-2.git 2>/dev/null
fi

# Install ltx-core and ltx-pipelines
echo "[4/7] Installing ltx-core and ltx-pipelines..."
cd /root/LTX-2
pip3 install -e packages/ltx-core 2>/dev/null
pip3 install -e packages/ltx-pipelines 2>/dev/null

# Download model weights
echo "[5/7] Downloading LTX-2.3 model weights..."
python3 -c "
import os
from huggingface_hub import hf_hub_download
m = '$MODELS_DIR'
if not os.path.exists(f'{m}/ltx-2.3-22b-distilled.safetensors'):
    print('  Downloading distilled checkpoint (43GB)...')
    hf_hub_download('Lightricks/LTX-2.3', 'ltx-2.3-22b-distilled.safetensors', local_dir=m)
if not os.path.exists(f'{m}/ltx-2.3-spatial-upscaler-x2-1.0.safetensors'):
    print('  Downloading spatial upsampler...')
    hf_hub_download('Lightricks/LTX-2.3', 'ltx-2.3-spatial-upscaler-x2-1.0.safetensors', local_dir=m)
print('Model weights ready')
"

# Download Gemma
echo "[6/7] Downloading Gemma model..."
python3 -c "
import os
from huggingface_hub import snapshot_download
g = '$MODELS_DIR/gemma'
if not os.path.exists(g) or len(os.listdir(g)) < 5:
    print('  Downloading Gemma...')
    snapshot_download('google/gemma-3-12b-it-qat-q4_0-unquantized', local_dir=g)
print('Gemma ready')
"

# Fix circular import in ltx-core
echo "[7/8] Patching ltx-core circular import..."
python3 << 'PATCHEOF'
import os
path = "/root/LTX-2/packages/ltx-core/src/ltx_core/loader/fuse_loras.py"
content = open(path).read()
content = content.replace('from ltx_core.quantization.fp8_cast import calculate_weight_float8\n', '')
content = content.replace('from ltx_core.quantization.fp8_scaled_mm import quantize_weight_to_fp8_per_tensor\n', '')
content = content.replace(
    '    if str(device).startswith("cuda"):\n        deltas = calculate_weight_float8(deltas, weight)',
    '    if str(device).startswith("cuda"):\n        from ltx_core.quantization.fp8_cast import calculate_weight_float8\n        deltas = calculate_weight_float8(deltas, weight)'
)
content = content.replace(
    '    new_fp8_weight, new_weight_scale = quantize_weight_to_fp8_per_tensor(new_weight)',
    '    from ltx_core.quantization.fp8_scaled_mm import quantize_weight_to_fp8_per_tensor\n    new_fp8_weight, new_weight_scale = quantize_weight_to_fp8_per_tensor(new_weight)'
)
open(path, 'w').write(content)
print('  Circular import patched')
PATCHEOF

# Verify
echo "[8/8] Verifying..."
python3 -c "
import torch
print(f'CUDA: {torch.cuda.is_available()}, GPU: {torch.cuda.get_device_name(0)}')
from ltx_core.quantization.policy import QuantizationPolicy
print('ltx_core: OK')
from ltx_pipelines.distilled import DistilledPipeline
print('ltx_pipelines: OK')
import os
for f in ['ltx-2.3-22b-distilled.safetensors', 'ltx-2.3-spatial-upscaler-x2-1.0.safetensors']:
    p = f'$MODELS_DIR/{f}'
    print(f'  {f}: {os.path.getsize(p)/1024**3:.1f}GB' if os.path.exists(p) else f'  {f}: MISSING!')
"

echo "=== Setup Complete ==="
