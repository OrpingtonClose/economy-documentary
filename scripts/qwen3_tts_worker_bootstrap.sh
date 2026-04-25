#!/usr/bin/env bash
# Qwen3-TTS Worker Bootstrap for Vast.ai VMs.
#
# Provisions a single Vast.ai GPU VM as a Qwen3-TTS worker pinned to
# one voice. Installs two supervised services:
#
#   1. infra-agent  (:29230) — guardian, destroys the VM on idle/lifetime
#   2. qwen3-tts-worker (:29231) — /tts/render + /health/vram
#
# Runs infra-agent via `strands_agents.infra_agent.runner` and the TTS
# worker via `strands_agents.qwen3_tts_worker.runner`. Both services
# live in the same Python venv so they share the telemetry module.
#
# One voice per VM is a hard invariant: WORKER_VOICE_ID is set at boot
# time here and never changes. Running this script again with a
# different WORKER_VOICE_ID on the same VM is an operator error — the
# playground registry will reject the re-registration.
#
# Required env:
#   WORKER_ID                Registry worker id (e.g. tts-alex-01)
#   WORKER_VOICE_ID          Pinned voice id (e.g. alex)
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
: "${WORKER_ID:?WORKER_ID must be set (e.g. tts-alex-01)}"
: "${WORKER_VOICE_ID:?WORKER_VOICE_ID must be set (one voice per VM)}"
: "${VAST_INSTANCE_ID:?VAST_INSTANCE_ID must be set (guardian cannot self-destroy without it)}"
: "${VAST_AI_API_KEY:?VAST_AI_API_KEY must be set}"
PLAYGROUND_BACKEND_URL="${PLAYGROUND_BACKEND_URL:-}"

REPO_URL="${REPO_URL:-https://github.com/OrpingtonClose/economy-documentary.git}"
REPO_REF="${REPO_REF:-main}"

WORK_DIR="/opt/economy-documentary"
VENV_DIR="/opt/economy-documentary-venv"
STATE_DIR="/var/lib/qwen3-tts-worker"
LOG_DIR="/var/log/qwen3-tts-worker"

echo "=== Qwen3-TTS worker bootstrap: WORKER_ID=$WORKER_ID VOICE=$WORKER_VOICE_ID ==="
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
    libsndfile1 \
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
# Python venv + server deps
#
# The worker + guardian only need a narrow slice of server/ (strands_agents
# subpackages). Rather than install the whole `server` package (which would
# pull in google-adk, litellm, agentops, etc. that we do not use here), we
# install the runtime deps directly and point PYTHONPATH at server/. This
# keeps the VM boot fast and keeps the dep surface minimal.
# ---------------------------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip setuptools wheel
pip install fastapi uvicorn requests soundfile numpy opentimelineio

# Real Qwen3-TTS deps. ``qwen-tts`` pulls in transformers and the
# Qwen3-TTS-Tokenizer-12Hz model loader. ``torch`` is installed first
# with a CUDA-12.1 wheel (the index URL works on every Vast.ai NVIDIA
# image we've tested). ``flash-attn`` is best-effort — if the build
# wheel is unavailable for the running torch/CUDA combo, the engine
# falls back to ``attn_implementation=eager`` automatically.
TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu121}"
pip install --index-url "$TORCH_INDEX_URL" torch torchvision torchaudio || \
    pip install torch torchvision torchaudio
pip install qwen-tts transformers accelerate
pip install --no-build-isolation flash-attn || \
    echo "warning: flash-attn install failed, falling back to eager attention"

# ---------------------------------------------------------------------------
# Resolve advertised endpoint + VRAM
# ---------------------------------------------------------------------------
PUBLIC_IPADDR="${PUBLIC_IPADDR:-}"
if [ -z "$PUBLIC_IPADDR" ]; then
    PUBLIC_IPADDR="$(curl -fsS https://api.ipify.org || echo 127.0.0.1)"
fi
export PUBLIC_IPADDR
WORKER_ENDPOINT_URL="${WORKER_ENDPOINT_URL:-http://${PUBLIC_IPADDR}:29231}"

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
# Two paths, detected at runtime:
#
#   * systemd-capable VM  — write units, `systemctl enable --now` them.
#     Survives reboots, restarts on failure automatically.
#
#   * plain Docker container (Vast.ai default `nvidia/cuda` image runs
#     with an SSH wrapper as PID 1, not systemd) — fall back to a
#     small supervisor loop. Each service is launched under `nohup`
#     with its PID written to /var/run/*.pid and stdout/stderr tee'd
#     to the same log files the systemd path uses. A companion watchdog
#     process re-launches either service if its PID disappears.
#
# The on-disk shape (log paths, env, ExecStart args) is identical
# across the two paths so debugging commands work the same.
# ---------------------------------------------------------------------------

INFRA_AGENT_ENV=(
    "PYTHONUNBUFFERED=1"
    "PYTHONPATH=$WORK_DIR/server"
    "WORKER_ID=$WORKER_ID"
    "VAST_INSTANCE_ID=$VAST_INSTANCE_ID"
    "VAST_AI_API_KEY=$VAST_AI_API_KEY"
    "PLAYGROUND_BACKEND_URL=$PLAYGROUND_BACKEND_URL"
    "GUARDIAN_IDLE_SECONDS=${GUARDIAN_IDLE_SECONDS:-900}"
    "GUARDIAN_MAX_LIFETIME_SECONDS=${GUARDIAN_MAX_LIFETIME_SECONDS:-14400}"
)

WORKER_ENV=(
    "PYTHONUNBUFFERED=1"
    "PYTHONPATH=$WORK_DIR/server"
    "WORKER_ID=$WORKER_ID"
    "WORKER_VOICE_ID=$WORKER_VOICE_ID"
    "WORKER_ENDPOINT_URL=$WORKER_ENDPOINT_URL"
    "WORKER_VRAM_GB=$WORKER_VRAM_GB"
    "PLAYGROUND_BACKEND_URL=$PLAYGROUND_BACKEND_URL"
    "INFRA_AGENT_BUMP_URL=http://127.0.0.1:29230/infra/bump"
    "PUBLIC_IPADDR=$PUBLIC_IPADDR"
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

    cat > /etc/systemd/system/qwen3-tts-worker.service <<UNIT
[Unit]
Description=Qwen3-TTS worker (one voice per VM)
After=infra-agent.service
Requires=infra-agent.service

[Service]
Type=simple
WorkingDirectory=$WORK_DIR
${worker_env_lines}ExecStart=$VENV_DIR/bin/python -m strands_agents.qwen3_tts_worker.runner
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_DIR/qwen3-tts-worker.log
StandardError=append:$LOG_DIR/qwen3-tts-worker.log

[Install]
WantedBy=multi-user.target
UNIT

    systemctl daemon-reload
    systemctl enable --now infra-agent.service
    systemctl enable --now qwen3-tts-worker.service
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
    WORKER_CMD+="exec $VENV_DIR/bin/python -m strands_agents.qwen3_tts_worker.runner"

    cat > /usr/local/bin/qwen3-tts-supervisor.sh <<SUP
#!/usr/bin/env bash
# Minimal non-systemd supervisor for infra-agent + qwen3-tts-worker.
# Restarts either service if its PID disappears. Logs supervision events
# to $SUPERVISOR_LOG.
set -u

WORK_DIR="$WORK_DIR"
LOG_DIR="$LOG_DIR"
INFRA_PID=/var/run/infra-agent.pid
WORKER_PID=/var/run/qwen3-tts-worker.pid

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
        >> "\$LOG_DIR/qwen3-tts-worker.log" 2>&1 &
    echo \$! > "\$WORKER_PID"
    echo "[\$(date -u +%FT%TZ)] started qwen3-tts-worker pid=\$(cat \$WORKER_PID)" \\
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
    chmod +x /usr/local/bin/qwen3-tts-supervisor.sh

    nohup /usr/local/bin/qwen3-tts-supervisor.sh >> "$SUPERVISOR_LOG" 2>&1 &
    echo $! > /var/run/qwen3-tts-supervisor.pid
    echo "supervisor pid=$(cat /var/run/qwen3-tts-supervisor.pid)"
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

echo "Waiting for qwen3-tts-worker health..."
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:29231/health" >/dev/null 2>&1; then
        echo "qwen3-tts-worker healthy"
        break
    fi
    sleep 1
done

echo "=== Bootstrap complete. Guardian will self-destroy after ${GUARDIAN_IDLE_SECONDS:-900}s idle or ${GUARDIAN_MAX_LIFETIME_SECONDS:-14400}s lifetime. ==="
