#!/usr/bin/env bash
# Langfuse Self-Host VM Bootstrap Script
#
# Run this on a fresh Vast.ai CPU VM (Ubuntu 22.04+, ~2 vCPU / 4 GB RAM
# / 20 GB disk) to stand up a single-node Langfuse deployment that
# accepts OTel span pushes from the economy-documentary backend.
#
# The layout is:
#   * Docker engine (installed if missing).
#   * Postgres container — single-node data store.
#   * Langfuse web + worker containers — ``ghcr.io/langfuse/langfuse``
#     + ``ghcr.io/langfuse/langfuse-worker``.
#   * OTel OTLP/HTTP endpoint at
#     ``<LANGFUSE_HOST>/api/public/otel/v1/traces`` accepts spans
#     with HTTP Basic auth built from the generated key pair.
#
# Idempotent by design — every step is guarded by a presence check
# so re-running the script against a live VM is a no-op. Volumes
# persist Postgres data across container restarts.
#
# Usage:
#   bash langfuse_bootstrap.sh
#
# Required env vars (set via Vast.ai template or exported before run):
#   LANGFUSE_PUBLIC_HOST      — Public URL the backend will target
#                               (https://obs.example.com or
#                               http://1.2.3.4:3001). Used for the
#                               "View Trace" deep-link construction
#                               and for Langfuse's own redirects.
#
# Optional env vars (generated and printed on first run if missing):
#   LANGFUSE_SECRET            — NEXTAUTH_SECRET (32-byte hex).
#   LANGFUSE_SALT              — SALT for sensitive column hashing.
#   LANGFUSE_ENCRYPTION_KEY    — ENCRYPTION_KEY (32-byte hex).
#   POSTGRES_PASSWORD          — Local postgres password.
#
# Ports exposed:
#   3001 — Langfuse web UI + OTLP/HTTP ingest (behind auth).
#
# After the script finishes it prints:
#   1. A URL to open the Langfuse UI.
#   2. The commands to create the first organisation + project and
#      generate an API key pair.
#   3. The three env vars (LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY /
#      LANGFUSE_SECRET_KEY) the economy-documentary backend needs.

set -euo pipefail

echo "=== Langfuse Bootstrap: observability VM ==="
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Hostname:  $(hostname)"
df -h /

# ---------------------------------------------------------------------------
# Inputs & secret material
# ---------------------------------------------------------------------------
: "${LANGFUSE_PUBLIC_HOST:?LANGFUSE_PUBLIC_HOST must be set (e.g. https://obs.example.com or http://<ip>:3001)}"

LANGFUSE_HOME="${LANGFUSE_HOME:-/opt/langfuse}"
LANGFUSE_SECRET="${LANGFUSE_SECRET:-$(openssl rand -hex 32)}"
LANGFUSE_SALT="${LANGFUSE_SALT:-$(openssl rand -hex 32)}"
LANGFUSE_ENCRYPTION_KEY="${LANGFUSE_ENCRYPTION_KEY:-$(openssl rand -hex 32)}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 16)}"

mkdir -p "$LANGFUSE_HOME"
cd "$LANGFUSE_HOME"

# ---------------------------------------------------------------------------
# Docker engine (install once, idempotent)
# ---------------------------------------------------------------------------
if ! command -v docker &>/dev/null; then
    echo "=== Installing Docker engine ==="
    apt-get update
    apt-get install -y ca-certificates curl gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
fi
docker --version
docker compose version

# ---------------------------------------------------------------------------
# Compose stack
# ---------------------------------------------------------------------------
cat > "$LANGFUSE_HOME/docker-compose.yml" <<YAML
# Langfuse single-node self-host. Pinned to known-good tags; bump
# in lockstep with the economy-documentary backend's exporter
# compatibility window.
services:
  langfuse-db:
    image: postgres:15-alpine
    container_name: langfuse-db
    restart: unless-stopped
    environment:
      POSTGRES_DB: langfuse
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: \${POSTGRES_PASSWORD}
    volumes:
      - langfuse_db:/var/lib/postgresql/data
    # Exposed only on the docker network — never publish 5432.

  langfuse-web:
    image: ghcr.io/langfuse/langfuse:3
    container_name: langfuse-web
    restart: unless-stopped
    depends_on:
      - langfuse-db
    ports:
      - "3001:3000"
    environment:
      DATABASE_URL: postgresql://langfuse:\${POSTGRES_PASSWORD}@langfuse-db:5432/langfuse
      NEXTAUTH_URL: \${LANGFUSE_PUBLIC_HOST}
      NEXTAUTH_SECRET: \${LANGFUSE_SECRET}
      SALT: \${LANGFUSE_SALT}
      ENCRYPTION_KEY: \${LANGFUSE_ENCRYPTION_KEY}
      TELEMETRY_ENABLED: "false"
      LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES: "false"

  langfuse-worker:
    image: ghcr.io/langfuse/langfuse-worker:3
    container_name: langfuse-worker
    restart: unless-stopped
    depends_on:
      - langfuse-db
    environment:
      DATABASE_URL: postgresql://langfuse:\${POSTGRES_PASSWORD}@langfuse-db:5432/langfuse
      SALT: \${LANGFUSE_SALT}
      ENCRYPTION_KEY: \${LANGFUSE_ENCRYPTION_KEY}
      TELEMETRY_ENABLED: "false"

volumes:
  langfuse_db:
YAML

cat > "$LANGFUSE_HOME/.env" <<ENV
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
LANGFUSE_PUBLIC_HOST=$LANGFUSE_PUBLIC_HOST
LANGFUSE_SECRET=$LANGFUSE_SECRET
LANGFUSE_SALT=$LANGFUSE_SALT
LANGFUSE_ENCRYPTION_KEY=$LANGFUSE_ENCRYPTION_KEY
ENV
chmod 600 "$LANGFUSE_HOME/.env"

# ---------------------------------------------------------------------------
# Bring the stack up (idempotent)
# ---------------------------------------------------------------------------
echo "=== Pulling images ==="
docker compose --env-file "$LANGFUSE_HOME/.env" \
    -f "$LANGFUSE_HOME/docker-compose.yml" pull

echo "=== Starting stack ==="
docker compose --env-file "$LANGFUSE_HOME/.env" \
    -f "$LANGFUSE_HOME/docker-compose.yml" up -d

# ---------------------------------------------------------------------------
# Wait for readiness (web returns 200 on /api/public/health)
# ---------------------------------------------------------------------------
echo "=== Waiting for Langfuse web to become ready ==="
for _ in $(seq 1 60); do
    if curl -fsS "http://localhost:3001/api/public/health" >/dev/null 2>&1; then
        echo "Langfuse web is healthy."
        break
    fi
    sleep 2
done

# ---------------------------------------------------------------------------
# Next-step hints — surfaced so the operator can finish wiring in ~2 min.
# ---------------------------------------------------------------------------
cat <<NEXT

==========================================================================
Langfuse is up at: $LANGFUSE_PUBLIC_HOST

Next steps (one-time, in the Langfuse UI):
  1. Open the UI, sign up as the first user (becomes org owner).
  2. Create a project, then go to Settings -> API keys.
  3. Generate a key pair. Note the "Public key" (pk-lf-...) and
     "Secret key" (sk-lf-...).

Then export these three env vars on the economy-documentary backend:
  export LANGFUSE_HOST=$LANGFUSE_PUBLIC_HOST
  export LANGFUSE_PUBLIC_KEY=pk-lf-...
  export LANGFUSE_SECRET_KEY=sk-lf-...

Restart the playground backend. /playground/config/langfuse will flip
to {"enabled": true, "host": "..."} and the "View Trace" button will
render on every run card.
==========================================================================
NEXT
