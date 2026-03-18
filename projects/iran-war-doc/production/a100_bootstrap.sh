#!/bin/bash
set -e

echo "=============================================="
echo "WAR ECONOMY — A100 80GB Bootstrap"
echo "LTX-2.3 (22B bf16) + Qwen3-TTS"
echo "=============================================="

export HF_TOKEN="${HF_TOKEN:-}"
export DEBIAN_FRONTEND=noninteractive

# =============================================================
# 1. System deps
# =============================================================
echo "[1/7] Installing system dependencies..."
apt-get update -qq && apt-get install -y -qq git git-lfs ffmpeg python3-pip wget curl 2>/dev/null
git lfs install 2>/dev/null || true

# =============================================================
# 2. Clone production repo
# =============================================================
echo "[2/7] Cloning production repo..."
cd /workspace
if [ ! -d "economy-documentary" ]; then
    git clone https://github.com/OrpingtonClose/economy-documentary.git
fi
cd economy-documentary
git pull origin main 2>/dev/null || true

PROD_DIR="/workspace/economy-documentary/projects/iran-war-doc/production"
echo "Production dir: $PROD_DIR"

# =============================================================
# 3. Clone LTX-2 inference repo + install
# =============================================================
echo "[3/7] Setting up LTX-2 inference environment..."
cd /workspace
if [ ! -d "LTX-2" ]; then
    git clone https://github.com/Lightricks/LTX-2.git
fi
cd LTX-2

# Install uv if not present
if ! command -v uv &> /dev/null; then
    pip install uv 2>/dev/null
fi

# Set up environment - use pip directly since uv sync can be finicky
pip install --upgrade pip 2>/dev/null
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126 2>/dev/null || \
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124 2>/dev/null || true

# Install ltx packages
pip install -e packages/ltx-core 2>/dev/null
pip install -e packages/ltx-pipelines 2>/dev/null

# Additional deps
pip install transformers accelerate sentencepiece protobuf soundfile scipy 2>/dev/null
pip install huggingface_hub safetensors 2>/dev/null

# =============================================================
# 4. Download LTX-2.3 dev checkpoint (46.15 GB)
# =============================================================
echo "[4/7] Downloading LTX-2.3 dev checkpoint (46.15 GB)..."
mkdir -p /workspace/models
LTX_CKPT="/workspace/models/ltx-2.3-22b-dev.safetensors"
if [ ! -f "$LTX_CKPT" ]; then
    python3 -c "
from huggingface_hub import hf_hub_download
import os
hf_hub_download(
    'Lightricks/LTX-2.3',
    'ltx-2.3-22b-dev.safetensors',
    local_dir='/workspace/models',
    token=os.environ.get('HF_TOKEN', '')
)
print('LTX-2.3 dev checkpoint downloaded')
"
else
    echo "LTX-2.3 checkpoint already exists"
fi

# =============================================================
# 5. Download Gemma 3 12B text encoder
# =============================================================
echo "[5/7] Downloading Gemma 3 12B text encoder..."
GEMMA_DIR="/workspace/models/gemma-3-12b-it-qat-q4_0-unquantized"
if [ ! -d "$GEMMA_DIR" ]; then
    python3 -c "
from huggingface_hub import snapshot_download
import os
snapshot_download(
    'google/gemma-3-12b-it-qat-q4_0-unquantized',
    local_dir='$GEMMA_DIR',
    token=os.environ.get('HF_TOKEN', '')
)
print('Gemma 3 12B downloaded')
"
else
    echo "Gemma 3 already downloaded"
fi

# =============================================================
# 6. Download Qwen3-TTS (only on VM 0)
# =============================================================
VM_ID="${VM_ID:-0}"
if [ "$VM_ID" = "0" ]; then
    echo "[6/7] Downloading Qwen3-TTS model (VM 0 only)..."
    QWEN_DIR="/workspace/models/Qwen3-TTS"
    if [ ! -d "$QWEN_DIR" ]; then
        python3 -c "
from huggingface_hub import snapshot_download
import os
snapshot_download(
    'Qwen/Qwen3-TTS',
    local_dir='$QWEN_DIR',
    token=os.environ.get('HF_TOKEN', '')
)
print('Qwen3-TTS downloaded')
"
    else
        echo "Qwen3-TTS already downloaded"
    fi
else
    echo "[6/7] Skipping Qwen3-TTS (VM $VM_ID — TTS runs on VM 0 only)"
fi

# =============================================================
# 7. Create output directories + verify GPU
# =============================================================
echo "[7/7] Verifying GPU and creating output directories..."
mkdir -p $PROD_DIR/audio
mkdir -p $PROD_DIR/clips
mkdir -p $PROD_DIR/scenes
mkdir -p $PROD_DIR/final
mkdir -p /workspace/outputs

python3 -c "
import torch
print(f'PyTorch: {torch.__version__}')
print(f'CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)}')
    print(f'VRAM: {torch.cuda.get_device_properties(0).total_mem / 1e9:.1f} GB')
    print(f'CUDA version: {torch.version.cuda}')
"

echo ""
echo "=============================================="
echo "Bootstrap complete!"
echo "=============================================="
echo ""
echo "Model checkpoint: $LTX_CKPT"
echo "Gemma root: $GEMMA_DIR"
echo "Production dir: $PROD_DIR"
echo ""
echo "Test generation:"
echo "  python3 -m ltx_pipelines.ti2vid_one_stage \\"
echo "    --checkpoint-path $LTX_CKPT \\"
echo "    --gemma-root $GEMMA_DIR \\"
echo "    --prompt 'A test scene' \\"
echo "    --output-path /workspace/outputs/test.mp4"
