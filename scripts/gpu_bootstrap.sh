#!/usr/bin/env bash
# GPU VM Bootstrap Script
# Run this on a freshly provisioned Vast.ai GPU VM to set up
# the documentary pipeline's video + TTS generation environment.
#
# Usage:
#   B2_KEY_ID=... B2_APPLICATION_KEY=... WORKER_MODE=tts|ltx|both bash gpu_bootstrap.sh
#
# WORKER_MODE controls which models are downloaded:
#   tts  — only Qwen3-TTS (~4.3 GB, ~2 min on slow connections)
#   ltx  — only LTX-2.3 video models (~48 GB)
#   both — everything (default if not set)
#
# Models are pulled from Backblaze B2 (pre-cached) for speed.
# Falls back to HuggingFace if B2 credentials are not set.
#
# Disk budget (ltx-pipelines: single-file checkpoint + gemma):
#   ltx-2-19b-dev.safetensors:        ~40   GB
#   Gemma-3 1B text encoder:          ~ 2.0 GB
#   Qwen3-TTS VoiceDesign:            ~ 4.3 GB
#   Total models:                      ~52   GB
#   OS + software + HF cache:          ~50   GB  (downloads cached then moved)
#   Peak disk during download:         ~100  GB  (checkpoint + HF cache)
#   Minimum disk required:             ~120  GB  (recommend 150+)

set -euo pipefail

# Default to 'both' if not set
WORKER_MODE="${WORKER_MODE:-both}"
echo "=== GPU Bootstrap: Documentary Pipeline (mode=$WORKER_MODE) ==="
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
df -h /

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    git \
    python3-pip \
    wget \
    curl

# ---------------------------------------------------------------------------
# Working directories
# ---------------------------------------------------------------------------
mkdir -p /workspace/{models,output}
cd /workspace

# ---------------------------------------------------------------------------
# CUDA compatibility — detect system CUDA and match PyTorch index
# ---------------------------------------------------------------------------
# Vast.ai VMs may have CUDA 11.x, 12.1, 12.4, etc.  We detect the
# system CUDA version and pick a matching PyTorch wheel index.
SYSTEM_CUDA=""
if command -v nvcc &>/dev/null; then
    SYSTEM_CUDA=$(nvcc --version 2>/dev/null | grep -oP 'release \K[0-9]+\.[0-9]+')
    echo "System CUDA: $SYSTEM_CUDA"
elif [ -f /usr/local/cuda/version.txt ]; then
    SYSTEM_CUDA=$(cat /usr/local/cuda/version.txt | grep -oP '[0-9]+\.[0-9]+')
    echo "System CUDA (from version.txt): $SYSTEM_CUDA"
else
    # Try nvidia-smi
    SYSTEM_CUDA=$(nvidia-smi 2>/dev/null | grep -oP 'CUDA Version: \K[0-9]+\.[0-9]+')
    echo "System CUDA (from nvidia-smi): $SYSTEM_CUDA"
fi

# Map system CUDA to PyTorch wheel index
CUDA_MAJOR=$(echo "$SYSTEM_CUDA" | cut -d. -f1)
CUDA_MINOR=$(echo "$SYSTEM_CUDA" | cut -d. -f2)
if [ "$CUDA_MAJOR" = "13" ]; then
    TORCH_INDEX="https://download.pytorch.org/whl/cu130"
elif [ "$CUDA_MAJOR" = "12" ] && [ "$CUDA_MINOR" -ge 4 ]; then
    TORCH_INDEX="https://download.pytorch.org/whl/cu124"
elif [ "$CUDA_MAJOR" = "12" ]; then
    TORCH_INDEX="https://download.pytorch.org/whl/cu121"
elif [ "$CUDA_MAJOR" = "11" ]; then
    TORCH_INDEX="https://download.pytorch.org/whl/cu118"
else
    # Default to cu130 — ltx-core 1.0 requires CUDA 13.0 runtime
    TORCH_INDEX="https://download.pytorch.org/whl/cu130"
fi
echo "Using PyTorch wheel index: $TORCH_INDEX"

# ---------------------------------------------------------------------------
# Python dependencies (install into system python — ephemeral VM)
# ---------------------------------------------------------------------------
# torch 2.6+ required for ltx-pipelines
pip install --no-cache-dir \
    'torch>=2.6.0' \
    'torchvision>=0.21.0' \
    'torchaudio>=2.6.0' \
    --index-url "$TORCH_INDEX"

# Ensure CUDA runtime libs are on LD_LIBRARY_PATH
# PyTorch ships its own libcudart — add its location to the search path
TORCH_LIB=$(python3 -c "import torch, os; print(os.path.dirname(torch.__file__) + '/lib')" 2>/dev/null || true)
if [ -n "$TORCH_LIB" ] && [ -d "$TORCH_LIB" ]; then
    export LD_LIBRARY_PATH="${TORCH_LIB}:${LD_LIBRARY_PATH:-}"
    echo "Added $TORCH_LIB to LD_LIBRARY_PATH"
    # Also persist for the gpu_worker process
    echo "export LD_LIBRARY_PATH=\"${TORCH_LIB}:\${LD_LIBRARY_PATH:-}\"" >> /etc/profile.d/torch_cuda.sh
fi

# ltx-pipelines: official Lightricks inference code for LTX-2.3
# No diffusers needed — ltx-pipelines uses the single-file checkpoint natively.
pip install --no-cache-dir \
    'ltx-pipelines>=1.0.0' \
    'ltx-core>=1.0.0' \
    'accelerate>=0.33.0' \
    'safetensors>=0.4.0' \
    'sentencepiece>=0.2.0' \
    'soundfile>=0.12.0' \
    'numpy>=1.26.0,<2.0.0' \
    'fastapi>=0.100.0' \
    'uvicorn>=0.20.0' \
    'pydantic>=2.0.0' \
    'b2>=4.0.0' \
    'qwen-tts>=0.1.0' \
    'opencv-python>=4.8.0'

# Install sox for qwen-tts audio processing
apt-get install -y sox libsox-dev

# ---------------------------------------------------------------------------
# Model downloads — ltx-pipelines uses single-file checkpoint + gemma
# ---------------------------------------------------------------------------
B2_BUCKET="ltx2-models-orpington"
pip install --no-cache-dir huggingface_hub 2>/dev/null

if [ -n "${B2_KEY_ID:-}" ] && [ -n "${B2_APPLICATION_KEY:-}" ]; then
    echo "=== Downloading models (B2 primary, HuggingFace fallback) ==="
    export B2_APPLICATION_KEY_ID="$B2_KEY_ID"
    b2 account authorize "$B2_KEY_ID" "$B2_APPLICATION_KEY"
fi

# --- Qwen3-TTS VoiceDesign (~4.3 GB) — needed for tts and both modes ---
if [ "$WORKER_MODE" = "ltx" ]; then
    echo "--- Skipping Qwen3-TTS (ltx-only mode) ---"
elif [ ! -f /workspace/models/qwen3-tts-voicedesign/model.safetensors ]; then
    echo "--- Qwen3-TTS VoiceDesign (~4.3 GB) ---"
    mkdir -p /workspace/models/qwen3-tts-voicedesign
    if [ -n "${B2_KEY_ID:-}" ]; then
        b2_tts_count=$(b2 ls "b2://${B2_BUCKET}/qwen3-tts-voicedesign/" 2>/dev/null | grep -c model.safetensors || true)
        if [ "${b2_tts_count}" -gt 0 ]; then
            b2 sync --threads 8 "b2://${B2_BUCKET}/qwen3-tts-voicedesign/" /workspace/models/qwen3-tts-voicedesign/
        else
            python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign', local_dir='/workspace/models/qwen3-tts-voicedesign')"
        fi
    else
        python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign', local_dir='/workspace/models/qwen3-tts-voicedesign')"
    fi
else
    echo "Qwen3-TTS VoiceDesign already present."
fi

# --- LTX-2.3 models (ltx-pipelines format) — needed for ltx and both modes ---
if [ "$WORKER_MODE" = "tts" ]; then
    echo "--- Skipping LTX-2.3 models (tts-only mode) ---"
else
    LTX_DIR=/workspace/models/ltx2
    mkdir -p "$LTX_DIR"

    # 1. Single-file checkpoint (~40 GB) — the core model weights
    if [ ! -f "$LTX_DIR/ltx-2-19b-dev.safetensors" ]; then
        echo "--- LTX-2 19B checkpoint (~40 GB) ---"
        python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    'Lightricks/LTX-2',
    filename='ltx-2-19b-dev.safetensors',
    local_dir='$LTX_DIR',
)
print('Checkpoint downloaded.')
"
    else
        echo "LTX-2 checkpoint already present."
    fi

    # 2. Gemma-3 1B text encoder — required by ltx-pipelines
    #    Download ALL files from Lightricks/LTX-2 (ungated) instead of
    #    google/gemma-3-1b-pt (gated, requires HF auth).
    #    Lightricks/LTX-2 has both text_encoder/ (weights) and tokenizer/ dirs.
    if [ ! -d "$LTX_DIR/gemma" ] || [ ! -f "$LTX_DIR/gemma/preprocessor_config.json" ]; then
        echo "--- Gemma-3 1B text encoder (from Lightricks/LTX-2, ungated) ---"
        mkdir -p "$LTX_DIR/gemma"
        python3 -c "
import os, shutil
from huggingface_hub import snapshot_download

gemma_dir = '$LTX_DIR/gemma'

# Download text_encoder/ and tokenizer/ from Lightricks/LTX-2 (ungated)
tmp_dir = '$LTX_DIR/_ltx2_download'
snapshot_download(
    'Lightricks/LTX-2',
    local_dir=tmp_dir,
    allow_patterns=['text_encoder/*', 'tokenizer/*'],
)
print('Downloaded text_encoder/ and tokenizer/ from Lightricks/LTX-2')

# MOVE (not copy) text_encoder files to gemma root — avoids doubling disk usage
te_dir = os.path.join(tmp_dir, 'text_encoder')
if os.path.isdir(te_dir):
    for f in os.listdir(te_dir):
        src = os.path.join(te_dir, f)
        dst = os.path.join(gemma_dir, f)
        if os.path.isfile(src):
            shutil.move(src, dst)
            print(f'  text_encoder/{f} -> gemma/')

# MOVE tokenizer files to gemma root
tok_dir = os.path.join(tmp_dir, 'tokenizer')
if os.path.isdir(tok_dir):
    for f in os.listdir(tok_dir):
        src = os.path.join(tok_dir, f)
        dst = os.path.join(gemma_dir, f)
        if os.path.isfile(src):
            shutil.move(src, dst)
            print(f'  tokenizer/{f} -> gemma/')

# Clean up temp download dir AND HuggingFace cache to reclaim disk
shutil.rmtree(tmp_dir, ignore_errors=True)
hf_cache = os.path.expanduser('~/.cache/huggingface')
if os.path.isdir(hf_cache):
    cache_size = sum(
        os.path.getsize(os.path.join(dp, fn))
        for dp, _, fns in os.walk(hf_cache) for fn in fns
    )
    shutil.rmtree(hf_cache, ignore_errors=True)
    print(f'Cleaned HF cache ({cache_size / 1e9:.1f} GB reclaimed)')
print('Gemma text encoder ready.')
"
    else
        echo "Gemma text encoder already present."
    fi
fi  # end WORKER_MODE != tts

echo ""
echo "=== Download complete ==="
du -sh /workspace/models/ltx2/ /workspace/models/qwen3-tts-voicedesign/ 2>/dev/null || true
df -h /

# ---------------------------------------------------------------------------
# Verify GPU
# ---------------------------------------------------------------------------
python3 -c "
import torch
print(f'CUDA available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)}')
    props = torch.cuda.get_device_properties(0)
    print(f'VRAM: {props.total_memory / 1e9:.1f} GB')
"

# ---------------------------------------------------------------------------
# Verify critical model files exist
# ---------------------------------------------------------------------------
echo ""
echo "=== Model verification ==="
OK=true

# Build verification list based on WORKER_MODE
VERIFY_FILES=""
if [ "$WORKER_MODE" != "ltx" ]; then
    VERIFY_FILES="$VERIFY_FILES /workspace/models/qwen3-tts-voicedesign/model.safetensors"
fi
if [ "$WORKER_MODE" != "tts" ]; then
    VERIFY_FILES="$VERIFY_FILES /workspace/models/ltx2/ltx-2-19b-dev.safetensors"
    VERIFY_FILES="$VERIFY_FILES /workspace/models/ltx2/gemma/config.json"
    VERIFY_FILES="$VERIFY_FILES /workspace/models/ltx2/gemma/preprocessor_config.json"
    VERIFY_FILES="$VERIFY_FILES /workspace/models/ltx2/gemma/tokenizer.model"
fi

for f in $VERIFY_FILES; do
    if [ -f "$f" ]; then
        echo "  OK: $f"
    else
        echo "  MISSING: $f"
        OK=false
    fi
done

if [ "$OK" = false ]; then
    echo "ERROR: Some model files are missing!"
    exit 1
fi

echo ""
echo "=== Bootstrap complete ==="
echo "To start the GPU worker:"
echo "  python /workspace/gpu_worker.py --port 8880"
