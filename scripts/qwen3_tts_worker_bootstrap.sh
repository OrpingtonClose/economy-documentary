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
# ---------------------------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip setuptools wheel

# Install the server package (editable, so the unit of deploy is the repo).
pip install -e "$WORK_DIR/server"
pip install fastapi uvicorn requests

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
# Systemd units
# ---------------------------------------------------------------------------
cat > /etc/systemd/system/infra-agent.service <<UNIT
[Unit]
Description=Per-VM infrastructure guardian (idle/lifetime watchdog)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$WORK_DIR
Environment=PYTHONUNBUFFERED=1
Environment=WORKER_ID=$WORKER_ID
Environment=VAST_INSTANCE_ID=$VAST_INSTANCE_ID
Environment=VAST_AI_API_KEY=$VAST_AI_API_KEY
Environment=PLAYGROUND_BACKEND_URL=$PLAYGROUND_BACKEND_URL
Environment=GUARDIAN_IDLE_SECONDS=${GUARDIAN_IDLE_SECONDS:-900}
Environment=GUARDIAN_MAX_LIFETIME_SECONDS=${GUARDIAN_MAX_LIFETIME_SECONDS:-14400}
ExecStart=$VENV_DIR/bin/python -m strands_agents.infra_agent.runner
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
Environment=PYTHONUNBUFFERED=1
Environment=WORKER_ID=$WORKER_ID
Environment=WORKER_VOICE_ID=$WORKER_VOICE_ID
Environment=WORKER_ENDPOINT_URL=$WORKER_ENDPOINT_URL
Environment=WORKER_VRAM_GB=$WORKER_VRAM_GB
Environment=PLAYGROUND_BACKEND_URL=$PLAYGROUND_BACKEND_URL
Environment=INFRA_AGENT_BUMP_URL=http://127.0.0.1:29230/infra/bump
Environment=PUBLIC_IPADDR=$PUBLIC_IPADDR
ExecStart=$VENV_DIR/bin/python -m strands_agents.qwen3_tts_worker.runner
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
