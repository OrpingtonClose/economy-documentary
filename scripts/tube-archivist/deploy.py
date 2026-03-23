#!/usr/bin/env python3
"""
Deploy Tube Archivist on a Vast.ai VM.

Provisions a cheap CPU instance, installs Docker + Docker Compose,
and deploys the full TA stack (TubeArchivist + ElasticSearch + Redis).

Usage:
    export VAST_API_KEY=your_key

    python3 scripts/tube-archivist/deploy.py                    # Deploy new TA instance
    python3 scripts/tube-archivist/deploy.py --instance-id ID   # Deploy on existing instance
    python3 scripts/tube-archivist/deploy.py --status           # Check TA status
    python3 scripts/tube-archivist/deploy.py --destroy          # Destroy TA VM
    python3 scripts/tube-archivist/deploy.py --ssh              # Print SSH command
    python3 scripts/tube-archivist/deploy.py --get-token        # Fetch API token from running instance
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONNECTION_FILE = SCRIPT_DIR / "ta_connection.json"

VAST_IMAGE = "nvidia/cuda:12.4.0-devel-ubuntu22.04"
SEARCH_CRITERIA = "cpu_ram>=8 disk_space>=100 inet_down>=200 dph_total<=0.20"

TA_PORT = 8000
POLL_INTERVAL_SSH = 10
POLL_TIMEOUT_SSH = 300
POLL_INTERVAL_TA = 15
POLL_TIMEOUT_TA = 600


def vastai(*args: str, parse_json: bool = False) -> str | list | dict:
    """Run a vastai CLI command and return stdout."""
    cmd = ["vastai"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"vastai error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    output = result.stdout.strip()
    if parse_json:
        return json.loads(output)
    return output


def generate_password(length: int = 24) -> str:
    return secrets.token_urlsafe(length)


def build_docker_compose() -> str:
    """Return the docker-compose.yml content for the TA stack."""
    return r"""services:
  tubearchivist:
    container_name: tubearchivist
    restart: unless-stopped
    image: bbilly1/tubearchivist:latest
    ports:
      - "8000:8000"
    volumes:
      - /data/ta/media:/youtube
      - /data/ta/cache:/cache
    environment:
      - TA_HOST=${TA_HOST}
      - TA_USERNAME=${TA_USERNAME:-admin}
      - TA_PASSWORD=${TA_PASSWORD}
      - ELASTIC_PASSWORD=${ES_PASSWORD}
      - REDIS_CON=redis://archivist-redis:6379
      - TZ=${TZ:-UTC}
      - TA_AUTO_UPDATE_YTDLP=true
    depends_on:
      - archivist-es
      - archivist-redis
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s

  archivist-redis:
    image: redis/redis-stack-server
    container_name: archivist-redis
    restart: unless-stopped
    expose:
      - "6379"
    volumes:
      - /data/ta/redis:/data

  archivist-es:
    image: bbilly1/tubearchivist-es
    container_name: archivist-es
    restart: unless-stopped
    environment:
      - "ELASTIC_PASSWORD=${ES_PASSWORD}"
      - "ES_JAVA_OPTS=-Xms512m -Xmx512m"
      - "xpack.security.enabled=true"
      - "discovery.type=single-node"
      - "path.repo=/usr/share/elasticsearch/data/snapshot"
    ulimits:
      memlock:
        soft: -1
        hard: -1
    volumes:
      - /data/ta/es:/usr/share/elasticsearch/data
    expose:
      - "9200"
"""


def build_onstart_script(ta_password: str, es_password: str) -> str:
    """Build the onstart bash script that provisions the VM."""
    compose_content = build_docker_compose()
    # Escape single quotes for embedding in bash heredoc
    compose_escaped = compose_content.replace("'", "'\\''")

    return f"""#!/bin/bash
set -e

# Install Docker + Docker Compose
apt-get update
apt-get install -y docker.io docker-compose-v2 curl

# Start Docker daemon
dockerd &
sleep 5

# Kernel tuning for ElasticSearch
sysctl -w vm.max_map_count=262144

# Create data directories
mkdir -p /data/ta/media /data/ta/cache /data/ta/es /data/ta/redis

# Fix ES permissions
chown 1000:0 -R /data/ta/es

# Write docker-compose.yml
cat > /root/docker-compose.yml << 'COMPOSE_EOF'
{compose_content}COMPOSE_EOF

# Write .env file
cat > /root/.env << 'ENV_EOF'
TA_HOST=0.0.0.0
TA_USERNAME=admin
TA_PASSWORD={ta_password}
ES_PASSWORD={es_password}
TZ=UTC
ENV_EOF

# Start the stack
cd /root
docker compose up -d

# Write connection info placeholder (updated by deploy.py after boot)
cat > /root/ta_connection.json << 'CONN_EOF'
{{"ta_password": "{ta_password}", "es_password": "{es_password}", "status": "booting"}}
CONN_EOF
"""


def find_cheapest_offer() -> dict:
    """Search Vast.ai for the cheapest suitable instance."""
    print("Searching for cheap instances...")
    output = vastai(
        "search", "offers",
        SEARCH_CRITERIA,
        "--order", "dph_total",
        "--type", "interruptible",
        "--limit", "5",
        "--raw",
        parse_json=True,
    )
    if not output:
        # Fallback: try on-demand
        print("No interruptible offers found, trying on-demand...")
        output = vastai(
            "search", "offers",
            SEARCH_CRITERIA,
            "--order", "dph_total",
            "--type", "on-demand",
            "--limit", "5",
            "--raw",
            parse_json=True,
        )
    if not output:
        print("No suitable instances found. Try relaxing search criteria.", file=sys.stderr)
        sys.exit(1)

    offer = output[0]
    print(f"  Best offer: ID={offer['id']}, "
          f"RAM={offer.get('cpu_ram', '?')}GB, "
          f"Disk={offer.get('disk_space', '?')}GB, "
          f"${offer.get('dph_total', '?')}/hr")
    return offer


def create_instance(offer_id: int, onstart_script: str) -> int:
    """Create a Vast.ai instance and return the instance ID."""
    print(f"Creating instance from offer {offer_id}...")
    output = vastai(
        "create", "instance", str(offer_id),
        "--image", VAST_IMAGE,
        "--disk", "100",
        "--onstart-cmd", onstart_script,
        "--raw",
        parse_json=True,
    )
    if isinstance(output, dict) and "new_contract" in output:
        instance_id = output["new_contract"]
    elif isinstance(output, dict) and "id" in output:
        instance_id = output["id"]
    else:
        # Parse from text output
        text = str(output)
        for word in text.split():
            if word.isdigit():
                instance_id = int(word)
                break
        else:
            print(f"Could not parse instance ID from: {output}", file=sys.stderr)
            sys.exit(1)
    print(f"  Instance created: {instance_id}")
    return instance_id


def get_instance_info(instance_id: int) -> dict:
    """Get instance details from Vast.ai."""
    output = vastai("show", "instance", str(instance_id), "--raw", parse_json=True)
    if isinstance(output, list):
        return output[0] if output else {}
    return output


def wait_for_ssh(instance_id: int) -> dict:
    """Poll until the instance is running and SSH is reachable."""
    print("Waiting for instance to boot and SSH to become available...")
    start = time.time()
    while time.time() - start < POLL_TIMEOUT_SSH:
        info = get_instance_info(instance_id)
        status = info.get("actual_status") or info.get("status_msg", "")
        ssh_host = info.get("ssh_host")
        ssh_port = info.get("ssh_port")

        if status == "running" and ssh_host and ssh_port:
            ssh_cmd = f"ssh -o StrictHostKeyChecking=no -p {ssh_port} root@{ssh_host}"
            # Test SSH connectivity
            try:
                result = subprocess.run(
                    ["ssh", "-o", "StrictHostKeyChecking=no",
                     "-o", "ConnectTimeout=5",
                     "-p", str(ssh_port), f"root@{ssh_host}",
                     "echo ok"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    print(f"  SSH ready: {ssh_cmd}")
                    return info
            except (subprocess.TimeoutExpired, Exception):
                pass

        elapsed = int(time.time() - start)
        print(f"  [{elapsed}s] Status: {status}, waiting...")
        time.sleep(POLL_INTERVAL_SSH)

    print("Timed out waiting for SSH.", file=sys.stderr)
    sys.exit(1)


def get_mapped_port(instance_id: int, container_port: int = 8000) -> int | None:
    """Get the externally mapped port for a container port."""
    info = get_instance_info(instance_id)
    ports = info.get("ports", {})
    # Vast.ai ports format: {"8000/tcp": [{"HostPort": "12345"}]}
    key = f"{container_port}/tcp"
    if key in ports:
        mapping = ports[key]
        if isinstance(mapping, list) and mapping:
            return int(mapping[0].get("HostPort", 0))
    # Fallback: check direct_port_count or return the port from the info
    return info.get("direct_port_start")


def build_ta_url(info: dict) -> str:
    """Construct the TA URL from instance info."""
    ssh_host = info.get("ssh_host", "")
    # Clean up host (remove ssh prefix if present)
    host = ssh_host.replace("ssh://", "").split(":")[0] if ssh_host else ""

    # Try to get mapped port
    ports = info.get("ports", {})
    ta_port = TA_PORT
    key = f"{TA_PORT}/tcp"
    if key in ports:
        mapping = ports[key]
        if isinstance(mapping, list) and mapping:
            ta_port = int(mapping[0].get("HostPort", TA_PORT))

    if host:
        return f"http://{host}:{ta_port}"
    return f"http://localhost:{ta_port}"


def wait_for_ta(ta_url: str) -> bool:
    """Poll TA health endpoint until it responds."""
    health_url = f"{ta_url}/health"
    print(f"Waiting for Tube Archivist at {health_url}...")
    start = time.time()
    while time.time() - start < POLL_TIMEOUT_TA:
        try:
            result = subprocess.run(
                ["curl", "-sf", "--max-time", "5", health_url],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                print("  Tube Archivist is ready!")
                return True
        except (subprocess.TimeoutExpired, Exception):
            pass
        elapsed = int(time.time() - start)
        print(f"  [{elapsed}s] Not ready yet, waiting...")
        time.sleep(POLL_INTERVAL_TA)

    print("Timed out waiting for TA to become healthy.", file=sys.stderr)
    return False


def ssh_exec(info: dict, command: str) -> str:
    """Execute a command on the remote instance via SSH."""
    ssh_host = info.get("ssh_host", "")
    ssh_port = info.get("ssh_port", 22)
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=10",
         "-p", str(ssh_port), f"root@{ssh_host}",
         command],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH command failed: {result.stderr.strip()}")
    return result.stdout.strip()


def get_api_token(info: dict) -> str:
    """Fetch the TA API token by running Django management command via SSH."""
    print("Fetching TA API token...")
    cmd = (
        'docker exec tubearchivist python3 manage.py shell -c '
        '"from rest_framework.authtoken.models import Token; '
        'from django.contrib.auth.models import User; '
        't, _ = Token.objects.get_or_create(user=User.objects.first()); '
        'print(t.key)"'
    )
    token = ssh_exec(info, cmd)
    if not token or len(token) < 10:
        raise RuntimeError(f"Invalid token received: {token!r}")
    print(f"  Token: {token[:8]}...")
    return token


def save_connection_info(data: dict) -> None:
    """Save connection info to local JSON file."""
    CONNECTION_FILE.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Connection info saved to {CONNECTION_FILE}")


def load_connection_info() -> dict:
    """Load connection info from local JSON file."""
    if not CONNECTION_FILE.exists():
        print(f"No connection file found at {CONNECTION_FILE}", file=sys.stderr)
        sys.exit(1)
    return json.loads(CONNECTION_FILE.read_text())


def get_ssh_command(info: dict) -> str:
    """Build SSH command string from instance info."""
    ssh_host = info.get("ssh_host", "")
    ssh_port = info.get("ssh_port", 22)
    return f"ssh -o StrictHostKeyChecking=no -p {ssh_port} root@{ssh_host}"


# ─── CLI commands ────────────────────────────────────────────────────────────


def cmd_deploy(args: argparse.Namespace) -> None:
    """Deploy a new Tube Archivist instance."""
    ta_password = generate_password()
    es_password = generate_password()
    onstart = build_onstart_script(ta_password, es_password)

    if args.instance_id:
        instance_id = args.instance_id
        print(f"Using existing instance {instance_id}")
        # Send onstart script to existing instance
        vastai("execute", str(instance_id), onstart)
    else:
        offer = find_cheapest_offer()
        instance_id = create_instance(offer["id"], onstart)

    # Wait for SSH
    info = wait_for_ssh(instance_id)
    ta_url = build_ta_url(info)

    # Wait for TA to boot
    ta_ready = wait_for_ta(ta_url)

    # Try to get API token
    api_token = None
    if ta_ready:
        try:
            api_token = get_api_token(info)
        except Exception as e:
            print(f"  Could not fetch token automatically: {e}")
            print("  Use --get-token later once TA is fully initialized.")

    connection = {
        "instance_id": instance_id,
        "ta_url": ta_url,
        "ta_api_token": api_token,
        "ta_username": "admin",
        "ta_password": ta_password,
        "es_password": es_password,
        "ssh_command": get_ssh_command(info),
        "deployed_at": datetime.now(timezone.utc).isoformat(),
    }
    save_connection_info(connection)

    print("\n" + "=" * 60)
    print("TUBE ARCHIVIST DEPLOYED")
    print("=" * 60)
    print(f"  URL:       {ta_url}")
    print(f"  Username:  admin")
    print(f"  Password:  {ta_password}")
    print(f"  API Token: {api_token or 'run --get-token after TA init'}")
    print(f"  SSH:       {get_ssh_command(info)}")
    print(f"  Instance:  {instance_id}")
    print("=" * 60)


def cmd_status(args: argparse.Namespace) -> None:
    """Check status of the deployed TA instance."""
    conn = load_connection_info()
    instance_id = conn["instance_id"]
    info = get_instance_info(instance_id)
    status = info.get("actual_status") or info.get("status_msg", "unknown")
    ta_url = conn.get("ta_url", "")

    print(f"Instance {instance_id}: {status}")

    if status == "running" and ta_url:
        try:
            result = subprocess.run(
                ["curl", "-sf", "--max-time", "5", f"{ta_url}/health"],
                capture_output=True, text=True, timeout=10,
            )
            ta_status = "healthy" if result.returncode == 0 else "not responding"
        except Exception:
            ta_status = "unreachable"
        print(f"Tube Archivist: {ta_status}")
        print(f"URL: {ta_url}")


def cmd_destroy(args: argparse.Namespace) -> None:
    """Destroy the TA Vast.ai instance."""
    conn = load_connection_info()
    instance_id = conn["instance_id"]
    print(f"Destroying instance {instance_id}...")
    vastai("destroy", "instance", str(instance_id))
    print("Instance destroyed.")
    CONNECTION_FILE.unlink(missing_ok=True)
    print("Connection file removed.")


def cmd_ssh(args: argparse.Namespace) -> None:
    """Print the SSH command for the deployed instance."""
    conn = load_connection_info()
    instance_id = conn["instance_id"]
    info = get_instance_info(instance_id)
    print(get_ssh_command(info))


def cmd_get_token(args: argparse.Namespace) -> None:
    """Fetch the API token from the running TA instance."""
    conn = load_connection_info()
    instance_id = conn["instance_id"]
    info = get_instance_info(instance_id)
    token = get_api_token(info)
    conn["ta_api_token"] = token
    save_connection_info(conn)
    print(f"API Token: {token}")


def main() -> None:
    if not os.environ.get("VAST_API_KEY"):
        print("Error: VAST_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Deploy Tube Archivist on Vast.ai",
    )
    parser.add_argument("--instance-id", type=int,
                        help="Deploy on an existing Vast.ai instance")
    parser.add_argument("--status", action="store_true",
                        help="Check status of deployed TA instance")
    parser.add_argument("--destroy", action="store_true",
                        help="Destroy the TA Vast.ai instance")
    parser.add_argument("--ssh", action="store_true",
                        help="Print SSH command for the instance")
    parser.add_argument("--get-token", action="store_true",
                        help="Fetch API token from running instance")
    args = parser.parse_args()

    if args.status:
        cmd_status(args)
    elif args.destroy:
        cmd_destroy(args)
    elif args.ssh:
        cmd_ssh(args)
    elif args.get_token:
        cmd_get_token(args)
    else:
        cmd_deploy(args)


if __name__ == "__main__":
    main()
