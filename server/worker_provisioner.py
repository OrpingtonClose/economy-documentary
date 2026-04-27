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


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


# Valid gpu_worker.py --mode values.  Historically the provisioner
# accidentally passed ``--mode video`` which gpu_worker.py rejects (see
# #63).  ``_WORKER_MODE_ALIASES`` normalises legacy names to the
# canonical value the worker expects.
_VALID_WORKER_MODES = ("tts", "ltx", "both")
_WORKER_MODE_ALIASES = {
    "video": "ltx",
    "ltx2": "ltx",
    "qwen_tts": "tts",
}


def normalize_worker_mode(mode: str) -> str:
    """Map legacy worker-mode aliases to canonical gpu_worker.py values.

    The canonical values accepted by ``scripts/gpu_worker.py --mode`` are
    ``tts``, ``ltx``, and ``both``.  Anything else raises ``ValueError``
    to prevent silent misconfiguration (issue #63).
    """
    if not mode:
        raise ValueError("worker_mode must be non-empty")
    m = mode.strip().lower()
    m = _WORKER_MODE_ALIASES.get(m, m)
    if m not in _VALID_WORKER_MODES:
        raise ValueError(
            f"Invalid worker_mode {mode!r}; must be one of "
            f"{_VALID_WORKER_MODES} (aliases: {sorted(_WORKER_MODE_ALIASES)})"
        )
    return m


# Control-plane health-endpoint port on the GPU VM.  gpu_worker.py serves
# the main worker API on spec.remote_port (8880 by default); the
# bootstrap/health controller listens on 5000 so we can probe it while
# the main worker is still loading models (issue #64).
_HEALTH_CONTROL_PORT = 5000


@dataclass
class WorkerSpec:
    """Specification for a GPU worker to provision."""

    role: str  # "tts" or "video"
    env_var: str  # e.g. "TTS_WORKER_URL"
    local_port: int  # localhost port for SSH tunnel (legacy fallback)
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
    # Direct connection fields — used with --direct port mapping
    public_ipaddr: str = ""  # VM's public IP for direct connections
    direct_port: int = 0  # mapped external port (from Vast.ai port info)
    worker_url: str = ""  # resolved worker URL (direct or tunnel)
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
# GAP 1.2: Model manifest — machine-readable model resource requirements
# ---------------------------------------------------------------------------

def _load_model_manifest() -> dict:
    """Load config/model_manifest.json if available.

    Returns the full manifest dict with 'models' and 'docker_images' sections.
    Falls back to empty dict if file is missing.
    """
    manifest_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "model_manifest.json",
    )
    try:
        with open(manifest_path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("Could not load model manifest from %s: %s", manifest_path, e)
        return {}


_FULL_MANIFEST = _load_model_manifest()
_MODEL_MANIFEST = _FULL_MANIFEST.get("models", {})


def _parse_version(v: str) -> tuple:
    """Parse a version string like '2.10.0' into a comparable tuple (2, 10, 0)."""
    # Strip build metadata (e.g. '2.7.0+cu126' -> '2.7.0') per PEP 440
    v = v.split("+")[0]
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)


def resolve_docker_image(worker_mode: str) -> tuple[str, str]:
    """Pick the best Docker image for a worker mode from the model manifest.

    Reads the model's min_torch / min_cuda requirements and the docker_images
    registry from model_manifest.json.  Returns the first image whose torch
    and CUDA versions satisfy the model constraints.

    Returns:
        (docker_image_tag, torch_wheel_index_url)
    """
    # Find the model entry that matches this worker_mode
    model_spec = None
    for _key, spec in _MODEL_MANIFEST.items():
        if spec.get("worker_mode") == worker_mode:
            model_spec = spec
            break

    # Defaults if manifest is missing or incomplete
    default_image = "pytorch/pytorch:2.10.0-cuda12.6-cudnn9-devel"
    default_index = "https://download.pytorch.org/whl/cu126"

    if not model_spec:
        logger.warning(
            "No model manifest entry for worker_mode=%s, using default image %s",
            worker_mode, default_image,
        )
        return default_image, default_index

    min_torch = _parse_version(model_spec.get("min_torch", "2.6.0"))
    min_cuda = _parse_version(model_spec.get("min_cuda", "12.4"))
    wheel_suffix = model_spec.get("torch_wheel_suffix", "cu126")
    torch_index = f"https://download.pytorch.org/whl/{wheel_suffix}"

    # Search the image registry for the first image that satisfies constraints
    images = _FULL_MANIFEST.get("docker_images", {}).get("images", [])
    for img in images:
        img_torch = _parse_version(img.get("torch_version", "0.0.0"))
        img_cuda = _parse_version(img.get("cuda_version", "0.0"))
        if img_torch >= min_torch and img_cuda >= min_cuda:
            tag = img["tag"]
            logger.info(
                "Resolved Docker image for %s: %s (torch %s >= %s, cuda %s >= %s)",
                worker_mode, tag,
                img.get("torch_version"), model_spec.get("min_torch"),
                img.get("cuda_version"), model_spec.get("min_cuda"),
            )
            return tag, torch_index

    # No image in registry satisfies constraints — use the first one and warn
    if images:
        fallback = images[0]["tag"]
        logger.warning(
            "No image satisfies min_torch=%s min_cuda=%s for %s, falling back to %s",
            model_spec.get("min_torch"), model_spec.get("min_cuda"),
            worker_mode, fallback,
        )
        return fallback, torch_index

    logger.warning("No docker_images in manifest, using default %s", default_image)
    return default_image, default_index


# ---------------------------------------------------------------------------
# Default worker specs — VRAM calculated from actual model sizes
# (GAP 1.2: values cross-checked against config/model_manifest.json)
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
# Video: LTX-2.3 (Lightricks/LTX-2.3, ltx-2.3-22b-dev.safetensors)
#   The pipeline uses a block-based lifecycle — only one major component
#   in VRAM at a time (text encoder → transformer → VAE decoder).
#   Peak VRAM = transformer alone: 22B params × 2 bytes (bf16) ≈ 46 GB
#   + activations/KV cache at 512×320 ≈ 2-4 GB overhead
#   -> min_vram_gb = 48 (safe floor: 46 GB weights + ~2-4 GB activations)
#   -> gpu_type = H200 (141 GB), H100 (80 GB), A100 (80 GB), etc.
#   -> disk: ~46 GB checkpoint + ~4 GB Gemma + ~30 GB OS + ~20 GB output ≈ 200 GB

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
    gpu_type="H200",           # 141 GB VRAM; broadened to H100/A100 if unavailable
    min_vram_gb=48,            # ~46 GB bf16 transformer + ~2-4 GB activations at 512×320
    max_price=5.00,            # fallback ceiling; overridden by weighted budget
    min_disk_gb=200,           # ~46 GB checkpoint + Gemma + OS + output
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
        from recovery import escalate_pipeline_error
        _credit_msg = (
            f"Insufficient Vast.ai credits: ${credits:.2f} "
            f"(reserve=${_CREDIT_RESERVE:.2f}). "
            f"Top up at https://cloud.vast.ai/billing/"
        )
        response = escalate_pipeline_error(
            operation_name="vast_credits_insufficient",
            error_msg=_credit_msg,
            severity="critical",
            default_action="abort",
            diagnosis_hint="Vast.ai account has insufficient credits for provisioning.",
        )
        if response.get("action") != "skip":
            raise RuntimeError(_credit_msg)
        return (0.0, 0.0)  # skip: return zero budgets so caller can handle gracefully

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
        from recovery import escalate_pipeline_error
        _credit_msg = (
            f"Insufficient Vast.ai credits: ${credits:.2f} "
            f"(reserve=${_CREDIT_RESERVE:.2f}). "
            f"Top up at https://cloud.vast.ai/billing/"
        )
        response = escalate_pipeline_error(
            operation_name="vast_credits_insufficient",
            error_msg=_credit_msg,
            severity="critical",
            default_action="abort",
            diagnosis_hint="Vast.ai account has insufficient credits for provisioning.",
        )
        if response.get("action") != "skip":
            raise RuntimeError(_credit_msg)
        return 0.0  # skip: return zero budget so caller can handle gracefully
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


def provision_vm(spec: WorkerSpec, excluded_offer_ids: set[int] | None = None) -> int:
    """Provision a Vast.ai GPU VM for the given worker spec.

    Returns the selected **offer** ID so callers can track it for retries
    (distinct from the instance ID stored in ``spec.vm_id``).
    *excluded_offer_ids* — offer IDs to skip (e.g. previously-tried slow hosts).
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
        f"reliability>0.95 "
        f"inet_down>200 "
        f"disk_space>={spec.min_disk_gb}"
    )

    search_result = _vast_cmd([
        "search", "offers",
        "--type", "on-demand",
        "--order", "inet_down-",  # fastest download first (image pull speed)
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
            f"reliability>0.90 "
            f"inet_down>100 "
            f"disk_space>={spec.min_disk_gb}"
        )
        search_result = _vast_cmd([
            "search", "offers",
            "--type", "on-demand",
            "--order", "inet_down-",
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

    # Exclude previously-tried offers (e.g. slow hosts during retry)
    if excluded_offer_ids and offers:
        before = len(offers)
        offers = [o for o in offers if int(o.get("id", 0)) not in excluded_offer_ids]
        if len(offers) < before:
            logger.info(
                "Excluded %d previously-tried offer(s); %d remain",
                before - len(offers), len(offers),
            )

    if not offers:
        from recovery import escalate_pipeline_error
        _no_gpu_msg = (
            f"No GPU offers found for {spec.role} worker "
            f"(min {spec.min_vram_gb}GB VRAM, max ${spec.max_price:.2f}/hr, "
            f"min disk {spec.min_disk_gb}GB). "
            f"VRAM floor is non-negotiable (no quantisation). "
            f"Current account budget allows up to ${spec.max_price:.2f}/hr."
        )
        response = escalate_pipeline_error(
            operation_name="vast_no_gpu_offers",
            error_msg=_no_gpu_msg,
            severity="critical",
            default_action="abort",
            diagnosis_hint=(
                f"No Vast.ai GPUs available matching requirements for {spec.role}. "
                "Try increasing budget ceiling or waiting for availability."
            ),
            agent_policy_type="production",
        )
        if response.get("action") != "skip":
            raise RuntimeError(_no_gpu_msg)
        return 0  # sentinel: no VM provisioned (callers must handle 0)

    # Sort by download speed (fastest first) with price as tiebreaker.
    # This preserves the --order inet_down- intent from the CLI search
    # instead of overriding it with a pure price sort.
    sorted_offers = sorted(
        offers,
        key=lambda o: (-float(o.get("inet_down", 0)), float(o.get("dph_total", 999))),
    )
    best = sorted_offers[0]
    offer_id = int(best.get("id", 0))

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

    # Normalise worker_mode early so legacy aliases ("video") can't leak
    # into the onstart command (issue #63 — gpu_worker.py only accepts
    # "tts", "ltx", "both").  We mutate the spec so the normalised value
    # is visible to the onstart formatter below.
    spec.worker_mode = normalize_worker_mode(spec.worker_mode)

    # Resolve Docker image + torch wheel index from model manifest
    _docker_image, _torch_index = resolve_docker_image(spec.worker_mode)
    logger.info("Docker image for %s worker: %s", spec.role, _docker_image)

    # Look up min_torch from the manifest so the bootstrap script can use it
    # instead of a hardcoded version threshold.
    _min_torch = "2.7.0"  # safe default
    for _key, _mspec in _MODEL_MANIFEST.items():
        if _mspec.get("worker_mode") == spec.worker_mode:
            _min_torch = _mspec.get("min_torch", "2.7.0")
            break

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
        # Resolve Docker image + torch wheel index from the model manifest.
        # TORCH_INDEX is passed to gpu_bootstrap.sh as a fallback for
        # pip install --upgrade scenarios.
        f"export TORCH_INDEX={shlex.quote(_torch_index)} && "
        f"export MIN_TORCH_VERSION={shlex.quote(_min_torch)} && "
        # Prevent CUDA OOM from memory fragmentation — the 22B LTX model
        # needs ~46GB for the transformer; expandable_segments lets PyTorch
        # reuse reserved-but-unallocated memory instead of failing.
        "export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True && "
        "apt-get update && apt-get install -y git curl ffmpeg libsndfile1 sox libsox-dev && "
        f"(git clone -b {shlex.quote(_branch)} --single-branch "
        "https://github.com/OrpingtonClose/economy-documentary.git "
        "/workspace/economy-documentary 2>&1 || "
        "git clone -b main --single-branch "
        "https://github.com/OrpingtonClose/economy-documentary.git "
        "/workspace/economy-documentary 2>&1 || "
        f"(cd /workspace/economy-documentary && git fetch origin {shlex.quote(_branch)} && "
        f"git checkout {shlex.quote(_branch)} && git pull origin {shlex.quote(_branch)})) && "
        # Install Python deps needed for gpu_worker.py to start (FastAPI + deps).
        # The Docker image already has torch pre-installed (resolved from manifest),
        # so we only need FastAPI + other non-torch deps for the health endpoint.
        "python3 -c 'import torch; print(f\"torch {torch.__version__} from {torch.__file__}\")' && "
        "pip install --break-system-packages --no-cache-dir "
        "'fastapi>=0.100.0' 'uvicorn>=0.20.0' 'pydantic>=2.0.0' "
        "'numpy>=1.26.0,<2.0.0' 'soundfile>=0.12.0' && "
        # Register NVIDIA pip package libs with ldconfig so CUDA shared
        # libraries are discoverable system-wide by any process.
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

    # GAP 2.3: Generate a descriptive label for the VM
    import uuid as _uuid
    _run_id = os.environ.get("DOCUMENTARY_RUN_ID", _uuid.uuid4().hex[:8])
    _label = f"documentary-{spec.role}-{_run_id}"

    # NOTE: Do NOT use --raw here.  `vastai create instance --raw` returns
    # an empty string.  Without --raw it returns text like:
    #   Started. {'success': True, 'new_contract': 34856082, ...}
    #
    # Port mapping (issue #64):
    # - spec.remote_port (8880): main worker API (TTS/LTX inference)
    # - _HEALTH_CONTROL_PORT (5000): bootstrap/health control plane that
    #   reports bootstrap phase (pulling image / loading model / ready)
    #   while the main worker is still loading.  Previously only 8880 was
    #   exposed, so the provisioner was blind until model load finished.
    _env_ports = (
        f"-p {spec.remote_port}:{spec.remote_port} "
        f"-p {_HEALTH_CONTROL_PORT}:{_HEALTH_CONTROL_PORT}"
    )
    create_result = _vast_cmd([
        "create", "instance",
        str(offer_id),
        "--image", _docker_image,
        "--disk", str(spec.disk_gb),
        "--ssh",
        "--direct",
        "--env", _env_ports,
        "--label", _label,  # GAP 2.3: VM labeling for identification
        "--onstart-cmd", onstart,
    ])
    logger.info("VM label: %s", _label)

    # Parse the response — could be dict (if CLI returns JSON) or a string
    # containing a Python dict literal like "Started. {'new_contract': ...}"
    if isinstance(create_result, dict):
        instance_id = create_result.get("new_contract")
        if instance_id:
            spec.vm_id = str(instance_id)
            # GAP 2.1: Register as owned so terminate_vm() accepts it
            from tools.vastai_tools import register_owned_vm
            register_owned_vm(spec.vm_id)
            logger.info("VM provisioned: instance_id=%s", spec.vm_id)
            return offer_id

    # Try to extract new_contract from text response
    if isinstance(create_result, str) and "new_contract" in create_result:
        match = re.search(r"'new_contract'\s*:\s*(\d+)", create_result)
        if match:
            spec.vm_id = match.group(1)
            # GAP 2.1: Register as owned so terminate_vm() accepts it
            from tools.vastai_tools import register_owned_vm
            register_owned_vm(spec.vm_id)
            logger.info("VM provisioned: instance_id=%s (parsed from text)", spec.vm_id)
            return offer_id

    raise RuntimeError(
        f"Failed to provision {spec.role} VM: unexpected response: {create_result}"
    )


def wait_for_vm_running(spec: WorkerSpec, timeout: int = 600) -> dict:
    """Wait for a provisioned VM to reach 'running' status.

    Returns the VM info dict with connection details.
    Extracts both SSH proxy info (legacy fallback) and direct connection
    info (public_ipaddr + port mapping) for direct HTTP access.
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
                    # Legacy SSH proxy info
                    spec.ssh_host = result.get("ssh_host", "")
                    spec.ssh_port = int(result.get("ssh_port", 0))
                    # Direct connection info
                    spec.public_ipaddr = result.get("public_ipaddr", "")
                    # Extract mapped port from Vast.ai ports dict.
                    # Format: {"8880/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8880"}]}
                    # or sometimes the external port differs from internal.
                    ports = result.get("ports", {}) or {}
                    port_key = f"{spec.remote_port}/tcp"
                    if port_key in ports:
                        port_bindings = ports[port_key]
                        if isinstance(port_bindings, list) and port_bindings:
                            spec.direct_port = int(
                                port_bindings[0].get("HostPort", 0)
                            )
                        elif isinstance(port_bindings, (int, str)):
                            spec.direct_port = int(port_bindings)
                    if spec.public_ipaddr and spec.direct_port:
                        logger.info(
                            "  %s VM %s direct connection: %s:%d",
                            spec.role, spec.vm_id,
                            spec.public_ipaddr, spec.direct_port,
                        )
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

    # Collect all available SSH identity files.  Vast.ai proxy hosts can be
    # inconsistent about which key they accept, so we try every available key
    # on each attempt (round-robin) rather than locking to a single one.
    _ssh_dir = os.path.expanduser("~/.ssh")
    _ssh_keys: list[str] = []
    for _fname in sorted(os.listdir(_ssh_dir)) if os.path.isdir(_ssh_dir) else []:
        _fpath = os.path.join(_ssh_dir, _fname)
        # Skip public keys, known_hosts, config, authorized_keys, dirs
        if (
            _fname.endswith(".pub")
            or _fname in ("known_hosts", "known_hosts.old", "config", "authorized_keys")
            or not os.path.isfile(_fpath)
        ):
            continue
        # Only include files that look like SSH private keys (start with -----)
        try:
            with open(_fpath, "r") as f:
                first_line = f.readline()
            if first_line.startswith("-----"):
                _ssh_keys.append(_fpath)
        except (OSError, UnicodeDecodeError):
            continue
    if not _ssh_keys:
        raise RuntimeError(
            f"Cannot set up SSH tunnel for {spec.role}: "
            f"no SSH identity files found in {_ssh_dir}"
        )

    def _build_tunnel_cmd(key_path: str) -> list[str]:
        return [
            "ssh",
            "-i", key_path,
            "-o", "IdentitiesOnly=yes",
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
        # Rotate through available keys so we don't fail all attempts
        # on a host that only accepts one of them.
        _ssh_key = _ssh_keys[(attempt - 1) % len(_ssh_keys)]
        tunnel_cmd = _build_tunnel_cmd(_ssh_key)

        logger.info(
            "Setting up SSH tunnel (attempt %d/%d, key=%s): "
            "localhost:%d -> %s:%d (via %s:%d)",
            attempt, max_retries, os.path.basename(_ssh_key),
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
# Direct connection (no SSH tunnel) — preferred for --direct VMs
# ---------------------------------------------------------------------------


def establish_direct_connection(
    spec: WorkerSpec, max_retries: int = 20, retry_delay: int = 15,
) -> str:
    """Establish a direct HTTP connection to the worker via public IP.

    Uses the VM's public_ipaddr and mapped port (from --direct + --env
    port mapping).  Falls back to polling ``vastai execute`` to check if
    the worker process is listening, then resolves the URL.

    Returns the direct worker URL (e.g. "http://1.2.3.4:8880").
    Raises RuntimeError if direct connection cannot be established.
    """
    # Strategy 1: Use port mapping from VM info (populated by wait_for_vm_running)
    if spec.public_ipaddr and spec.direct_port:
        direct_url = f"http://{spec.public_ipaddr}:{spec.direct_port}"
        logger.info(
            "Trying direct connection to %s at %s...",
            spec.role, direct_url,
        )
        # Poll until the worker port is reachable (onstart script may still
        # be bootstrapping — installing deps, downloading models, starting
        # the FastAPI server).
        for attempt in range(1, max_retries + 1):
            try:
                req = Request(f"{direct_url}/health")
                with urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode())
                    logger.info(
                        "Direct connection to %s ESTABLISHED (attempt %d): %s",
                        spec.role, attempt, data.get("status", "unknown"),
                    )
                    spec.worker_url = direct_url
                    return direct_url
            except Exception:
                if attempt % 4 == 0:
                    # Periodic diagnostics via vastai tools
                    _log_vm_diagnostics(spec)
                if attempt < max_retries:
                    logger.info(
                        "  Direct connection attempt %d/%d to %s — "
                        "worker not yet reachable, retrying in %ds...",
                        attempt, max_retries, spec.role, retry_delay,
                    )
                    time.sleep(retry_delay)
                    continue
        # Direct port was mapped but worker never responded
        logger.warning(
            "Direct connection to %s at %s failed after %d attempts",
            spec.role, direct_url, max_retries,
        )

    # Strategy 2: No port mapping available — try vastai execute to
    # check if worker is running inside the VM and get its internal URL.
    if spec.vm_id:
        logger.info(
            "No direct port mapping for %s — using vastai execute "
            "to check worker status on VM %s",
            spec.role, spec.vm_id,
        )
        for attempt in range(1, min(max_retries, 5) + 1):
            try:
                result = _vast_cmd([
                    "execute", spec.vm_id,
                    f"curl -s http://localhost:{spec.remote_port}/health",
                ])
                if isinstance(result, str) and "ok" in result.lower():
                    logger.info(
                        "Worker %s is running inside VM %s (via vastai execute)",
                        spec.role, spec.vm_id,
                    )
                    # Worker is running but we can't reach it directly.
                    # Fall through to SSH tunnel fallback.
                    break
            except Exception as exc:
                logger.warning(
                    "vastai execute health check failed (attempt %d): %s",
                    attempt, exc,
                )
            time.sleep(retry_delay)

    raise RuntimeError(
        f"Direct connection to {spec.role} worker failed — "
        f"public_ip={spec.public_ipaddr}, direct_port={spec.direct_port}, "
        f"vm_id={spec.vm_id}"
    )


def _log_vm_diagnostics(spec: WorkerSpec) -> None:
    """Fetch and log diagnostics from a VM using vastai CLI tools.

    Uses ``vastai logs`` and ``vastai execute`` to understand what's
    happening inside the VM without needing an SSH connection.
    """
    if not spec.vm_id:
        return

    # Fetch container logs
    try:
        logs = _vast_cmd(["logs", spec.vm_id, "--tail", "20"])
        if isinstance(logs, str) and logs.strip():
            logger.info(
                "VM %s (%s) container logs (last 20 lines):\n%s",
                spec.vm_id, spec.role, logs.strip(),
            )
    except Exception as exc:
        logger.debug("Could not fetch logs for VM %s: %s", spec.vm_id, exc)

    # Check if worker process is running
    try:
        ps_result = _vast_cmd([
            "execute", spec.vm_id,
            "ps aux | grep gpu_worker || echo 'no worker process'",
        ])
        if isinstance(ps_result, str) and ps_result.strip():
            logger.info(
                "VM %s (%s) process check: %s",
                spec.vm_id, spec.role, ps_result.strip()[:200],
            )
    except Exception as exc:
        logger.debug(
            "Could not check processes on VM %s: %s", spec.vm_id, exc,
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


# Hard ceiling for wait_for_worker_healthy (issue #62).  15 minutes is a
# generous upper bound for the slowest path (pull 80GB image + download
# model weights + load into VRAM on a cold host).  Longer waits almost
# always indicate a stuck host, so we fail loud instead of hanging forever.
WORKER_HEALTH_TIMEOUT_MAX_SECONDS = 15 * 60
# Heartbeat interval for wait_for_worker_healthy (issue #62).  The
# pipeline operator sees elapsed time + last known bootstrap phase at
# this cadence even when the phase hasn't changed.
WORKER_HEALTH_HEARTBEAT_SECONDS = 30


def wait_for_worker_healthy(
    spec: WorkerSpec,
    timeout: int = WORKER_HEALTH_TIMEOUT_MAX_SECONDS,
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

    Issue #62: the caller-supplied ``timeout`` is clamped to
    ``WORKER_HEALTH_TIMEOUT_MAX_SECONDS`` (15 minutes) so a bad caller
    can't hang the pipeline forever.  A heartbeat is logged every
    ``WORKER_HEALTH_HEARTBEAT_SECONDS`` (30s) showing elapsed time and
    the last known bootstrap phase — the operator always knows whether
    the VM is pulling the image, loading the model, or already ready.
    On timeout a clear RuntimeError-equivalent ``False`` return is
    produced and the provisioner logs a fatal summary.
    """
    # Clamp to the hard ceiling.  Lower values (e.g. from tests) are
    # respected; higher values are capped.
    effective_timeout = max(1, min(int(timeout), WORKER_HEALTH_TIMEOUT_MAX_SECONDS))
    if effective_timeout < timeout:
        logger.warning(
            "wait_for_worker_healthy: clamping timeout %ds -> %ds (hard ceiling, #62)",
            timeout, effective_timeout,
        )

    # Use resolved worker_url if available (direct connection), else legacy tunnel
    url = spec.worker_url or f"http://localhost:{spec.local_port}"
    logger.info(
        "Waiting for %s worker at %s to become healthy (timeout %ds)...",
        spec.role, url, effective_timeout,
    )

    start = time.time()
    last_status = "unknown"
    last_bootstrap_phase = ""
    last_bootstrap_detail = ""
    last_heartbeat = start
    while time.time() - start < effective_timeout:
        elapsed = int(time.time() - start)

        # Check if tunnel is still alive (only relevant for SSH tunnel connections)
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
            bootstrap_detail = bootstrap.get("detail", "")
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
                    spec.role, bootstrap_phase, bootstrap_detail, elapsed,
                )
                last_bootstrap_phase = bootstrap_phase
                last_bootstrap_detail = bootstrap_detail

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

        # Issue #62: periodic heartbeat so operators see progress even
        # when no phase transition has occurred for a while.
        now = time.time()
        if now - last_heartbeat >= WORKER_HEALTH_HEARTBEAT_SECONDS:
            last_heartbeat = now
            phase = last_bootstrap_phase or last_status
            detail = last_bootstrap_detail or ""
            logger.info(
                "  heartbeat: %s worker still waiting — elapsed=%ds / %ds "
                "(phase=%s%s)",
                spec.role, elapsed, effective_timeout,
                phase or "starting",
                f", {detail}" if detail else "",
            )

        time.sleep(poll_interval)

    # Issue #62: fail loud with the last known state so the operator can
    # see exactly where the worker got stuck.
    logger.error(
        "%s worker at %s did NOT become healthy within %ds "
        "(last phase=%s, last status=%s).  Fail-loud per #62.",
        spec.role, url, effective_timeout,
        last_bootstrap_phase or "unknown",
        last_status,
    )
    spec.bootstrap_error = (
        f"timeout after {effective_timeout}s "
        f"(last phase={last_bootstrap_phase or 'unknown'}, "
        f"last status={last_status})"
    )
    spec.bootstrap_error_category = "timeout"
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
        # Check which workers actually need provisioning.
        #
        # Issue #65: honour pre-set worker URLs.  If the operator exports
        # ``TTS_WORKER_URL`` / ``GPU_WORKER_URL`` / ``VIDEO_WORKER_URLS``
        # we trust those and skip provisioning entirely — even if the
        # worker isn't *immediately* reachable (it may still be warming).
        # Probing and re-provisioning on behalf of the user wastes
        # credits and blows away their hand-pinned fleet.
        need_tts = False
        need_video = False
        for spec in specs_needed:
            # Multi-URL takes precedence for video workers so a fleet
            # configured via VIDEO_WORKER_URLS (#71) is also honoured.
            video_fleet = ""
            if spec.role == "video":
                video_fleet = os.environ.get("VIDEO_WORKER_URLS", "").strip()

            preset_url = os.environ.get(spec.env_var, "").strip()
            # For video, fall through to the first VIDEO_WORKER_URLS entry
            # if GPU_WORKER_URL wasn't set explicitly.
            if not preset_url and video_fleet:
                first = next(
                    (u.strip() for u in video_fleet.split(",") if u.strip()),
                    "",
                )
                preset_url = first

            if preset_url or video_fleet:
                logger.info(
                    "%s worker: honouring pre-set env var %s=%s%s — "
                    "skipping Vast.ai provisioning (#65)",
                    spec.role, spec.env_var, preset_url,
                    f" (fleet VIDEO_WORKER_URLS={video_fleet})" if video_fleet else "",
                )
                # Best-effort health probe for visibility in logs; we do
                # NOT fall back to provisioning if it fails.  The pipeline
                # contract check will surface unreachable pre-set workers
                # as a loud failure downstream.
                reachable = False
                try:
                    reachable = check_worker_health(preset_url, spec.capability)
                except Exception as exc:
                    logger.debug(
                        "Pre-set %s worker health probe raised: %s — "
                        "continuing without provisioning",
                        spec.role, exc,
                    )
                logger.info(
                    "  pre-set %s worker health probe: %s",
                    spec.role, "healthy" if reachable else "not-yet-healthy (trusting env var)",
                )
                spec.worker_url = preset_url
                spec.status = "healthy" if reachable else "externally_managed"
                spec.ready_event.set()
                if preset_url:
                    os.environ[spec.env_var] = preset_url
                continue

            # No pre-set URL — fall back to the legacy behaviour of
            # probing localhost:<local_port> (e.g. SSH tunnel) and
            # queueing provisioning on miss.
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
            new_url = spec.worker_url or f"http://localhost:{spec.local_port}"
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

        if spec.status in ("healthy", "externally_managed"):
            return True

        logger.info(
            "Stage waiting for %s worker (status=%s, timeout=%ds)...",
            role, spec.status, timeout,
        )

        # Poll with short waits (1s) instead of one long blocking wait.
        # This prevents the async event loop from freezing — the ADK
        # callbacks run on the event loop thread, so a long blocking
        # wait would freeze the entire server (HTTP, SSE, dashboard).
        import time as _time
        _deadline = _time.monotonic() + timeout
        while not spec.ready_event.is_set():
            remaining = _deadline - _time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"{role} worker provisioning timed out after {timeout}s"
                )
            spec.ready_event.wait(timeout=min(1.0, remaining))

        if spec.status == "failed":
            raise RuntimeError(
                f"Cannot proceed: {role} worker provisioning failed: "
                f"{spec.error}"
            )

        if spec.status in ("healthy", "externally_managed"):
            # "externally_managed" = pre-set env var worker that wasn't
            # immediately reachable; honour it per #65 (operator knows
            # best — downstream pipeline contracts will surface any
            # lingering unreachability).
            logger.info(
                "%s worker is ready — stage may proceed (status=%s)",
                role, spec.status,
            )
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
        """Provision a VM, connect, and wait for health.

        Full lifecycle for a single worker.  The wall-clock ``timeout``
        covers all steps end-to-end.  The health-wait step gets whatever
        time remains after provisioning + VM boot + connection, with a
        minimum of 120s so the wait isn't uselessly short.

        Connection strategy (in order):
        1. **Direct HTTP** via VM's public IP + mapped port (``--direct``
           + ``--env '-p 8880:8880'``).  No SSH needed.  Uses
           ``establish_direct_connection()`` which polls the /health
           endpoint and uses ``vastai execute``/``vastai logs`` for
           diagnostics.
        2. **SSH tunnel** (legacy fallback) if direct connection fails.

        If the VM stays in "loading" (Docker image pull) for too long,
        connections fail on both direct and SSH, or the worker's bootstrap
        fails (e.g., network issues, CUDA OOM), the VM is destroyed and
        re-provisioned on a different host.
        Up to ``_MAX_PROVISION_RETRIES`` retries are attempted.
        """
        _MAX_PROVISION_RETRIES = 2
        _LOADING_TIMEOUT = 300  # seconds before we consider a host "slow"
        _start = time.time()
        _excluded_offers: set[int] = set()  # offer IDs of bad hosts

        for attempt in range(1 + _MAX_PROVISION_RETRIES):
            # Step 1: Provision VM (skip previously-tried bad offers)
            selected_offer_id = provision_vm(
                spec, excluded_offer_ids=_excluded_offers or None,
            )

            # Handle skip sentinel: provisioning was skipped by human
            if selected_offer_id == 0:
                spec.status = "skipped"
                logger.warning(
                    "%s VM provisioning skipped (human chose skip) — "
                    "no VM created for %s",
                    spec.role, spec.role,
                )
                return

            # Step 2: Wait for VM to be running — use a shorter timeout
            # for image pull so we can retry on a faster host.  On the
            # final attempt give the full remaining budget (up to 600s)
            # since there are no more retries to benefit from the cap.
            is_final = (attempt >= _MAX_PROVISION_RETRIES)
            cap = 600 if is_final else _LOADING_TIMEOUT
            elapsed = int(time.time() - _start)
            vm_timeout = max(min(timeout - elapsed, cap), 60)
            try:
                wait_for_vm_running(spec, timeout=vm_timeout)
            except RuntimeError:
                if attempt < _MAX_PROVISION_RETRIES:
                    # Track this offer so we don't pick it again
                    if selected_offer_id:
                        _excluded_offers.add(selected_offer_id)
                    logger.warning(
                        "%s VM %s (offer %s) stuck loading after %ds "
                        "— destroying and retrying on a different host "
                        "(attempt %d/%d, excluded offers: %s)",
                        spec.role, spec.vm_id, selected_offer_id,
                        vm_timeout, attempt + 2, 1 + _MAX_PROVISION_RETRIES,
                        _excluded_offers,
                    )
                    self._destroy_and_reset_spec(spec)
                    continue
                else:
                    raise  # all retries exhausted

            # Step 3: Connect to worker — try direct first, SSH tunnel as fallback.
            # Direct connection uses the VM's public IP + mapped port (no SSH).
            # This avoids the SSH proxy reliability issues (Connection refused).
            connection_ok = False
            try:
                direct_url = establish_direct_connection(
                    spec,
                    max_retries=40,  # ~10 min of polling (15s intervals)
                    retry_delay=15,
                )
                spec.worker_url = direct_url
                connection_ok = True
                logger.info(
                    "%s worker connected via DIRECT: %s",
                    spec.role, direct_url,
                )
            except RuntimeError as direct_err:
                logger.warning(
                    "Direct connection to %s failed: %s — "
                    "falling back to SSH tunnel",
                    spec.role, direct_err,
                )
                # Fallback: SSH tunnel
                try:
                    setup_ssh_tunnel(spec)
                    spec.worker_url = f"http://localhost:{spec.local_port}"
                    connection_ok = True
                    logger.info(
                        "%s worker connected via SSH TUNNEL: %s",
                        spec.role, spec.worker_url,
                    )
                except RuntimeError:
                    logger.warning(
                        "SSH tunnel to %s also failed", spec.role,
                    )

            if not connection_ok:
                if attempt < _MAX_PROVISION_RETRIES:
                    if selected_offer_id:
                        _excluded_offers.add(selected_offer_id)
                    logger.warning(
                        "%s VM %s (offer %s) — both direct and SSH "
                        "connections failed — destroying and retrying "
                        "(attempt %d/%d, excluded offers: %s)",
                        spec.role, spec.vm_id, selected_offer_id,
                        attempt + 2, 1 + _MAX_PROVISION_RETRIES,
                        _excluded_offers,
                    )
                    self._destroy_and_reset_spec(spec)
                    continue
                else:
                    raise RuntimeError(
                        f"{spec.role} worker on VM {spec.vm_id}: "
                        f"both direct and SSH connections failed after "
                        f"all retries"
                    )

            # Step 4: Wait for worker to be healthy
            # Bootstrap + model download can take 15-30 min (95GB at ~65 MB/s)
            elapsed = int(time.time() - _start)
            remaining = max(timeout - elapsed, 120)
            healthy = wait_for_worker_healthy(spec, timeout=remaining)
            if healthy:
                break  # VM running + connected + healthy — done

            # Step 4b: Bootstrap failed — retry on a different host.
            # Previously this was a hard failure, but bootstrap errors
            # (apt-get network issues, CUDA OOM, missing deps) are often
            # host-specific and succeed on a different machine.
            if spec.bootstrap_error and attempt < _MAX_PROVISION_RETRIES:
                if selected_offer_id:
                    _excluded_offers.add(selected_offer_id)
                logger.warning(
                    "%s VM %s (offer %s) bootstrap FAILED "
                    "(category=%s): %s "
                    "— destroying and retrying on a different host "
                    "(attempt %d/%d, excluded offers: %s)",
                    spec.role, spec.vm_id, selected_offer_id,
                    spec.bootstrap_error_category, spec.bootstrap_error,
                    attempt + 2, 1 + _MAX_PROVISION_RETRIES,
                    _excluded_offers,
                )
                # Kill the SSH tunnel before destroying the VM — the tunnel
                # is bound to spec.local_port and would block the next attempt.
                if spec.tunnel_proc and spec.tunnel_proc.poll() is None:
                    spec.tunnel_proc.terminate()
                    try:
                        spec.tunnel_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        spec.tunnel_proc.kill()
                    spec.tunnel_proc = None
                self._destroy_and_reset_spec(spec)
                # Reset bootstrap error for next attempt
                spec.bootstrap_error = ""
                spec.bootstrap_error_category = ""
                continue

            # Not retryable — raise immediately
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

    def _destroy_and_reset_spec(self, spec: WorkerSpec) -> None:
        """Destroy a VM and reset spec fields for re-provisioning."""
        if spec.vm_id:
            try:
                _vast_cmd(["destroy", "instance", spec.vm_id])
                spec.vm_id = ""
            except Exception as exc:
                logger.warning(
                    "Failed to destroy VM %s: %s — "
                    "VM may continue billing as orphan!",
                    spec.vm_id, exc,
                )
                # Keep spec.vm_id so cleanup() can retry destruction
        spec.ssh_host = ""
        spec.ssh_port = 0
        spec.public_ipaddr = ""
        spec.direct_port = 0
        spec.worker_url = ""

    def _cleanup_single_worker(
        self, spec: WorkerSpec, *, force_destroy: bool = False,
    ) -> None:
        """Clean up a single worker's resources (tunnel + VM).

        Args:
            force_destroy: If True, destroy the VM to stop billing.
                Used by _cleanup_all_on_failure for orphaned VMs.
                Normal cleanup (user-initiated) leaves VMs running
                so the user can inspect them.
        """
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
            if force_destroy:
                logger.info(
                    "Destroying %s VM %s to stop billing (failure cleanup)",
                    spec.role, spec.vm_id,
                )
                try:
                    _vast_cmd(["destroy", "instance", spec.vm_id])
                except Exception as exc:
                    logger.warning(
                        "Failed to destroy VM %s: %s — destroy manually "
                        "via 'vastai destroy instance %s'",
                        spec.vm_id, exc, spec.vm_id,
                    )
            else:
                logger.info(
                    "VM %s (%s) left running — destroy manually via "
                    "'vastai destroy instance %s' when done",
                    spec.vm_id, spec.role, spec.vm_id,
                )

    def _cleanup_all_on_failure(self) -> None:
        """Clean up all provisioned resources after a failure.

        Unlike normal cleanup(), this destroys VMs to prevent billing
        for orphaned instances that will never be used.
        """
        with self._lock:
            specs = list(self._specs)
        for spec in specs:
            self._cleanup_single_worker(spec, force_destroy=True)

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
                    url = spec.worker_url or f"http://localhost:{spec.local_port}"
                    infra.add_worker(url, role)
                    logger.info(
                        "Registered %s worker at %s with InfraAgent",
                        spec.role, url,
                    )

            logger.info("InfraAgent started for continuous monitoring")
        except Exception as exc:
            logger.warning("Failed to start InfraAgent: %s", exc)

    def _start_infra_agent_if_ready(self) -> None:
        """Start InfraAgent once all workers are ready.

        A worker counts as "ready" when it is either ``healthy`` (we
        provisioned it) or ``externally_managed`` (#65: honoured a
        pre-set env var).  Treating externally-managed workers as not
        ready would prevent InfraAgent from ever starting in fleets
        that pin worker URLs via env vars.
        """
        ready_states = ("healthy", "externally_managed")
        with self._lock:
            all_ready = all(s.status in ready_states for s in self._specs)
            already_started = self._provisioned
        if all_ready and not already_started:
            with self._lock:
                self._provisioned = True
            self._start_infra_agent()

    def cleanup(self, *, destroy_vms: bool = True) -> None:
        """Clean up: kill SSH tunnels, optionally destroy VMs, stop InfraAgent.

        Args:
            destroy_vms: If True (default), destroy VMs to stop billing.
                Pass False to leave VMs running for manual inspection.
        """
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
            self._cleanup_single_worker(spec, force_destroy=destroy_vms)

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
