"""
AudioProvisionerAgent — merges provisioner with audio agent.

Owns TTS end-to-end: allocate workers, generate narration, evaluate
quality, scale workers.  The agent uses raw LLM reasoning with vast CLI
output + Letta-based memory.  No hardcoded model registries — the agent
searches Vast.ai, reads the results, and reasons about what GPU to pick.
It remembers past decisions in memory.

Architecture:
    The agent is a RecoveryAgent subclass with a rich set of tools
    covering the full TTS lifecycle:

    1. READ   — find narration gaps in OTIO
    2. PLAN   — compute workload from gaps (Qwen3-TTS ~6x realtime)
    3. PROVISION — search GPUs, pick one, create VM, wait for health
    4. GENERATE — for each gap, call TTS worker, write clip to OTIO
    5. EVALUATE — check quality, rebalance if needed
    6. CLEANUP — destroy VMs

    VM info is stored in the agent's working memory (not OTIO) because
    VMs are ephemeral infrastructure.  The agent tracks which VMs it
    provisioned, their URLs, and their status.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from recovery_agents import AgentTool, RecoveryAgent
from tools.otio_file_ops import (
    resolve_timeline_path,
    otio_read,
    otio_read_modify_write,
    TRACK_A1,
    TRACK_V1,
)
from tools.otio_metadata import read_pipeline_metadata, write_pipeline_metadata
from tools.otio_lifecycle import guard_mutation, get_otio_lifecycle_state

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. OTIO reading tools
# ---------------------------------------------------------------------------

def _tool_read_pipeline_data(key: str) -> str:
    """Read pipeline metadata from the OTIO timeline.

    Uses the stateless file-based metadata API.
    """
    try:
        tp = resolve_timeline_path()
        val = read_pipeline_metadata(tp, key)
        if val is None:
            return json.dumps({
                "error": f"Key '{key}' not found in OTIO timeline",
                "contract_violation": True,
                "reason": f"Upstream stage has not produced '{key}'",
            })
        return json.dumps({"key": key, "value": val})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_find_narration_gaps() -> str:
    """Scan A1_Narration track for Gaps (missing narration).

    Cross-references with read_pipeline_data("scenes") to get the text,
    voice, and language for each gap.  Returns a JSON list of TTS jobs
    that need to be generated.

    Each job has: scene_num, phrase_idx, voice_role, text, language,
    gap_duration_sec.
    """
    import opentimelineio as otio

    try:
        tp = resolve_timeline_path()

        # Read scenes metadata for cross-reference
        scenes = read_pipeline_metadata(tp, "scenes")
        if scenes is None:
            return json.dumps({
                "error": "Cannot read scenes metadata: key not found",
                "gaps": [],
            })
        if isinstance(scenes, str):
            scenes = json.loads(scenes)
        if not isinstance(scenes, list):
            scenes = []

        # Build a lookup: (scene_num, phrase_idx) -> scene voice info
        voice_lookup: dict[tuple[int, int], dict] = {}
        for scene in scenes:
            sn = int(scene.get("scene_num", 0))
            voices = scene.get("voices", [])
            for idx, v in enumerate(voices):
                text = v.get("text", "").strip()
                if text:
                    voice_lookup[(sn, idx)] = {
                        "voice_role": v.get("voice", v.get("role", f"V{idx+1}")),
                        "text": text,
                        "language": v.get("language", "en"),
                    }

        # Read the OTIO timeline
        timeline = otio_read(tp)

        # Find Gaps in A1_Narration
        gaps = []
        for track in timeline.tracks:
            if track.name != TRACK_A1:
                continue
            phrase_idx = 0
            for item in track:
                doc_meta = item.metadata.get("documentary", {})
                scene_num = int(doc_meta.get("scene_num", 0))
                if isinstance(item, otio.schema.Gap):
                    gap_dur = 0.0
                    if item.source_range:
                        gap_dur = item.source_range.duration.to_seconds()
                    # Cross-reference with scenes metadata
                    voice_info = voice_lookup.get((scene_num, phrase_idx), {})
                    gaps.append({
                        "scene_num": scene_num,
                        "phrase_idx": phrase_idx,
                        "voice_role": voice_info.get("voice_role", "V1"),
                        "text": voice_info.get("text", ""),
                        "language": voice_info.get("language", "en"),
                        "gap_duration_sec": round(gap_dur, 3),
                    })
                elif isinstance(item, otio.schema.Clip):
                    phrase_idx += 1
                    continue
                phrase_idx += 1

        return json.dumps({
            "total_gaps": len(gaps),
            "gaps": gaps,
        })
    except Exception as e:
        return json.dumps({"error": str(e), "gaps": []})


def _tool_get_scene_durations() -> str:
    """Get per-scene duration budgets — narration vs video vs total.

    Reads the OTIO timeline directly and computes per-scene breakdowns.
    """
    import opentimelineio as otio

    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)

        scenes: dict = {}
        for track in timeline.tracks:
            for item in track:
                doc_meta = item.metadata.get("documentary", {})
                scene_num = doc_meta.get("scene_num", 0)
                if not scene_num:
                    continue
                if scene_num not in scenes:
                    scenes[scene_num] = {
                        "narration_sec": 0,
                        "video_sec": 0,
                        "narration_clips": 0,
                        "video_clips": 0,
                    }
                dur = 0.0
                if item.source_range:
                    dur = item.source_range.duration.to_seconds()
                if track.name == TRACK_A1 and isinstance(item, otio.schema.Clip):
                    scenes[scene_num]["narration_sec"] += dur
                    scenes[scene_num]["narration_clips"] += 1
                elif track.name == TRACK_V1:
                    if isinstance(item, otio.schema.Clip):
                        scenes[scene_num]["video_sec"] += dur
                        scenes[scene_num]["video_clips"] += 1
                    elif isinstance(item, otio.schema.Gap) and dur > 0:
                        scenes[scene_num]["video_sec"] += dur

        return json.dumps({"scenes": scenes}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# 2. OTIO writing tools
# ---------------------------------------------------------------------------

def _tool_write_pipeline_data(key: str, value_json: str, provenance_json: str = "{}") -> str:
    """Write pipeline metadata to the OTIO timeline with provenance.

    Uses the stateless file-based metadata API.
    """
    try:
        tp = resolve_timeline_path()

        try:
            value = json.loads(value_json)
        except (json.JSONDecodeError, TypeError):
            value = value_json

        try:
            provenance = json.loads(provenance_json) if provenance_json else None
        except (json.JSONDecodeError, TypeError):
            provenance = None

        return write_pipeline_metadata(tp, key, value, provenance)
    except Exception as e:
        return json.dumps({"error": str(e), "key": key})


def _tool_add_clip(
    track: str,
    scene_num: int,
    phrase_idx: int,
    clip_path: str,
    duration: float,
    provenance_json: str = "{}",
) -> str:
    """Add a clip to the OTIO timeline with provenance.

    Uses guard_mutation() for lifecycle enforcement and
    otio_read_modify_write() for atomic read-modify-write.
    """
    import opentimelineio as otio

    try:
        tp = resolve_timeline_path()
        guard_mutation(tp, "add_clip")

        try:
            provenance = json.loads(provenance_json) if provenance_json else {}
        except (json.JSONDecodeError, TypeError):
            provenance = {}

        clip_meta: dict = {}
        if provenance:
            clip_meta["_provenance"] = provenance

        def _add_clip_mutate(timeline: otio.schema.Timeline) -> None:
            for t in timeline.tracks:
                if t.name == track:
                    clip = otio.schema.Clip(
                        name=f"scene_{scene_num}_phrase_{phrase_idx}",
                        source_range=otio.opentime.TimeRange(
                            start_time=otio.opentime.RationalTime(0, 24),
                            duration=otio.opentime.RationalTime.from_seconds(duration, 24),
                        ),
                    )
                    clip.media_reference = otio.schema.ExternalReference(
                        target_url=clip_path,
                    )
                    clip.metadata["documentary"] = clip_meta
                    t.append(clip)
                    break
            else:
                logger.warning("Track '%s' not found in timeline", track)

        otio_read_modify_write(tp, _add_clip_mutate)
        return json.dumps({"added": True, "track": track, "scene_num": scene_num, "phrase_idx": phrase_idx})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_validate_timeline(phase: str) -> str:
    """Validate timeline structural integrity for a given pipeline phase.

    Reads the timeline via otio_read() and delegates to the
    phase-specific validators from callbacks.timeline_guardian.
    """
    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)

        from callbacks.timeline_guardian import _VALIDATORS
        validator = _VALIDATORS.get(phase)
        if not validator:
            return json.dumps({"valid": False, "error": f"Unknown phase: {phase}"})

        error = validator(timeline, {})
        if error:
            return json.dumps({"valid": False, "phase": phase, "errors": error})

        return json.dumps({"valid": True, "phase": phase, "message": "All checks passed"})
    except Exception as e:
        return json.dumps({"valid": False, "error": str(e)})


# ---------------------------------------------------------------------------
# 3. Provisioning tools (thin wrappers around vastai CLI)
# ---------------------------------------------------------------------------

def _tool_search_gpu_offers(query: str) -> str:
    """Search Vast.ai for GPU offers using a raw query string.

    The agent constructs the query string based on its reasoning.
    Example: "gpu_ram>=8 dph<=1.0 inet_down>=50 rentable=true"

    Returns raw JSON results from the vastai CLI.  The agent reads
    the results and reasons about which GPU to pick.
    """
    from worker_provisioner import _vast_cmd

    try:
        result = _vast_cmd([
            "search", "offers",
            "--type", "on-demand",
            "--order", "inet_down-",
            "--raw",
            query,
        ])
        if isinstance(result, list):
            # Format for agent readability
            catalog = []
            for o in result[:30]:
                catalog.append({
                    "id": int(o.get("id", 0)),
                    "gpu_name": o.get("gpu_name", "unknown"),
                    "gpu_ram_gb": round(float(o.get("gpu_ram", 0)) / 1024, 1),
                    "num_gpus": o.get("num_gpus", 1),
                    "dph_total": round(float(o.get("dph_total", 0)), 4),
                    "disk_space_gb": round(float(o.get("disk_space", 0)), 0),
                    "inet_down": round(float(o.get("inet_down", 0)), 0),
                    "inet_up": round(float(o.get("inet_up", 0)), 0),
                    "reliability": round(float(o.get("reliability", 0)), 3),
                    "rentable": o.get("rentable", False),
                    "verified": o.get("verified", False),
                    "country": o.get("country", ""),
                })
            return json.dumps({
                "query": query,
                "total_results": len(result),
                "offers_returned": len(catalog),
                "offers": catalog,
            })
        return json.dumps({
            "query": query,
            "result": str(result)[:2000],
        })
    except Exception as e:
        return json.dumps({"error": str(e), "query": query})


def _tool_provision_vm(
    offer_id: int,
    disk_gb: int = 64,
    worker_mode: str = "tts",
    docker_image: str = "",
    env_vars_json: str = "{}",
) -> str:
    """Provision a Vast.ai VM from a specific offer.

    Runs `vastai create instance` for the given offer.  Returns VM
    details including the instance ID.

    Args:
        offer_id: The offer ID from search_gpu_offers results.
        disk_gb: Disk size in GB.  Enforced minimums: tts=64, ltx=300.
        worker_mode: Worker mode — "tts", "ltx", or "both".
        docker_image: Docker image to use.  If empty, resolved from
            the model manifest.
        env_vars_json: Additional environment variables as JSON dict.
    """
    # Enforce disk minimums — Vast.ai offer metadata is unreliable about
    # actual disk size, and model downloads need more than specs suggest.
    DISK_MINIMUMS = {"tts": 64, "ltx": 150, "both": 150}
    min_disk = DISK_MINIMUMS.get(worker_mode, 150)
    if disk_gb < min_disk:
        import logging as _log
        _log.getLogger(__name__).warning(
            "provision_vm: disk_gb=%d below minimum %d for %s — raising to minimum",
            disk_gb, min_disk, worker_mode,
        )
        disk_gb = min_disk

    from worker_provisioner import (
        _vast_cmd,
        _HEALTH_CONTROL_PORT,
        normalize_worker_mode,
        resolve_docker_image,
    )
    from tools.vastai_tools import register_owned_vm
    import shlex
    import subprocess

    worker_mode = normalize_worker_mode(worker_mode)

    if not docker_image:
        docker_image, _torch_index = resolve_docker_image(worker_mode)
    else:
        _torch_index = "https://download.pytorch.org/whl/cu126"

    # Build onstart script
    b2_key_id = os.environ.get("B2_KEY_ID", "")
    b2_app_key = os.environ.get("B2_APPLICATION_KEY", "")
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

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

    # Parse extra env vars
    try:
        extra_env = json.loads(env_vars_json) if env_vars_json else {}
    except (json.JSONDecodeError, TypeError):
        extra_env = {}

    remote_port = 8880
    onstart = (
        f"export B2_KEY_ID={shlex.quote(b2_key_id)} && "
        f"export B2_APPLICATION_KEY={shlex.quote(b2_app_key)} && "
        f"export WORKER_MODE={shlex.quote(worker_mode)} && "
        f"export DASHSCOPE_API_KEY={shlex.quote(dashscope_key)} && "
        f"export OPENROUTER_API_KEY={shlex.quote(openrouter_key)} && "
        f"export TORCH_INDEX={shlex.quote(_torch_index)} && "
        "export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True && "
        "apt-get update && apt-get install -y git curl ffmpeg libsndfile1 sox libsox-dev && "
        f"(git clone -b {shlex.quote(_branch)} --single-branch "
        "https://github.com/OrpingtonClose/economy-documentary.git "
        "/workspace/economy-documentary 2>&1 || "
        "git clone -b main --single-branch "
        "https://github.com/OrpingtonClose/economy-documentary.git "
        "/workspace/economy-documentary 2>&1) && "
        "pip install --break-system-packages --no-cache-dir "
        "'fastapi>=0.100.0' 'uvicorn>=0.20.0' 'pydantic>=2.0.0' "
        "'numpy>=1.26.0,<2.0.0' 'soundfile>=0.12.0' && "
        f"python3 /workspace/economy-documentary/scripts/gpu_worker.py "
        f"--mode {shlex.quote(worker_mode)} --port {remote_port}"
    )

    import uuid as _uuid
    _run_id = os.environ.get("DOCUMENTARY_RUN_ID", _uuid.uuid4().hex[:8])
    _label = f"documentary-tts-{_run_id}"

    _env_ports = (
        f"-p {remote_port}:{remote_port} "
        f"-p {_HEALTH_CONTROL_PORT}:{_HEALTH_CONTROL_PORT}"
    )

    try:
        create_result = _vast_cmd([
            "create", "instance",
            str(offer_id),
            "--image", docker_image,
            "--disk", str(disk_gb),
            "--ssh",
            "--direct",
            "--env", _env_ports,
            "--label", _label,
            "--onstart-cmd", onstart,
        ])
    except RuntimeError as e:
        return json.dumps({
            "status": "error",
            "offer_id": offer_id,
            "error": str(e),
        })

    # Parse response
    vm_id = None
    if isinstance(create_result, dict):
        vm_id = create_result.get("new_contract")
    elif isinstance(create_result, str) and "new_contract" in create_result:
        import re
        match = re.search(r"'new_contract'\s*:\s*(\d+)", create_result)
        if match:
            vm_id = match.group(1)

    if vm_id:
        register_owned_vm(str(vm_id))
        return json.dumps({
            "status": "created",
            "offer_id": offer_id,
            "vm_id": str(vm_id),
            "label": _label,
        })

    return json.dumps({
        "status": "error",
        "offer_id": offer_id,
        "error": f"Unexpected response: {create_result}",
    })


_audio_check_vm_last_call: dict[str, float] = {}
_AUDIO_CHECK_VM_MIN_INTERVAL = 10.0


def _tool_check_vm_status(vm_id: str) -> str:
    """Check a Vast.ai VM's status, connection details, and health.

    Runs `vastai show instance` and returns the status.
    Rate-limited: returns a "wait" response if called more than once
    per 10 seconds for the same VM.
    """
    import time as _time
    now = _time.monotonic()
    last = _audio_check_vm_last_call.get(vm_id, 0)
    if now - last < _AUDIO_CHECK_VM_MIN_INTERVAL:
        return json.dumps({
            "vm_id": vm_id,
            "status": "rate_limited",
            "message": f"Called too soon — wait {_AUDIO_CHECK_VM_MIN_INTERVAL - (now - last):.0f}s. Use exponential backoff: 10s, 30s, 60s, 120s.",
        })
    _audio_check_vm_last_call[vm_id] = now

    from worker_provisioner import _vast_cmd

    try:
        result = _vast_cmd(["show", "instance", vm_id, "--raw"])
    except RuntimeError as e:
        return json.dumps({"status": "error", "vm_id": vm_id, "error": str(e)})

    if not isinstance(result, dict):
        return json.dumps({
            "status": "error",
            "vm_id": vm_id,
            "raw": str(result)[:500],
        })

    actual_status = result.get("actual_status", "unknown")
    public_ipaddr = result.get("public_ipaddr", "")
    ports = result.get("ports", {}) or {}
    direct_port = 0
    port_key = "8880/tcp"
    if port_key in ports:
        port_bindings = ports[port_key]
        if isinstance(port_bindings, list) and port_bindings:
            direct_port = int(port_bindings[0].get("HostPort", 0))
        elif isinstance(port_bindings, (int, str)):
            direct_port = int(port_bindings)

    ssh_host = result.get("ssh_host", "")
    ssh_port = result.get("ssh_port", 0)

    # Try worker endpoint if running
    health_text = None
    if actual_status == "running" and public_ipaddr and direct_port:
        try:
            health_url = f"http://{public_ipaddr}:{direct_port}/"
            req = Request(health_url)
            with urlopen(req, timeout=10) as resp:
                health_text = resp.read().decode().strip()
        except Exception as e:
            health_text = f"error: {e}"

    return json.dumps({
        "status": actual_status,
        "vm_id": vm_id,
        "public_ipaddr": public_ipaddr,
        "direct_port": direct_port,
        "ssh_host": ssh_host,
        "ssh_port": ssh_port,
        "health_text": health_text,
    })


_audio_check_health_last_call: dict[str, float] = {}
_AUDIO_CHECK_HEALTH_MIN_INTERVAL = 10.0


def _tool_check_worker_health(url: str, capability: str) -> str:
    """HTTP GET to worker / endpoint. Rate-limited per URL.

    Returns plain text status.

    Args:
        url: Worker URL (e.g. "http://1.2.3.4:8880").
        capability: Capability to check (e.g. "tts", "ltx").
    """
    import time as _time
    now = _time.monotonic()
    last = _audio_check_health_last_call.get(url, 0)
    if now - last < _AUDIO_CHECK_HEALTH_MIN_INTERVAL:
        return json.dumps({
            "url": url,
            "healthy": False,
            "rate_limited": True,
            "message": f"Called too soon — wait {_AUDIO_CHECK_HEALTH_MIN_INTERVAL - (now - last):.0f}s. Use exponential backoff: 10s, 30s, 60s, 120s.",
        })
    _audio_check_health_last_call[url] = now

    from worker_provisioner import check_worker_health

    try:
        # Get raw health text
        health_url = f"{url.rstrip('/')}/"
        req = Request(health_url)
        with urlopen(req, timeout=10) as resp:
            text = resp.read().decode().strip()
        is_healthy = text.startswith("ok") and f"{capability}=yes" in text
        return json.dumps({
            "healthy": is_healthy,
            "url": url,
            "capability": capability,
            "health_text": text,
        })
    except Exception as e:
        return json.dumps({
            "healthy": False,
            "url": url,
            "capability": capability,
            "error": str(e),
        })


def _tool_terminate_vm(vm_id: str) -> str:
    """Terminate a Vast.ai VM instance.

    Runs `vastai destroy instance`.
    """
    from worker_provisioner import _vast_cmd

    try:
        result = _vast_cmd(["destroy", "instance", "--yes", vm_id])
        return json.dumps({
            "status": "destroyed",
            "vm_id": vm_id,
            "result": str(result)[:200],
        })
    except RuntimeError as e:
        return json.dumps({
            "status": "error",
            "vm_id": vm_id,
            "error": str(e),
        })


def _tool_list_active_vms() -> str:
    """List all active Vast.ai VM instances.

    Runs `vastai show instances --raw`.
    """
    from worker_provisioner import _vast_cmd

    try:
        result = _vast_cmd(["show", "instances", "--raw"])
        if isinstance(result, list):
            vms = []
            for vm in result:
                vms.append({
                    "id": str(vm.get("id", "")),
                    "label": vm.get("label", ""),
                    "actual_status": vm.get("actual_status", "unknown"),
                    "gpu_name": vm.get("gpu_name", ""),
                    "public_ipaddr": vm.get("public_ipaddr", ""),
                    "dph_total": round(float(vm.get("dph_total", 0)), 4),
                })
            return json.dumps({"total": len(vms), "instances": vms})
        return json.dumps({"result": str(result)[:2000]})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_get_account_credits() -> str:
    """Get Vast.ai account credit balance.

    Runs `vastai show user --raw`.
    """
    from worker_provisioner import _vast_cmd

    try:
        result = _vast_cmd(["show", "user", "--raw"])
        if isinstance(result, dict):
            credit = float(result.get("credit", 0.0))
            balance = float(result.get("balance", 0.0))
            return json.dumps({
                "credit": credit,
                "balance": balance,
                "total": round(credit + balance, 2),
            })
        return json.dumps({"result": str(result)[:500]})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# 4. TTS generation tools
# ---------------------------------------------------------------------------

def _tool_generate_narration(
    scene_num: int,
    voice_role: str,
    text: str,
    output_dir: str = "",
    language: str = "",
    worker_url: str = "",
) -> str:
    """Generate narration WAV file using Qwen3-TTS on a worker.

    HTTP POST to the TTS worker.  If worker_url is not provided, fails
    with an error — the agent must provision a worker first.

    Args:
        scene_num: Scene number (1-based).
        voice_role: Voice role identifier (e.g., "V1", "V2", "V3").
        text: Narration text to synthesize.
        output_dir: Optional output directory override.
        language: Language code ("en" or "ru").
        worker_url: URL of the TTS worker (e.g. "http://1.2.3.4:8880").
    """
    import hashlib
    import wave

    _OUTPUT_BASE = os.environ.get(
        "TTS_OUTPUT_DIR", "/tmp/documentary-pipeline/audio"
    )
    _SAMPLE_RATE = 24000

    if not worker_url:
        return json.dumps({
            "scene_num": scene_num,
            "status": "failed",
            "error": (
                "No TTS worker URL provided. You must provision a VM "
                "first and pass its URL. Use search_gpu_offers + "
                "provision_vm + check_vm_status to get a worker URL."
            ),
        })

    out_dir = output_dir or _OUTPUT_BASE
    os.makedirs(out_dir, exist_ok=True)

    filename = f"scene_{scene_num:03d}_{voice_role}.wav"
    wav_path = os.path.join(out_dir, filename)

    # Determine language from voice suffix if not explicit
    voice = voice_role
    if voice_role.endswith("_RU"):
        voice = voice_role[:-3]
        lang = language if language else "ru"
    elif voice_role.endswith("_EN"):
        voice = voice_role[:-3]
        lang = language if language else "en"
    else:
        lang = language if language else "en"

    # Check cache
    text_hash = hashlib.sha256(text.encode()).hexdigest()[:12]
    sidecar_path = wav_path.replace(".wav", ".txt")
    if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 0:
        if os.path.isfile(sidecar_path):
            try:
                with open(sidecar_path, "r") as sf:
                    cached_hash = sf.read().strip()
                if cached_hash == text_hash:
                    try:
                        with wave.open(wav_path, "r") as wf:
                            actual_duration = wf.getnframes() / wf.getframerate()
                        return json.dumps({
                            "status": "skipped",
                            "mode": "cached",
                            "wav_path": wav_path,
                            "duration": round(actual_duration, 2),
                            "worker_url": worker_url,
                        })
                    except wave.Error:
                        pass
            except OSError:
                pass

    # Call TTS worker
    payload = json.dumps({
        "text": text,
        "voice": voice,
        "language": lang,
        "scene_num": scene_num,
    }).encode("utf-8")

    tts_url = f"{worker_url.rstrip('/')}/tts"
    req = Request(tts_url, data=payload, headers={"Content-Type": "application/json"})

    try:
        with urlopen(req, timeout=300) as resp:
            wav_bytes = resp.read()
            actual_duration = float(resp.headers.get("X-Audio-Duration", "0"))
            actual_sample_rate = int(resp.headers.get("X-Sample-Rate", str(_SAMPLE_RATE)))
            gen_time = float(resp.headers.get("X-Gen-Time", "0"))
    except URLError as e:
        return json.dumps({
            "scene_num": scene_num,
            "voice_role": voice_role,
            "status": "failed",
            "error": f"TTS worker unreachable at {worker_url}: {e}",
        })
    except Exception as e:
        return json.dumps({
            "scene_num": scene_num,
            "voice_role": voice_role,
            "status": "failed",
            "error": f"TTS generation failed: {e}",
        })

    # Write WAV file
    os.makedirs(os.path.dirname(wav_path) or ".", exist_ok=True)
    with open(wav_path, "wb") as f:
        f.write(wav_bytes)

    # Write sidecar for cache
    try:
        with open(sidecar_path, "w") as f:
            f.write(text_hash)
    except OSError:
        pass

    # Upload to B2
    try:
        from tools.b2_checkpoint import upload_tts_clip
        upload_tts_clip(wav_path, sidecar_path)
    except Exception as b2_err:
        logger.warning("B2 upload failed for TTS clip %s: %s", wav_path, b2_err)

    return json.dumps({
        "status": "generated",
        "wav_path": wav_path,
        "duration": round(actual_duration, 2),
        "sample_rate": actual_sample_rate,
        "gen_time": round(gen_time, 2),
        "worker_url": worker_url,
    })


def _tool_align_narration(wav_path: str) -> str:
    """Run WhisperX alignment on a WAV file.

    Returns word-level timestamps and total duration.

    Args:
        wav_path: Path to the WAV file to align.
    """
    from tools.whisperx_tools import align_narration

    try:
        result = align_narration(wav_path, text="", language="en")
        return result
    except Exception as e:
        return json.dumps({"error": str(e), "wav_path": wav_path})


# ---------------------------------------------------------------------------
# 5. Quality evaluation
# ---------------------------------------------------------------------------

def _tool_evaluate_narration_quality() -> str:
    """Evaluate narration quality by comparing projected total with target.

    Reads alignment data from OTIO pipeline metadata and compares the
    projected total narration duration with the target total from scene
    budgets.  Returns the ratio and a verdict.
    """
    try:
        tp = resolve_timeline_path()

        # Read alignment data
        alignment = read_pipeline_metadata(tp, "whisperx_alignment")
        if alignment is None:
            return json.dumps({
                "error": "Cannot read alignment: key not found",
                "ratio": 0.0,
                "verdict": "no_data",
            })
        if isinstance(alignment, str):
            alignment = json.loads(alignment)

        # Read scenes for target total
        scenes = read_pipeline_metadata(tp, "scenes")
        if scenes is None:
            return json.dumps({
                "error": "Cannot read scenes: key not found",
                "ratio": 0.0,
                "verdict": "no_data",
            })
        if isinstance(scenes, str):
            scenes = json.loads(scenes)

        # Compute target total
        target_total = sum(float(s.get("duration_sec", 0) or 0) for s in scenes)
        if target_total <= 0:
            return json.dumps({
                "error": "Target total is zero",
                "ratio": 0.0,
                "verdict": "no_target",
            })

        # Compute measured total from alignment
        measured_total = 0.0
        clip_count = 0
        for key, data in (alignment or {}).items():
            if not isinstance(key, str) or not key.startswith("scene_"):
                continue
            if not isinstance(data, dict):
                continue
            dur = float(data.get("total_duration", 0) or 0)
            if dur > 0:
                measured_total += dur
                clip_count += 1

        if measured_total <= 0:
            return json.dumps({
                "target_total": round(target_total, 1),
                "measured_total": 0.0,
                "clip_count": 0,
                "ratio": 0.0,
                "verdict": "no_clips",
            })

        ratio = measured_total / target_total
        if ratio >= 0.80:
            verdict = "pass"
        elif ratio >= 0.60:
            verdict = "marginal"
        else:
            verdict = "fail"

        return json.dumps({
            "target_total": round(target_total, 1),
            "measured_total": round(measured_total, 1),
            "clip_count": clip_count,
            "ratio": round(ratio, 3),
            "pct": round(ratio * 100, 1),
            "verdict": verdict,
        })
    except Exception as e:
        return json.dumps({"error": str(e), "ratio": 0.0, "verdict": "error"})


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_AUDIO_PROVISIONER_TOOLS = [
    # -- OTIO reading tools --
    AgentTool(
        name="read_pipeline_data",
        description=(
            "Read pipeline metadata from the OTIO timeline. Keys include: "
            "scenes, whisperx_alignment, visual_concepts, visual_style, "
            "style_lock, content_analysis. If the key doesn't exist, "
            "returns an error — the upstream stage has not produced this data."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Pipeline metadata key (scenes, whisperx_alignment, etc.)",
                },
            },
            "required": ["key"],
        },
        fn=_tool_read_pipeline_data,
    ),
    AgentTool(
        name="find_narration_gaps",
        description=(
            "Scan the A1_Narration track for Gaps — places where narration "
            "is missing. Cross-references with scenes metadata to get the "
            "text, voice role, and language for each gap. Returns a JSON list "
            "of TTS jobs that need to be generated."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        fn=lambda: _tool_find_narration_gaps(),
    ),
    AgentTool(
        name="get_scene_durations",
        description=(
            "Get per-scene duration budgets — narration vs video vs total. "
            "Shows how much time each scene has, how much narration fills, "
            "and how much video needs."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        fn=lambda: _tool_get_scene_durations(),
    ),

    # -- OTIO writing tools --
    AgentTool(
        name="write_pipeline_data",
        description=(
            "Write pipeline metadata to the OTIO timeline with provenance. "
            "Use this to persist intermediate data like alignment results."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Pipeline metadata key",
                },
                "value_json": {
                    "type": "string",
                    "description": "JSON string of the value to write",
                },
                "provenance_json": {
                    "type": "string",
                    "description": "JSON string of the ArtifactProvenance record",
                },
            },
            "required": ["key", "value_json"],
        },
        fn=_tool_write_pipeline_data,
    ),
    AgentTool(
        name="add_clip",
        description=(
            "Add a clip to the OTIO timeline with provenance. Use this "
            "after generating narration to write the WAV clip into the "
            "A1_Narration track."
        ),
        parameters={
            "type": "object",
            "properties": {
                "track": {
                    "type": "string",
                    "description": "Track name (A1_Narration for narration clips)",
                },
                "scene_num": {
                    "type": "integer",
                    "description": "Scene number",
                },
                "phrase_idx": {
                    "type": "integer",
                    "description": "Phrase index within scene",
                },
                "clip_path": {
                    "type": "string",
                    "description": "Path to the WAV file",
                },
                "duration": {
                    "type": "number",
                    "description": "Duration in seconds",
                },
                "provenance_json": {
                    "type": "string",
                    "description": "JSON string of the ArtifactProvenance record",
                },
            },
            "required": ["track", "scene_num", "phrase_idx", "clip_path", "duration"],
        },
        fn=_tool_add_clip,
    ),
    AgentTool(
        name="validate_timeline",
        description=(
            "Validate timeline structural integrity for a pipeline phase. "
            "Checks: correct track structure, no missing clips, no "
            "zero-duration gaps, no duplicate clip names."
        ),
        parameters={
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "enum": ["scenario", "audio", "visual_direction", "production", "assembly"],
                    "description": "Pipeline phase to validate against",
                },
            },
            "required": ["phase"],
        },
        fn=_tool_validate_timeline,
    ),

    # -- Provisioning tools --
    AgentTool(
        name="search_gpu_offers",
        description=(
            "Search Vast.ai for GPU offers using a raw query string. "
            "YOU construct the query based on your reasoning about what "
            "GPU is needed. Example: 'gpu_ram>=8 dph<=1.0 inet_down>=50 "
            "rentable=true'. Returns raw JSON results. Read the results "
            "and reason about which GPU to pick."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Vast.ai search query string. Examples: "
                        "'gpu_ram>=8 dph<=1.0 inet_down>=50 rentable=true', "
                        "'gpu_name=RTX_4000 gpu_ram>=8 rentable=true', "
                        "'gpu_ram>=48 dph<=5.0 rentable=true'"
                    ),
                },
            },
            "required": ["query"],
        },
        fn=_tool_search_gpu_offers,
    ),
    AgentTool(
        name="provision_vm",
        description=(
            "Provision a Vast.ai VM from a specific offer. You pick the "
            "offer_id from search_gpu_offers results. Returns VM details "
            "including the instance ID. After provisioning, use "
            "check_vm_status to wait for the VM to become running, then "
            "check_worker_health to verify the TTS worker is ready."
        ),
        parameters={
            "type": "object",
            "properties": {
                "offer_id": {
                    "type": "integer",
                    "description": "Offer ID from search_gpu_offers results",
                },
                "disk_gb": {
                    "type": "integer",
                    "description": "Disk size in GB (default: 64)",
                },
                "worker_mode": {
                    "type": "string",
                    "description": "Worker mode: 'tts', 'ltx', or 'both' (default: 'tts')",
                },
                "docker_image": {
                    "type": "string",
                    "description": "Docker image to use (empty = auto-resolve from manifest)",
                },
                "env_vars_json": {
                    "type": "string",
                    "description": "Additional environment variables as JSON dict",
                },
            },
            "required": ["offer_id"],
        },
        fn=_tool_provision_vm,
    ),
    AgentTool(
        name="check_vm_status",
        description=(
            "Check a Vast.ai VM's status, connection details, and health "
            "endpoint. Returns the VM status (running, etc.), public IP, "
            "direct port, and health endpoint response."
        ),
        parameters={
            "type": "object",
            "properties": {
                "vm_id": {
                    "type": "string",
                    "description": "Instance ID from provision_vm",
                },
            },
            "required": ["vm_id"],
        },
        fn=_tool_check_vm_status,
    ),
    AgentTool(
        name="check_worker_health",
        description=(
            "HTTP GET to a worker's / endpoint. Returns plain text "
            "detail including bootstrap status and model loading state. "
            "Use this after check_vm_status shows the VM is running."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Worker URL (e.g. 'http://1.2.3.4:8880')",
                },
                "capability": {
                    "type": "string",
                    "description": "Capability to check (e.g. 'tts', 'ltx')",
                },
            },
            "required": ["url", "capability"],
        },
        fn=_tool_check_worker_health,
    ),
    AgentTool(
        name="terminate_vm",
        description=(
            "Terminate (destroy) a Vast.ai VM instance. Use this during "
            "CLEANUP phase to release GPU resources after TTS is done."
        ),
        parameters={
            "type": "object",
            "properties": {
                "vm_id": {
                    "type": "string",
                    "description": "Instance ID to destroy",
                },
            },
            "required": ["vm_id"],
        },
        fn=_tool_terminate_vm,
    ),
    AgentTool(
        name="list_active_vms",
        description=(
            "List all active Vast.ai VM instances. Shows ID, label, "
            "status, GPU name, and cost per hour."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        fn=lambda: _tool_list_active_vms(),
    ),
    AgentTool(
        name="get_account_credits",
        description=(
            "Get Vast.ai account credit balance. Check this before "
            "provisioning to make sure you have enough credits."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        fn=lambda: _tool_get_account_credits(),
    ),

    # -- TTS generation tools --
    AgentTool(
        name="generate_narration",
        description=(
            "Generate narration WAV file using Qwen3-TTS on a worker. "
            "You MUST provide worker_url — the URL of a provisioned TTS "
            "worker. If no worker_url is provided, the tool fails. "
            "Use search_gpu_offers + provision_vm + check_vm_status + "
            "check_worker_health to get a healthy worker URL first."
        ),
        parameters={
            "type": "object",
            "properties": {
                "scene_num": {
                    "type": "integer",
                    "description": "Scene number (1-based)",
                },
                "voice_role": {
                    "type": "string",
                    "description": "Voice role identifier (e.g. 'V1', 'V2', 'V3')",
                },
                "text": {
                    "type": "string",
                    "description": "Narration text to synthesize",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Optional output directory override",
                },
                "language": {
                    "type": "string",
                    "description": "Language code ('en' or 'ru')",
                },
                "worker_url": {
                    "type": "string",
                    "description": "URL of the TTS worker (e.g. 'http://1.2.3.4:8880')",
                },
            },
            "required": ["scene_num", "voice_role", "text", "worker_url"],
        },
        fn=_tool_generate_narration,
    ),
    AgentTool(
        name="align_narration",
        description=(
            "Run WhisperX alignment on a WAV file. Returns word-level "
            "timestamps and total duration. Use this after generating "
            "narration to get precise timing data."
        ),
        parameters={
            "type": "object",
            "properties": {
                "wav_path": {
                    "type": "string",
                    "description": "Path to the WAV file to align",
                },
            },
            "required": ["wav_path"],
        },
        fn=_tool_align_narration,
    ),

    # -- Quality evaluation --
    AgentTool(
        name="evaluate_narration_quality",
        description=(
            "Evaluate narration quality by comparing the projected total "
            "narration duration with the target total from scene budgets. "
            "Returns the ratio and a verdict: 'pass' (>=80%), 'marginal' "
            "(60-80%), or 'fail' (<60%)."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        fn=lambda: _tool_evaluate_narration_quality(),
    ),
]


# ---------------------------------------------------------------------------
# Agent instruction — rich, phase-based
# ---------------------------------------------------------------------------

_AUDIO_PROVISIONER_INSTRUCTION = """\
You are the AudioProvisionerAgent — you own TTS end-to-end.

You allocate GPU workers, generate narration, evaluate quality, and
scale workers.  You use raw LLM reasoning with vast CLI output and
Letta-based memory.  No hardcoded model registries — you search Vast.ai,
read the results, and reason about what GPU to pick.  You remember past
decisions in memory.

YOUR DOMAIN: narration quality, TTS infrastructure, and the full
lifecycle from gap detection through generation to quality evaluation.

PHASES:

1. READ — Find narration gaps in OTIO
   - Call find_narration_gaps() to scan the A1_Narration track
   - Call read_pipeline_data("scenes") to understand the full scene structure
   - Call get_scene_durations() to see duration budgets

2. PLAN — Compute workload from gaps
   - Count the total number of TTS jobs from the gaps
   - Estimate total text length (words) across all gaps
   - Qwen3-TTS throughput is approximately 6x realtime:
     * 10 seconds of narration takes ~1.7 seconds to generate
     * 100 words ≈ 30 seconds of narration ≈ 5 seconds to generate
   - Decide: how many workers do you need? One worker can handle
     most workloads. Two workers for >50 gaps.
   - Estimate total GPU-hours needed and check account credits

3. PROVISION — Search GPUs, pick one, create VM, wait for health
   - Call get_account_credits() to check your budget
   - Call search_gpu_offers() with a query you construct:
     * For TTS: Qwen3-TTS is a 1.7B model needing ~8GB VRAM
     * Example query: "gpu_ram>=8 dph<=1.0 inet_down>=50 rentable=true"
     * If no results, broaden: "gpu_ram>=8 dph<=2.0 rentable=true"
   - Read the offer results and REASON about which GPU to pick:
     * Prefer cheaper GPUs (RTX 3060, 4060, etc.) for TTS
     * Higher inet_down = faster Docker image pull
     * Higher reliability = less likely to crash
   - Call provision_vm() with the chosen offer_id
   - Call check_vm_status() in a loop until the VM is "running"
   - Call check_worker_health() until the worker is healthy
   - Store the worker URL in your working memory

4. GENERATE — For each gap, call TTS worker, write clip to OTIO
   - For each gap from find_narration_gaps():
     * Call generate_narration() with the gap's text, voice, language,
       and the worker URL from your memory
     * If successful, call add_clip() to write the WAV into the
       A1_Narration track
     * If failed, note the failure and continue (don't stop)
   - After all gaps are processed, call align_narration() on each
     generated WAV to get precise timing
   - Write alignment data to OTIO with write_pipeline_data()

5. EVALUATE — Check quality, rebalance if needed
   - Call evaluate_narration_quality() to compare projected vs target
   - If ratio >= 80%: PASS — proceed to cleanup
   - If ratio 60-80%: MARGINAL — consider regenerating short clips
     with expanded text, or adding more narration
   - If ratio < 60%: FAIL — this is a serious shortfall. Consider:
     * Asking the scenario agent for more scenes or longer scenes
     * Regenerating clips with expanded narration text
     * If nothing works, escalate to human

6. CLEANUP — Destroy VMs
   - Call terminate_vm() for each VM you provisioned
   - Call list_active_vms() to verify all VMs are destroyed
   - This saves credits — don't leave VMs running!

MEMORY:
- Store VM info (vm_id, worker_url, status) in your working memory
- VMs are ephemeral infrastructure — they don't go in OTIO
- Remember past GPU choices that worked well or failed
- Remember which GPU types are reliable for TTS

ESCALATION LADDER (PERMISSIVE — audio reconciliation IS the mechanism):
Audio is permissive because multiple attempts are cheap and reconciliation
produces the authoritative OTIO.  Each tier has a generous budget:

L0 DOMAIN FIX (8 attempts):
  - Reseed TTS with different voice parameters
  - Rephrase narration text (shorter, simpler)
  - Adjust silence padding between phrases
  - Retry with different temperature / top_p

L1 RETRY (4 attempts):
  - Consult audio-understanding on the failure
  - Multi-shot generation (generate 2-3 variants, pick best)
  - Adjust generation parameters based on error analysis

L2 CREATIVE (2 attempts):
  - Try alternative voice from the voice catalog
  - Try alternative TTS provider if available
  - Split long phrases into shorter segments

L3 COLLABORATIVE (1 attempt):
  - Coordinate with scenario agent for text simplification
  - Coordinate with OTIO gate for duration budget adjustment
  - May request begin_escalation(REPLACE) to modify authoritative OTIO

L4 HUMAN (1 decision):
  - Present full diagnostic chain (what was tried, what failed)
  - Request human decision on how to proceed

FAILURE CLASSIFICATION:
Before escalating, CLASSIFY the failure:
- CONTENT failure (bad narration, timing drift, quality): use audio ladder above
- INFRA failure (CUDA error, OOM, timeout, preemption): use infra ladder below
- UNCLEAR: run short_diagnostic() on the worker to reclassify

INFRA LADDER (separate from content budget):
L0 FIX (4 attempts): retry on a different healthy worker
L1 RETRY (2 attempts): recycle suspect worker, redispatch
L2 CREATIVE (1 attempt): scale fleet, hot-swap GPU tier, different region
L3 COLLABORATIVE (1 attempt): coordinate with content ladder, down-spec params
L4 HUMAN (1 decision)

NEVER condemn a worker from a single bad clip — require 2+ independent
infra signals (job failure + infra_agent report) before terminating a VM.

RULES:
- Qwen3-TTS is a hard requirement — never substitute edge-tts or other models
- VRAM is a hard floor — never go below 8GB for TTS
- Always check credits before provisioning
- Always cleanup VMs when done
- If a worker fails health check, try a different offer
- If no offers exist, try broader search (higher price, any GPU type)
- If budget is exhausted, escalate to human
- Never silently skip narration — every gap must be filled or escalated
- Track ladder state in OTIO: write_pipeline_data("audio_ladder", {level, attempts, history})
- Use begin_escalation(REPLACE) before modifying authoritative OTIO
"""


# ---------------------------------------------------------------------------
# AudioProvisionerAgent class
# ---------------------------------------------------------------------------

class AudioProvisionerAgent(RecoveryAgent):
    """Agent that owns TTS end-to-end: provision, generate, evaluate, cleanup.

    Merges the provisioner with the audio agent.  Uses raw LLM reasoning
    with vast CLI output + Letta-based memory.  No hardcoded model
    registries — the agent searches Vast.ai, reads the results, and
    reasons about what GPU to pick.

    The agent tracks VMs in its working memory (not OTIO) because VMs
    are ephemeral infrastructure.
    """

    def __init__(self) -> None:
        super().__init__(
            name="audio_provisioner",
            instruction=_AUDIO_PROVISIONER_INSTRUCTION,
            tools=_AUDIO_PROVISIONER_TOOLS,
            max_tool_rounds=12,  # May need many rounds: search, provision, wait, generate, evaluate
        )
