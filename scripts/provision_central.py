#!/usr/bin/env python3
"""
Provision a Central Unit VM on Vast.ai for the documentary pipeline.

The central unit runs the FastAPI backend + Next.js dashboard on a cheap
CPU-only (or minimal-GPU) VM.  GPU workers connect to it remotely.

Usage:
    export VAST_API_KEY=...
    python provision_central.py [--max-price 0.30] [--disk 30] [--ssh-key ~/.ssh/id_rsa.pub]

After provisioning:
    1. SSH into the VM
    2. Run: bash /workspace/central_bootstrap.sh
    3. Access dashboard at http://<vm-ip>:3000
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


def _vast_cmd(args: list[str]) -> dict | list | str:
    """Run a vastai CLI command and return parsed output."""
    api_key = os.environ.get("VAST_API_KEY", "")
    if not api_key:
        print("ERROR: VAST_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    cmd = ["vastai", "--api-key", api_key] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"ERROR: vastai command failed: {result.stderr[:500]}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()


def find_cheap_cpu_offer(max_price: float, min_disk: int) -> dict:
    """Find the cheapest Vast.ai offer suitable for central unit.

    Central unit needs:
    - CPU only (no GPU required, but any GPU is fine)
    - 4+ CPU cores
    - 8+ GB RAM
    - Enough disk for repo + deps (~10 GB)
    - Good network (for SSE streaming to user + GPU worker comms)
    """
    print(f"Searching for offers: max ${max_price}/hr, {min_disk}GB disk...")

    # Search for cheap offers — we don't need GPU, but Vast.ai
    # primarily offers GPU machines.  Filter for cheapest available.
    result = _vast_cmd([
        "search", "offers",
        "--type", "on-demand",
        "--min-cpu-cores", "4",
        "--min-ram", "8",
        "--min-disk", str(min_disk),
        "--max-dph", str(max_price),
        "--order", "dph_total",
        "--raw",
    ])

    offers = result if isinstance(result, list) else []
    if not offers:
        print("No offers found matching criteria.", file=sys.stderr)
        print(f"Try increasing --max-price (currently ${max_price}/hr)", file=sys.stderr)
        sys.exit(1)

    # Sort by price ascending
    sorted_offers = sorted(offers, key=lambda o: float(o.get("dph_total", 999)))
    best = sorted_offers[0]

    print(f"\nBest offer:")
    print(f"  ID:    {best.get('id')}")
    print(f"  GPU:   {best.get('gpu_name', 'none')} ({best.get('num_gpus', 0)}x)")
    print(f"  CPU:   {best.get('cpu_cores_effective', '?')} cores")
    print(f"  RAM:   {best.get('cpu_ram', 0) / 1024:.1f} GB")
    print(f"  Disk:  {best.get('disk_space', 0):.0f} GB")
    print(f"  Price: ${float(best.get('dph_total', 0)):.3f}/hr")
    print(f"  Net:   {best.get('inet_up', 0):.0f} Mbps up / {best.get('inet_down', 0):.0f} Mbps down")

    return best


def provision_vm(offer_id: str, disk_gb: int, ssh_key_path: str | None) -> str:
    """Create a Vast.ai instance from the selected offer."""
    print(f"\nProvisioning VM from offer {offer_id}...")

    # Use a lightweight base image — central unit doesn't need CUDA
    create_args = [
        "create", "instance",
        str(offer_id),
        "--image", "ubuntu:22.04",
        "--disk", str(disk_gb),
        "--onstart-cmd", "apt-get update && apt-get install -y git curl && "
                         "(cd /workspace/economy-documentary && git pull origin main || "
                         "git clone https://github.com/OrpingtonClose/economy-documentary.git /workspace/economy-documentary) && "
                         "bash /workspace/economy-documentary/scripts/central_bootstrap.sh",
        "--raw",
    ]

    if ssh_key_path and os.path.exists(ssh_key_path):
        with open(ssh_key_path) as f:
            ssh_key = f.read().strip()
        create_args.extend(["--ssh-key", ssh_key])

    result = _vast_cmd(create_args)

    if isinstance(result, dict):
        instance_id = result.get("new_contract")
        if instance_id:
            return str(instance_id)
        print(f"Unexpected response: {result}", file=sys.stderr)
        sys.exit(1)

    print(f"Unexpected response type: {result}", file=sys.stderr)
    sys.exit(1)


def wait_for_running(instance_id: str, timeout: int = 300) -> dict:
    """Wait for the VM to reach 'running' status."""
    print(f"\nWaiting for VM {instance_id} to start (timeout {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        result = _vast_cmd(["show", "instance", instance_id, "--raw"])
        if isinstance(result, dict):
            status = result.get("actual_status", result.get("status_msg", "unknown"))
            print(f"  Status: {status} ({int(time.time() - start)}s)", end="\r")
            if status == "running":
                print(f"\n  VM is running!")
                return result
        time.sleep(10)

    print(f"\nERROR: VM did not reach 'running' within {timeout}s", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Provision a central unit VM for the documentary pipeline"
    )
    parser.add_argument(
        "--max-price", type=float, default=0.30,
        help="Maximum price per hour in USD (default: $0.30/hr)"
    )
    parser.add_argument(
        "--disk", type=int, default=30,
        help="Disk space in GB (default: 30)"
    )
    parser.add_argument(
        "--ssh-key", type=str, default=os.path.expanduser("~/.ssh/id_rsa.pub"),
        help="Path to SSH public key for VM access"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Search for offers but don't provision"
    )
    args = parser.parse_args()

    offer = find_cheap_cpu_offer(args.max_price, args.disk)

    if args.dry_run:
        print("\n--dry-run: skipping provisioning")
        return

    offer_id = offer.get("id")
    if not offer_id:
        print("ERROR: selected offer has no ID", file=sys.stderr)
        sys.exit(1)

    instance_id = provision_vm(offer_id, args.disk, args.ssh_key)
    print(f"Instance ID: {instance_id}")

    vm_info = wait_for_running(instance_id)

    # Extract connection info
    ssh_host = vm_info.get("ssh_host", "")
    ssh_port = vm_info.get("ssh_port", "")
    public_ipaddr = vm_info.get("public_ipaddr", ssh_host)

    # Vast.ai maps ports — find the mapped ports for 3000 and 8000
    ports = vm_info.get("ports", {})

    print("\n" + "=" * 60)
    print("CENTRAL UNIT VM PROVISIONED")
    print("=" * 60)
    print(f"Instance ID: {instance_id}")
    print(f"Public IP:   {public_ipaddr}")
    if ssh_host and ssh_port:
        print(f"SSH:         ssh -p {ssh_port} root@{ssh_host}")
    print(f"Ports:       {json.dumps(ports, indent=2)}")
    print()
    print("The bootstrap script is running automatically via onstart-cmd.")
    print("Once complete (~5 min), access:")
    print(f"  Dashboard: http://{public_ipaddr}:3000")
    print(f"  Backend:   http://{public_ipaddr}:8000")
    print(f"  Health:    http://{public_ipaddr}:8000/health")
    print()
    print("To configure GPU workers after bootstrap:")
    print(f"  ssh -p {ssh_port} root@{ssh_host}" if ssh_host and ssh_port else "  (SSH info not yet available — check Vast.ai dashboard)")
    print("  vim /workspace/economy-documentary/server/.env")
    print("  supervisorctl restart documentary:backend")
    print("=" * 60)

    # Save connection info to a local file for reference
    info_file = os.path.expanduser("~/.central-unit-info.json")
    with open(info_file, "w") as f:
        json.dump({
            "instance_id": instance_id,
            "public_ip": public_ipaddr,
            "ssh_host": ssh_host,
            "ssh_port": ssh_port,
            "ports": ports,
            "dashboard_url": f"http://{public_ipaddr}:3000",
            "backend_url": f"http://{public_ipaddr}:8000",
        }, f, indent=2)
    print(f"\nConnection info saved to {info_file}")


if __name__ == "__main__":
    main()
