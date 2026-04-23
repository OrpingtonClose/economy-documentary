#!/usr/bin/env bash
# Langfuse Self-Host VM Bootstrap Script
#
# Run this on a fresh Vast.ai CPU VM (Ubuntu 22.04+, ~4 vCPU / 16 GB
# RAM / 100 GB disk) to stand up a single-node Langfuse v3 deployment
# that accepts OTel span pushes from the economy-documentary backend.
#
# Why we track upstream's compose file instead of inlining our own:
# Langfuse v3 is a six-service stack (postgres + clickhouse + redis +
# minio + langfuse-web + langfuse-worker). Hand-rolling a partial
# compose (e.g. postgres-only) silently breaks v3 ingest because the
# worker expects ClickHouse on boot. The upstream
# ``docker-compose.yml`` is the single source of truth for which
# services are required; we pull it at a pinned ref so CI remains
# reproducible.
#
# The layout is:
#   * Docker engine (installed if missing).
#   * Upstream ``langfuse/langfuse`` repo cloned to ``$LANGFUSE_HOME``
#     (idempotent: ``git fetch && checkout`` on re-run).
#   * The six-service compose stack started via ``docker compose up -d``.
#   * OTel OTLP/HTTP endpoint at
#     ``<LANGFUSE_HOST>/api/public/otel/v1/traces`` accepts spans
#     with HTTP Basic auth built from the generated key pair.
#
# Idempotent by design:
#   * Docker install guarded by ``command -v docker``.
#   * Upstream clone guarded by ``.git`` presence; re-runs ``git fetch``
#     + ``git checkout $LANGFUSE_UPSTREAM_REF``.
#   * Secret material written once — re-runs source the existing
#     ``.env`` and preserve the original NEXTAUTH_SECRET / SALT /
#     ENCRYPTION_KEY / POSTGRES_PASSWORD. Rotating those on a
#     running stack would break Postgres authentication and
#     Langfuse's encrypted columns.
#
# Usage:
#   LANGFUSE_PUBLIC_HOST=http://<ip>:3000 bash langfuse_bootstrap.sh
#
# Required env vars (set via Vast.ai template or exported before run):
#   LANGFUSE_PUBLIC_HOST      — Public URL the backend will target
#                               (https://obs.example.com or
#                               http://1.2.3.4:3000). Used for the
#                               "View Trace" deep-link construction
#                               and for Langfuse's own redirects.
#
# Optional env vars (generated and written to ``$LANGFUSE_HOME/.env``
# on first run; preserved on subsequent runs):
#   LANGFUSE_UPSTREAM_REF      — git ref to check out on the upstream
#                                clone. Defaults to ``main``; pin to
#                                a tag (e.g. ``v3.170.0``) for
#                                reproducible deploys.
#   LANGFUSE_SECRET            — NEXTAUTH_SECRET (32-byte hex).
#   LANGFUSE_SALT              — SALT for sensitive column hashing.
#   LANGFUSE_ENCRYPTION_KEY    — ENCRYPTION_KEY (32-byte hex).
#   POSTGRES_PASSWORD          — Local postgres password.
#   CLICKHOUSE_PASSWORD        — ClickHouse admin password.
#   REDIS_AUTH                 — Redis auth token.
#   MINIO_ROOT_PASSWORD        — MinIO root user password (also used
#                                for S3 access-key secret).
#
# Ports exposed by upstream's compose (on host):
#   3000 — Langfuse web UI + OTLP/HTTP ingest (behind auth).
#   9090 — MinIO console (only accessible from localhost by default
#          on the upstream compose; expose via ``LANGFUSE_PUBLIC_HOST``
#          security-group rules as needed).
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
: "${LANGFUSE_PUBLIC_HOST:?LANGFUSE_PUBLIC_HOST must be set (e.g. https://obs.example.com or http://<ip>:3000)}"

LANGFUSE_HOME="${LANGFUSE_HOME:-/opt/langfuse}"
LANGFUSE_UPSTREAM_REF_DEFAULT="main"

mkdir -p "$LANGFUSE_HOME"

# Source the existing ``.env`` (if any) *before* the fallback
# expansions below so a re-run preserves the secrets that were
# written on the first invocation. Without this, the
# ``${VAR:-$(openssl rand ...)}`` expansions generate fresh values
# on every run, which breaks Postgres/Langfuse authentication
# because Postgres was initialised with the old password and the
# encrypted columns were sealed with the old ENCRYPTION_KEY.
#
# We must NOT let the source step clobber operator-supplied inputs on
# a re-run (e.g. an operator migrating the VM to a new IP will pass a
# new LANGFUSE_PUBLIC_HOST). Save such inputs before the source and
# restore them afterward.
_SAVED_PUBLIC_HOST="$LANGFUSE_PUBLIC_HOST"
_SAVED_UPSTREAM_REF="${LANGFUSE_UPSTREAM_REF:-}"
if [[ -f "$LANGFUSE_HOME/.env" ]]; then
    echo "=== Sourcing existing $LANGFUSE_HOME/.env (idempotent re-run) ==="
    # shellcheck disable=SC1091
    set -a
    source "$LANGFUSE_HOME/.env"
    set +a
fi
LANGFUSE_PUBLIC_HOST="$_SAVED_PUBLIC_HOST"
if [[ -n "$_SAVED_UPSTREAM_REF" ]]; then
    LANGFUSE_UPSTREAM_REF="$_SAVED_UPSTREAM_REF"
fi

LANGFUSE_UPSTREAM_REF="${LANGFUSE_UPSTREAM_REF:-$LANGFUSE_UPSTREAM_REF_DEFAULT}"
LANGFUSE_SECRET="${LANGFUSE_SECRET:-$(openssl rand -hex 32)}"
LANGFUSE_SALT="${LANGFUSE_SALT:-$(openssl rand -hex 32)}"
LANGFUSE_ENCRYPTION_KEY="${LANGFUSE_ENCRYPTION_KEY:-$(openssl rand -hex 32)}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 16)}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-$(openssl rand -hex 16)}"
REDIS_AUTH="${REDIS_AUTH:-$(openssl rand -hex 16)}"
MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-$(openssl rand -hex 16)}"

# ---------------------------------------------------------------------------
# Docker engine (install once, idempotent)
# ---------------------------------------------------------------------------
if ! command -v docker &>/dev/null; then
    echo "=== Installing Docker engine ==="
    apt-get update
    apt-get install -y ca-certificates curl gnupg lsb-release git
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
command -v git >/dev/null 2>&1 || apt-get install -y git
docker --version
docker compose version

# ---------------------------------------------------------------------------
# Clone (or update) the upstream Langfuse repo for its compose file.
# ---------------------------------------------------------------------------
LANGFUSE_UPSTREAM_DIR="$LANGFUSE_HOME/upstream"
if [[ ! -d "$LANGFUSE_UPSTREAM_DIR/.git" ]]; then
    echo "=== Cloning langfuse/langfuse -> $LANGFUSE_UPSTREAM_DIR ==="
    git clone --depth 1 --branch "$LANGFUSE_UPSTREAM_REF" \
        https://github.com/langfuse/langfuse.git "$LANGFUSE_UPSTREAM_DIR" \
        || git clone https://github.com/langfuse/langfuse.git "$LANGFUSE_UPSTREAM_DIR"
fi
(
    cd "$LANGFUSE_UPSTREAM_DIR"
    git fetch --tags origin "$LANGFUSE_UPSTREAM_REF" \
        || git fetch origin
    git checkout "$LANGFUSE_UPSTREAM_REF"
)

# ---------------------------------------------------------------------------
# Write ``.env`` used by upstream's compose. Variables keep the names
# the upstream compose expects (see langfuse/langfuse docker-compose.yml).
# ---------------------------------------------------------------------------
cat > "$LANGFUSE_HOME/.env" <<ENV
LANGFUSE_UPSTREAM_REF=$LANGFUSE_UPSTREAM_REF
LANGFUSE_PUBLIC_HOST=$LANGFUSE_PUBLIC_HOST
NEXTAUTH_URL=$LANGFUSE_PUBLIC_HOST
NEXTAUTH_SECRET=$LANGFUSE_SECRET
SALT=$LANGFUSE_SALT
ENCRYPTION_KEY=$LANGFUSE_ENCRYPTION_KEY
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
CLICKHOUSE_PASSWORD=$CLICKHOUSE_PASSWORD
REDIS_AUTH=$REDIS_AUTH
MINIO_ROOT_PASSWORD=$MINIO_ROOT_PASSWORD
LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY=$MINIO_ROOT_PASSWORD
LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY=$MINIO_ROOT_PASSWORD
LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY=$MINIO_ROOT_PASSWORD
TELEMETRY_ENABLED=false
# Keep the legacy aliases so callers of older bootstrap versions can
# still source ``.env`` and get the same three variables they expect.
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
    -f "$LANGFUSE_UPSTREAM_DIR/docker-compose.yml" pull

echo "=== Starting stack ==="
docker compose --env-file "$LANGFUSE_HOME/.env" \
    -f "$LANGFUSE_UPSTREAM_DIR/docker-compose.yml" up -d

# ---------------------------------------------------------------------------
# Wait for readiness (web returns 200 on /api/public/health)
# ---------------------------------------------------------------------------
echo "=== Waiting for Langfuse web to become ready ==="
LANGFUSE_HEALTHY=false
for _ in $(seq 1 120); do
    if curl -fsS "http://localhost:3000/api/public/health" >/dev/null 2>&1; then
        echo "Langfuse web is healthy."
        LANGFUSE_HEALTHY=true
        break
    fi
    sleep 2
done
if [[ "$LANGFUSE_HEALTHY" != "true" ]]; then
    echo "ERROR: Langfuse web did not become healthy within 240s." >&2
    echo "Run 'docker compose -f $LANGFUSE_UPSTREAM_DIR/docker-compose.yml logs' for diagnostics." >&2
    exit 1
fi

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
