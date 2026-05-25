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
    """Run a vastai CLI command and return parsed output.

    The vastai CLI v1.0 has inconsistent output formats:
    - ``search offers --raw`` returns JSON arrays
    - ``show instance --raw`` returns JSON objects
    - ``create instance`` (without --raw) returns Python-repr like
      ``Started. {'success': True, 'new_contract': 12345, ...}``
    - ``create instance --raw`` returns **empty** output
    - ``destroy instance`` returns plain text

    We try JSON first, then fall back to ``ast.literal_eval``.
    """
    _vast_key_path = "/Users/orpington/api_keys/LLMS/vast_api_key.txt"
    api_key = ""
    if os.path.exists(_vast_key_path):
        with open(_vast_key_path) as _f:
            api_key = _f.read().strip()
    if not api_key:
        print("ERROR: Vast.ai API key not found", file=sys.stderr)
        sys.exit(1)

    cmd = ["vastai", "--api-key", api_key] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: vastai command failed: {result.stderr[:500]}", file=sys.stderr)
        sys.exit(1)
    stdout = result.stdout.strip()
    if not stdout:
        return {"output": ""}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    # Try Python repr (create instance returns e.g.
    # "Started. {'success': True, 'new_contract': 12345, ...}")
    import ast
    import re
    match = re.search(r"(\{.*\})", stdout, re.DOTALL)
    if match:
        try:
            return ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            pass
    return stdout


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

    # Fetch ALL on-demand offers, then filter in Python.
    # The vastai CLI v1.0's query-string filtering is unreliable when
    # called via subprocess (silently returns empty results for complex
    # queries), so we always fetch the full list and filter ourselves.
    result = _vast_cmd([
        "search", "offers",
        "--type", "on-demand",
        "--raw",
    ])

    all_offers = result if isinstance(result, list) else []
    if not all_offers:
        print("Vast.ai returned no offers at all.", file=sys.stderr)
        sys.exit(1)

    # Filter in Python — reliable regardless of CLI version
    offers = []
    for o in all_offers:
        cpu_cores = float(o.get("cpu_cores_effective", 0))
        disk = float(o.get("disk_space", 0))
        price = float(o.get("dph_total", 999))
        rentable = o.get("rentable", False)
        if cpu_cores < 4:
            continue
        if disk < min_disk:
            continue
        if price > max_price:
            continue
        if not rentable:
            continue
        offers.append(o)

    print(f"  {len(offers)}/{len(all_offers)} offers match criteria")
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
    # SSH keys are managed via Vast.ai account settings, not CLI flags.
    # NOTE: Do NOT pass --raw — vastai CLI returns empty stdout with --raw
    # for create commands.  Without --raw it returns Python repr that we
    # can parse with ast.literal_eval.
    create_args = [
        "create", "instance",
        str(offer_id),
        "--image", "ubuntu:22.04",
        "--disk", str(disk_gb),
        "--ssh",
        "--direct",
        "--env", "-p 3000:3000 -p 8000:8000 -p 80:80",
        "--onstart-cmd",
        "apt-get update && apt-get install -y git curl && "
        "(cd /workspace/economy-documentary && git pull origin main || "
        "git clone https://github.com/OrpingtonClose/economy-documentary.git /workspace/economy-documentary) && "
        "bash /workspace/economy-documentary/scripts/central_bootstrap.sh",
    ]

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
