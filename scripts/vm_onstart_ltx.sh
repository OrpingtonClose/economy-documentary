#!/usr/bin/env bash
# VM onstart for LTX-2.3 video workers.
# Sets up the environment so the VM agent can generate video clips.
set -e
cd /workspace

apt-get update -qq
apt-get install -y -qq git curl wget ffmpeg libsndfile1 python3-pip python3-venv

# Install uv (Astral package manager) for the LTX-2 monorepo
if ! test -f /root/.local/bin/uv; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Clone Lightricks/LTX-2 monorepo (pinned commit for stability)
LTX2_REPO_URL="https://github.com/Lightricks/LTX-2.git"
LTX2_REPO_REF="41d924371612b692c0fd1e4d9d94c3dfb3c02cb3"
LTX2_DIR="/workspace/ltx-2-repo"

if [ ! -d "$LTX2_DIR/.git" ]; then
    git clone "$LTX2_REPO_URL" "$LTX2_DIR"
fi
git -C "$LTX2_DIR" fetch origin "$LTX2_REPO_REF"
git -C "$LTX2_DIR" checkout "$LTX2_REPO_REF"

# Sync dependencies into the monorepo's venv
cd "$LTX2_DIR"
/root/.local/bin/uv python install 3.12
/root/.local/bin/uv sync --python 3.12
cd /workspace

/workspace/ltx-2-repo/.venv/bin/python -c "import ltx_pipelines.ti2vid_one_stage; print('ltx_pipelines OK')" || {
    echo "ERROR: ltx_pipelines not importable" >&2
    exit 1
}

# Pre-download LTX-2.3 video model weights.
mkdir -p /workspace/models/ltx23
/workspace/ltx-2-repo/.venv/bin/python -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    repo_id='Lightricks/LTX-2.3',
    revision='76730e634e70a28f4e8d51f5e29c08e40e2d8e74',
    filename='ltx-2.3-22b-dev.safetensors',
    local_dir='/workspace/models/ltx23',
)
print('LTX checkpoint ready')
"

# The text-encoder weights required by LTX-2.3 are NOT pre-downloaded here.
# The VM agent downloads them on first use if missing, using its bash tool.
# All reasoning on this VM is performed by deepseek-v4-flash.

# Clone the documentary repo for runner scripts (idempotent)
if [ ! -d /workspace/repo/.git ]; then
    git clone --depth 1 --branch strands-migration \
        https://github.com/OrpingtonClose/economy-documentary.git repo
fi

# Write DeepSeek API key for the VM agent
echo "$1" > /workspace/.deepseek_key

# Write Vast.ai credentials for the self-destruct monitor
echo "$2" > /workspace/.vast_api_key

# Start the VM agent
nohup python repo/scripts/vm_agent.py --port 8880 \
    > /workspace/agent.log 2>&1 &

echo "started"
