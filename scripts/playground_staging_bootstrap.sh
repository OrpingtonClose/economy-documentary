#!/usr/bin/env bash
# Component Playground — staging VM bootstrap
#
# Runs on a freshly provisioned Vast.ai CPU VM (ubuntu:22.04) and brings
# up the standalone playground:
#
#   - FastAPI backend (`server.server:app`) on :8000
#   - Next.js `frontend-playground/` on :3100
#   - Nginx reverse proxy on :80
#
# This is intentionally NOT the production pipeline (no GPU workers, no
# TTS worker pool, no B2 render outputs). The playground is a workbench
# that exposes the 15 atomic components individually, backed by whatever
# LLM keys are passed in via the environment.
#
# Usage:
#   bash playground_staging_bootstrap.sh
#
# Required env vars (set in the Vast.ai template or exported before run):
#   - at least one of: GOOGLE_API_KEY, OPENAI_API_KEY, KIMI_API_KEY,
#     MOONSHOT_API_KEY, DASHSCOPE_API_KEY, ALIBABA_API_KEY
#     (models with no reachable key stay red in the UI — that's the
#      contract. Do not fake them.)
#
# Optional env vars:
#   - B2_KEY_ID / B2_APPLICATION_KEY      B2 artifact reads
#   - PLAYGROUND_USER_CASES_DIR           write cases outside the repo
#   - PLAYGROUND_GIT_BRANCH               override branch (default: main)
#
# Idempotent: safe to re-run to pick up a new commit on the branch.

set -euo pipefail

REPO_URL="${PLAYGROUND_GIT_URL:-https://github.com/OrpingtonClose/economy-documentary.git}"
BRANCH="${PLAYGROUND_GIT_BRANCH:-main}"
REPO_DIR="/workspace/economy-documentary"

echo "=== Playground staging bootstrap ==="
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Hostname:  $(hostname)"
echo "Branch:    ${BRANCH}"
df -h /

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
    git curl ca-certificates gnupg software-properties-common \
    supervisor nginx jq

# strands-agents-evals 0.1.15 requires Python ≥ 3.12; ubuntu:22.04 ships
# 3.10 by default. Install 3.12 from the deadsnakes PPA and use it as
# the interpreter for the playground.
if ! command -v python3.12 >/dev/null 2>&1; then
    echo "=== Installing Python 3.12 (deadsnakes PPA) ==="
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update
    apt-get install -y python3.12 python3.12-venv python3.12-dev
    curl -fsSL https://bootstrap.pypa.io/get-pip.py | python3.12 -
fi
PY=python3.12
echo "interpreter: $($PY --version)"

# Node.js 20 LTS (for Next.js 14)
if ! command -v node >/dev/null 2>&1 || [[ "$(node -v)" != v20* ]]; then
    echo "=== Installing Node.js 20 LTS ==="
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
fi
echo "node: $(node -v)  npm: $(npm -v)  python: $(python3 --version)"

# ---------------------------------------------------------------------------
# Repo checkout
# ---------------------------------------------------------------------------
mkdir -p /workspace
if [ -d "${REPO_DIR}/.git" ]; then
    echo "=== Updating existing repo ==="
    git -C "${REPO_DIR}" fetch --depth=1 origin "${BRANCH}"
    git -C "${REPO_DIR}" reset --hard "origin/${BRANCH}"
else
    echo "=== Cloning repository (${BRANCH}) ==="
    git clone --depth=1 --branch "${BRANCH}" "${REPO_URL}" "${REPO_DIR}"
fi
cd "${REPO_DIR}"

# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------
# The repo uses poetry, but the central bootstrap pattern shows a plain
# `pip install` of the runtime set works on Vast.ai and avoids carrying
# poetry into prod. Match that pattern — we only need the subset the
# playground actually imports at request time.
#
# This list MUST stay in sync with server/pyproject.toml. CI will catch
# drift: the playground catalog experiment imports every component.
echo "=== Installing Python dependencies (uv resolver, $PY interpreter) ==="
# pip's backtracking resolver takes forever on this dependency set
# (strands-agents + strands-agents-evals + deepagents + langgraph all
# share pinned pydantic/protobuf constraints). uv's PubGrub-style
# resolver handles it in seconds. Install uv once, then use it for
# everything.
if ! command -v uv >/dev/null 2>&1; then
    curl -fsSL https://astral.sh/uv/install.sh | sh
    export PATH="/root/.local/bin:${PATH}"
fi

# The playground server only needs the runtime slice — not the full
# documentary pipeline (no GPU workers, no TTS, no B2 renders). This is
# a deliberately minimal set: if a component at runtime needs something
# missing, the dedicated-model reachability probe will surface it as a
# MODEL_UNREACHABLE red dot, which is the fail-closed contract.
uv pip install --system --python "$PY" \
    'fastapi>=0.100.0' \
    'uvicorn[standard]>=0.20.0' \
    'pydantic>=2.0.0' \
    'python-dotenv>=1.0.0' \
    'httpx>=0.24.0' \
    'litellm' \
    'opentimelineio>=0.17.0' \
    'numpy' \
    'b2sdk>=2.10.4,<3.0.0' \
    'opentelemetry-sdk>=1.20.0' \
    'opentelemetry-api>=1.20.0' \
    'strands-agents==1.36.0' \
    'strands-agents-evals==0.1.15' \
    'deepagents==0.5.3' \
    'langchain-core' \
    'langgraph'

# ---------------------------------------------------------------------------
# Frontend (build for production)
# ---------------------------------------------------------------------------
echo "=== Installing + building frontend-playground ==="
cd "${REPO_DIR}/frontend-playground"
# Use `npm ci` when a lockfile is present — reproducible and fast.
if [ -f "package-lock.json" ]; then
    npm ci
else
    npm install
fi
# The Next build needs PLAYGROUND_API_URL in scope so the rewrite rule
# bakes in correctly — backend lives on the same VM at :8000.
export PLAYGROUND_API_URL="http://127.0.0.1:8000"
npm run build

# ---------------------------------------------------------------------------
# Env file for the backend
# ---------------------------------------------------------------------------
echo "=== Writing backend .env ==="
ENV_FILE="${REPO_DIR}/server/.env"
mkdir -p "$(dirname "${ENV_FILE}")"
# Start fresh every bootstrap so a rotated key isn't shadowed by a stale
# file. Env vars exported into this script win.
: > "${ENV_FILE}"

cat >> "${ENV_FILE}" <<'ENVEOF'
# --- Playground staging ---
DOCUMENTARY_TEST_MODE=false
# Persist user-authored cases off the ephemeral repo tree so re-pulling
# `main` doesn't wipe them. Override via PLAYGROUND_USER_CASES_DIR env.
PLAYGROUND_USER_CASES_DIR=/workspace/playground-user-cases
ENVEOF

mkdir -p /workspace/playground-user-cases

# Carry over whichever API keys / tuning knobs were set in the VM
# template. Anything not set stays unset — the playground will surface
# that as a red reachability dot, which is correct behaviour.
for var in \
    GOOGLE_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY \
    KIMI_API_KEY MOONSHOT_API_KEY \
    DASHSCOPE_API_KEY ALIBABA_API_KEY \
    GROQ_API_KEY FIREWORKS_API_KEY TOGETHER_API_KEY \
    OPENROUTER_API_KEY DEEPSEEK_API_KEY GLM_API_KEY MISTRAL_API_KEY \
    B2_KEY_ID B2_APPLICATION_KEY \
    PLAYGROUND_USER_CASES_DIR \
    ADK_MODEL OPENAI_API_BASE \
    MAX_CONCURRENT_LLM MAX_CONTEXT_TOKENS
do
    val="${!var:-}"
    if [ -n "${val}" ]; then
        if grep -q "^${var}=" "${ENV_FILE}" 2>/dev/null; then
            grep -v "^${var}=" "${ENV_FILE}" > "${ENV_FILE}.tmp" \
                && mv "${ENV_FILE}.tmp" "${ENV_FILE}"
        fi
        printf '%s=%s\n' "${var}" "${val}" >> "${ENV_FILE}"
    fi
done

echo "Backend .env written (keys redacted): "
grep -oE '^[A-Z_]+=' "${ENV_FILE}" | sort -u

# ---------------------------------------------------------------------------
# Supervisor — manage backend + frontend as long-running processes
# ---------------------------------------------------------------------------
echo "=== Configuring supervisor ==="
cat > /etc/supervisor/conf.d/playground.conf <<'SUPEOF'
[program:playground-backend]
command=python3.12 -m uvicorn playground_server:app --host 0.0.0.0 --port 8000 --app-dir /workspace/economy-documentary/server
directory=/workspace/economy-documentary/server
autostart=true
autorestart=true
startretries=5
stderr_logfile=/var/log/playground-backend.err.log
stdout_logfile=/var/log/playground-backend.out.log
environment=HOME="/root",PATH="/usr/local/bin:/usr/bin:/bin",PYTHONUNBUFFERED="1"

[program:playground-frontend]
command=npx next start -p 3100
directory=/workspace/economy-documentary/frontend-playground
autostart=true
autorestart=true
startretries=5
stderr_logfile=/var/log/playground-frontend.err.log
stdout_logfile=/var/log/playground-frontend.out.log
environment=HOME="/root",PATH="/usr/local/bin:/usr/bin:/bin",PLAYGROUND_API_URL="http://127.0.0.1:8000",NODE_ENV="production"

[group:playground]
programs=playground-backend,playground-frontend
SUPEOF

# ---------------------------------------------------------------------------
# Nginx — single public port 80, proxy to frontend; frontend's
# next.config.js rewrite already forwards /playground/* to the backend.
# ---------------------------------------------------------------------------
echo "=== Configuring nginx ==="
cat > /etc/nginx/sites-available/playground <<'NGINXEOF'
server {
    listen 80 default_server;
    server_name _;

    # Everything hits the Next.js app; /playground/* is rewritten to the
    # backend by the app itself (frontend-playground/next.config.js).
    location / {
        proxy_pass http://127.0.0.1:3100;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }

    # Direct backend access (for curl + debugging).
    location /playground/ {
        proxy_pass http://127.0.0.1:8000/playground/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
        proxy_read_timeout 86400;
    }

    location = /health {
        proxy_pass http://127.0.0.1:8000/health;
    }
}
NGINXEOF
ln -sf /etc/nginx/sites-available/playground /etc/nginx/sites-enabled/playground
rm -f /etc/nginx/sites-enabled/default

# ---------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------
echo "=== Starting services ==="
supervisord -c /etc/supervisor/supervisord.conf 2>/dev/null || true
supervisorctl reread
supervisorctl update
supervisorctl start playground:* || true
if nginx -t; then
    nginx -s reload 2>/dev/null || nginx || echo "WARN: nginx failed to start"
else
    echo "WARN: nginx config test failed"
fi

# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------
sleep 3
echo
echo "=== Playground staging bootstrap complete ==="
echo "Services:"
supervisorctl status || true
echo
echo "Local checks:"
curl -fsS --max-time 5 http://127.0.0.1:8000/playground/components | head -c 200 \
    || echo "  (backend not ready yet — check /var/log/playground-backend.err.log)"
echo
curl -fsS --max-time 5 http://127.0.0.1:3100/ | head -c 120 \
    || echo "  (frontend not ready yet — check /var/log/playground-frontend.err.log)"
echo
echo "Public URLs (replace <ip> with Vast.ai public IP):"
echo "  UI:       http://<ip>/components"
echo "  Backend:  http://<ip>/playground/components"
echo "  Health:   http://<ip>/health"
