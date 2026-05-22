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
import shlex
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


def provision_workers() -> dict:
    """Provision GPU workers using WorkerProvisioner (GAP 1.1).

    Delegates to the canonical WorkerProvisioner which enforces:
    - Correct VRAM floors (80 GB for LTX, not 48)
    - VM ownership registration (GAP 2.1)
    - Model manifest specs (GAP 1.2)
    - Credit-aware budgeting

    Returns dict with worker URLs and VM info.
    """
    sys.path.insert(0, os.path.join(REPO_DIR, "server"))
    from worker_provisioner import WorkerProvisioner

    provisioner = WorkerProvisioner()
    provisioner.ensure_workers_ready()

    # Collect worker info for downstream use
    result = {
        "vm_ids": provisioner.get_vm_ids(),
        "provisioner": provisioner,
    }

    # Extract worker URLs from specs
    for role in ("video", "tts"):
        spec = provisioner._get_spec(role)
        if spec and spec.vm_id:
            key_prefix = spec.capability  # "ltx" or "tts"
            result[f"{key_prefix}_url"] = f"http://localhost:{spec.local_port}"
            result[f"{key_prefix}_vm_id"] = spec.vm_id
            result[f"{key_prefix}_ssh_host"] = spec.ssh_host
            result[f"{key_prefix}_ssh_port"] = str(spec.ssh_port)

    logger.info("Workers provisioned: %s", list(result.keys()))
    return result


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
    bootstrap_env = f"B2_KEY_ID={shlex.quote(b2_key_id)} B2_APPLICATION_KEY={shlex.quote(b2_app_key)}"
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
            "nohup python3 /workspace/gpu_worker.py --port 8880 "
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
                ssh_args + ["curl -s http://localhost:8880/"],
                timeout=10,
                check=False,
            )
            text = result.stdout.strip()
            if text.startswith("ok"):
                logger.info("GPU worker ready! (%s)", text)
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

    # Use poetry run to ensure we're in the correct virtualenv with all deps
    cmd = [
        "poetry", "run", "python", "run_pipeline.py",
        "--topic", topic,
        "--corpus", corpus_path,
        "--language", language,
    ]

    logger.info("Running pipeline: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        cwd=SERVER_DIR,
        env=env,
        timeout=7200,  # 2 hours — 15 clips × ~5 min each + assembly
    )
    return result.returncode


def upload_to_b2(
    local_dir: str,
    bucket: str,
    prefix: str,
) -> None:
    """Upload output artifacts to B2."""
    logger.info("Uploading %s -> b2://%s/%s/", local_dir, bucket, prefix)

    b2_key_id = os.environ.get("B2_KEY_ID", "")
    b2_app_key = os.environ.get("B2_APPLICATION_KEY", "")
    if not b2_key_id or not b2_app_key:
        raise RuntimeError("B2_KEY_ID and B2_APPLICATION_KEY must be set for upload")

    # Authorize B2 CLI before upload
    auth_cmd = ["b2", "account", "authorize", b2_key_id, b2_app_key]
    auth_result = subprocess.run(auth_cmd, capture_output=True, text=True, timeout=30)
    if auth_result.returncode != 0:
        logger.error("B2 auth failed: %s", auth_result.stderr[:500])
        raise RuntimeError("B2 authorization failed")

    cmd = [
        "b2", "sync",
        "--threads", "8",
        local_dir,
        f"b2://{bucket}/{prefix}/",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        logger.error("B2 upload failed: %s", result.stderr[:500])
        raise RuntimeError("B2 upload failed")
    logger.info("Upload complete.")


def terminate_vm(instance_id: str) -> None:
    """Terminate a Vast.ai VM — ONLY if this pipeline created it (GAP 2.2)."""
    # GAP 2.2: Use the ownership-guarded terminate from vastai_tools
    sys.path.insert(0, os.path.join(REPO_DIR, "server"))
    try:
        from tools.vastai_tools import terminate_vm as _guarded_terminate
        result_json = _guarded_terminate(instance_id)
        import json as _json
        result = _json.loads(result_json)
        if result.get("status") == "refused":
            logger.error("VM termination REFUSED: %s", result.get("error", ""))
        else:
            logger.info("VM %s terminated.", instance_id)
    except ImportError:
        # Fallback if server package not importable — still log warning
        logger.warning(
            "Could not import ownership guard. Terminating VM %s directly "
            "(ownership check skipped).", instance_id,
        )
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
    parser.add_argument("--gpu-type", default="A100_SXM4", help="GPU type (must have 48GB+ VRAM)")
    parser.add_argument("--max-price", type=float, default=1.50, help="Max $/hr")
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
        # Step 1: Provision workers via WorkerProvisioner (GAP 1.1)
        # This handles VM creation, bootstrap, SSH tunnels, health checks,
        # and VM ownership registration — all in one canonical code path.
        provisioner = None
        if not args.skip_provision and not gpu_worker_url:
            workers = provision_workers()
            provisioner = workers.get("provisioner")
            # Use the LTX worker URL as the primary GPU worker
            gpu_worker_url = workers.get("ltx_url", "http://localhost:8881")
            # Set TTS worker URL in env for the pipeline
            tts_url = workers.get("tts_url", "")
            if tts_url:
                os.environ["TTS_WORKER_URL"] = tts_url
            # Set video worker URL
            os.environ["VIDEO_WORKER_URLS"] = gpu_worker_url
        elif args.vm_host and args.vm_port:
            vm = {
                "instance_id": "manual",
                "ssh_host": args.vm_host,
                "ssh_port": args.vm_port,
            }
            if not gpu_worker_url:
                # Existing VM but no worker URL — start worker + tunnel
                start_gpu_worker(vm)
                tunnel = setup_ssh_tunnel(vm)
                gpu_worker_url = "http://localhost:8880"

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

        # GAP 1.1: Clean up provisioner (kills SSH tunnels, destroys VMs)
        if provisioner is not None and not args.skip_terminate:
            try:
                provisioner.cleanup()
            except Exception as e:
                logger.error("Provisioner cleanup failed: %s", e)


if __name__ == "__main__":
    main()
