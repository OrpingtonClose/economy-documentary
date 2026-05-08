#!/usr/bin/env python3
"""
Provision a Vast.ai CPU VM for the Component Playground staging
environment. The VM is brought up with ``ubuntu:22.04``, the repo is
cloned, and ``playground_staging_bootstrap.sh`` is invoked via
``--onstart-cmd``.

Usage::

    export VAST_API_KEY=...
    export GOOGLE_API_KEY=...
    export OPENAI_API_KEY=...
    python scripts/provision_playground_staging.py \
        [--max-price 0.15] [--disk 25] [--branch main] [--dry-run]

After the VM reaches ``running``, the script prints the public URL and
writes connection info to ``~/.playground-staging-info.json``.

No GPU. No TTS. No B2 render outputs. This VM hosts the workbench only;
its purpose is to let a human click through each of the 15 components
and inspect inputs / outputs / evaluator scores against the *declared*
model for each component.

This script is a CPU-only sibling of ``scripts/provision_central.py``.
The two are intentionally kept separate: the central unit is a pipeline
orchestrator with GPU-worker dependencies, the playground is a sealed
inspection UI with no downstream fan-out.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any

# Env vars forwarded from the caller's shell into the Vast.ai instance
# so the bootstrap script can pick them up. Anything not set in the
# caller stays unset on the VM.
FORWARDED_ENV_VARS: tuple[str, ...] = (
    # Generative model keys (the playground's declared-model set).
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "DASHSCOPE_API_KEY",
    "ALIBABA_API_KEY",
    "GROQ_API_KEY",
    "FIREWORKS_API_KEY",
    "TOGETHER_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "GLM_API_KEY",
    "MISTRAL_API_KEY",
    # Artifact storage (optional — only needed if a component reads B2).
    "B2_KEY_ID",
    "B2_APPLICATION_KEY",
    # Tuning knobs.
    "OPENAI_API_BASE",
    "MAX_CONCURRENT_LLM",
    "MAX_CONTEXT_TOKENS",
)

PORTS_EXPOSED = "-p 80:80 -p 3100:3100 -p 8000:8000"
BOOTSTRAP_PATH = "/workspace/economy-documentary/scripts/playground_staging_bootstrap.sh"


def _vast_cmd(args: list[str]) -> Any:
    """Invoke the vastai CLI and parse whichever shape it returns.

    The CLI alternates between JSON, Python repr, and plain text
    depending on the subcommand. Same helper shape as
    ``scripts/provision_central.py``.
    """
    api_key = os.environ.get("VAST_API_KEY") or os.environ.get("VAST_AI_API_KEY", "")
    if not api_key:
        print("ERROR: VAST_API_KEY (or VAST_AI_API_KEY) not set", file=sys.stderr)
        sys.exit(1)
    cmd = ["vastai", "--api-key", api_key, *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
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
    match = re.search(r"(\{.*\})", stdout, re.DOTALL)
    if match:
        try:
            return ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            pass
    return stdout


def find_cheap_cpu_offer(max_price: float, min_disk: int) -> dict:
    """Pick the cheapest on-demand CPU offer that fits the playground."""
    print(f"Searching Vast.ai offers: ≤${max_price}/hr, ≥{min_disk}GB disk, ≥4 CPU, ≥8GB RAM …")
    result = _vast_cmd(["search", "offers", "--type", "on-demand", "--raw"])
    all_offers = result if isinstance(result, list) else []
    if not all_offers:
        print("Vast.ai returned zero offers.", file=sys.stderr)
        sys.exit(1)

    candidates: list[dict] = []
    for o in all_offers:
        cpu_cores = float(o.get("cpu_cores_effective", 0))
        disk = float(o.get("disk_space", 0))
        price = float(o.get("dph_total", 999))
        ram_gb = float(o.get("cpu_ram", 0)) / 1024.0
        rentable = bool(o.get("rentable", False))
        if cpu_cores < 4 or ram_gb < 8 or disk < min_disk:
            continue
        if price > max_price or not rentable:
            continue
        candidates.append(o)

    print(f"  {len(candidates)}/{len(all_offers)} offers match criteria")
    if not candidates:
        print(f"No matching offers — try raising --max-price (currently ${max_price:.3f}/hr).",
              file=sys.stderr)
        sys.exit(1)

    # Tiebreak: price ascending, then network up-speed descending.
    candidates.sort(key=lambda o: (float(o.get("dph_total", 999)),
                                   -float(o.get("inet_up", 0))))
    best = candidates[0]
    print("\nSelected offer:")
    print(f"  id:    {best.get('id')}")
    print(f"  cpu:   {best.get('cpu_cores_effective', '?')} cores")
    print(f"  ram:   {float(best.get('cpu_ram', 0)) / 1024.0:.1f} GB")
    print(f"  disk:  {best.get('disk_space', 0):.0f} GB")
    print(f"  net:   {best.get('inet_up', 0):.0f}↑ / {best.get('inet_down', 0):.0f}↓ Mbps")
    print(f"  price: ${float(best.get('dph_total', 0)):.3f}/hr")
    return best


def _forwarded_env_pairs() -> list[str]:
    """Return ``--env VAR=value`` pairs for whichever forwarded vars are set."""
    pairs: list[str] = []
    for var in FORWARDED_ENV_VARS:
        val = os.environ.get(var)
        if not val:
            continue
        # env_flag is space-delimited and fed into a shell via vastai's
        # --env, so any value containing spaces or metacharacters would
        # get mis-split. OPENAI_API_BASE (URL) and ADK_MODEL (freeform)
        # are the realistic offenders; quote unconditionally.
        pairs.append(f"-e {var}={shlex.quote(val)}")
    return pairs


def provision_vm(offer_id: str, disk_gb: int, branch: str) -> str:
    """Create a Vast.ai instance that bootstraps itself on first boot."""
    print(f"\nProvisioning VM from offer {offer_id} (branch {branch}) …")
    env_pairs = _forwarded_env_pairs()
    # Forward the selected branch into the VM so the bootstrap's
    # `git fetch && git reset --hard` picks it up. Without this the
    # bootstrap defaults to `main` and silently undoes the onstart clone.
    env_pairs.append(f"-e PLAYGROUND_GIT_BRANCH={shlex.quote(branch)}")
    env_flag = f"{PORTS_EXPOSED} " + " ".join(env_pairs)

    # The onstart command runs as root once the container is up. Clone
    # (or update) the repo then hand off to the bootstrap script. The
    # bootstrap is idempotent so re-running onstart is safe.
    # --branch comes from the user's CLI and lands inside a shell string,
    # so quote it to keep a pathological value from running arbitrary
    # commands on the VM.
    quoted_branch = shlex.quote(branch)
    onstart = (
        "apt-get update && apt-get install -y git curl && "
        f"(git -C /workspace/economy-documentary pull --ff-only || "
        f"git clone --branch {quoted_branch} --depth 1 "
        f"https://github.com/OrpingtonClose/economy-documentary.git "
        f"/workspace/economy-documentary || true) && "
        f"bash {BOOTSTRAP_PATH}"
    )

    create_args = [
        "create", "instance", str(offer_id),
        "--image", "ubuntu:22.04",
        "--disk", str(disk_gb),
        "--ssh",
        "--direct",
        "--env", env_flag,
        "--onstart-cmd", onstart,
    ]
    result = _vast_cmd(create_args)
    if isinstance(result, dict):
        instance_id = result.get("new_contract")
        if instance_id:
            return str(instance_id)
        print(f"Unexpected response shape: {result}", file=sys.stderr)
        sys.exit(1)
    print(f"Unexpected response type: {result!r}", file=sys.stderr)
    sys.exit(1)


def wait_for_running(instance_id: str, timeout: int = 300) -> dict:
    """Poll ``show instance`` until status is ``running`` (or timeout)."""
    print(f"\nWaiting for instance {instance_id} to reach running (≤{timeout}s) …")
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = _vast_cmd(["show", "instance", instance_id, "--raw"])
        if isinstance(info, dict):
            status = info.get("actual_status", info.get("status_msg", "unknown"))
            elapsed = int(timeout - max(0, deadline - time.time()))
            print(f"  status: {status}  ({elapsed}s)", end="\r", flush=True)
            if status == "running":
                print(f"\n  instance is running.")
                return info
        time.sleep(10)
    print(f"\nERROR: instance did not reach running within {timeout}s.",
          file=sys.stderr)
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision a Vast.ai CPU VM for the Component Playground staging env.",
    )
    parser.add_argument("--max-price", type=float, default=0.15,
                        help="Max $/hr (default: 0.15)")
    parser.add_argument("--disk", type=int, default=25,
                        help="Disk GB (default: 25)")
    parser.add_argument("--branch", type=str, default="main",
                        help="Git branch to bootstrap from (default: main)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Search for offers but do not provision")
    args = parser.parse_args()

    offer = find_cheap_cpu_offer(args.max_price, args.disk)
    if args.dry_run:
        print("\n--dry-run: skipping provisioning.")
        return

    offer_id = offer.get("id")
    if offer_id is None:
        print("ERROR: selected offer has no id.", file=sys.stderr)
        sys.exit(1)
    instance_id = provision_vm(offer_id, args.disk, args.branch)
    print(f"instance_id: {instance_id}")
    info = wait_for_running(instance_id)

    ssh_host = info.get("ssh_host", "")
    ssh_port = info.get("ssh_port", "")
    public_ip = info.get("public_ipaddr", ssh_host)
    ports = info.get("ports", {})

    print("\n" + "=" * 60)
    print("PLAYGROUND STAGING VM PROVISIONED")
    print("=" * 60)
    print(f"instance_id: {instance_id}")
    print(f"public_ip:   {public_ip}")
    if ssh_host and ssh_port:
        print(f"ssh:         ssh -p {ssh_port} root@{ssh_host}")
    print(f"ports:       {json.dumps(ports, indent=2)}")
    print()
    print("Bootstrap is running in-VM via --onstart-cmd (~3-5 min).")
    print("Once green, navigate to:")
    print(f"  UI:      http://{public_ip}/components")
    print(f"  Backend: http://{public_ip}/playground/components")
    print(f"  Health:  http://{public_ip}/health")
    print()
    print("Logs on the VM:")
    print("  /var/log/playground-backend.{out,err}.log")
    print("  /var/log/playground-frontend.{out,err}.log")
    print("  journalctl -u nginx")
    print("=" * 60)

    info_file = os.path.expanduser("~/.playground-staging-info.json")
    with open(info_file, "w") as f:
        json.dump({
            "instance_id": instance_id,
            "public_ip": public_ip,
            "ssh_host": ssh_host,
            "ssh_port": ssh_port,
            "ports": ports,
            "ui_url": f"http://{public_ip}/components",
            "backend_url": f"http://{public_ip}/playground/components",
        }, f, indent=2)
    print(f"\nConnection info saved to {info_file}")


if __name__ == "__main__":
    main()
