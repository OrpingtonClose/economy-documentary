#!/usr/bin/env bash
# LTX-Video Worker Bootstrap for Vast.ai VMs.
#
# Provisions a single Vast.ai GPU VM as an LTX-Video render worker.
# Installs two supervised services:
#
#   1. infra-agent  (:29230) — guardian, destroys the VM on idle/lifetime
#   2. ltx-video-worker (:29232) — /video/render + /health/vram
#
# Runs infra-agent via `strands_agents.infra_agent.runner` and the video
# worker via `strands_agents.ltx_video_worker.runner`. Both services
# live in the same Python venv so they share the telemetry module.
#
# Sizing policy (see docs/strands-migration/lessons/gpu-sizing.md):
# first VMs overprovision — H200 / ~500 GB disk. Optimise downward
# after observing real peak VRAM / disk usage across a handful of
# successful runs.
#
# Required env:
#   WORKER_ID                Registry worker id (e.g. video-h200-01)
#   VAST_INSTANCE_ID         From Vast.ai. Injected by provisioner.
#   VAST_AI_API_KEY          For the guardian's self-destroy call.
#
# Optional env:
#   PLAYGROUND_BACKEND_URL      Registry backend. If unset, worker runs
#                               unregistered (smoke-test / dev mode).
#   WORKER_VRAM_GB              Defaults to `nvidia-smi` probe.
#   GUARDIAN_IDLE_SECONDS       Defaults to 900 (see infra_agent).
#   GUARDIAN_MAX_LIFETIME_SECONDS Defaults to 14400.
#   REPO_URL                    Git URL; defaults to the OrpingtonClose mirror.
#   REPO_REF                    Branch/tag/sha; defaults to main.

set -euo pipefail

# ---------------------------------------------------------------------------
# Required env sanity
# ---------------------------------------------------------------------------
: "${WORKER_ID:?WORKER_ID must be set (e.g. video-h200-01)}"
: "${VAST_INSTANCE_ID:?VAST_INSTANCE_ID must be set (guardian cannot self-destroy without it)}"
: "${VAST_AI_API_KEY:?VAST_AI_API_KEY must be set}"
PLAYGROUND_BACKEND_URL="${PLAYGROUND_BACKEND_URL:-}"

REPO_URL="${REPO_URL:-https://github.com/OrpingtonClose/economy-documentary.git}"
REPO_REF="${REPO_REF:-main}"

# Lightricks/LTX-2 monorepo: where the official
# ``ltx_pipelines.ti2vid_one_stage`` BASIC CLI lives. Pinned to a
# specific commit so the engine subprocess wrapper sees a known argv
# shape. Override LTX2_REPO_REF if a security patch ships and we need
# to bump.
LTX2_REPO_URL="${LTX2_REPO_URL:-https://github.com/Lightricks/LTX-2.git}"
LTX2_REPO_REF="${LTX2_REPO_REF:-41d924371612b692c0fd1e4d9d94c3dfb3c02cb3}"
LTX2_REPO_DIR="${LTX_VIDEO_LTX2_ROOT:-/opt/ltx-2-repo}"

WORK_DIR="/opt/economy-documentary"
VENV_DIR="/opt/economy-documentary-venv"
STATE_DIR="/var/lib/ltx-video-worker"
LOG_DIR="/var/log/ltx-video-worker"

# The Lightricks/LTX-2 README requires Python ≥3.10 and ships with
# torch ~2.7 / cu129 wheels. We use uv to manage the monorepo's venv
# because that's what their pyproject.toml expects (workspace install).
LTX2_PYTHON_VERSION="${LTX2_PYTHON_VERSION:-3.12}"

# HF model snapshots. Both pins MUST match
# ``server/strands_agents/ltx_video_worker/_model_pin.py`` exactly —
# the engine startup runs ``verify_pin`` against these directories
# and refuses to render on a hash mismatch.
LTX_VIDEO_HF_REPO="${LTX_VIDEO_HF_REPO:-Lightricks/LTX-2.3}"
LTX_VIDEO_HF_REVISION="${LTX_VIDEO_HF_REVISION:-76730e634e70a28f4e8d51f5e29c08e40e2d8e74}"
# The official ``google/gemma-3-12b-it-qat-q4_0-unquantized`` repo is
# gated. Lightricks publishes a byte-identical, non-gated mirror under
# their own org — used here so the bootstrap doesn't need an HF token.
LTX_VIDEO_GEMMA_HF_REPO="${LTX_VIDEO_GEMMA_HF_REPO:-Lightricks/gemma-3-12b-it-qat-q4_0-unquantized}"
LTX_VIDEO_GEMMA_HF_REVISION="${LTX_VIDEO_GEMMA_HF_REVISION:-d62fe4f1995ade703b49a0f3c0d0f161237ef437}"

echo "=== LTX-Video worker bootstrap: WORKER_ID=$WORKER_ID ==="
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
apt-get update
apt-get install -y --no-install-recommends \
    git \
    python3 \
    python3-pip \
    python3-venv \
    ffmpeg \
    curl \
    ca-certificates
rm -rf /var/lib/apt/lists/*

mkdir -p "$STATE_DIR" "$LOG_DIR"

# ---------------------------------------------------------------------------
# Checkout repo
#
# `git clone --branch` does not accept commit SHAs, so we always go through
# the fetch+checkout FETCH_HEAD path which does. Keeps REPO_REF uniformly
# "branch, tag, or SHA".
# ---------------------------------------------------------------------------
if [ ! -d "$WORK_DIR/.git" ]; then
    git clone --no-checkout "$REPO_URL" "$WORK_DIR"
fi
git -C "$WORK_DIR" fetch --depth 1 origin "$REPO_REF"
git -C "$WORK_DIR" checkout FETCH_HEAD

# ---------------------------------------------------------------------------
# Worker venv (light: FastAPI surface + huggingface_hub for snapshot
# pre-downloads and verify_pin hashing). The actual LTX-2.3 inference
# runs in a separate, uv-managed venv inside the LTX-2 monorepo so the
# worker's import graph stays clean.
# ---------------------------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip setuptools wheel
pip install fastapi uvicorn requests numpy "huggingface_hub>=0.24"

# ---------------------------------------------------------------------------
# uv (Python package manager) + Lightricks/LTX-2 monorepo
#
# LTX-2.3 ships only via the Lightricks/LTX-2 monorepo's
# ``ltx_pipelines.ti2vid_one_stage`` BASIC pipeline. ``uv sync`` resolves
# the workspace's pyproject.toml, including PyTorch 2.7 / cu129
# wheels, into a self-contained venv at ``$LTX2_REPO_DIR/.venv``.
# The worker subprocess invokes that interpreter via
# ``LTX_VIDEO_LTX2_PYTHON``.
# ---------------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv (Astral) ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installer drops the binary at ~/.local/bin or /root/.local/bin
    export PATH="$HOME/.local/bin:/root/.local/bin:$PATH"
fi

if [ ! -d "$LTX2_REPO_DIR/.git" ]; then
    git clone "$LTX2_REPO_URL" "$LTX2_REPO_DIR"
fi
git -C "$LTX2_REPO_DIR" fetch origin "$LTX2_REPO_REF"
git -C "$LTX2_REPO_DIR" checkout "$LTX2_REPO_REF"

(
    cd "$LTX2_REPO_DIR"
    # ``uv sync --frozen`` would error if the lockfile is stale; we
    # use a regular sync so a fresh clone resolves transitively.
    # ``--python`` pins the interpreter so we never silently fall
    # back to whatever ``python3`` is on PATH.
    uv python install "$LTX2_PYTHON_VERSION"
    uv sync --python "$LTX2_PYTHON_VERSION"
)

LTX_VIDEO_LTX2_PYTHON="$LTX2_REPO_DIR/.venv/bin/python"
if [ ! -x "$LTX_VIDEO_LTX2_PYTHON" ]; then
    echo "ERROR: LTX-2 venv interpreter missing at $LTX_VIDEO_LTX2_PYTHON" >&2
    exit 1
fi

echo "ltx-2 venv interpreter: $LTX_VIDEO_LTX2_PYTHON"
"$LTX_VIDEO_LTX2_PYTHON" -c "import ltx_pipelines.ti2vid_one_stage; print('ltx_pipelines.ti2vid_one_stage importable')" || {
    echo "ERROR: ltx_pipelines.ti2vid_one_stage not importable from $LTX_VIDEO_LTX2_PYTHON" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Pre-download pinned model weights (snapshots, not symlinks-only).
#
# ``HF_ENDPOINT`` defaults to hf-mirror.com because some Vast.ai
# datacenter IP blocks have no TCP egress to huggingface.co. The mirror
# serves identical bytes for these manifests. Both repos are public /
# non-gated (LTX-2.3 + Lightricks' Gemma-3 mirror), so no HF_TOKEN is
# required — but we forward it if the operator chooses to set one.
# ---------------------------------------------------------------------------
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
HF_HUB_ENABLE_HF_TRANSFER="${HF_HUB_ENABLE_HF_TRANSFER:-0}"
export HF_ENDPOINT HF_HUB_ENABLE_HF_TRANSFER
if [ -n "${HF_TOKEN:-}" ]; then
    export HF_TOKEN
fi

echo "Pre-downloading $LTX_VIDEO_HF_REPO at $LTX_VIDEO_HF_REVISION ..."
"$VENV_DIR/bin/python" - <<PY
import os
from huggingface_hub import snapshot_download
# BASIC pipeline reads the full base checkpoint only; distilled
# checkpoints / spatial-temporal upscalers / distilled LoRAs are not
# loaded by ``ti2vid_one_stage`` so we skip downloading them.
snapshot_download(
    repo_id="$LTX_VIDEO_HF_REPO",
    revision="$LTX_VIDEO_HF_REVISION",
    allow_patterns=[
        "ltx-2.3-22b-dev.safetensors",
    ],
    token=os.environ.get("HF_TOKEN"),
)
PY

echo "Pre-downloading $LTX_VIDEO_GEMMA_HF_REPO at $LTX_VIDEO_GEMMA_HF_REVISION ..."
"$VENV_DIR/bin/python" - <<PY
import os
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="$LTX_VIDEO_GEMMA_HF_REPO",
    revision="$LTX_VIDEO_GEMMA_HF_REVISION",
    token=os.environ.get("HF_TOKEN"),
)
PY

# ---------------------------------------------------------------------------
# Resolve advertised endpoint + VRAM
# ---------------------------------------------------------------------------
PUBLIC_IPADDR="${PUBLIC_IPADDR:-}"
if [ -z "$PUBLIC_IPADDR" ]; then
    PUBLIC_IPADDR="$(curl -fsS https://api.ipify.org || echo 127.0.0.1)"
fi
export PUBLIC_IPADDR
WORKER_ENDPOINT_URL="${WORKER_ENDPOINT_URL:-http://${PUBLIC_IPADDR}:29232}"

if [ -z "${WORKER_VRAM_GB:-}" ]; then
    if command -v nvidia-smi >/dev/null 2>&1; then
        WORKER_VRAM_GB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1 | awk '{printf "%d\n", $1/1024}')"
    else
        WORKER_VRAM_GB="1"
    fi
fi

echo "WORKER_ENDPOINT_URL=$WORKER_ENDPOINT_URL"
echo "WORKER_VRAM_GB=$WORKER_VRAM_GB"

# ---------------------------------------------------------------------------
# Process supervision
#
# Identical shape to scripts/qwen3_tts_worker_bootstrap.sh — systemd
# when the VM has it, nohup supervisor fallback for plain Docker
# containers (Vast.ai nvidia/cuda images). Log paths, env shape, and
# module entry-points are the only things that differ between the two
# worker bootstraps.
# ---------------------------------------------------------------------------

# HF_ENDPOINT was set above for the pre-download step. Reuse the same
# value for the long-running services so any cache-miss reload (e.g.
# after eviction) hits the same mirror.

INFRA_AGENT_ENV=(
    "PYTHONUNBUFFERED=1"
    "PYTHONPATH=$WORK_DIR/server"
    "WORKER_ID=$WORKER_ID"
    "VAST_INSTANCE_ID=$VAST_INSTANCE_ID"
    "VAST_AI_API_KEY=$VAST_AI_API_KEY"
    "PLAYGROUND_BACKEND_URL=$PLAYGROUND_BACKEND_URL"
    "GUARDIAN_IDLE_SECONDS=${GUARDIAN_IDLE_SECONDS:-900}"
    "GUARDIAN_MAX_LIFETIME_SECONDS=${GUARDIAN_MAX_LIFETIME_SECONDS:-14400}"
    "HF_ENDPOINT=$HF_ENDPOINT"
    "HF_HUB_ENABLE_HF_TRANSFER=$HF_HUB_ENABLE_HF_TRANSFER"
)

WORKER_ENV=(
    "PYTHONUNBUFFERED=1"
    "PYTHONPATH=$WORK_DIR/server"
    "WORKER_ID=$WORKER_ID"
    "WORKER_ENDPOINT_URL=$WORKER_ENDPOINT_URL"
    "WORKER_VRAM_GB=$WORKER_VRAM_GB"
    "PLAYGROUND_BACKEND_URL=$PLAYGROUND_BACKEND_URL"
    "INFRA_AGENT_BUMP_URL=http://127.0.0.1:29230/infra/bump"
    "PUBLIC_IPADDR=$PUBLIC_IPADDR"
    "HF_ENDPOINT=$HF_ENDPOINT"
    "HF_HUB_ENABLE_HF_TRANSFER=$HF_HUB_ENABLE_HF_TRANSFER"
    "LTX_VIDEO_LTX2_ROOT=$LTX2_REPO_DIR"
    "LTX_VIDEO_LTX2_CWD=$LTX2_REPO_DIR"
    "LTX_VIDEO_LTX2_PYTHON=$LTX_VIDEO_LTX2_PYTHON"
)

# Detect systemd-as-PID-1. The canonical signal is the /run/systemd/system
# directory — it exists iff systemd booted the machine. Do NOT use
# `systemctl is-system-running --quiet`: it returns non-zero on the
# "degraded" state (≥1 failed unit), which is common on healthy
# production hosts and would spuriously demote us to the nohup fallback.
HAS_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    HAS_SYSTEMD=1
fi

if [ "$HAS_SYSTEMD" = "1" ]; then
    echo "systemd detected — installing unit files"

    infra_env_lines=""
    for kv in "${INFRA_AGENT_ENV[@]}"; do
        infra_env_lines+="Environment=$kv"$'\n'
    done
    worker_env_lines=""
    for kv in "${WORKER_ENV[@]}"; do
        worker_env_lines+="Environment=$kv"$'\n'
    done

    cat > /etc/systemd/system/infra-agent.service <<UNIT
[Unit]
Description=Per-VM infrastructure guardian (idle/lifetime watchdog)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$WORK_DIR
${infra_env_lines}ExecStart=$VENV_DIR/bin/python -m strands_agents.infra_agent.runner
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_DIR/infra-agent.log
StandardError=append:$LOG_DIR/infra-agent.log

[Install]
WantedBy=multi-user.target
UNIT

    cat > /etc/systemd/system/ltx-video-worker.service <<UNIT
[Unit]
Description=LTX-Video worker (per-scene render)
After=infra-agent.service
Requires=infra-agent.service

[Service]
Type=simple
WorkingDirectory=$WORK_DIR
${worker_env_lines}ExecStart=$VENV_DIR/bin/python -m strands_agents.ltx_video_worker.runner
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_DIR/ltx-video-worker.log
StandardError=append:$LOG_DIR/ltx-video-worker.log

[Install]
WantedBy=multi-user.target
UNIT

    systemctl daemon-reload
    systemctl enable --now infra-agent.service
    systemctl enable --now ltx-video-worker.service
else
    echo "systemd not available — falling back to nohup supervisor"

    mkdir -p /var/run
    SUPERVISOR_LOG="$LOG_DIR/supervisor.log"

    # Values may contain shell metacharacters (quotes, $, &, etc.). Each
    # KEY=VALUE is shell-escaped via printf %q so the generated supervisor
    # script survives arbitrary secret contents.
    INFRA_CMD=""
    for kv in "${INFRA_AGENT_ENV[@]}"; do
        key="${kv%%=*}"
        value="${kv#*=}"
        INFRA_CMD+="export ${key}=$(printf '%q' "$value"); "
    done
    INFRA_CMD+="exec $VENV_DIR/bin/python -m strands_agents.infra_agent.runner"

    WORKER_CMD=""
    for kv in "${WORKER_ENV[@]}"; do
        key="${kv%%=*}"
        value="${kv#*=}"
        WORKER_CMD+="export ${key}=$(printf '%q' "$value"); "
    done
    WORKER_CMD+="exec $VENV_DIR/bin/python -m strands_agents.ltx_video_worker.runner"

    cat > /usr/local/bin/ltx-video-supervisor.sh <<SUP
#!/usr/bin/env bash
# Minimal non-systemd supervisor for infra-agent + ltx-video-worker.
# Restarts either service if its PID disappears. Logs supervision events
# to $SUPERVISOR_LOG.
set -u

WORK_DIR="$WORK_DIR"
LOG_DIR="$LOG_DIR"
INFRA_PID=/var/run/infra-agent.pid
WORKER_PID=/var/run/ltx-video-worker.pid

start_infra() {
    cd "\$WORK_DIR"
    nohup bash -c '$INFRA_CMD' \\
        >> "\$LOG_DIR/infra-agent.log" 2>&1 &
    echo \$! > "\$INFRA_PID"
    echo "[\$(date -u +%FT%TZ)] started infra-agent pid=\$(cat \$INFRA_PID)" \\
        >> "$SUPERVISOR_LOG"
}

start_worker() {
    cd "\$WORK_DIR"
    nohup bash -c '$WORKER_CMD' \\
        >> "\$LOG_DIR/ltx-video-worker.log" 2>&1 &
    echo \$! > "\$WORKER_PID"
    echo "[\$(date -u +%FT%TZ)] started ltx-video-worker pid=\$(cat \$WORKER_PID)" \\
        >> "$SUPERVISOR_LOG"
}

is_alive() {
    local pid_file="\$1"
    [ -f "\$pid_file" ] || return 1
    local pid
    pid="\$(cat "\$pid_file" 2>/dev/null || true)"
    [ -n "\$pid" ] || return 1
    kill -0 "\$pid" 2>/dev/null
}

start_infra
# Give the guardian a moment to bind :29230 before the worker tries to bump it
sleep 2
start_worker

while true; do
    is_alive "\$INFRA_PID" || start_infra
    is_alive "\$WORKER_PID" || start_worker
    sleep 5
done
SUP
    chmod +x /usr/local/bin/ltx-video-supervisor.sh

    nohup /usr/local/bin/ltx-video-supervisor.sh >> "$SUPERVISOR_LOG" 2>&1 &
    echo $! > /var/run/ltx-video-supervisor.pid
    echo "supervisor pid=$(cat /var/run/ltx-video-supervisor.pid)"
fi

# ---------------------------------------------------------------------------
# Quick readiness probe
# ---------------------------------------------------------------------------
echo "Waiting for infra-agent health..."
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:29230/health" >/dev/null 2>&1; then
        echo "infra-agent healthy"
        break
    fi
    sleep 1
done

echo "Waiting for ltx-video-worker health..."
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:29232/health" >/dev/null 2>&1; then
        echo "ltx-video-worker healthy"
        break
    fi
    sleep 1
done

echo "=== Bootstrap complete. Guardian will self-destroy after ${GUARDIAN_IDLE_SECONDS:-900}s idle or ${GUARDIAN_MAX_LIFETIME_SECONDS:-14400}s lifetime. ==="
