#!/usr/bin/env python3
"""Production Run Orchestrator.

Handles the full workflow:
1. Provision Vast.ai GPU VM
2. Bootstrap VM (install deps, download models from B2)
3. Start GPU worker service on VM
4. Run documentary pipeline in production mode
5. Upload output artifacts to B2
6. Terminate VM

Usage:
    python scripts/production_run.py \
        --topic "Cloudberry Jam" \
        --corpus /path/to/corpus.md \
        --language dual_ru_en \
        --b2-output-prefix cloudberry-v1

Env vars required:
    VAST_API_KEY        — Vast.ai API key
    B2_KEY_ID           — Backblaze B2 key ID
    B2_APPLICATION_KEY  — Backblaze B2 application key
    OPENROUTER_API_KEY  — OpenRouter API key (for LLM calls)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("production_run")

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(REPO_DIR, "server")


def run_cmd(cmd: list[str], timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    logger.info("Running: %s", " ".join(cmd[:6]) + ("..." if len(cmd) > 6 else ""))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        logger.error("Command failed (rc=%d): %s", result.returncode, result.stderr[:500])
        raise RuntimeError(f"Command failed: {' '.join(cmd[:4])}")
    return result


def vastai_cmd(args: list[str], timeout: int = 60) -> dict:
    """Run a vastai CLI command."""
    api_key = os.environ.get("VAST_API_KEY", "")
    if not api_key:
        raise RuntimeError("VAST_API_KEY not set")
    cmd = ["vastai", "--api-key", api_key] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"vastai failed: {result.stderr[:500]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"output": result.stdout.strip()}


def provision_vm(gpu_type: str = "RTX_4090", max_price: float = 0.50) -> dict:
    """Provision a Vast.ai GPU VM and wait for it to be ready."""
    logger.info("Searching for %s offers (max $%.2f/hr)...", gpu_type, max_price)

    # Search for offers
    offers = vastai_cmd([
        "search", "offers",
        "--type", "on-demand",
        "--gpu-name", gpu_type,
        "--min-gpu-ram", "24",
        "--max-dph", str(max_price),
        "--raw",
    ])

    if not isinstance(offers, list) or not offers:
        raise RuntimeError(f"No {gpu_type} offers found under ${max_price}/hr")

    # Sort by price
    offers.sort(key=lambda o: float(o.get("dph_total", 999)))
    best = offers[0]
    offer_id = best["id"]

    logger.info(
        "Best offer: #%s %s %.1fGB $%.3f/hr",
        offer_id, best.get("gpu_name"), float(best.get("gpu_ram", 0)) / 1024,
        float(best.get("dph_total", 0)),
    )

    # Create instance
    result = vastai_cmd([
        "create", "instance", str(offer_id),
        "--image", "pytorch/pytorch:2.3.0-cuda12.1-cudnn8-devel",
        "--disk", "80",
        "--raw",
    ])

    instance_id = result.get("new_contract")
    if not instance_id:
        raise RuntimeError(f"Failed to create instance: {result}")

    logger.info("Instance created: %s. Waiting for ready...", instance_id)

    # Wait for instance to be running (up to 5 min)
    for i in range(60):
        time.sleep(5)
        status = vastai_cmd(["show", "instance", str(instance_id), "--raw"])
        actual_status = status.get("actual_status", "")
        if actual_status == "running":
            ssh_host = status.get("ssh_host", "")
            ssh_port = status.get("ssh_port", "")
            logger.info("VM ready: ssh -p %s root@%s", ssh_port, ssh_host)
            return {
                "instance_id": str(instance_id),
                "ssh_host": ssh_host,
                "ssh_port": str(ssh_port),
                "gpu_name": best.get("gpu_name", ""),
                "price": float(best.get("dph_total", 0)),
            }
        if i % 6 == 0:
            logger.info("  Waiting... status=%s (%ds)", actual_status, i * 5)

    raise RuntimeError(f"VM {instance_id} did not become ready in 5 minutes")


def bootstrap_vm(vm: dict) -> None:
    """Bootstrap the VM with models and worker service."""
    ssh_args = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-p", vm["ssh_port"],
        f"root@{vm['ssh_host']}",
    ]

    b2_key_id = os.environ.get("B2_KEY_ID", "")
    b2_app_key = os.environ.get("B2_APPLICATION_KEY", "")

    # Copy bootstrap script and gpu_worker to VM
    scp_args = [
        "scp", "-o", "StrictHostKeyChecking=no",
        "-P", vm["ssh_port"],
    ]

    scripts_dir = os.path.join(REPO_DIR, "scripts")
    run_cmd(scp_args + [
        os.path.join(scripts_dir, "gpu_bootstrap.sh"),
        os.path.join(scripts_dir, "gpu_worker.py"),
        f"root@{vm['ssh_host']}:/workspace/",
    ], timeout=30, check=True)

    # Run bootstrap
    logger.info("Running bootstrap on VM (this downloads models from B2)...")
    bootstrap_env = f"B2_KEY_ID='{b2_key_id}' B2_APPLICATION_KEY='{b2_app_key}'"
    run_cmd(
        ssh_args + [f"{bootstrap_env} bash /workspace/gpu_bootstrap.sh"],
        timeout=600,
        check=True,
    )
    logger.info("Bootstrap complete.")


def start_gpu_worker(vm: dict) -> str:
    """Start the GPU worker on the VM and return the URL."""
    ssh_args = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-p", vm["ssh_port"],
        f"root@{vm['ssh_host']}",
    ]

    # Start worker in background
    run_cmd(
        ssh_args + [
            "source /workspace/.venv/bin/activate && "
            "nohup python /workspace/gpu_worker.py --port 8880 "
            "> /workspace/worker.log 2>&1 &"
        ],
        timeout=10,
        check=False,
    )

    # Wait for worker to be ready
    worker_url = f"http://{vm['ssh_host']}:8880"
    logger.info("Waiting for GPU worker at %s ...", worker_url)

    for i in range(30):
        time.sleep(2)
        try:
            result = run_cmd(
                ssh_args + ["curl -s http://localhost:8880/health"],
                timeout=10,
                check=False,
            )
            if '"status":"ok"' in result.stdout or '"status": "ok"' in result.stdout:
                logger.info("GPU worker ready!")

                # Pre-load models
                logger.info("Pre-loading models...")
                run_cmd(
                    ssh_args + [
                        "curl -s -X POST http://localhost:8880/load-models"
                    ],
                    timeout=300,
                    check=False,
                )
                return worker_url
        except Exception:
            pass
        if i % 5 == 0:
            logger.info("  Waiting for worker... (%ds)", i * 2)

    raise RuntimeError("GPU worker did not start in 60 seconds")


def setup_ssh_tunnel(vm: dict, local_port: int = 8880) -> subprocess.Popen:
    """Set up SSH tunnel to forward GPU worker port."""
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-N", "-L", f"{local_port}:localhost:8880",
        "-p", vm["ssh_port"],
        f"root@{vm['ssh_host']}",
    ]
    logger.info("Setting up SSH tunnel: localhost:%d -> VM:8880", local_port)
    proc = subprocess.Popen(cmd)
    time.sleep(2)
    if proc.poll() is not None:
        raise RuntimeError("SSH tunnel failed to start")
    return proc


def run_pipeline(
    topic: str,
    corpus_path: str,
    language: str,
    gpu_worker_url: str,
) -> int:
    """Run the documentary pipeline in production mode."""
    env = os.environ.copy()
    env["GPU_WORKER_URL"] = gpu_worker_url
    # Make sure test mode is off
    env.pop("DOCUMENTARY_TEST_MODE", None)

    cmd = [
        sys.executable, "run_pipeline.py",
        "--topic", topic,
        "--corpus", corpus_path,
        "--language", language,
    ]

    logger.info("Running pipeline: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        cwd=SERVER_DIR,
        env=env,
        timeout=1800,  # 30 min max
    )
    return result.returncode


def upload_to_b2(
    local_dir: str,
    bucket: str,
    prefix: str,
) -> None:
    """Upload output artifacts to B2."""
    logger.info("Uploading %s -> b2://%s/%s/", local_dir, bucket, prefix)

    # Set B2 env vars
    env = os.environ.copy()
    env["B2_APPLICATION_KEY_ID"] = env.get("B2_KEY_ID", "")

    cmd = [
        "b2", "sync",
        "--threads", "8",
        local_dir,
        f"b2://{bucket}/{prefix}/",
    ]

    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        logger.error("B2 upload failed: %s", result.stderr[:500])
        raise RuntimeError("B2 upload failed")
    logger.info("Upload complete.")


def terminate_vm(instance_id: str) -> None:
    """Terminate a Vast.ai VM."""
    logger.info("Terminating VM %s...", instance_id)
    try:
        vastai_cmd(["destroy", "instance", instance_id])
        logger.info("VM terminated.")
    except Exception as e:
        logger.error("Failed to terminate VM: %s", e)


def main():
    parser = argparse.ArgumentParser(description="Production Documentary Run")
    parser.add_argument("--topic", required=True, help="Documentary topic")
    parser.add_argument("--corpus", required=True, help="Path to corpus file")
    parser.add_argument("--language", default="dual_ru_en", help="Language mode")
    parser.add_argument("--gpu-type", default="RTX_4090", help="GPU type")
    parser.add_argument("--max-price", type=float, default=0.50, help="Max $/hr")
    parser.add_argument("--b2-output-prefix", default="", help="B2 output prefix")
    parser.add_argument("--b2-bucket", default="economy-vid-assets", help="B2 bucket")
    parser.add_argument("--skip-provision", action="store_true", help="Skip VM provisioning")
    parser.add_argument("--vm-host", default="", help="Existing VM SSH host")
    parser.add_argument("--vm-port", default="", help="Existing VM SSH port")
    parser.add_argument("--gpu-worker-url", default="", help="Existing GPU worker URL")
    parser.add_argument("--skip-upload", action="store_true", help="Skip B2 upload")
    parser.add_argument("--skip-terminate", action="store_true", help="Keep VM running")
    args = parser.parse_args()

    if not args.b2_output_prefix:
        # Auto-generate: topic-slug-v1
        slug = args.topic.lower().replace(" ", "-")[:20]
        args.b2_output_prefix = f"{slug}-v1"

    vm = None
    tunnel = None
    gpu_worker_url = args.gpu_worker_url

    try:
        # Step 1: Provision VM (unless skipped)
        if not args.skip_provision and not gpu_worker_url:
            vm = provision_vm(args.gpu_type, args.max_price)

            # Step 2: Bootstrap VM
            bootstrap_vm(vm)

            # Step 3: Start GPU worker
            start_gpu_worker(vm)

            # Step 4: Set up SSH tunnel
            tunnel = setup_ssh_tunnel(vm)
            gpu_worker_url = "http://localhost:8880"
        elif args.vm_host and args.vm_port:
            vm = {
                "instance_id": "manual",
                "ssh_host": args.vm_host,
                "ssh_port": args.vm_port,
            }

        if not gpu_worker_url:
            raise RuntimeError("No GPU worker URL available")

        # Step 5: Run pipeline
        logger.info("=" * 60)
        logger.info("Starting production pipeline run")
        logger.info("  Topic: %s", args.topic)
        logger.info("  Corpus: %s", args.corpus)
        logger.info("  Language: %s", args.language)
        logger.info("  GPU worker: %s", gpu_worker_url)
        logger.info("=" * 60)

        rc = run_pipeline(args.topic, args.corpus, args.language, gpu_worker_url)

        if rc != 0:
            logger.error("Pipeline failed with exit code %d", rc)
            sys.exit(rc)

        logger.info("Pipeline completed successfully!")

        # Step 6: Upload to B2
        if not args.skip_upload:
            output_dirs = [
                ("/tmp/documentary-pipeline/audio", f"{args.b2_output_prefix}/audio"),
                ("/tmp/documentary-pipeline/video", f"{args.b2_output_prefix}/clips"),
                ("/tmp/documentary-pipeline/output", f"{args.b2_output_prefix}/final"),
                ("/tmp/documentary-pipeline/timelines", f"{args.b2_output_prefix}/timelines"),
            ]
            for local_dir, prefix in output_dirs:
                if os.path.isdir(local_dir) and os.listdir(local_dir):
                    upload_to_b2(local_dir, args.b2_bucket, prefix)

        logger.info("=" * 60)
        logger.info("PRODUCTION RUN COMPLETE")
        logger.info("  Output prefix: %s/%s/", args.b2_bucket, args.b2_output_prefix)
        logger.info("=" * 60)

    finally:
        # Clean up
        if tunnel:
            tunnel.terminate()
            tunnel.wait()

        if vm and not args.skip_terminate and vm.get("instance_id") != "manual":
            terminate_vm(vm["instance_id"])


if __name__ == "__main__":
    main()
