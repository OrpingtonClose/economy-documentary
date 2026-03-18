#!/bin/bash
set -e

echo "=============================================="
echo "WAR ECONOMY — VM Bootstrap Script"
echo "RTX 5090 32GB — LTX-2.3 + Qwen3-TTS"
echo "=============================================="

# =============================================================
# 1. System deps
# =============================================================
echo "[1/6] Installing system dependencies..."
apt-get update -qq && apt-get install -y -qq git ffmpeg python3-pip wget 2>/dev/null

# =============================================================
# 2. Clone repo
# =============================================================
echo "[2/6] Cloning production repo..."
cd /workspace
if [ ! -d "economy-documentary" ]; then
    git clone https://github.com/OrpingtonClose/economy-documentary.git
fi
cd economy-documentary
git pull origin main

PROD_DIR="/workspace/economy-documentary/projects/iran-war-doc/production"
echo "Production dir: $PROD_DIR"

# =============================================================
# 3. Python deps
# =============================================================
echo "[3/6] Installing Python dependencies..."
pip install --upgrade pip 2>/dev/null
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128 2>/dev/null || true
pip install transformers diffusers accelerate sentencepiece protobuf soundfile scipy 2>/dev/null
pip install b2sdk backblaze-b2 requests 2>/dev/null

# =============================================================
# 4. Download LTX-Video 2.3
# =============================================================
echo "[4/6] Downloading LTX-Video 2.3 model..."
mkdir -p /workspace/models
if [ ! -d "/workspace/models/ltx-video-2.3" ]; then
    python3 -c "
from huggingface_hub import snapshot_download
snapshot_download(
    'Lightricks/LTX-Video-2.3',
    local_dir='/workspace/models/ltx-video-2.3',
    token=os.environ.get('HF_TOKEN', '')
)
print('LTX-2.3 download complete')
" 2>&1 | tail -5
else
    echo "LTX-2.3 already downloaded"
fi

# =============================================================
# 5. Download Qwen3-TTS
# =============================================================
echo "[5/6] Downloading Qwen3-TTS model..."
if [ ! -d "/workspace/models/Qwen3-TTS" ]; then
    python3 -c "
from huggingface_hub import snapshot_download
snapshot_download(
    'Qwen/Qwen3-TTS',
    local_dir='/workspace/models/Qwen3-TTS',
    token=os.environ.get('HF_TOKEN', '')
)
print('Qwen3-TTS download complete')
" 2>&1 | tail -5
else
    echo "Qwen3-TTS already downloaded"
fi

# =============================================================
# 6. Create output directories
# =============================================================
echo "[6/6] Creating output directories..."
mkdir -p $PROD_DIR/audio
mkdir -p $PROD_DIR/clips
mkdir -p $PROD_DIR/scenes
mkdir -p $PROD_DIR/final

echo ""
echo "=============================================="
echo "Bootstrap complete. Ready to run pipeline."
echo "=============================================="
echo ""
echo "Next steps:"
echo "  cd $PROD_DIR"
echo "  python3 pipeline.py --phase narration --dry-run"
echo "  # Then run the generated scripts"
echo ""
echo "Or use the runner:"
echo "  python3 vm_runner.py --phase narration"
echo "  python3 vm_runner.py --phase video"
echo "  python3 vm_runner.py --phase assemble"
echo "  python3 vm_runner.py --phase final"
echo "  python3 vm_runner.py --phase upload"
