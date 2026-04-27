#!/usr/bin/env bash
# Central Unit VM Bootstrap Script
# Run this on a freshly provisioned Vast.ai VM to set up the documentary
# pipeline's orchestrator (FastAPI backend + Next.js dashboard).
#
# This is the "brain" VM — cheap CPU-only, always-on, public URL.
# GPU workers (video, TTS) run on separate VMs and connect here.
#
# Usage:
#   bash central_bootstrap.sh
#
# Required env vars (set in Vast.ai template or pass on CLI):
#   OPENAI_API_KEY        — OpenRouter API key for LLM agents
#   OPENAI_API_BASE       — OpenRouter base URL (https://openrouter.ai/api/v1)
#   ADK_MODEL             — Model name (e.g. openai/z-ai/glm-5.1)
#
# Optional env vars:
#   B2_KEY_ID             — Backblaze B2 key for artifact caching
#   B2_APPLICATION_KEY    — Backblaze B2 app key
#   VAST_API_KEY          — For provisioning GPU workers from the dashboard
#   DASHSCOPE_API_KEY     — For Qwen visual QA
#   GPU_WORKER_URL        — Pre-configured GPU worker URL (if already running)
#   TTS_WORKER_URL        — Pre-configured TTS worker URL (if already running)
#   VIDEO_WORKER_URLS     — Comma-separated GPU worker URLs for parallel gen
#   PRODUCTION_ORCHESTRATOR — Set to "1" to enable LLM-driven planning orchestrator
#                             for video production (default: deterministic callback)
#
# Ports exposed:
#   3000 — Next.js dashboard (user-facing)
#   8000 — FastAPI backend (AG-UI + dashboard API)
#
# Disk budget:
#   ~2 GB (repo + node_modules + Python deps)
#   No GPU models needed — this is CPU-only orchestration.

set -euo pipefail

echo "=== Central Unit Bootstrap: Documentary Pipeline ==="
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Hostname: $(hostname)"
df -h /

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
apt-get update && apt-get install -y \
    ffmpeg \
    git \
    curl \
    python3-pip \
    python3-venv \
    supervisor \
    nginx \
    jq

# ---------------------------------------------------------------------------
# Node.js 20 LTS (for Next.js frontend)
# ---------------------------------------------------------------------------
if ! command -v node &>/dev/null || [[ "$(node -v)" != v20* ]]; then
    echo "=== Installing Node.js 20 LTS ==="
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
fi
echo "Node.js: $(node -v)"
echo "npm: $(npm -v)"

# ---------------------------------------------------------------------------
# Clone / update repository
# ---------------------------------------------------------------------------
REPO_DIR="/workspace/economy-documentary"
if [ -d "$REPO_DIR/.git" ]; then
    echo "=== Updating existing repo ==="
    cd "$REPO_DIR"
    git pull origin main
else
    echo "=== Cloning repository ==="
    git clone https://github.com/OrpingtonClose/economy-documentary.git "$REPO_DIR"
    cd "$REPO_DIR"
fi

# ---------------------------------------------------------------------------
# Python dependencies (server)
# ---------------------------------------------------------------------------
echo "=== Installing Python dependencies ==="
cd "$REPO_DIR/server"
pip install --no-cache-dir \
    'google-adk>=1.5.0' \
    'ag-ui-adk>=0.5.0' \
    'fastapi>=0.100.0' \
    'uvicorn[standard]>=0.20.0' \
    'python-dotenv>=1.0.0' \
    'opentimelineio>=0.17.0' \
    'litellm>=1.0.0' \
    'b2sdk>=2.10.4,<3.0.0' \
    'httpx>=0.24.0' \
    'pydantic>=2.0.0' \
    'opentelemetry-sdk>=1.20.0' \
    'opentelemetry-api>=1.20.0' \
    'vastai'

# ---------------------------------------------------------------------------
# Frontend dependencies
# ---------------------------------------------------------------------------
echo "=== Installing frontend dependencies ==="
cd "$REPO_DIR/frontend"
npm install

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------
echo "=== Configuring environment ==="
ENV_FILE="$REPO_DIR/server/.env"

# Preserve existing .env if present, otherwise create from template
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'ENVEOF'
# Model — GLM-5.1 via OpenRouter
ADK_MODEL=openai/z-ai/glm-5.1
OPENAI_API_BASE=https://openrouter.ai/api/v1
# OPENAI_API_KEY=  # Set via environment or Vast.ai template

# Concurrency
MAX_CONCURRENT_LLM=2
MAX_CONTEXT_TOKENS=128000

# Pipeline
TIMELINE_DIR=/workspace/documentary-output/timelines
TTS_OUTPUT_DIR=/workspace/documentary-output/audio
VIDEO_OUTPUT_DIR=/workspace/documentary-output/video

# Plugins
CONTEXT_INVOCATIONS_TO_KEEP=2
TOOL_MAX_RETRIES=2
GPU_CONCURRENCY=1
TTS_CONCURRENCY=2
VASTAI_CONCURRENCY=3
LITELLM_REQUEST_TIMEOUT=600

# GPU Workers — set these to your GPU VM addresses
# TTS_WORKER_URL=http://<tts-vm-ip>:8881
# GPU_WORKER_URL=http://<video-vm-ip>:8880
# VIDEO_WORKER_URLS=http://<video-vm1-ip>:8880,http://<video-vm2-ip>:8880
ENVEOF
fi

# Overlay env vars from the shell environment into .env
# (Vast.ai template vars take precedence)
for var in OPENAI_API_KEY OPENAI_API_BASE ADK_MODEL \
           B2_KEY_ID B2_APPLICATION_KEY VAST_API_KEY \
           DASHSCOPE_API_KEY GPU_WORKER_URL TTS_WORKER_URL \
           VIDEO_WORKER_URLS; do
    val="${!var:-}"
    if [ -n "$val" ]; then
        # Remove existing line (if any) and append fresh value.
        # Avoids all sed/bash escaping issues with $, &, \, backticks.
        if grep -q "^${var}=" "$ENV_FILE" 2>/dev/null; then
            grep -v "^${var}=" "$ENV_FILE" > "${ENV_FILE}.tmp" && mv "${ENV_FILE}.tmp" "$ENV_FILE"
        fi
        printf '%s=%s\n' "$var" "$val" >> "$ENV_FILE"
    fi
done

# ---------------------------------------------------------------------------
# Output directories
# ---------------------------------------------------------------------------
mkdir -p /workspace/documentary-output/{timelines,audio,video,assembly}

# ---------------------------------------------------------------------------
# Supervisor configuration (process manager)
# ---------------------------------------------------------------------------
echo "=== Configuring supervisor ==="
cat > /etc/supervisor/conf.d/documentary.conf << 'SUPEOF'
[program:backend]
command=python3 server.py
directory=/workspace/economy-documentary/server
autostart=true
autorestart=true
startretries=5
stderr_logfile=/var/log/documentary-backend.err.log
stdout_logfile=/var/log/documentary-backend.out.log
environment=HOME="/root",PATH="/usr/local/bin:/usr/bin:/bin"

[program:frontend]
command=npx next start -p 3000
directory=/workspace/economy-documentary/frontend
autostart=true
autorestart=true
startretries=5
stderr_logfile=/var/log/documentary-frontend.err.log
stdout_logfile=/var/log/documentary-frontend.out.log
environment=HOME="/root",PATH="/usr/local/bin:/usr/bin:/bin",BACKEND_URL="http://localhost:8000"

[group:documentary]
programs=backend,frontend
SUPEOF

# ---------------------------------------------------------------------------
# Build frontend for production
# ---------------------------------------------------------------------------
echo "=== Building frontend ==="
cd "$REPO_DIR/frontend"
npm run build

# ---------------------------------------------------------------------------
# Nginx reverse proxy (optional — serves both on port 80)
# ---------------------------------------------------------------------------
cat > /etc/nginx/sites-available/documentary << 'NGINXEOF'
server {
    listen 80;
    server_name _;

    # Frontend (Next.js)
    location / {
        proxy_pass http://localhost:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }

    # Backend API
    location /api/backend/ {
        rewrite ^/api/backend/(.*) /$1 break;
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400;
    }

    # Dashboard SSE endpoints
    location /dashboard/ {
        proxy_pass http://localhost:8000/dashboard/;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
    }

    # Health check
    location /health {
        proxy_pass http://localhost:8000/health;
    }
}
NGINXEOF

ln -sf /etc/nginx/sites-available/documentary /etc/nginx/sites-enabled/documentary
rm -f /etc/nginx/sites-enabled/default

# ---------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------
echo "=== Starting services ==="
# Vast.ai containers have no init system — start supervisord explicitly
supervisord -c /etc/supervisor/supervisord.conf 2>/dev/null || true
supervisorctl reread
supervisorctl update
supervisorctl start documentary:*
if nginx -t; then
    nginx -s reload 2>/dev/null || nginx || echo "WARNING: nginx failed to start (check port 80)"
else
    echo "WARNING: nginx config test failed, skipping"
fi

# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------
echo ""
echo "=== Central Unit Bootstrap Complete ==="
echo ""
echo "Services:"
supervisorctl status documentary:*
echo ""
echo "Access:"
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):3000"
echo "  Backend:   http://$(hostname -I | awk '{print $1}'):8000"
echo "  Health:    http://$(hostname -I | awk '{print $1}'):8000/health"
echo ""
echo "Logs:"
echo "  Backend:  tail -f /var/log/documentary-backend.out.log"
echo "  Frontend: tail -f /var/log/documentary-frontend.out.log"
echo ""
echo "To configure GPU workers, update /workspace/economy-documentary/server/.env"
echo "and restart: supervisorctl restart documentary:backend"
