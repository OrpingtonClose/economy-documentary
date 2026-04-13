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
#   ltx  — only LTX-2.3 video models (~94 GB)
#   both — everything (default if not set)
#
# Models are pulled from Backblaze B2 (pre-cached) for speed.
# Falls back to HuggingFace if B2 credentials are not set.
#
# Disk budget (official Lightricks checkpoint + supporting components):
#   Official checkpoint (safetensors): ~46.1 GB
#   text_encoder (transformers fmt):   ~46.6 GB
#   vae + audio_vae + vocoder:         ~ 2.7 GB
#   connectors + latent_upsampler:     ~ 7.3 GB
#   transformer config (no weights):   ~ 0.0 GB
#   Qwen3-TTS VoiceDesign:            ~ 4.3 GB
#   Total models:                      ~107  GB
#   OS + software + output:            ~30   GB
#   Minimum disk required:             ~140  GB  (recommend 200+)

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
# torch 2.6+ required for diffusers 0.37 attention_dispatch compat
# Driver supports CUDA 13.0, cu124 wheels work fine
pip install --no-cache-dir \
    'torch>=2.6.0' \
    'torchvision>=0.21.0' \
    'torchaudio>=2.6.0' \
    --index-url https://download.pytorch.org/whl/cu124

# diffusers >= 0.37.0 required for LTX2Pipeline
pip install --no-cache-dir \
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

# diffusers from git — the HuggingFace model dg845/LTX-2.3-Diffusers requires
# LTX2VocoderWithBWE and updated transformer config fields (audio_cross_attn_mod,
# gated_attn, perturbed_attn) that only exist in the dev branch (>= 0.38.0.dev0).
# Stable 0.37.0 does NOT work. See huggingface/diffusers#13217.
pip install --no-cache-dir \
    git+https://github.com/huggingface/diffusers.git

# ---------------------------------------------------------------------------
# B2 model download — selective (skip duplicate formats to save ~50 GB)
# ---------------------------------------------------------------------------
B2_BUCKET="ltx2-models-orpington"

if [ -n "${B2_KEY_ID:-}" ] && [ -n "${B2_APPLICATION_KEY:-}" ]; then
    echo "=== Downloading models from B2 (selective) ==="
    export B2_APPLICATION_KEY_ID="$B2_KEY_ID"
    b2 account authorize "$B2_KEY_ID" "$B2_APPLICATION_KEY"

    # --- Qwen3-TTS VoiceDesign (~4.3 GB) — needed for tts and both modes ---
    if [ "$WORKER_MODE" = "ltx" ]; then
        echo "--- Skipping Qwen3-TTS (ltx-only mode) ---"
    elif [ ! -f /workspace/models/qwen3-tts-voicedesign/model.safetensors ]; then
        echo "--- Qwen3-TTS VoiceDesign (~4.3 GB) ---"
        mkdir -p /workspace/models/qwen3-tts-voicedesign
        pip install --no-cache-dir huggingface_hub 2>/dev/null
        # Try B2 first, fall back to HuggingFace
        b2_tts_count=$(b2 ls "b2://${B2_BUCKET}/qwen3-tts-voicedesign/" 2>/dev/null | grep -c model.safetensors || true)
        if [ "${b2_tts_count}" -gt 0 ]; then
            b2 sync --threads 8 "b2://${B2_BUCKET}/qwen3-tts-voicedesign/" /workspace/models/qwen3-tts-voicedesign/
        else
            echo "  Not in B2 (or empty), downloading from HuggingFace..."
            python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign', local_dir='/workspace/models/qwen3-tts-voicedesign')"
        fi
    else
        echo "Qwen3-TTS VoiceDesign already present."
    fi

    # --- LTX-2.3 diffusers components — needed for ltx and both modes ---
    if [ "$WORKER_MODE" = "tts" ]; then
        echo "--- Skipping LTX-2.3 models (tts-only mode, saves ~94 GB download) ---"
    else
    LTX_DIR=/workspace/models/ltx2
    mkdir -p "$LTX_DIR"

    # Root config
    echo "--- LTX-2.3 config ---"
    b2 file download "b2://${B2_BUCKET}/ltx2/model_index.json" \
        "$LTX_DIR/model_index.json" 2>/dev/null || true

    # text_encoder: ONLY transformers format (model-*.safetensors)
    # The diffusion_pytorch_model-*.safetensors are a duplicate set (~50 GB) — skip them
    echo "--- text_encoder (~46.6 GB, transformers format only) ---"
    mkdir -p "$LTX_DIR/text_encoder"
    for f in config.json generation_config.json model.safetensors.index.json; do
        if [ ! -f "$LTX_DIR/text_encoder/$f" ]; then
            b2 file download "b2://${B2_BUCKET}/ltx2/text_encoder/$f" \
                "$LTX_DIR/text_encoder/$f" 2>/dev/null || true
        fi
    done
    # Download model shards selectively (transformers format only)
    # b2 ls returns full paths like "ltx2/text_encoder/model-00001-of-00011.safetensors"
    # so we extract just the basename for matching
    (b2 ls "b2://${B2_BUCKET}/ltx2/text_encoder/" 2>/dev/null || true) | while read -r fullpath; do
        filename=$(basename "$fullpath")
        case "$filename" in
            model-*.safetensors)
                if [ ! -f "$LTX_DIR/text_encoder/$filename" ]; then
                    echo "  downloading: $filename"
                    b2 file download "b2://${B2_BUCKET}/ltx2/text_encoder/$filename" \
                        "$LTX_DIR/text_encoder/$filename"
                else
                    echo "  already have: $filename"
                fi
                ;;
            diffusion_pytorch_model*)
                echo "  skipping duplicate format: $filename"
                ;;
        esac
    done

    # Official Lightricks single-file checkpoint (~46.1 GB)
    # Downloaded from HuggingFace. gpu_worker.py uses from_single_file() to load it.
    echo "--- Official Lightricks checkpoint (~46.1 GB) ---"
    CKPT_FILE="$LTX_DIR/ltx-2.3-22b-dev.safetensors"
    if [ ! -f "$CKPT_FILE" ]; then
        # Try B2 first (faster if cached), fall back to HuggingFace
        if b2 ls "b2://${B2_BUCKET}/ltx2/ltx-2.3-22b-dev.safetensors" &>/dev/null 2>&1; then
            echo "  Downloading from B2..."
            b2 file download "b2://${B2_BUCKET}/ltx2/ltx-2.3-22b-dev.safetensors" "$CKPT_FILE"
        else
            echo "  Not in B2, downloading from HuggingFace..."
            pip install --no-cache-dir huggingface_hub 2>/dev/null
            python3 -c "from huggingface_hub import hf_hub_download; hf_hub_download('Lightricks/LTX-2.3', 'ltx-2.3-22b-dev.safetensors', local_dir='$LTX_DIR')"
        fi
    else
        echo "  Already present: $CKPT_FILE"
    fi

    # transformer config (needed by from_single_file for architecture info)
    # Small download — just the config.json, not the full weights
    echo "--- transformer config ---"
    mkdir -p "$LTX_DIR/transformer"
    if [ ! -f "$LTX_DIR/transformer/config.json" ]; then
        # Download just the config from dg845 (has the diffusers-compatible config)
        pip install --no-cache-dir huggingface_hub 2>/dev/null
        python3 -c "from huggingface_hub import hf_hub_download; hf_hub_download('dg845/LTX-2.3-Diffusers', 'transformer/config.json', local_dir='$LTX_DIR')"
    fi

    # Small components (< 3 GB each)
    for subdir in scheduler tokenizer vae audio_vae vocoder connectors latent_upsampler; do
        if b2 ls "b2://${B2_BUCKET}/ltx2/${subdir}/" &>/dev/null; then
            echo "--- ${subdir} ---"
            mkdir -p "$LTX_DIR/${subdir}"
            b2 sync --threads 8 "b2://${B2_BUCKET}/ltx2/${subdir}/" "$LTX_DIR/${subdir}/"
        fi
    done

    # --- Validate connector config ---
    # The connector must have per_modality_projections for LTX-2.3
    echo "--- Validating connector config ---"
    if ! python3 -c "
import json, sys
conn = json.load(open('$LTX_DIR/connectors/config.json'))
assert conn.get('per_modality_projections') is True, \
    f'connectors/config.json missing per_modality_projections=true (got {conn.get(\"per_modality_projections\")}). Likely stale LTX-2 weights.'
print(f'  OK: per_modality_projections={conn.get(\"per_modality_projections\")}')
" 2>&1; then
        echo "  WARNING: Connector config invalid. Re-downloading from HuggingFace..."
        rm -rf "$LTX_DIR/connectors"
        pip install --no-cache-dir huggingface_hub 2>/dev/null
        python3 -c "from huggingface_hub import snapshot_download; snapshot_download('dg845/LTX-2.3-Diffusers', local_dir='$LTX_DIR', allow_patterns='connectors/*')"
    fi
    fi  # end WORKER_MODE != tts

    echo ""
    echo "=== Download complete ==="
    du -sh /workspace/models/ltx2/ /workspace/models/qwen3-tts-voicedesign/ 2>/dev/null || true
    df -h /
else
    echo "WARNING: B2 credentials not set. Downloading from HuggingFace instead."
    pip install --no-cache-dir huggingface_hub

    python3 -c "
from huggingface_hub import snapshot_download
import os

worker_mode = os.environ.get('WORKER_MODE', 'both')

if worker_mode != 'ltx' and not os.path.exists('/workspace/models/qwen3-tts-voicedesign/model.safetensors'):
    print('Downloading Qwen3-TTS VoiceDesign from HuggingFace...')
    snapshot_download('Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign',
                      local_dir='/workspace/models/qwen3-tts-voicedesign')

if worker_mode != 'tts':
    import os
    ltx_dir = '/workspace/models/ltx2'
    ckpt = os.path.join(ltx_dir, 'ltx-2.3-22b-dev.safetensors')
    if not os.path.exists(ckpt):
        print('Downloading official Lightricks checkpoint from HuggingFace...')
        from huggingface_hub import hf_hub_download
        hf_hub_download('Lightricks/LTX-2.3', 'ltx-2.3-22b-dev.safetensors', local_dir=ltx_dir)
    if not os.path.exists(os.path.join(ltx_dir, 'model_index.json')):
        print('Downloading LTX-2.3 supporting components from HuggingFace...')
        snapshot_download('dg845/LTX-2.3-Diffusers', local_dir=ltx_dir,
                          ignore_patterns=['transformer/*.safetensors'])
"
fi

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
    VERIFY_FILES="$VERIFY_FILES /workspace/models/ltx2/model_index.json"
    VERIFY_FILES="$VERIFY_FILES /workspace/models/ltx2/text_encoder/config.json"
    VERIFY_FILES="$VERIFY_FILES /workspace/models/ltx2/text_encoder/model.safetensors.index.json"
    VERIFY_FILES="$VERIFY_FILES /workspace/models/ltx2/transformer/config.json"
    VERIFY_FILES="$VERIFY_FILES /workspace/models/ltx2/vae/config.json"
    VERIFY_FILES="$VERIFY_FILES /workspace/models/ltx2/connectors/config.json"
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
