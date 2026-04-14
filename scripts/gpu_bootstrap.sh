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
#   ltx-2.3-22b-dev.safetensors:      ~46.1 GB
#   Gemma-3 1B text encoder:          ~ 2.0 GB
#   Qwen3-TTS VoiceDesign:            ~ 4.3 GB
#   Total models:                      ~52   GB
#   OS + software + output:            ~30   GB
#   Minimum disk required:             ~85   GB  (recommend 120+)

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
# Python dependencies (install into system python — ephemeral VM)
# ---------------------------------------------------------------------------
# torch 2.6+ required for ltx-pipelines
pip install --no-cache-dir \
    'torch>=2.6.0' \
    'torchvision>=0.21.0' \
    'torchaudio>=2.6.0' \
    --index-url https://download.pytorch.org/whl/cu124

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

    # 1. Single-file checkpoint (~46 GB) — the core model weights
    if [ ! -f "$LTX_DIR/ltx-2.3-22b-dev.safetensors" ]; then
        echo "--- LTX-2.3 checkpoint (~46 GB) ---"
        python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    'Lightricks/LTX-2',
    filename='ltx-2.3-22b-dev.safetensors',
    local_dir='$LTX_DIR',
)
print('Checkpoint downloaded.')
"
    else
        echo "LTX-2.3 checkpoint already present."
    fi

    # 2. Gemma-3 1B text encoder — required by ltx-pipelines
    #    We need BOTH:
    #      - Model weights from google/gemma-3-1b-pt (model.safetensors, config.json)
    #      - Tokenizer/processor files from Lightricks/LTX-2 tokenizer/ dir
    #        (preprocessor_config.json is missing from google/gemma-3-1b-pt but
    #         ltx-core's module_ops_from_gemma_root() requires it)
    if [ ! -d "$LTX_DIR/gemma" ] || [ ! -f "$LTX_DIR/gemma/preprocessor_config.json" ]; then
        echo "--- Gemma-3 1B text encoder (weights + tokenizer) ---"
        mkdir -p "$LTX_DIR/gemma"
        python3 -c "
from huggingface_hub import snapshot_download, hf_hub_download

# 1. Download model weights + basic tokenizer from google/gemma-3-1b-pt
snapshot_download(
    'google/gemma-3-1b-pt',
    local_dir='$LTX_DIR/gemma',
)
print('Gemma weights downloaded.')

# 2. Overlay Lightricks tokenizer files (adds preprocessor_config.json etc.)
for fname in ['preprocessor_config.json', 'processor_config.json', 'chat_template.jinja']:
    try:
        hf_hub_download(
            'Lightricks/LTX-2',
            filename=f'tokenizer/{fname}',
            local_dir='$LTX_DIR/gemma',
            local_dir_use_symlinks=False,
        )
        print(f'Downloaded tokenizer/{fname}')
    except Exception as e:
        print(f'Warning: could not download tokenizer/{fname}: {e}')

# Move files from tokenizer/ subdirectory to gemma root if needed
import os, shutil
tok_subdir = os.path.join('$LTX_DIR/gemma', 'tokenizer')
if os.path.isdir(tok_subdir):
    for f in os.listdir(tok_subdir):
        src = os.path.join(tok_subdir, f)
        dst = os.path.join('$LTX_DIR/gemma', f)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
            print(f'Copied {f} to gemma root')
    shutil.rmtree(tok_subdir, ignore_errors=True)

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
    VERIFY_FILES="$VERIFY_FILES /workspace/models/ltx2/ltx-2.3-22b-dev.safetensors"
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
