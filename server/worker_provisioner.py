"""
Worker provisioner — automatic GPU worker lifecycle management.

Architecture: **parallel lazy provisioning**

Instead of blocking the entire pipeline until all workers are ready,
provisioning runs in background threads.  The pipeline starts immediately
(scenario generation needs no GPU) and each stage waits only for the
specific worker it needs:

    t=0   Start TTS + Video provisioning in background threads
    t=0   Start scenario generation IMMEDIATELY (no GPU needed)
    t=3m  Scenario done.  Audio stage calls wait_for_worker("tts")
    t=5m  TTS ready -> audio proceeds.  Video still bootstrapping...
    t=10m Audio done -> visual direction (LLM only, no GPU)
    t=12m Visual direction done -> production calls wait_for_worker("video")
    t=20m Video ready -> production proceeds

This saves ~15-20 minutes vs the old sequential blocking approach.

VRAM calculation (bf16, no quantisation):

    TTS  (Qwen3-TTS-12Hz-1.7B):  1.7B x 2 bytes = 3.4 GB weights
         + KV cache + activations ~ 5-8 GB runtime -> min_vram = 8 GB

    Video (LTX-2.3 diffusers):   text_encoder ~46.6 GB
                                  transformer  ~37.8 GB
                                  vae + audio  ~ 6.6 GB
                                  Total loaded ~ 71 GB bf16
         + inference overhead   -> min_vram = 80 GB
         (gpu_worker.py: "Requires 80GB+ VRAM")

Budget: weighted split — video GPU costs ~20x more than TTS GPU,
so equal-splitting the budget wastes money on TTS and starves video.

Architecture invariants preserved:
- One model per VM — TTS and video on separate VMs.
- Workers must be healthy before any stage that needs them.
- VRAM requirements are **hard floors** — never lowered for cost.
- Never silently degrade — if provisioning fails, raise loud.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip().lower() in (
    "1",
    "true",
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class WorkerSpec:
    """Specification for a GPU worker to provision."""

    role: str  # "tts" or "video"
    env_var: str  # e.g. "TTS_WORKER_URL"
    local_port: int  # localhost port for SSH tunnel
    remote_port: int  # port on the GPU VM
    capability: str  # health check key (e.g. "tts", "ltx")
    gpu_type: str = "A100_SXM4"
    min_vram_gb: int = 48
    max_price: float = 2.00
    min_disk_gb: int = 50  # per-worker disk search filter
    disk_gb: int = 64  # --disk arg for vast create instance
    worker_mode: str = "tts"  # gpu_worker.py --mode argument
    vm_id: str = ""  # populated after provisioning
    ssh_host: str = ""
    ssh_port: int = 0
    tunnel_proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    # Parallel provisioning status — used by background threads
    status: str = "pending"  # "pending", "provisioning", "healthy", "failed"
    error: str = ""  # error message if status == "failed"
    ready_event: threading.Event = field(default_factory=threading.Event)
    # Bootstrap error detail — populated by wait_for_worker_healthy when the
    # worker's /health endpoint reports a bootstrap failure.  This gives the
    # provisioner (and recovery middleware) structured information about WHY
    # the worker failed, not just that it did.
    bootstrap_error: str = ""
    bootstrap_error_category: str = ""  # "auth", "network", "disk", "missing_file", "runtime"


# ---------------------------------------------------------------------------
# Default worker specs — VRAM calculated from actual model sizes
# ---------------------------------------------------------------------------
#
# TTS: Qwen3-TTS-12Hz-1.7B-VoiceDesign
#   1.7B params x 2 bytes (bf16) = 3.4 GB model weights
#   + KV cache + activations ~ 5-8 GB total
#   -> min_vram_gb = 8 (safe floor with headroom)
#   -> gpu_type = any cheap GPU with >= 12 GB (RTX 3060, 3070, etc.)
#   -> disk: WORKER_MODE=tts skips LTX models, only downloads ~4.3 GB TTS model
#     so TTS VM needs ~50 GB (4.3 GB model + ~30 GB OS/software + headroom)
#
# Video: LTX-2.3 (dg845/LTX-2.3-Diffusers)
#   text_encoder: ~46.6 GB (transformers format)
#   transformer:  ~37.8 GB (diffusers format)
#   vae + audio_vae + vocoder + connectors + latent_upsampler: ~6.6 GB
#   Total loaded in VRAM: ~71 GB bf16
#   + inference overhead (latent upsampler, two-pass pipeline): ~9 GB
#   -> min_vram_gb = 80 (gpu_worker.py: "Requires 80GB+ VRAM")
#   -> gpu_type = A100_SXM4 (80 GB) or H100/H200
#   -> disk: ~95 GB models + ~30 GB OS + ~20 GB output = ~200 GB

TTS_SPEC = WorkerSpec(
    role="tts",
    env_var="TTS_WORKER_URL",
    local_port=8880,
    remote_port=8880,
    capability="tts",
    gpu_type="RTX_4000",      # cheap GPU; broadened automatically if unavailable
    min_vram_gb=8,             # 1.7B model at bf16 = 3.4 GB + overhead
    max_price=1.00,            # fallback ceiling; overridden by weighted budget
    min_disk_gb=50,            # ~4.3 GB TTS model + ~30 GB OS/software (WORKER_MODE=tts skips LTX)
    disk_gb=64,                # --disk arg (comfortable headroom for TTS-only)
    worker_mode="tts",
)

VIDEO_SPEC = WorkerSpec(
    role="video",
    env_var="GPU_WORKER_URL",
    local_port=8881,
    remote_port=8880,
    capability="ltx",
    gpu_type="A100_SXM4",     # 80 GB VRAM; broadened to H100/H200 if unavailable
    min_vram_gb=80,            # ~71 GB bf16 model loaded fully on GPU
    max_price=5.00,            # fallback ceiling; overridden by weighted budget
    min_disk_gb=200,           # ~95 GB models + OS + output
    disk_gb=224,               # --disk arg
    worker_mode="ltx",
)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def check_worker_health(url: str, capability: str, timeout: int = 10) -> bool:
    """Check if a worker at the given URL is healthy and has the capability loaded.

    Returns True if healthy, False otherwise.
    """
    health_url = f"{url.rstrip('/')}/health"
    try:
        req = Request(health_url)
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") != "ok":
            return False
        loaded_key = f"{capability}_loaded"
        return bool(data.get(loaded_key, False))
    except (URLError, OSError, json.JSONDecodeError, TimeoutError, Exception):
        return False


def check_worker_reachable(url: str, timeout: int = 5) -> bool:
    """Check if a worker URL is reachable (responds to /health, any status)."""
    health_url = f"{url.rstrip('/')}/health"
    try:
        req = Request(health_url)
        with urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Vast.ai account & budget
# ---------------------------------------------------------------------------

# Minimum credit reserve — never spend the last few dollars so the account
# doesn't hit zero mid-run.
_CREDIT_RESERVE = 5.0

# Estimated maximum pipeline duration in hours.  Used to convert credits
# into a safe per-worker $/hr ceiling.
_ESTIMATED_RUN_HOURS = 2.0

# Per-worker price ceilings by GPU tier.
# TTS GPUs (8-24 GB) cost $0.05-0.30/hr on Vast.ai.
# Video GPUs (80 GB+) cost $1.50-4.00/hr on Vast.ai.
_TTS_PRICE_CEILING = 1.00
_VIDEO_PRICE_CEILING = 10.00

# Budget weight — video uses ~90% of the GPU budget because an 80GB+
# GPU costs ~20x more than a cheap 12 GB TTS GPU.
_TTS_BUDGET_WEIGHT = 0.10
_VIDEO_BUDGET_WEIGHT = 0.90


def get_account_credits() -> float:
    """Query Vast.ai account and return available credit balance in USD."""
    result = _vast_cmd(["show", "user", "--raw"])
    if isinstance(result, dict):
        credit = float(result.get("credit", 0.0))
        balance = float(result.get("balance", 0.0))
        total = credit + balance
        logger.info(
            "Vast.ai account: credit=$%.2f, balance=$%.2f, total=$%.2f",
            credit, balance, total,
        )
        return total
    raise RuntimeError(f"Could not read Vast.ai account info: {result}")


def calculate_weighted_budgets(
    estimated_hours: float = _ESTIMATED_RUN_HOURS,
) -> tuple[float, float]:
    """Calculate per-worker $/hr budgets weighted by GPU tier.

    Video GPUs cost ~20x more than TTS GPUs, so equal-splitting the
    budget wastes money on TTS and leaves too little for video.

    Returns (tts_budget, video_budget) in $/hr.
    """
    credits = get_account_credits()
    usable = credits - _CREDIT_RESERVE
    if usable <= 0:
        raise RuntimeError(
            f"Insufficient Vast.ai credits: ${credits:.2f} "
            f"(reserve=${_CREDIT_RESERVE:.2f}). "
            f"Top up at https://cloud.vast.ai/billing/"
        )

    total_hourly = usable / max(estimated_hours, 0.5)

    tts_budget = min(total_hourly * _TTS_BUDGET_WEIGHT, _TTS_PRICE_CEILING)
    video_budget = min(total_hourly * _VIDEO_BUDGET_WEIGHT, _VIDEO_PRICE_CEILING)

    # Safety: if combined exceeds total, scale down proportionally
    combined = tts_budget + video_budget
    if combined > total_hourly:
        scale = total_hourly / combined
        tts_budget *= scale
        video_budget *= scale

    logger.info(
        "Weighted budget: $%.2f usable / %.1fh = $%.2f/hr total. "
        "TTS: $%.2f/hr (weight %.0f%%, ceiling $%.2f). "
        "Video: $%.2f/hr (weight %.0f%%, ceiling $%.2f).",
        usable, estimated_hours, total_hourly,
        tts_budget, _TTS_BUDGET_WEIGHT * 100, _TTS_PRICE_CEILING,
        video_budget, _VIDEO_BUDGET_WEIGHT * 100, _VIDEO_PRICE_CEILING,
    )
    return tts_budget, video_budget


# Keep the old function for backward compatibility (infra_agent etc.)
def calculate_budget_per_worker(
    num_workers: int,
    estimated_hours: float = _ESTIMATED_RUN_HOURS,
) -> float:
    """Legacy equal-split budget.  Prefer calculate_weighted_budgets()."""
    credits = get_account_credits()
    usable = credits - _CREDIT_RESERVE
    if usable <= 0:
        raise RuntimeError(
            f"Insufficient Vast.ai credits: ${credits:.2f} "
            f"(reserve=${_CREDIT_RESERVE:.2f}). "
            f"Top up at https://cloud.vast.ai/billing/"
        )
    budget = usable / max(num_workers, 1) / max(estimated_hours, 0.5)
    capped = min(budget, _VIDEO_PRICE_CEILING)
    return capped


# ---------------------------------------------------------------------------
# Vast.ai provisioning
# ---------------------------------------------------------------------------


def _vast_cmd(args: list[str]) -> dict | list | str:
    """Run a vastai CLI command and return parsed output."""
    api_key = os.environ.get("VAST_API_KEY", "")
    if not api_key:
        raise RuntimeError("VAST_API_KEY not set — cannot provision GPU workers")

    cmd = ["vastai", "--api-key", api_key] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(
                f"vastai command failed (rc={result.returncode}): "
                f"{result.stderr[:500]}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()
    except FileNotFoundError:
        raise RuntimeError("vastai CLI not installed")
    except subprocess.TimeoutExpired:
        raise RuntimeError("vastai command timed out")


def provision_vm(spec: WorkerSpec) -> str:
    """Provision a Vast.ai GPU VM for the given worker spec.

    Returns the instance ID.
    """
    logger.info(
        "Provisioning %s worker: gpu=%s, vram>=%dGB, max $%.2f/hr, "
        "disk>=%dGB (--disk %d)",
        spec.role, spec.gpu_type, spec.min_vram_gb, spec.max_price,
        spec.min_disk_gb, spec.disk_gb,
    )

    # Search for offers — VRAM is a hard floor, never compromised.
    # Use query-string filter format for vastai CLI.
    # IMPORTANT: The vastai CLI search filter treats gpu_ram in **GB**,
    # but the API response returns gpu_ram in **MB**.  Empirically verified:
    #   gpu_ram>=8   -> 64 offers (GTX 1070 Ti with gpu_ram=8192 in response)
    #   gpu_ram>=8192 -> 0 offers
    vram_gb = spec.min_vram_gb
    vram_mb = spec.min_vram_gb * 1024  # for Python-side post-filter only
    query = (
        f"gpu_name={spec.gpu_type} "
        f"gpu_ram>={vram_gb} "
        f"dph_total<={spec.max_price} "
        f"rentable=true "
        f"disk_space>={spec.min_disk_gb}"
    )

    search_result = _vast_cmd([
        "search", "offers",
        "--type", "on-demand",
        "--order", "dph_total",
        "--raw",
        query,
    ])

    offers = search_result if isinstance(search_result, list) else []

    # If no offers for exact GPU type, broaden to ANY GPU that meets the
    # VRAM floor.  Never lower VRAM — that would force quantisation.
    if not offers:
        logger.warning(
            "No %s offers found, broadening search to any GPU with >=%dGB VRAM",
            spec.gpu_type, spec.min_vram_gb,
        )
        query = (
            f"gpu_ram>={vram_gb} "
            f"dph_total<={spec.max_price} "
            f"rentable=true "
            f"disk_space>={spec.min_disk_gb}"
        )
        search_result = _vast_cmd([
            "search", "offers",
            "--type", "on-demand",
            "--order", "dph_total",
            "--raw",
            query,
        ])
        offers = search_result if isinstance(search_result, list) else []

    # Python-side filtering as safety net (CLI filters can be unreliable)
    if offers:
        filtered = []
        for o in offers:
            o_vram = float(o.get("gpu_ram", 0))
            o_price = float(o.get("dph_total", 999))
            o_disk = float(o.get("disk_space", 0))
            if (
                o_vram >= vram_mb
                and o_price <= spec.max_price
                and o_disk >= spec.min_disk_gb
            ):
                filtered.append(o)
        if len(filtered) < len(offers):
            logger.info(
                "Python-side filter: %d/%d offers passed "
                "(vram>=%dMB, price<=$%.2f, disk>=%dGB)",
                len(filtered), len(offers),
                vram_mb, spec.max_price, spec.min_disk_gb,
            )
        offers = filtered

    if not offers:
        raise RuntimeError(
            f"No GPU offers found for {spec.role} worker "
            f"(min {spec.min_vram_gb}GB VRAM, max ${spec.max_price:.2f}/hr, "
            f"min disk {spec.min_disk_gb}GB). "
            f"VRAM floor is non-negotiable (no quantisation). "
            f"Current account budget allows up to ${spec.max_price:.2f}/hr."
        )

    # Sort by price and pick cheapest
    sorted_offers = sorted(
        offers, key=lambda o: float(o.get("dph_total", 999))
    )
    best = sorted_offers[0]
    offer_id = best.get("id")

    logger.info(
        "Selected offer %s: %s %dx, %.1fGB VRAM, $%.3f/hr, %.0fGB disk",
        offer_id,
        best.get("gpu_name", "unknown"),
        best.get("num_gpus", 1),
        float(best.get("gpu_ram", 0)) / 1024,
        float(best.get("dph_total", 0)),
        float(best.get("disk_space", 0)),
    )

    # Create instance with bootstrap onstart
    b2_key_id = os.environ.get("B2_KEY_ID", "")
    b2_app_key = os.environ.get("B2_APPLICATION_KEY", "")
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

    # Use 'export' so env vars survive through the && chain.
    # Inline VAR=val only applies to the immediate next command,
    # but Vast.ai's onstart runner may wrap the whole string in sh -c
    # which loses inline vars for later commands in the chain.

    # Auto-detect the current git branch so VMs clone the same branch
    # (the bootstrap script may have fixes not yet merged to main).
    try:
        _branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            text=True,
        ).strip()
        if not _branch or _branch == "HEAD":
            _branch = "main"
    except Exception:
        _branch = "main"
    logger.info("VMs will clone branch: %s", _branch)

    # Architecture: the worker starts FIRST (FastAPI immediately reachable),
    # then runs bootstrap + model loading in a background thread.  The /health
    # endpoint reports structured bootstrap status so the provisioner can see
    # exactly what's happening and escalate failures immediately — no more
    # blind timeouts.  The onstart installs minimal system deps + pip deps
    # needed for gpu_worker.py to start, then launches the worker which
    # handles the rest (model downloads, loading) internally.
    onstart = (
        f"export B2_KEY_ID={shlex.quote(b2_key_id)} && "
        f"export B2_APPLICATION_KEY={shlex.quote(b2_app_key)} && "
        f"export WORKER_MODE={shlex.quote(spec.worker_mode)} && "
        f"export DASHSCOPE_API_KEY={shlex.quote(dashscope_key)} && "
        f"export OPENROUTER_API_KEY={shlex.quote(openrouter_key)} && "
        # Pass TORCH_INDEX so the bootstrap script uses the same CUDA wheel
        # index as this onstart command (prevents cu124 overwriting cu130).
        "export TORCH_INDEX=https://download.pytorch.org/whl/cu130 && "
        "apt-get update && apt-get install -y git curl ffmpeg libsndfile1 sox libsox-dev && "
        f"git clone -b {shlex.quote(_branch)} --single-branch "
        "https://github.com/OrpingtonClose/economy-documentary.git "
        "/workspace/economy-documentary 2>/dev/null || "
        f"(cd /workspace/economy-documentary && git fetch origin {shlex.quote(_branch)} && "
        f"git checkout {shlex.quote(_branch)} && git pull origin {shlex.quote(_branch)}) && "
        # Install Python deps needed for gpu_worker.py to start (FastAPI + torch).
        # The bootstrap script installs the rest (ltx-pipelines, qwen-tts, etc.)
        # but we need enough to start the health endpoint immediately.
        # IMPORTANT: The Docker image has conda torch 2.6.0 which satisfies
        # 'torch>=2.6.0', so pip would skip the cu130 install.  We must force-
        # uninstall the conda version first so pip actually installs cu130 wheels.
        "pip uninstall -y torch torchvision torchaudio 2>/dev/null; "
        "pip install --no-cache-dir "
        "'torch>=2.6.0' 'torchvision>=0.21.0' 'torchaudio>=2.6.0' "
        "--index-url https://download.pytorch.org/whl/cu130 && "
        "pip install --no-cache-dir "
        "'fastapi>=0.100.0' 'uvicorn>=0.20.0' 'pydantic>=2.0.0' "
        "'numpy>=1.26.0,<2.0.0' 'soundfile>=0.12.0' && "
        # Register NVIDIA pip package libs with ldconfig so libcudart.so.13
        # is discoverable system-wide by any process (including ltx-core).
        # PyTorch cu130 installs nvidia-cuda-runtime to site-packages/nvidia/*/lib/
        "python3 -c \""
        "import os,site,pathlib;"
        "nv_dirs=[str(p) for sp in site.getsitepackages() "
        "for p in pathlib.Path(sp,'nvidia').glob('*/lib') if p.is_dir()];"
        "open('/etc/ld.so.conf.d/nvidia-pip.conf','w').write(chr(10).join(nv_dirs)+chr(10)) if nv_dirs else None;"
        "print(f'Registered {len(nv_dirs)} nvidia lib dirs')\" && "
        "ldconfig && "
        # Start the worker — it handles bootstrap internally and reports
        # structured status via /health endpoint.
        "python3 /workspace/economy-documentary/scripts/gpu_worker.py "
        f"--mode {shlex.quote(spec.worker_mode)} --port {spec.remote_port}"
    )

    # NOTE: Do NOT use --raw here.  `vastai create instance --raw` returns
    # an empty string.  Without --raw it returns text like:
    #   Started. {'success': True, 'new_contract': 34856082, ...}
    create_result = _vast_cmd([
        "create", "instance",
        str(offer_id),
        "--image", "pytorch/pytorch:2.6.0-cuda12.4-cudnn9-devel",
        "--disk", str(spec.disk_gb),
        "--ssh",
        "--direct",
        "--onstart-cmd", onstart,
    ])

    # Parse the response — could be dict (if CLI returns JSON) or a string
    # containing a Python dict literal like "Started. {'new_contract': ...}"
    if isinstance(create_result, dict):
        instance_id = create_result.get("new_contract")
        if instance_id:
            spec.vm_id = str(instance_id)
            logger.info("VM provisioned: instance_id=%s", spec.vm_id)
            return spec.vm_id

    # Try to extract new_contract from text response
    if isinstance(create_result, str) and "new_contract" in create_result:
        match = re.search(r"'new_contract'\s*:\s*(\d+)", create_result)
        if match:
            spec.vm_id = match.group(1)
            logger.info("VM provisioned: instance_id=%s (parsed from text)", spec.vm_id)
            return spec.vm_id

    raise RuntimeError(
        f"Failed to provision {spec.role} VM: unexpected response: {create_result}"
    )


def wait_for_vm_running(spec: WorkerSpec, timeout: int = 600) -> dict:
    """Wait for a provisioned VM to reach 'running' status.

    Returns the VM info dict with SSH connection details.
    """
    logger.info(
        "Waiting for %s VM %s to start (timeout %ds)...",
        spec.role, spec.vm_id, timeout,
    )
    start = time.time()
    while time.time() - start < timeout:
        try:
            result = _vast_cmd(["show", "instance", spec.vm_id, "--raw"])
            if isinstance(result, dict):
                status = result.get(
                    "actual_status", result.get("status_msg", "unknown")
                )
                elapsed = int(time.time() - start)
                logger.info(
                    "  %s VM %s: status=%s (%ds)",
                    spec.role, spec.vm_id, status, elapsed,
                )
                if status == "running":
                    spec.ssh_host = result.get("ssh_host", "")
                    spec.ssh_port = int(result.get("ssh_port", 0))
                    return result
        except Exception as exc:
            logger.warning("  Error checking VM status: %s", exc)

        time.sleep(15)

    raise RuntimeError(
        f"{spec.role} VM {spec.vm_id} did not reach 'running' "
        f"within {timeout}s"
    )


# ---------------------------------------------------------------------------
# SSH tunnel management
# ---------------------------------------------------------------------------


def setup_ssh_tunnel(
    spec: WorkerSpec, max_retries: int = 12, retry_delay: int = 15,
) -> subprocess.Popen:
    """Set up an SSH tunnel from localhost:local_port to the GPU VM.

    Retries up to *max_retries* times with *retry_delay* seconds between
    attempts because the VM's SSH daemon may not be ready immediately
    after Vast.ai reports status=running.

    Returns the tunnel subprocess.
    """
    if not spec.ssh_host or not spec.ssh_port:
        raise RuntimeError(
            f"Cannot set up SSH tunnel for {spec.role}: "
            f"no SSH connection details (host={spec.ssh_host}, port={spec.ssh_port})"
        )

    tunnel_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-N",  # no remote command
        "-L", f"{spec.local_port}:localhost:{spec.remote_port}",
        "-p", str(spec.ssh_port),
        f"root@{spec.ssh_host}",
    ]

    last_err = ""
    for attempt in range(1, max_retries + 1):
        logger.info(
            "Setting up SSH tunnel (attempt %d/%d): localhost:%d -> %s:%d (via %s:%d)",
            attempt, max_retries,
            spec.local_port, spec.ssh_host, spec.remote_port,
            spec.ssh_host, spec.ssh_port,
        )

        proc = subprocess.Popen(
            tunnel_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # Give the tunnel a moment to establish
        time.sleep(3)

        if proc.poll() is not None:
            last_err = proc.stderr.read().decode() if proc.stderr else ""
            logger.warning(
                "SSH tunnel attempt %d/%d for %s failed: %s",
                attempt, max_retries, spec.role, last_err.strip(),
            )
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            raise RuntimeError(
                f"SSH tunnel for {spec.role} failed after {max_retries} attempts: {last_err}"
            )

        # Close stderr pipe to prevent buffer-fill blocking the SSH process.
        # We only needed it for the immediate-failure diagnostic above.
        if proc.stderr:
            proc.stderr.close()

        spec.tunnel_proc = proc
        logger.info(
            "SSH tunnel established: localhost:%d -> %s VM %s",
            spec.local_port, spec.role, spec.vm_id,
        )
        return proc

    # Should not reach here, but just in case
    raise RuntimeError(
        f"SSH tunnel for {spec.role} failed after {max_retries} attempts: {last_err}"
    )


# ---------------------------------------------------------------------------
# Wait for worker health
# ---------------------------------------------------------------------------


def _get_worker_health_detail(url: str, timeout: int = 10) -> dict | None:
    """Fetch full health JSON from a worker, including bootstrap status.

    Returns the parsed dict, or None if unreachable.
    """
    health_url = f"{url.rstrip('/')}/health"
    try:
        req = Request(health_url)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def wait_for_worker_healthy(
    spec: WorkerSpec,
    timeout: int = 900,
    poll_interval: int = 15,
) -> bool:
    """Wait for a worker to become healthy after provisioning.

    The worker needs time to:
    1. Boot the VM and start gpu_worker.py (FastAPI starts immediately)
    2. Run bootstrap in background (install deps, download models)
    3. Load the model into VRAM

    The worker's /health endpoint reports structured bootstrap status so we
    can see exactly what's happening and escalate failures immediately
    rather than waiting for a blind timeout.
    """
    url = f"http://localhost:{spec.local_port}"
    logger.info(
        "Waiting for %s worker at %s to become healthy (timeout %ds)...",
        spec.role, url, timeout,
    )

    start = time.time()
    last_status = "unknown"
    last_bootstrap_phase = ""
    while time.time() - start < timeout:
        elapsed = int(time.time() - start)

        # Check if tunnel is still alive
        if spec.tunnel_proc and spec.tunnel_proc.poll() is not None:
            logger.warning(
                "SSH tunnel for %s died — restarting...", spec.role
            )
            try:
                setup_ssh_tunnel(spec)
            except Exception as exc:
                logger.error("Failed to restart tunnel: %s", exc)

        # Fetch full health detail (includes bootstrap status)
        health_data = _get_worker_health_detail(url, timeout=10)

        if health_data is not None:
            # --- Bootstrap error escalation ---
            bootstrap = health_data.get("bootstrap") or {}
            bootstrap_phase = bootstrap.get("phase", "")
            bootstrap_error = bootstrap.get("error", "")
            bootstrap_category = bootstrap.get("error_category", "")

            if bootstrap_phase == "error":
                # Bootstrap has failed — escalate immediately instead of
                # waiting for the full timeout.  This is the key integration
                # with the recovery architecture: structured error information
                # flows from the VM back to the provisioner.
                logger.error(
                    "BOOTSTRAP FAILED on %s worker (category=%s): %s",
                    spec.role, bootstrap_category, bootstrap_error,
                )
                # Store error on the spec so callers can inspect it
                spec.bootstrap_error = bootstrap_error
                spec.bootstrap_error_category = bootstrap_category
                return False

            # Log phase transitions
            if bootstrap_phase and bootstrap_phase != last_bootstrap_phase:
                logger.info(
                    "  %s worker bootstrap phase: %s — %s (%ds)",
                    spec.role, bootstrap_phase,
                    bootstrap.get("detail", ""), elapsed,
                )
                last_bootstrap_phase = bootstrap_phase

            # Check if model is loaded (healthy)
            if health_data.get("status") == "ok":
                loaded_key = f"{spec.capability}_loaded"
                if health_data.get(loaded_key, False):
                    logger.info(
                        "%s worker at %s is HEALTHY after %ds",
                        spec.role, url, elapsed,
                    )
                    return True
                if last_status != "reachable_not_loaded":
                    logger.info(
                        "  %s worker reachable but model not loaded yet (%ds)",
                        spec.role, elapsed,
                    )
                    last_status = "reachable_not_loaded"
        else:
            if last_status != "unreachable":
                logger.info(
                    "  %s worker not yet reachable (%ds) — "
                    "VM still bootstrapping...",
                    spec.role, elapsed,
                )
                last_status = "unreachable"

        time.sleep(poll_interval)

    logger.error(
        "%s worker at %s did not become healthy within %ds",
        spec.role, url, timeout,
    )
    return False


# ---------------------------------------------------------------------------
# High-level orchestrator — parallel lazy provisioning
# ---------------------------------------------------------------------------


class WorkerProvisioner:
    """Manages the full lifecycle of GPU workers for the pipeline.

    Two-phase API for parallel lazy provisioning:

    1. ``start_provisioning()`` — **non-blocking**.  Kicks off background
       threads to provision each worker in parallel.  Call from
       ``_init_pipeline_state`` so VMs start booting while the scenario
       stage runs (no GPU needed).

    2. ``wait_for_worker(role)`` — **blocking per-worker**.  Called by
       each stage's before_callback to wait for only the worker it needs.
       Audio waits for TTS; production waits for video.

    3. ``cleanup()`` — kill tunnels, destroy VMs, stop InfraAgent.
    """

    def __init__(self) -> None:
        self._specs: list[WorkerSpec] = []
        self._lock = threading.Lock()
        self._provisioned = False
        self._threads: dict[str, threading.Thread] = {}
        self._provision_start_error: str = ""
        # Event signalled after start_provisioning() populates _specs
        # (or fails).  wait_for_worker() waits on this before checking
        # _specs so it doesn't race against the background launcher.
        self._specs_ready = threading.Event()

    # ------------------------------------------------------------------
    # Phase 1: Non-blocking — kick off background provisioning
    # ------------------------------------------------------------------

    def start_provisioning(
        self,
        require_tts: bool = True,
        require_video: bool = True,
    ) -> None:
        """Start provisioning workers in parallel background threads.

        Returns immediately.  Each worker's status is tracked in its
        WorkerSpec.status and signalled via WorkerSpec.ready_event.

        Call ``wait_for_worker(role)`` later to block until a specific
        worker is ready.
        """
        # Clear any stale error from a previous run so re-runs aren't
        # poisoned by old failures on this singleton.
        self._provision_start_error = ""
        self._specs_ready.clear()

        if _TEST_MODE:
            logger.info(
                "WorkerProvisioner: TEST MODE — skipping worker provisioning"
            )
            self._specs_ready.set()
            return

        # Build specs from defaults
        specs_needed: list[WorkerSpec] = []
        if require_tts:
            specs_needed.append(WorkerSpec(
                role="tts",
                env_var="TTS_WORKER_URL",
                local_port=TTS_SPEC.local_port,
                remote_port=TTS_SPEC.remote_port,
                capability="tts",
                gpu_type=TTS_SPEC.gpu_type,
                min_vram_gb=TTS_SPEC.min_vram_gb,
                max_price=TTS_SPEC.max_price,
                min_disk_gb=TTS_SPEC.min_disk_gb,
                disk_gb=TTS_SPEC.disk_gb,
                worker_mode="tts",
            ))
        if require_video:
            specs_needed.append(WorkerSpec(
                role="video",
                env_var="GPU_WORKER_URL",
                local_port=VIDEO_SPEC.local_port,
                remote_port=VIDEO_SPEC.remote_port,
                capability="ltx",
                gpu_type=VIDEO_SPEC.gpu_type,
                min_vram_gb=VIDEO_SPEC.min_vram_gb,
                max_price=VIDEO_SPEC.max_price,
                min_disk_gb=VIDEO_SPEC.min_disk_gb,
                disk_gb=VIDEO_SPEC.disk_gb,
                worker_mode="ltx",
            ))

        with self._lock:
            self._specs = specs_needed
        # Signal that _specs is populated so wait_for_worker() can proceed.
        self._specs_ready.set()

        # --- Credit-aware weighted budget ---
        # Check which workers actually need provisioning
        need_tts = False
        need_video = False
        for spec in specs_needed:
            url = os.environ.get(spec.env_var, "")
            if not url:
                url = f"http://localhost:{spec.local_port}"
            if check_worker_health(url, spec.capability):
                logger.info(
                    "%s worker at %s already healthy — marking ready",
                    spec.role, url,
                )
                spec.status = "healthy"
                spec.ready_event.set()
                os.environ[spec.env_var] = url
            else:
                spec.status = "pending"
                if spec.role == "tts":
                    need_tts = True
                else:
                    need_video = True

        if need_tts or need_video:
            try:
                tts_budget, video_budget = calculate_weighted_budgets()
                for spec in specs_needed:
                    if spec.status != "healthy":
                        if spec.role == "tts":
                            spec.max_price = tts_budget
                        else:
                            spec.max_price = video_budget
                logger.info(
                    "Weighted budgets applied: TTS=$%.2f/hr, Video=$%.2f/hr. "
                    "VRAM floors: TTS>=%dGB, Video>=%dGB.",
                    tts_budget, video_budget,
                    TTS_SPEC.min_vram_gb, VIDEO_SPEC.min_vram_gb,
                )
            except Exception as exc:
                logger.error("Budget calculation failed: %s", exc)
                for spec in specs_needed:
                    if spec.status != "healthy":
                        spec.status = "failed"
                        spec.error = str(exc)
                        spec.ready_event.set()
                return

        # Launch background threads for workers that need provisioning
        for spec in specs_needed:
            if spec.status == "pending":
                t = threading.Thread(
                    target=self._provision_worker_thread,
                    args=(spec,),
                    name=f"provision-{spec.role}",
                    daemon=True,
                )
                self._threads[spec.role] = t
                t.start()
                logger.info(
                    "Background provisioning started for %s worker",
                    spec.role,
                )

    def _provision_worker_thread(self, spec: WorkerSpec) -> None:
        """Background thread: provision a single worker end-to-end.

        Updates spec.status and signals spec.ready_event when done.
        """
        spec.status = "provisioning"
        try:
            self._provision_and_connect(spec, timeout=2400)
            spec.status = "healthy"

            # Update env var so contracts see the new URL
            new_url = f"http://localhost:{spec.local_port}"
            os.environ[spec.env_var] = new_url
            logger.info(
                "Background provisioning COMPLETE for %s: %s=%s (VM %s)",
                spec.role, spec.env_var, new_url, spec.vm_id,
            )
        except Exception as exc:
            spec.status = "failed"
            spec.error = str(exc)
            logger.error(
                "Background provisioning FAILED for %s: %s", spec.role, exc,
            )
            # Only clean up the SSH tunnel — do NOT destroy the VM.
            # The user explicitly forbade auto-destroying VMs on failure.
            # The VM stays running so it can be debugged or retried.
            if spec.tunnel_proc and spec.tunnel_proc.poll() is None:
                logger.info(
                    "Cleaning up SSH tunnel for %s (pid=%d)",
                    spec.role, spec.tunnel_proc.pid,
                )
                spec.tunnel_proc.terminate()
                try:
                    spec.tunnel_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    spec.tunnel_proc.kill()
        finally:
            spec.ready_event.set()

    # ------------------------------------------------------------------
    # Phase 2: Blocking per-worker — called by stage before_callbacks
    # ------------------------------------------------------------------

    def wait_for_worker(
        self,
        role: str,
        timeout: int = 2700,
    ) -> bool:
        """Wait for a specific worker to be ready.

        Called by stage before_callbacks:
        - Audio stage calls wait_for_worker("tts")
        - Production stage calls wait_for_worker("video")

        Returns True if the worker is healthy.
        Raises RuntimeError if provisioning failed or timed out.
        """
        if _TEST_MODE:
            return True

        # Wait for start_provisioning() to populate _specs.  When
        # provisioning runs in a background thread there's a window
        # where _specs is still empty.  Use a generous 120s ceiling
        # (start_provisioning spec-building is <30s in practice).
        if not self._specs_ready.wait(timeout=120):
            raise RuntimeError(
                "Timed out waiting for start_provisioning() to "
                "populate worker specs (120s)"
            )

        # If start_provisioning() itself failed in the background thread,
        # surface that error clearly instead of the confusing "No spec found".
        if self._provision_start_error:
            raise RuntimeError(
                f"Worker provisioning failed to start: "
                f"{self._provision_start_error}"
            )

        spec = self._get_spec(role)
        if spec is None:
            raise RuntimeError(
                f"No {role} worker spec found — "
                f"was start_provisioning() called?"
            )

        if spec.status == "healthy":
            return True

        logger.info(
            "Stage waiting for %s worker (status=%s, timeout=%ds)...",
            role, spec.status, timeout,
        )

        # Wait for background thread to finish
        ready = spec.ready_event.wait(timeout=timeout)
        if not ready:
            raise RuntimeError(
                f"{role} worker provisioning timed out after {timeout}s"
            )

        if spec.status == "failed":
            raise RuntimeError(
                f"Cannot proceed: {role} worker provisioning failed: "
                f"{spec.error}"
            )

        if spec.status == "healthy":
            logger.info("%s worker is ready — stage may proceed", role)
            # Start InfraAgent once all workers are ready
            self._start_infra_agent_if_ready()
            return True

        raise RuntimeError(
            f"{role} worker in unexpected state: {spec.status}"
        )

    def _get_spec(self, role: str) -> Optional[WorkerSpec]:
        """Get the WorkerSpec for a given role."""
        with self._lock:
            for spec in self._specs:
                if spec.role == role:
                    return spec
        return None

    # ------------------------------------------------------------------
    # Legacy blocking API (backward compat)
    # ------------------------------------------------------------------

    def ensure_workers_ready(
        self,
        require_tts: bool = True,
        require_video: bool = True,
        provision_timeout: int = 2700,
    ) -> dict:
        """Ensure all required workers are healthy, provisioning if needed.

        Legacy blocking API — provisions all workers and blocks until all
        are healthy.  New code should use start_provisioning() +
        wait_for_worker() for parallelism.

        Returns a status dict with worker details.
        """
        # Use the new parallel infrastructure but block until all are done
        self.start_provisioning(
            require_tts=require_tts,
            require_video=require_video,
        )

        if _TEST_MODE:
            return {"status": "test_mode", "workers": []}

        status = {"workers": [], "provisioned": [], "already_healthy": []}

        for spec in self._specs:
            try:
                self.wait_for_worker(spec.role, timeout=provision_timeout)
                if spec.vm_id:
                    status["provisioned"].append(spec.role)
                else:
                    status["already_healthy"].append(spec.role)
                status["workers"].append({
                    "role": spec.role,
                    "url": f"http://localhost:{spec.local_port}",
                    "status": "healthy",
                    "provisioned": bool(spec.vm_id),
                    "vm_id": spec.vm_id,
                })
            except Exception as exc:
                status["workers"].append({
                    "role": spec.role,
                    "status": "failed",
                    "error": str(exc),
                })
                # Clean up ALL workers on any failure
                self._cleanup_all_on_failure()
                raise

        with self._lock:
            self._provisioned = True

        status["status"] = "ready"
        logger.info(
            "WorkerProvisioner: all workers ready. "
            "Already healthy: %s. Provisioned: %s.",
            status["already_healthy"], status["provisioned"],
        )
        return status

    def _provision_and_connect(
        self, spec: WorkerSpec, timeout: int = 2400
    ) -> None:
        """Provision a VM, set up tunnel, and wait for health.

        Full lifecycle for a single worker.  The wall-clock ``timeout``
        covers all steps end-to-end.  The health-wait step gets whatever
        time remains after provisioning + VM boot + SSH setup, with a
        minimum of 120s so the wait isn't uselessly short.
        """
        _start = time.time()

        # Step 1: Provision VM
        provision_vm(spec)

        # Step 2: Wait for VM to be running
        elapsed = int(time.time() - _start)
        vm_timeout = max(min(timeout - elapsed, 600), 60)
        wait_for_vm_running(spec, timeout=vm_timeout)

        # Step 3: Set up SSH tunnel
        setup_ssh_tunnel(spec)

        # Step 4: Wait for worker to be healthy
        # Bootstrap + model download can take 15-30 min (95GB at ~65 MB/s)
        elapsed = int(time.time() - _start)
        remaining = max(timeout - elapsed, 120)  # honour timeout; 120s floor avoids useless waits
        healthy = wait_for_worker_healthy(spec, timeout=remaining)
        if not healthy:
            # Include bootstrap error details if available — this is the
            # structured information from the worker's /health endpoint.
            if spec.bootstrap_error:
                raise RuntimeError(
                    f"{spec.role} worker BOOTSTRAP FAILED on VM {spec.vm_id} "
                    f"(category={spec.bootstrap_error_category}): "
                    f"{spec.bootstrap_error}"
                )
            raise RuntimeError(
                f"{spec.role} worker on VM {spec.vm_id} did not become "
                f"healthy within {remaining}s after provisioning"
            )

    def _cleanup_single_worker(self, spec: WorkerSpec) -> None:
        """Clean up a single worker's resources (tunnel + VM)."""
        if spec.tunnel_proc and spec.tunnel_proc.poll() is None:
            logger.info(
                "Cleaning up SSH tunnel for %s (pid=%d)",
                spec.role, spec.tunnel_proc.pid,
            )
            spec.tunnel_proc.terminate()
            try:
                spec.tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                spec.tunnel_proc.kill()

        if spec.vm_id:
            logger.info(
                "Destroying %s VM %s to stop billing",
                spec.role, spec.vm_id,
            )
            try:
                _vast_cmd(["destroy", "instance", spec.vm_id])
            except Exception as exc:
                logger.warning(
                    "Failed to destroy VM %s: %s", spec.vm_id, exc,
                )

    def _cleanup_all_on_failure(self) -> None:
        """Clean up all provisioned resources after a failure."""
        with self._lock:
            specs = list(self._specs)
        for spec in specs:
            self._cleanup_single_worker(spec)

    def _start_infra_agent(self) -> None:
        """Start the InfraAgent and register provisioned workers."""
        try:
            from infra_agent import WorkerRole, start_infra_agent

            infra = start_infra_agent(
                poll_interval=30.0, max_consecutive_failures=3
            )
            infra.start()

            # Register each provisioned worker so the InfraAgent monitors it
            # even if it was started before env vars were set.
            for spec in self._specs:
                if spec.vm_id:  # only register actually-provisioned workers
                    role = (
                        WorkerRole.TTS if spec.role == "tts"
                        else WorkerRole.VIDEO
                    )
                    url = f"http://localhost:{spec.local_port}"
                    infra.add_worker(url, role)
                    logger.info(
                        "Registered %s worker at %s with InfraAgent",
                        spec.role, url,
                    )

            logger.info("InfraAgent started for continuous monitoring")
        except Exception as exc:
            logger.warning("Failed to start InfraAgent: %s", exc)

    def _start_infra_agent_if_ready(self) -> None:
        """Start InfraAgent once all workers are ready."""
        with self._lock:
            all_ready = all(s.status == "healthy" for s in self._specs)
            already_started = self._provisioned
        if all_ready and not already_started:
            with self._lock:
                self._provisioned = True
            self._start_infra_agent()

    def cleanup(self) -> None:
        """Clean up: kill SSH tunnels, destroy VMs, and stop InfraAgent."""
        # Wait for any in-flight provisioning threads to finish
        for role, thread in self._threads.items():
            if thread.is_alive():
                logger.info(
                    "Waiting for %s provisioning thread to finish...", role,
                )
                thread.join(timeout=30)

        with self._lock:
            specs = list(self._specs)

        for spec in specs:
            self._cleanup_single_worker(spec)

        # Clear specs so stale VM IDs aren't referenced again
        with self._lock:
            self._specs = []
            self._provisioned = False
            self._threads = {}
        # Clear stale error and specs_ready so re-runs aren't poisoned
        self._provision_start_error = ""
        self._specs_ready.clear()

        # Shutdown InfraAgent
        try:
            from infra_agent import get_infra_agent

            agent = get_infra_agent()
            if agent:
                agent.shutdown()
                logger.info("InfraAgent stopped")
        except Exception:
            pass

    def get_vm_ids(self) -> list[str]:
        """Return the IDs of all provisioned VMs."""
        with self._lock:
            return [s.vm_id for s in self._specs if s.vm_id]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_provisioner: Optional[WorkerProvisioner] = None
_provisioner_lock = threading.Lock()


def get_provisioner() -> WorkerProvisioner:
    """Return the global WorkerProvisioner singleton."""
    global _provisioner
    with _provisioner_lock:
        if _provisioner is None:
            _provisioner = WorkerProvisioner()
    return _provisioner
